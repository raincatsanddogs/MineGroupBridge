"""Low-overhead, event-driven reload support for local configuration files."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nonebot import get_driver, logger
from watchfiles import Change, awatch

ReloadCallback = Callable[[Path], Awaitable[None] | None]
Fingerprint = tuple[str, bytes | str]


def replace_model_state(target: Any, source: Any) -> None:
    """Replace a Pydantic model's public state while preserving object identity."""

    object.__setattr__(target, "__dict__", source.__dict__.copy())
    attributes = (
        (
            "__pydantic_fields_set__",
            "__pydantic_extra__",
            "__pydantic_private__",
        )
        if hasattr(source, "__pydantic_fields_set__")
        else ("__fields_set__",)
    )
    for attribute in attributes:
        if not hasattr(source, attribute):
            continue
        value = getattr(source, attribute)
        if isinstance(value, (dict, set)):
            value = value.copy()
        object.__setattr__(target, attribute, value)


@dataclass(frozen=True, slots=True)
class _Registration:
    path: Path
    callback: ReloadCallback


class ConfigReloadService:
    """Watch exact files through one native watcher and no polling loop."""

    def __init__(self) -> None:
        self._registrations: dict[str, _Registration] = {}
        self._fingerprints: dict[Path, Fingerprint] = {}
        self._task: asyncio.Task[None] | None = None
        self._active_stop: asyncio.Event | None = None
        self._registrations_changed = asyncio.Event()
        self._closing = False

    @staticmethod
    def _normalize(path: Path | str) -> Path:
        return Path(path).resolve(strict=False)

    @staticmethod
    def _fingerprint(path: Path) -> Fingerprint:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return ("missing", "")
        except OSError as error:
            return ("error", f"{type(error).__name__}:{error}")
        return ("content", hashlib.blake2b(data, digest_size=16).digest())

    def register(
        self,
        key: str,
        path: Path | str,
        callback: ReloadCallback,
    ) -> None:
        """Register or replace one exact watched file."""

        normalized = self._normalize(path)
        previous = self._registrations.get(key)
        self._registrations[key] = _Registration(normalized, callback)
        self._fingerprints[normalized] = self._fingerprint(normalized)
        if (
            previous is not None
            and previous.path != normalized
            and not any(
                current.path == previous.path
                for current in self._registrations.values()
            )
        ):
            self._fingerprints.pop(previous.path, None)
        self._restart_active_watcher()

    def unregister(self, key: str) -> None:
        registration = self._registrations.pop(key, None)
        if registration is None:
            return
        if not any(
            current.path == registration.path
            for current in self._registrations.values()
        ):
            self._fingerprints.pop(registration.path, None)
        self._restart_active_watcher()

    def _restart_active_watcher(self) -> None:
        self._registrations_changed.set()
        if self._active_stop is not None:
            self._active_stop.set()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._closing = False
        self._task = asyncio.create_task(
            self._run(),
            name="config-reload-watcher",
        )

    async def shutdown(self) -> None:
        self._closing = True
        self._restart_active_watcher()
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self._task = None
        self._active_stop = None

    async def _dispatch_changes(self, changed_paths: set[Path]) -> None:
        callbacks: list[tuple[Path, ReloadCallback]] = []
        for path in changed_paths:
            fingerprint = self._fingerprint(path)
            if self._fingerprints.get(path) == fingerprint:
                continue
            self._fingerprints[path] = fingerprint
            callbacks.extend(
                (path, registration.callback)
                for registration in self._registrations.values()
                if registration.path == path
            )

        for path, callback in callbacks:
            await self._run_callback(path, callback)

    @staticmethod
    async def _run_callback(path: Path, callback: ReloadCallback) -> None:
        try:
            result = callback(path)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001
            logger.exception(f"重载配置文件失败：{path}")

    async def _run(self) -> None:
        while not self._closing:
            registrations = tuple(self._registrations.values())
            watched_paths = frozenset(item.path for item in registrations)
            watch_roots = tuple(
                sorted(
                    {path.parent for path in watched_paths if path.parent.is_dir()},
                    key=str,
                )
            )
            self._registrations_changed.clear()

            if not watch_roots:
                await self._registrations_changed.wait()
                continue

            stop_event = asyncio.Event()
            self._active_stop = stop_event

            def exact_file_filter(
                _change: Change,
                path: str,
                targets: frozenset[Path] = watched_paths,
            ) -> bool:
                return self._normalize(path) in targets

            try:
                async for changes in awatch(
                    *watch_roots,
                    watch_filter=exact_file_filter,
                    debounce=300,
                    step=50,
                    stop_event=stop_event,
                    recursive=False,
                    force_polling=False,
                ):
                    changed_paths = {
                        self._normalize(path)
                        for _change, path in changes
                        if self._normalize(path) in watched_paths
                    }
                    if changed_paths:
                        await self._dispatch_changes(changed_paths)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("配置文件监听器异常退出，等待监听目标更新后重试")
                await self._registrations_changed.wait()
            finally:
                if self._active_stop is stop_event:
                    self._active_stop = None


config_reload_service = ConfigReloadService()

driver = get_driver()


@driver.on_startup
async def _start_config_reload_service() -> None:
    config_reload_service.start()


@driver.on_shutdown
async def _stop_config_reload_service() -> None:
    await config_reload_service.shutdown()


__all__ = [
    "ConfigReloadService",
    "config_reload_service",
    "replace_model_state",
]
