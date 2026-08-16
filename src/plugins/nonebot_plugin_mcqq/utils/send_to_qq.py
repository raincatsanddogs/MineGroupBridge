import asyncio
import time
from collections import OrderedDict, deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal, cast
from urllib.parse import urlsplit

from nonebot import get_bots, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.adapters.qq import AuditException
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq import Message as QQMessage
from nonebot.adapters.qq import MessageSegment as QQMessageSegment

from ..config import BotRateLimit, Server, plugin_config  # noqa: TID252
from .parse_mc_msg import (
    GROUP_MENTION_PATTERN,
    MediaPart,
    MentionPart,
    MessagePart,
    TextPart,
    clean_minecraft_formatting,
    parse_mc_message,
    replace_media_names,
)
from .sensitive_words import filter_current_sensitive_text

MINUTE_SECONDS = 60.0
HOUR_SECONDS = 3600.0
DROP_LOG_INTERVAL_SECONDS = 10.0
ROUTE_SEND_CONCURRENCY = 4

TargetKind = Literal["group", "guild"]
RouteKey = tuple[str, TargetKind, str, str]
DeliveryStatus = Literal["sent", "defer", "drop"]
MEDIA_LABELS = {"image": "图片", "audio": "音频", "video": "视频"}


class _OneBotMediaConfirmationError(TimeoutError):
    """OneBot 富媒体发送在主超时及宽限期内均未返回。"""

    def __init__(self) -> None:
        super().__init__("OneBot 富媒体发送等待确认超时")


@dataclass(frozen=True, slots=True)
class _OutboundPayload:
    """一个实际 QQ API 调用对应的有序消息内容及失败回退。"""

    parts: tuple[MessagePart, ...]
    fallback_parts: tuple[MessagePart, ...] | None = None

    @property
    def has_media(self) -> bool:
        return any(isinstance(part, MediaPart) for part in self.parts)

    @property
    def has_group_mentions(self) -> bool:
        return any(isinstance(part, MentionPart) for part in self.parts)

    def fallback(self) -> "_OutboundPayload | None":
        if self.fallback_parts is None:
            return None
        return _OutboundPayload(self.fallback_parts)


def _media_failure_context(payload: _OutboundPayload | None) -> str:
    """Return log-safe media metadata without exposing paths or query strings."""
    if payload is None:
        return ""
    details = []
    for part in payload.parts:
        if isinstance(part, MediaPart):
            host = urlsplit(part.url).hostname or "未知主机"
            details.append(f"{part.media_type}@{host}")
    return ", ".join(details)


def _is_media_timeout(
    error: Exception,
    payload: _OutboundPayload | None,
) -> bool:
    error_text = str(error).casefold()
    return bool(
        payload is not None
        and payload.has_media
        and (
            isinstance(error, TimeoutError)
            or "timeout" in error_text
            or "timed out" in error_text
        )
    )


def _onebot_result_message_id(result: object) -> int | None:
    """从 OneBot send_group_msg 返回结果中提取 message_id。"""
    raw = result
    if isinstance(result, dict):
        raw = result.get("message_id") or result.get("message-id")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


async def _confirm_onebot_media_send(bot: OneBot, result: object) -> None:
    """send_group_msg 已返回时，尽量用返回的 message_id 查证消息存在。

    get_msg 只作为附加确认；实现不支持或查询失败时，仍以 send_group_msg
    的成功返回为准，避免把已经发出的消息误判为失败而重复回退。
    """
    message_id = _onebot_result_message_id(result)
    if plugin_config.mc_to_qq_rich_media_grace_seconds <= 0:
        return
    if message_id is None:
        logger.debug(
            "[MC_QQ]丨OneBot 富媒体结果未包含 message_id，"
            "按 send_group_msg 返回成功处理"
        )
        return
    try:
        await asyncio.wait_for(
            bot.get_msg(message_id=message_id),
            timeout=plugin_config.mc_to_qq_rich_media_grace_seconds,
        )
    except Exception as error:  # noqa: BLE001
        logger.debug(
            "[MC_QQ]丨OneBot get_msg 验证未完成，"
            f"仍以 send_group_msg 返回为准：{error!r}"
        )


