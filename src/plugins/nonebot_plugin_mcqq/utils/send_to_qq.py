import asyncio
import re
import time
from collections import OrderedDict, deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal, cast

from nonebot import get_bots, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.adapters.qq import AuditException
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq import Message as QQMessage
from nonebot.adapters.qq import MessageSegment as QQMessageSegment

from ..config import BotRateLimit, Server, plugin_config  # noqa: TID252
from .sensitive_words import filter_current_sensitive_text

MINUTE_SECONDS = 60.0
HOUR_SECONDS = 3600.0
DROP_LOG_INTERVAL_SECONDS = 10.0

TargetKind = Literal["group", "guild"]
RouteKey = tuple[str, TargetKind, str, str]
DeliveryStatus = Literal["sent", "defer", "drop"]


@dataclass(slots=True)
class _PendingText:
    """低资源待发项：限流队列只保留文本，不持有图片字节。"""

    sequence: int
    text: str
    force_plain: bool = False


@dataclass(slots=True)
class _RouteState:
    """一个 Minecraft 服务器到单个 QQ 目标的发送状态。"""

    key: RouteKey
    bot_ids: tuple[str, ...]
    forward_batch_header: str
    pending: deque[_PendingText] = field(default_factory=deque)
    cursor: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def server_name(self) -> str:
        return self.key[0]

    @property
    def target_kind(self) -> TargetKind:
        return self.key[1]

    @property
    def adapter(self) -> str:
        return self.key[2]

    @property
    def target_id(self) -> str:
        return self.key[3]


@dataclass(slots=True)
class _BotWindow:
    """单个 Bot 的分钟、小时滑动窗口。"""

    minute_attempts: deque[float] = field(default_factory=deque)
    hour_attempts: deque[float] = field(default_factory=deque)

    def _prune(self, now: float) -> None:
        while self.minute_attempts and now - self.minute_attempts[0] >= MINUTE_SECONDS:
            self.minute_attempts.popleft()
        while self.hour_attempts and now - self.hour_attempts[0] >= HOUR_SECONDS:
            self.hour_attempts.popleft()

    @staticmethod
    def _window_retry_after(
        attempts: deque[float],
        limit: int,
        window_seconds: float,
        now: float,
    ) -> float:
        if not limit or len(attempts) < limit:
            return 0

        # 若运行时下调限制，需等待足够多的旧记录过期，而非只看队首。
        release_index = len(attempts) - limit
        return max(0, attempts[release_index] + window_seconds - now)

    def retry_after(self, limit: BotRateLimit, now: float) -> float:
        self._prune(now)
        return max(
            self._window_retry_after(
                self.minute_attempts,
                limit.rpm,
                MINUTE_SECONDS,
                now,
            ),
            self._window_retry_after(
                self.hour_attempts,
                limit.rph,
                HOUR_SECONDS,
                now,
            ),
        )

    def reserve(self, limit: BotRateLimit, now: float) -> None:
        # 仅为启用的窗口保存时间戳，避免不限流 Bot 产生无意义状态。
        if limit.rpm:
            self.minute_attempts.append(now)
        if limit.rph:
            self.hour_attempts.append(now)


@dataclass(slots=True)
class _BotSelection:
    bot: OneBot | QQBot | None
    online_count: int
    retry_after: float | None