async def _await_onebot_media_result(
    send_task: asyncio.Task[object],
) -> tuple[object, bool]:
    """富媒体主超时后不立即降级，继续宽限等待 message_id 返回。

    返回 ``(result, need_confirm)``；仅当主超时后才需要调用 get_msg 查证。
    """
    timeout = plugin_config.mc_to_qq_rich_media_timeout
    grace = plugin_config.mc_to_qq_rich_media_grace_seconds
    try:
        return await asyncio.wait_for(asyncio.shield(send_task), timeout=timeout), False
    except TimeoutError:
        # wait_for 也会原样传播底层协程自己抛出的 TimeoutError。若任务已经
        # 结束，直接读取其结果，避免把确定的适配器错误误判为外层主超时。
        if send_task.done():
            return send_task.result(), False
        if grace <= 0:
            raise
        logger.debug(
            f"[MC_QQ]丨OneBot 富媒体发送主超时，继续等待 {grace}s 以确认 message_id"
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(send_task),
                timeout=grace,
            )
        except TimeoutError:
            # 处理任务恰好在宽限边界结束的竞态；任务自身的 TimeoutError
            # 也应保留原始异常，而不是被改写成“等待确认超时”。
            if send_task.done():
                return send_task.result(), True
            raise _OneBotMediaConfirmationError from None
        return result, True


@dataclass(slots=True)
class _PendingText:
    """低资源待发项：可保留远程 URL，但不持有本地图片字节。"""

    sequence: int
    text: str
    parse_group_mentions: bool = False
    has_group_mentions: bool = False
    force_plain: bool = False
    payload: _OutboundPayload | None = None
    has_media: bool = False

    @property
    def structured(self) -> bool:
        return self.payload is not None


@dataclass(slots=True)
class _RouteState:
    """一个 Minecraft 服务器到单个 QQ 目标的发送状态。"""

    key: RouteKey
    bot_ids: tuple[str, ...]
    forward_batch_header: str
    pending: deque[_PendingText] = field(default_factory=deque)
    cursor: int = 0
    send_slots: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(ROUTE_SEND_CONCURRENCY)
    )
    flushing: bool = False
    active: bool = True

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


def _onebot_text_with_group_mentions(text: str) -> str | OneBotMessage:
    parts = parse_mc_message(
        text,
        parse_group_mentions=True,
        parse_rich_media=False,
    )
    if not any(isinstance(part, MentionPart) for part in parts):
        return text
    return _render_onebot_parts(tuple(parts))


def _qq_text_with_group_mentions(text: str) -> str | QQMessage:
    parts = parse_mc_message(
        text,
        parse_group_mentions=True,
        parse_rich_media=False,
    )
    if not any(isinstance(part, MentionPart) for part in parts):
        return text
    return _render_qq_parts(tuple(parts), allow_mentions=True, allow_media=True)


def _media_label(part: MediaPart) -> str:
    return f"[{MEDIA_LABELS[part.media_type]}: {part.name}]" if part.name else ""


def _parts_to_fallback_text(parts: tuple[MessagePart, ...]) -> str:
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            chunks.append(part.text)
        elif isinstance(part, MentionPart):
            chunks.append(part.raw)
        else:
            chunks.append(part.raw_fallback)
    return "".join(chunks)


def _fallback_parts(parts: tuple[MessagePart, ...]) -> tuple[MessagePart, ...]:
    fallback: list[MessagePart] = []
    for part in parts:
        candidate: MessagePart = (
            TextPart(part.raw_fallback) if isinstance(part, MediaPart) else part
        )
        if (
            fallback
            and isinstance(fallback[-1], TextPart)
            and isinstance(candidate, TextPart)
        ):
            fallback[-1] = TextPart(fallback[-1].text + candidate.text)
        else:
            fallback.append(candidate)
    return tuple(fallback)


def _render_onebot_parts(parts: tuple[MessagePart, ...]) -> OneBotMessage:
    message = OneBotMessage()
    for part in parts:
        if isinstance(part, TextPart):
            if part.text:
                message += OneBotMessageSegment.text(part.text)
        elif isinstance(part, MentionPart):
            message += OneBotMessageSegment.at(part.member_id)
        else:
            if label := _media_label(part):
                message += OneBotMessageSegment.text(label)
            if part.media_type == "image":
                message += OneBotMessageSegment.image(part.url)
            elif part.media_type == "audio":
                message += OneBotMessageSegment.record(part.url)
            else:
                message += OneBotMessageSegment.video(part.url)
    return message


def _render_qq_parts(  # noqa: C901
    parts: tuple[MessagePart, ...],
    *,
    allow_mentions: bool,
    allow_media: bool,
) -> QQMessage:
    message = QQMessage()
    for part in parts:
        if isinstance(part, TextPart):
            if part.text:
                message += QQMessageSegment.text(part.text)
        elif isinstance(part, MentionPart):
            if allow_mentions:
                message += QQMessageSegment.mention_user(part.member_id)
            elif part.raw:
                message += QQMessageSegment.text(part.raw)
        elif allow_media:
            if label := _media_label(part):
                message += QQMessageSegment.text(label)
            if part.media_type == "image":
                message += QQMessageSegment.image(part.url)
            elif part.media_type == "audio":
                message += QQMessageSegment.audio(part.url)
            else:
                message += QQMessageSegment.video(part.url)
        else:
            message += QQMessageSegment.text(part.raw_fallback)
    return message


def _coalesce_parts(parts: list[MessagePart]) -> tuple[MessagePart, ...]:
    result: list[MessagePart] = []
    for part in parts:
        if result and isinstance(result[-1], TextPart) and isinstance(part, TextPart):
            result[-1] = TextPart(result[-1].text + part.text)
        elif not isinstance(part, TextPart) or part.text:
            result.append(part)
    return tuple(result)


def _payload_or_plain(
    parts: tuple[MessagePart, ...],
) -> tuple[str, _OutboundPayload | None]:
    if not parts:
        return "", None
    if all(isinstance(part, TextPart) for part in parts):
        return "".join(part.text for part in parts if isinstance(part, TextPart)), None
    payload = _OutboundPayload(
        parts,
        _fallback_parts(parts)
        if any(isinstance(part, MediaPart) for part in parts)
        else None,
    )
    return _parts_to_fallback_text(parts), payload


def _compile_route_payloads(
    parts: tuple[MessagePart, ...],
    route: _RouteState,
) -> list[tuple[str, _OutboundPayload | None]]:
    if route.adapter == "onebot":
        text, payload = _payload_or_plain(parts)
        return [(text, payload)] if text or payload is not None else []

    units: list[tuple[str, _OutboundPayload | None]] = []
    buffer: list[MessagePart] = []
    for part in parts:
        if isinstance(part, MentionPart) and route.target_kind != "group":
            buffer.append(TextPart(part.raw))
            continue
        supported_media = isinstance(part, MediaPart) and (
            route.target_kind == "group" or part.media_type == "image"
        )
        if not supported_media:
            buffer.append(
                TextPart(part.raw_fallback) if isinstance(part, MediaPart) else part
            )
            continue

        buffer.append(part)
        unit_parts = _coalesce_parts(buffer)
        text, payload = _payload_or_plain(unit_parts)
        units.append((text, payload))
        buffer = []

    if trailing := _coalesce_parts(buffer):
        text, payload = _payload_or_plain(trailing)
        if text or payload is not None:
            units.append((text, payload))
    return units