class _SendDispatcher:
    """集中管理 MC→QQ 的限流、轮换、积压与合并发送。"""

    def __init__(self) -> None:
        self._routes: dict[RouteKey, _RouteState] = {}
        self._windows: dict[str, _BotWindow] = {}
        self._global_pending: OrderedDict[int, RouteKey] = OrderedDict()
        self._sequence = 0
        self._state_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._closing = False
        self._drop_counts: dict[str, int] = {}
        self._last_drop_log = 0.0

    def routes_for_server(
        self,
        server_name: str,
        server: Server,
    ) -> list[_RouteState]:
        """合并服务器内的重复目标，并按配置顺序汇集候选 Bot。"""
        grouped: OrderedDict[
            tuple[TargetKind, str, str],
            list[str],
        ] = OrderedDict()

        for group in server.group_list:
            key = ("group", group.adapter, group.group_id)
            bot_ids = grouped.setdefault(key, [])
            for bot_id in group.candidate_bot_ids:
                if bot_id not in bot_ids:
                    bot_ids.append(bot_id)

        for guild in server.guild_list:
            key = ("guild", guild.adapter, guild.channel_id)
            bot_ids = grouped.setdefault(key, [])
            for bot_id in guild.candidate_bot_ids:
                if bot_id not in bot_ids:
                    bot_ids.append(bot_id)

        routes: list[_RouteState] = []
        for (target_kind, adapter, target_id), bot_ids in grouped.items():
            route_key: RouteKey = (
                server_name,
                target_kind,
                adapter,
                target_id,
            )
            if route := self._routes.get(route_key):
                route.bot_ids = tuple(bot_ids)
                route.forward_batch_header = server.forward_batch_header
                if route.bot_ids:
                    route.cursor %= len(route.bot_ids)
                else:
                    route.cursor = 0
            else:
                route = _RouteState(
                    key=route_key,
                    bot_ids=tuple(bot_ids),
                    forward_batch_header=server.forward_batch_header,
                )
                self._routes[route_key] = route
            routes.append(route)
        return routes

    @staticmethod
    def _bot_matches_route(bot: object, route: _RouteState) -> bool:
        if route.adapter == "onebot":
            return route.target_kind == "group" and isinstance(bot, OneBot)
        if route.adapter == "qq":
            return isinstance(bot, QQBot)
        return False

    def _select_bot_locked(
        self,
        route: _RouteState,
        excluded_bot_ids: set[str],
    ) -> _BotSelection:
        """原子选择 Bot 并预占一次额度，防止并发突破窗口上限。"""
        if not route.bot_ids:
            return _BotSelection(None, 0, None)

        now = time.monotonic()
        bots = get_bots()
        online_count = 0
        earliest_retry: float | None = None
        bot_count = len(route.bot_ids)

        for offset in range(bot_count):
            index = (route.cursor + offset) % bot_count
            bot_id = route.bot_ids[index]
            if bot_id in excluded_bot_ids:
                continue
            bot = bots.get(bot_id)
            if bot is None or not self._bot_matches_route(bot, route):
                continue

            online_count += 1
            limit = plugin_config.bot_rate_limits.get(bot_id)
            if limit is None or (limit.rpm == 0 and limit.rph == 0):
                route.cursor = (index + 1) % bot_count
                return _BotSelection(
                    cast("OneBot | QQBot", bot),
                    online_count,
                    None,
                )

            window = self._windows.setdefault(bot_id, _BotWindow())
            retry_after = window.retry_after(limit, now)
            if retry_after > 0:
                earliest_retry = (
                    retry_after
                    if earliest_retry is None
                    else min(earliest_retry, retry_after)
                )
                continue

            window.reserve(limit, now)
            route.cursor = (index + 1) % bot_count
            return _BotSelection(
                cast("OneBot | QQBot", bot),
                online_count,
                None,
            )

        return _BotSelection(None, online_count, earliest_retry)

    def _record_drop_locked(self, reason: str, count: int = 1) -> None:
        """合并高频淘汰日志，避免限流风暴反过来消耗系统资源。"""
        self._drop_counts[reason] = self._drop_counts.get(reason, 0) + count
        now = time.monotonic()
        if now - self._last_drop_log < DROP_LOG_INTERVAL_SECONDS:
            return

        summary = "，".join(
            f"{drop_reason} {drop_count} 条"
            for drop_reason, drop_count in self._drop_counts.items()
        )
        logger.warning(f"[MC_QQ]丨待发队列已丢弃消息：{summary}")
        self._drop_counts.clear()
        self._last_drop_log = now

    def _remove_sequence_from_route_locked(
        self,
        route_key: RouteKey,
        sequence: int,
    ) -> None:
        route = self._routes.get(route_key)
        if route is None:
            return
        for pending in route.pending:
            if pending.sequence == sequence:
                route.pending.remove(pending)
                return

    def _drop_global_oldest_locked(self) -> None:
        sequence, route_key = self._global_pending.popitem(last=False)
        self._remove_sequence_from_route_locked(route_key, sequence)
        self._record_drop_locked("达到全局上限")

    def _enqueue_locked(self, route: _RouteState, text: str) -> bool:
        if not text:
            return False

        if len(route.pending) >= plugin_config.send_route_queue_max_messages:
            dropped = route.pending.popleft()
            self._global_pending.pop(dropped.sequence, None)
            self._record_drop_locked("达到单路由上限")

        while len(self._global_pending) >= plugin_config.send_global_queue_max_messages:
            self._drop_global_oldest_locked()

        self._sequence += 1
        pending = _PendingText(sequence=self._sequence, text=text)
        route.pending.append(pending)
        self._global_pending[pending.sequence] = route.key
        self._ensure_worker_locked()
        self._wake_event.set()
        return True

    def _ensure_worker_locked(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker_loop(),
                name="mcqq-send-dispatcher",
            )

    def _take_batch_locked(self, route: _RouteState) -> list[_PendingText]:
        if not route.pending:
            return []

        force_plain = route.pending[0].force_plain
        batch: list[_PendingText] = []
        while (
            route.pending
            and len(batch) < plugin_config.send_batch_max_messages
            and route.pending[0].force_plain == force_plain
        ):
            pending = route.pending.popleft()
            self._global_pending.pop(pending.sequence, None)
            batch.append(pending)
        return batch

    def _drop_global_newest_locked(self) -> None:
        sequence, route_key = self._global_pending.popitem(last=True)
        self._remove_sequence_from_route_locked(route_key, sequence)
        self._record_drop_locked("为失败回退保留旧消息")

    def _requeue_front_locked(
        self,
        route: _RouteState,
        batch: list[_PendingText],
    ) -> None:
        """失败批次保持原顺序回到队首，并优先淘汰更新的消息。"""
        while (
            len(route.pending) + len(batch)
            > plugin_config.send_route_queue_max_messages
        ):
            dropped = route.pending.pop()
            self._global_pending.pop(dropped.sequence, None)
            self._record_drop_locked("为失败回退保留旧消息")

        while (
            len(self._global_pending) + len(batch)
            > plugin_config.send_global_queue_max_messages
        ):
            self._drop_global_newest_locked()

        for pending in reversed(batch):
            route.pending.appendleft(pending)
            self._global_pending[pending.sequence] = route.key
            self._global_pending.move_to_end(pending.sequence, last=False)
        self._ensure_worker_locked()
        self._wake_event.set()

    async def dispatch(  # noqa: C901, PLR0912
        self,
        route: _RouteState,
        text: str,
        img_bytes: bytes | None,
        *,
        queue_when_limited: bool,
    ) -> None:
        async with route.send_lock:
            async with self._state_lock:
                if route.pending:
                    if queue_when_limited:
                        queued = self._enqueue_locked(route, text)
                        if queued and img_bytes:
                            logger.debug(
                                f"[MC_QQ]丨路由 {route.target_id} 已有积压，"
                                "图片已丢弃，仅缓存文字"
                            )
                    else:
                        logger.debug(
                            f"[MC_QQ]丨路由 {route.target_id} 已有积压，"
                            "时效性消息不进入队列"
                        )
                    return

            attempted_bot_ids: set[str] = set()
            while True:
                async with self._state_lock:
                    selection = self._select_bot_locked(route, attempted_bot_ids)

                if selection.bot is None:
                    if selection.online_count and selection.retry_after is not None:
                        if queue_when_limited:
                            async with self._state_lock:
                                queued = self._enqueue_locked(route, text)
                            if queued and img_bytes:
                                logger.debug(
                                    f"[MC_QQ]丨路由 {route.target_id} 已达到频率限制，"
                                    "图片已丢弃，仅缓存文字"
                                )
                        else:
                            logger.debug(
                                f"[MC_QQ]丨路由 {route.target_id} 已达到频率限制，"
                                "时效性消息已丢弃"
                            )
                    elif attempted_bot_ids:
                        logger.error(
                            f"[MC_QQ]丨发送至 {route.target_id} 失败，"
                            "所有在线候选 Bot 均已尝试"
                        )
                    else:
                        logger.error(
                            f"[MC_QQ]丨发送至 {route.target_id} 失败，"
                            "没有匹配且在线的候选 Bot"
                        )
                    return

                bot_id = str(selection.bot.self_id)
                try:
                    await self._send_immediate_api(
                        selection.bot,
                        route,
                        text,
                        img_bytes,
                    )
                except Exception as error:  # noqa: BLE001
                    attempted_bot_ids.add(bot_id)
                    logger.error(
                        f"[MC_QQ]丨Bot {bot_id} 发送至 {route.target_id} "
                        f"出现异常：{error!r}"
                    )
                else:
                    return

    async def _send_immediate_api(  # noqa: PLR0912
        self,
        bot: OneBot | QQBot,
        route: _RouteState,
        text: str,
        img_bytes: bytes | None,
    ) -> None:
        try:
            if isinstance(bot, OneBot):
                if img_bytes:
                    message = OneBotMessage()
                    if text:
                        message += OneBotMessageSegment.text(text)
                    message += OneBotMessageSegment.image(img_bytes)
                else:
                    message = text
                await bot.send_group_msg(
                    group_id=int(route.target_id),
                    message=message,
                )
                return

            if route.target_kind == "group":
                if img_bytes:
                    message = QQMessage()
                    if text:
                        message += QQMessageSegment.text(text)
                    message += QQMessageSegment.file_image(img_bytes)
                    await bot.send_to_group(
                        group_openid=route.target_id,
                        message=message,
                    )
                else:
                    await bot.post_group_messages(
                        group_openid=route.target_id,
                        msg_type=0,
                        content=text,
                    )
            else:
                if img_bytes:
                    message = QQMessage()
                    if text:
                        message += QQMessageSegment.text(text)
                    message += QQMessageSegment.file_image(img_bytes)
                else:
                    message = text
                await bot.send_to_channel(
                    channel_id=route.target_id,
                    message=message,
                )
        except AuditException as error:
            await self._handle_audit(error, route)

    @staticmethod
    async def _handle_audit(
        error: AuditException,
        route: _RouteState,
    ) -> None:
        logger.debug(f"[MC_QQ]丨发送至 {route.target_id} 的消息正在审核中")
        try:
            audit_result = await error.get_audit_result(3)
            logger.debug(f"[MC_QQ]丨审核结果：{audit_result.get_event_name()}")
        except Exception as audit_error:  # noqa: BLE001
            logger.error(
                f"[MC_QQ]丨获取 {route.target_id} 的审核结果失败：{audit_error!r}"
            )

    async def _reserve_for_batch(
        self,
        route: _RouteState,
        attempted_bot_ids: set[str],
    ) -> _BotSelection:
        async with self._state_lock:
            return self._select_bot_locked(route, attempted_bot_ids)

    async def _send_forward_api(
        self,
        bot: OneBot,
        route: _RouteState,
        batch: list[_PendingText],
    ) -> None:
        nodes = OneBotMessage()
        if route.forward_batch_header:
            nodes += OneBotMessageSegment.node_custom(
                user_id=int(bot.self_id),
                nickname="MineGroupBridge",
                content=route.forward_batch_header,
            )
        for pending in batch:
            nodes += OneBotMessageSegment.node_custom(
                user_id=int(bot.self_id),
                nickname="MineGroupBridge",
                content=pending.text,
            )

        # 节点数量不计入 RPM；整个合并转发 API 调用只预占一次额度。
        await bot.call_api(
            "send_group_forward_msg",
            group_id=int(route.target_id),
            messages=nodes,
        )

    @staticmethod
    def _plain_batch_text(
        route: _RouteState,
        batch: list[_PendingText],
    ) -> str:
        texts = [pending.text for pending in batch]
        if batch[0].force_plain and route.forward_batch_header:
            texts.insert(0, route.forward_batch_header)
        return "\n".join(texts)

    async def _send_plain_batch_api(
        self,
        bot: OneBot | QQBot,
        route: _RouteState,
        text: str,
    ) -> None:
        await self._send_immediate_api(bot, route, text, None)

    async def _attempt_plain_batch(
        self,
        route: _RouteState,
        batch: list[_PendingText],
    ) -> tuple[DeliveryStatus, float | None]:
        attempted_bot_ids: set[str] = set()
        text = self._plain_batch_text(route, batch)

        while True:
            selection = await self._reserve_for_batch(route, attempted_bot_ids)
            if selection.bot is None:
                if selection.online_count and selection.retry_after is not None:
                    return "defer", selection.retry_after
                if attempted_bot_ids:
                    logger.error(
                        f"[MC_QQ]丨纯文本批次发送至 {route.target_id} 失败，"
                        "所有在线候选 Bot 均已尝试"
                    )
                else:
                    logger.error(
                        f"[MC_QQ]丨纯文本批次发送至 {route.target_id} 失败，"
                        "没有匹配且在线的候选 Bot"
                    )
                return "drop", None

            bot_id = str(selection.bot.self_id)
            try:
                await self._send_plain_batch_api(
                    selection.bot,
                    route,
                    text,
                )
            except Exception as error:  # noqa: BLE001
                attempted_bot_ids.add(bot_id)
                logger.error(
                    f"[MC_QQ]丨Bot {bot_id} 发送纯文本批次至 "
                    f"{route.target_id} 出现异常：{error!r}"
                )
            else:
                return "sent", None

    async def _deliver_queued_batch(
        self,
        route: _RouteState,
        batch: list[_PendingText],
    ) -> tuple[DeliveryStatus, float | None]:
        should_forward = (
            route.adapter == "onebot"
            and route.target_kind == "group"
            and len(batch) > 1
            and not batch[0].force_plain
        )
        if not should_forward:
            return await self._attempt_plain_batch(route, batch)

        selection = await self._reserve_for_batch(route, set())
        if selection.bot is None:
            if selection.online_count and selection.retry_after is not None:
                return "defer", selection.retry_after
            logger.error(
                f"[MC_QQ]丨合并转发至 {route.target_id} 失败，没有匹配且在线的候选 Bot"
            )
            return "drop", None

        try:
            assert isinstance(selection.bot, OneBot)
            await self._send_forward_api(selection.bot, route, batch)
        except Exception as error:  # noqa: BLE001
            logger.warning(
                f"[MC_QQ]丨合并转发至 {route.target_id} 失败，"
                f"将仅保留文字重试：{error!r}"
            )
            # 图片在入队时已经释放；该标记保证后续不再尝试合并转发。
            for pending in batch:
                pending.force_plain = True
            return await self._attempt_plain_batch(route, batch)
        else:
            return "sent", None

    async def _flush_route(
        self,
        route: _RouteState,
    ) -> tuple[bool, float | None]:
        async with route.send_lock:
            async with self._state_lock:
                batch = self._take_batch_locked(route)
            if not batch:
                return False, None

            status, retry_after = await self._deliver_queued_batch(route, batch)
            if status == "defer":
                async with self._state_lock:
                    self._requeue_front_locked(route, batch)
                return False, retry_after
            return True, None

    async def _flush_once(self) -> tuple[bool, float | None, bool]:
        async with self._state_lock:
            routes = [route for route in self._routes.values() if route.pending]

        made_progress = False
        retry_delays: list[float] = []
        for route in routes:
            progressed, retry_after = await self._flush_route(route)
            made_progress = made_progress or progressed
            if retry_after is not None:
                retry_delays.append(retry_after)

        async with self._state_lock:
            has_pending = bool(self._global_pending)
        return (
            made_progress,
            min(retry_delays) if retry_delays else None,
            has_pending,
        )

    async def _worker_loop(self) -> None:
        """单个中央任务负责所有路由的额度恢复和批次唤醒。"""
        try:
            while not self._closing:
                self._wake_event.clear()
                made_progress, retry_after, has_pending = await self._flush_once()
                if made_progress:
                    await asyncio.sleep(0)
                    continue

                if not has_pending:
                    await self._wake_event.wait()
                    continue

                timeout = max(0.05, retry_after or 1)
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._wake_event.wait(),
                        timeout=timeout,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[MC_QQ]丨发送调度任务异常退出")

    async def shutdown(self) -> None:
        self._closing = True
        self._wake_event.set()
        task = self._worker_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        async with self._state_lock:
            self._global_pending.clear()
            for route in self._routes.values():
                route.pending.clear()
            self._windows.clear()


_dispatcher = _SendDispatcher()


async def send_mc_msg_to_qq(
    server_name: str,
    result: str,
    img_bytes: bytes | None = None,
    *,
    queue_when_limited: bool = True,
) -> None:
    """发送 MC 消息；时效性事件可禁止在限流时进入待发队列。"""
    server = plugin_config.server_dict.get(server_name)
    if server is None:
        logger.error(f"未知的服务器: {server_name}")
        return

    msg_result = re.sub(r"[&§].", "", result)
    if plugin_config.display_server_name:
        display_name = server.nickname or f"[{server_name}]"
        msg_result = f"{display_name} {msg_result}"

    # 在最终可见文本进入限流队列前过滤，确保昵称、服务器名和通知同样生效。
    filtered_result = filter_current_sensitive_text(msg_result)
    if filtered_result is None:
        logger.info(f"[MC_QQ]丨服务器 {server_name} 的消息命中敏感词，已屏蔽")
        return
    msg_result = filtered_result

    routes = _dispatcher.routes_for_server(server_name, server)
    for route in routes:
        await _dispatcher.dispatch(
            route,
            msg_result,
            img_bytes,
            queue_when_limited=queue_when_limited,
        )


@get_driver().on_shutdown
async def _shutdown_send_dispatcher() -> None:
    """关闭时释放唯一调度任务及全部内存队列。"""
    await _dispatcher.shutdown()