def _filter_message_parts(
    parts: list[MessagePart],
) -> tuple[MessagePart, ...] | None:
    """仅过滤最终可见字段；任一 block 命中即屏蔽整个逻辑消息。"""
    filtered_parts: list[MessagePart] = []
    for part in parts:
        if isinstance(part, TextPart):
            cleaned_text = clean_minecraft_formatting(part.text)
            filtered_text = filter_current_sensitive_text(cleaned_text)
            if filtered_text is None:
                return None
            if filtered_text:
                filtered_parts.append(TextPart(filtered_text))
            continue
        if isinstance(part, MentionPart):
            filtered_parts.append(part)
            continue

        filtered_names: list[str] = []
        for name in part.name_values:
            cleaned_name = clean_minecraft_formatting(name)
            filtered_name = filter_current_sensitive_text(cleaned_name)
            if filtered_name is None:
                return None
            filtered_names.append(filtered_name)
        filtered_parts.append(replace_media_names(part, tuple(filtered_names)))
    return _coalesce_parts(filtered_parts)


def _parse_and_filter_message(
    text: str,
    server: Server,
    *,
    parse_group_mentions: bool,
    parse_rich_media: bool,
) -> tuple[MessagePart, ...] | None:
    rich_media_enabled = parse_rich_media and plugin_config.mc_to_qq_rich_media_enable
    media_template = (
        server.chat_upgrade_media_url_template
        or plugin_config.chat_upgrade_media_url_template
    )
    parts = parse_mc_message(
        text,
        parse_group_mentions=parse_group_mentions,
        parse_rich_media=rich_media_enabled,
        media_url_template=media_template,
        media_limit=plugin_config.mc_to_qq_max_media_per_message,
    )
    return _filter_message_parts(parts)


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
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._drop_counts: dict[str, int] = {}
        self._last_drop_log = 0.0

    @staticmethod
    def _group_route_targets(
        server: Server,
    ) -> OrderedDict[tuple[TargetKind, str, str], list[str]]:
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
        return grouped

    def routes_for_server(
        self,
        server_name: str,
        server: Server,
    ) -> list[_RouteState]:
        """合并服务器内的重复目标，并按配置顺序汇集候选 Bot。"""
        grouped = self._group_route_targets(server)

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

    def _drop_route_pending_locked(self, route: _RouteState) -> None:
        dropped_count = len(route.pending)
        for pending in route.pending:
            self._global_pending.pop(pending.sequence, None)
        route.pending.clear()
        if dropped_count:
            self._record_drop_locked("路由已删除", dropped_count)

    async def reconfigure(self) -> None:
        """Apply route and limit changes without rebuilding unused route objects."""

        desired_routes: dict[RouteKey, tuple[tuple[str, ...], str]] = {}
        for server_name, server in plugin_config.server_dict.items():
            for (target_kind, adapter, target_id), bot_ids in self._group_route_targets(
                server
            ).items():
                desired_routes[(server_name, target_kind, adapter, target_id)] = (
                    tuple(bot_ids),
                    server.forward_batch_header,
                )

        async with self._state_lock:
            for route_key, route in tuple(self._routes.items()):
                desired = desired_routes.get(route_key)
                if desired is None:
                    route.active = False
                    self._drop_route_pending_locked(route)
                    del self._routes[route_key]
                    continue

                route.bot_ids, route.forward_batch_header = desired
                route.active = True
                if route.bot_ids:
                    route.cursor %= len(route.bot_ids)
                else:
                    route.cursor = 0

                while len(route.pending) > plugin_config.send_route_queue_max_messages:
                    dropped = route.pending.popleft()
                    self._global_pending.pop(dropped.sequence, None)
                    self._record_drop_locked("达到单路由上限")

            while (
                len(self._global_pending) > plugin_config.send_global_queue_max_messages
            ):
                self._drop_global_oldest_locked()

            enabled_limits = {
                bot_id
                for bot_id, limit in plugin_config.bot_rate_limits.items()
                if limit.rpm or limit.rph
            }
            for bot_id in tuple(self._windows):
                if bot_id not in enabled_limits:
                    del self._windows[bot_id]

            self._wake_event.set()

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

    def _enqueue_locked(
        self,
        route: _RouteState,
        text: str,
        *,
        parse_group_mentions: bool = False,
        payload: _OutboundPayload | None = None,
    ) -> bool:
        if not text and payload is None:
            return False

        if len(route.pending) >= plugin_config.send_route_queue_max_messages:
            dropped = route.pending.popleft()
            self._global_pending.pop(dropped.sequence, None)
            self._record_drop_locked("达到单路由上限")

        while len(self._global_pending) >= plugin_config.send_global_queue_max_messages:
            self._drop_global_oldest_locked()

        self._sequence += 1
        has_group_mentions = route.target_kind == "group" and (
            (payload is not None and payload.has_group_mentions)
            or (parse_group_mentions and GROUP_MENTION_PATTERN.search(text) is not None)
        )
        pending = _PendingText(
            sequence=self._sequence,
            text=text,
            parse_group_mentions=parse_group_mentions,
            has_group_mentions=has_group_mentions,
            payload=payload,
            has_media=payload is not None and payload.has_media,
        )
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

        first = route.pending[0]
        batch_key = (
            first.force_plain,
            first.parse_group_mentions,
            first.has_group_mentions,
            first.structured,
            first.has_media,
        )
        batch: list[_PendingText] = []
        while (
            route.pending
            and len(batch)
            < (1 if first.structured else plugin_config.send_batch_max_messages)
            and (
                route.pending[0].force_plain,
                route.pending[0].parse_group_mentions,
                route.pending[0].has_group_mentions,
                route.pending[0].structured,
                route.pending[0].has_media,
            )
            == batch_key
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
        if not route.active:
            self._record_drop_locked("路由已删除", len(batch))
            return

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

    async def dispatch(  # noqa: C901, PLR0912, PLR0913
        self,
        route: _RouteState,
        text: str,
        img_bytes: bytes | None,
        *,
        queue_when_limited: bool,
        parse_group_mentions: bool = False,
        payload: _OutboundPayload | None = None,
    ) -> None:
        async with route.send_slots:
            async with self._state_lock:
                if route.pending or (route.flushing and queue_when_limited):
                    if queue_when_limited:
                        queued = self._enqueue_locked(
                            route,
                            text,
                            parse_group_mentions=parse_group_mentions,
                            payload=payload,
                        )
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
            media_failure_context = _media_failure_context(payload)
            while True:
                async with self._state_lock:
                    selection = self._select_bot_locked(route, attempted_bot_ids)

                if selection.bot is None:
                    if selection.online_count and selection.retry_after is not None:
                        if queue_when_limited:
                            async with self._state_lock:
                                queued = self._enqueue_locked(
                                    route,
                                    text,
                                    parse_group_mentions=parse_group_mentions,
                                    payload=payload,
                                )
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
                        return

                    if (
                        attempted_bot_ids
                        and payload is not None
                        and (fallback := payload.fallback()) is not None
                    ):
                        logger.warning(
                            f"[MC_QQ]丨发送至 {route.target_id} 的富媒体失败，"
                            "仅回退该发送单元的原始 bracket 文本"
                        )
                        fallback_text = _parts_to_fallback_text(fallback.parts)
                        async with self._state_lock:
                            fallback_selection = self._select_bot_locked(route, set())
                        if fallback_selection.bot is None:
                            logger.warning(
                                f"[MC_QQ]丨发送至 {route.target_id} 的 bracket "
                                "回退无可用 Bot，已丢弃该回退"
                            )
                            return

                        fallback_bot_id = str(fallback_selection.bot.self_id)
                        try:
                            await self._send_immediate_api(
                                fallback_selection.bot,
                                route,
                                fallback_text,
                                None,
                                parse_group_mentions=False,
                                payload=fallback,
                            )
                        except Exception as error:  # noqa: BLE001
                            logger.error(
                                f"[MC_QQ]丨Bot {fallback_bot_id} 发送回退文本至 "
                                f"{route.target_id} 出现异常：{error!r}"
                            )
                        return

                    if attempted_bot_ids:
                        media_log = (
                            f"；失败媒体：{media_failure_context}"
                            if media_failure_context
                            else ""
                        )
                        logger.error(
                            f"[MC_QQ]丨发送至 {route.target_id} 失败，"
                            f"所有在线候选 Bot 均已尝试{media_log}"
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
                        parse_group_mentions=parse_group_mentions,
                        payload=payload,
                    )
                except Exception as error:  # noqa: BLE001
                    attempted_bot_ids.add(bot_id)
                    if _is_media_timeout(error, payload):
                        attempted_bot_ids.update(route.bot_ids)
                        logger.warning(
                            f"[MC_QQ]丨Bot {bot_id} 富媒体发送确认超时，"
                            "跳过其余候选 Bot 并降级"
                        )
                    logger.error(
                        f"[MC_QQ]丨Bot {bot_id} 发送至 {route.target_id} "
                        f"出现异常：{error!r}"
                    )
                else:
                    return

    @staticmethod
    async def _send_onebot_group_api(  # noqa: PLR0912, PLR0913
        bot: OneBot,
        route: _RouteState,
        text: str,
        img_bytes: bytes | None,
        *,
        parse_group_mentions: bool,
        payload: _OutboundPayload | None = None,
    ) -> None:
        rendered_text: str | OneBotMessage
        if payload is not None:
            rendered_text = _render_onebot_parts(payload.parts)
        elif parse_group_mentions:
            rendered_text = _onebot_text_with_group_mentions(text)
        else:
            rendered_text = text
        if img_bytes:
            if isinstance(rendered_text, OneBotMessage):
                message = rendered_text
            else:
                message = OneBotMessage()
                if rendered_text:
                    message += OneBotMessageSegment.text(rendered_text)
            message += OneBotMessageSegment.image(img_bytes)
        else:
            message = rendered_text
        send_call = bot.send_group_msg(
            group_id=int(route.target_id),
            message=message,
        )
        if payload is not None and payload.has_media:
            send_task = asyncio.create_task(send_call)
            try:
                result, need_confirm = await _await_onebot_media_result(send_task)
                if need_confirm:
                    await _confirm_onebot_media_send(bot, result)
            finally:
                if not send_task.done():
                    send_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await send_task
        else:
            await send_call

    @staticmethod
    async def _send_qq_group_api(  # noqa: PLR0913
        bot: QQBot,
        route: _RouteState,
        text: str,
        img_bytes: bytes | None,
        *,
        parse_group_mentions: bool,
        payload: _OutboundPayload | None = None,
    ) -> None:
        rendered_text: str | QQMessage
        if payload is not None:
            rendered_text = _render_qq_parts(
                payload.parts,
                allow_mentions=True,
                allow_media=True,
            )
        elif parse_group_mentions:
            rendered_text = _qq_text_with_group_mentions(text)
        else:
            rendered_text = text
        if img_bytes:
            if isinstance(rendered_text, QQMessage):
                message = rendered_text
            else:
                message = QQMessage()
                if rendered_text:
                    message += QQMessageSegment.text(rendered_text)
            message += QQMessageSegment.file_image(img_bytes)
            await bot.send_to_group(
                group_openid=route.target_id,
                message=message,
            )
        elif isinstance(rendered_text, QQMessage):
            await bot.send_to_group(
                group_openid=route.target_id,
                message=rendered_text,
            )
        else:
            await bot.post_group_messages(
                group_openid=route.target_id,
                msg_type=0,
                content=rendered_text,
            )

    @staticmethod
    async def _send_qq_channel_api(
        bot: QQBot,
        route: _RouteState,
        text: str,
        img_bytes: bytes | None,
        *,
        payload: _OutboundPayload | None = None,
    ) -> None:
        if payload is not None:
            message: str | QQMessage = _render_qq_parts(
                payload.parts,
                allow_mentions=False,
                allow_media=True,
            )
        elif img_bytes:
            message = QQMessage()
            if text:
                message += QQMessageSegment.text(text)
        else:
            message = text
        if img_bytes:
            if not isinstance(message, QQMessage):
                rendered = QQMessage()
                if message:
                    rendered += QQMessageSegment.text(message)
                message = rendered
            message += QQMessageSegment.file_image(img_bytes)
        await bot.send_to_channel(
            channel_id=route.target_id,
            message=message,
        )

    async def _send_immediate_api(  # noqa: PLR0913
        self,
        bot: OneBot | QQBot,
        route: _RouteState,
        text: str,
        img_bytes: bytes | None,
        *,
        parse_group_mentions: bool = False,
        payload: _OutboundPayload | None = None,
    ) -> None:
        try:
            if isinstance(bot, OneBot):
                await self._send_onebot_group_api(
                    bot,
                    route,
                    text,
                    img_bytes,
                    parse_group_mentions=parse_group_mentions,
                    payload=payload,
                )
                return

            if route.target_kind == "group":
                await self._send_qq_group_api(
                    bot,
                    route,
                    text,
                    img_bytes,
                    parse_group_mentions=parse_group_mentions,
                    payload=payload,
                )
            else:
                await self._send_qq_channel_api(
                    bot,
                    route,
                    text,
                    img_bytes,
                    payload=payload,
                )
        except AuditException as error:
            self._schedule_audit_result(error, route)

    def _schedule_audit_result(
        self,
        error: AuditException,
        route: _RouteState,
    ) -> None:
        task = asyncio.create_task(
            self._handle_audit(error, route),
            name=f"mcqq-audit-{route.target_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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
        *,
        parse_group_mentions: bool,
        payload: _OutboundPayload | None = None,
    ) -> None:
        await self._send_immediate_api(
            bot,
            route,
            text,
            None,
            parse_group_mentions=parse_group_mentions,
            payload=payload,
        )

    async def _attempt_plain_batch(  # noqa: C901, PLR0912
        self,
        route: _RouteState,
        batch: list[_PendingText],
    ) -> tuple[DeliveryStatus, float | None]:
        attempted_bot_ids: set[str] = set()
        pending = batch[0]
        payload = pending.payload if len(batch) == 1 else None
        text = (
            pending.text
            if payload is not None
            else self._plain_batch_text(route, batch)
        )
        parse_group_mentions = batch[0].parse_group_mentions
        media_failure_context = _media_failure_context(payload)

        while True:
            selection = await self._reserve_for_batch(route, attempted_bot_ids)
            if selection.bot is None:
                if selection.online_count and selection.retry_after is not None:
                    return "defer", selection.retry_after
                if (
                    attempted_bot_ids
                    and payload is not None
                    and (fallback := payload.fallback()) is not None
                ):
                    logger.warning(
                        f"[MC_QQ]丨积压富媒体发送至 {route.target_id} 失败，"
                        "仅回退该发送单元的原始 bracket 文本"
                    )
                    fallback_text = _parts_to_fallback_text(fallback.parts)
                    pending.payload = fallback
                    pending.text = fallback_text
                    pending.has_media = False
                    pending.force_plain = True

                    fallback_selection = await self._reserve_for_batch(route, set())
                    if fallback_selection.bot is None:
                        logger.warning(
                            f"[MC_QQ]丨积压富媒体回退至 {route.target_id} "
                            "无可用 Bot，已丢弃该回退"
                        )
                        return "drop", None

                    fallback_bot_id = str(fallback_selection.bot.self_id)
                    try:
                        await self._send_plain_batch_api(
                            fallback_selection.bot,
                            route,
                            fallback_text,
                            parse_group_mentions=False,
                            payload=fallback,
                        )
                    except Exception as error:  # noqa: BLE001
                        logger.error(
                            f"[MC_QQ]丨Bot {fallback_bot_id} 发送回退文本至 "
                            f"{route.target_id} 出现异常：{error!r}"
                        )
                        return "drop", None
                    else:
                        return "sent", None
                if attempted_bot_ids:
                    media_log = (
                        f"；失败媒体：{media_failure_context}"
                        if media_failure_context
                        else ""
                    )
                    logger.error(
                        f"[MC_QQ]丨消息批次发送至 {route.target_id} 失败，"
                        f"所有在线候选 Bot 均已尝试{media_log}"
                    )
                else:
                    logger.error(
                        f"[MC_QQ]丨消息批次发送至 {route.target_id} 失败，"
                        "没有匹配且在线的候选 Bot"
                    )
                return "drop", None

            bot_id = str(selection.bot.self_id)
            try:
                if payload is None:
                    await self._send_plain_batch_api(
                        selection.bot,
                        route,
                        text,
                        parse_group_mentions=parse_group_mentions,
                    )
                else:
                    await self._send_plain_batch_api(
                        selection.bot,
                        route,
                        text,
                        parse_group_mentions=parse_group_mentions,
                        payload=payload,
                    )
            except Exception as error:  # noqa: BLE001
                attempted_bot_ids.add(bot_id)
                if _is_media_timeout(error, payload):
                    attempted_bot_ids.update(route.bot_ids)
                    logger.warning(
                        f"[MC_QQ]丨Bot {bot_id} 积压富媒体发送确认超时，"
                        "跳过其余候选 Bot 并降级"
                    )
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
            and not batch[0].has_group_mentions
            and not batch[0].structured
            and not batch[0].has_media
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
        async with route.send_slots:
            async with self._state_lock:
                batch = self._take_batch_locked(route)
                if batch:
                    route.flushing = True
            if not batch:
                return False, None

            try:
                status, retry_after = await self._deliver_queued_batch(route, batch)
            finally:
                async with self._state_lock:
                    route.flushing = False
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

        background_tasks = tuple(self._background_tasks)
        for background_task in background_tasks:
            background_task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        async with self._state_lock:
            self._global_pending.clear()
            for route in self._routes.values():
                route.pending.clear()
            self._windows.clear()


_dispatcher = _SendDispatcher()


async def send_mc_msg_to_qq(  # noqa: PLR0913
    server_name: str,
    result: str,
    img_bytes: bytes | None = None,
    *,
    queue_when_limited: bool = True,
    parse_group_mentions: bool = False,
    parse_rich_media: bool = False,
) -> None:
    """发送 MC 消息；时效性事件可禁止在限流时进入待发队列。"""
    server = plugin_config.server_dict.get(server_name)
    if server is None:
        logger.error(f"未知的服务器: {server_name}")
        return

    msg_result = result
    if plugin_config.display_server_name:
        display_name = server.nickname or f"[{server_name}]"
        msg_result = f"{display_name} {msg_result}"

    parts = _parse_and_filter_message(
        msg_result,
        server,
        parse_group_mentions=parse_group_mentions,
        parse_rich_media=parse_rich_media,
    )
    if parts is None:
        logger.info(f"[MC_QQ]丨服务器 {server_name} 的消息命中敏感词，已屏蔽")
        return

    routes = _dispatcher.routes_for_server(server_name, server)
    for route in routes:
        units = (
            _compile_route_payloads(parts, route)
            if isinstance(route, _RouteState)
            else [_payload_or_plain(parts)]
        )
        for index, (unit_text, payload) in enumerate(units):
            unit_image = img_bytes if index == len(units) - 1 else None
            if payload is None:
                await _dispatcher.dispatch(
                    route,
                    unit_text,
                    unit_image,
                    queue_when_limited=queue_when_limited,
                    parse_group_mentions=parse_group_mentions,
                )
            else:
                await _dispatcher.dispatch(
                    route,
                    unit_text,
                    unit_image,
                    queue_when_limited=queue_when_limited,
                    parse_group_mentions=parse_group_mentions,
                    payload=payload,
                )


async def reconfigure_dispatcher() -> None:
    """Apply hot-reloaded route and limit settings to live dispatcher state."""

    await _dispatcher.reconfigure()


@get_driver().on_shutdown
async def _shutdown_send_dispatcher() -> None:
    """关闭时释放唯一调度任务及全部内存队列。"""
    await _dispatcher.shutdown()
