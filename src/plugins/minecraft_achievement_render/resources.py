from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from weakref import WeakValueDictionary

import httpx
from nonebot import get_driver, logger  # pyright: ignore[reportMissingImports]
from PIL import Image, ImageDraw, UnidentifiedImageError

from .config import plugin_config

MODULE_DIR = Path(__file__).parent
CACHE_DIR = MODULE_DIR / "cache"
ADVANCEMENTS_FILE = MODULE_DIR / "templates" / "advancements.json"
ITEM_ASSET_ROOT = (
    "https://raw.githubusercontent.com/Owen1212055/mc-assets/main/item-assets/"
)
LIGHT_ASSET_VERSION = "26.2"
LIGHT_ASSET_ROOT = (
    "https://raw.githubusercontent.com/InventivetalentDev/minecraft-assets/"
    f"{LIGHT_ASSET_VERSION}/assets/minecraft/textures/item/"
)
MOJANG_NAME_URL = "https://api.mojang.com/users/profiles/minecraft/{name}"
MOJANG_PROFILE_URL = (
    "https://sessionserver.mojang.com/session/minecraft/profile/{uuid}?unsigned=true"
)
PROFILE_CACHE_TTL = 24 * 60 * 60
NEGATIVE_CACHE_TTL = 10 * 60
HTTP_TIMEOUT = 10.0
HTTP_MAX_CONNECTIONS = 10
HTTP_MAX_KEEPALIVE_CONNECTIONS = 5
COMPONENT_CACHE_SIZE = 32
PROFILE_UUID_INT_PARTS = 4
MIN_SKIN_WIDTH = 64
MIN_SKIN_HEIGHT = 32
LIGHT_LEVELS = range(16)
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")

CACHE_DIR.mkdir(parents=True, exist_ok=True)

with ADVANCEMENTS_FILE.open(encoding="utf-8") as file:
    _raw_advancements: dict[str, Any] = json.load(file)


@dataclass(frozen=True, slots=True)
class ItemIconSpec:
    """A normalized advancement icon definition."""

    item_id: str
    components: Mapping[str, Any]


class MojangRateLimiter:
    """A non-blocking rolling-window limiter for Mojang profile requests."""

    def __init__(
        self,
        limit: int = 600,
        window_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._requests: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    def _discard_expired(self, now: float) -> None:
        boundary = now - self.window_seconds
        while self._requests and self._requests[0] <= boundary:
            self._requests.popleft()

    async def acquire(self) -> bool:
        """Consume one request slot, returning false instead of waiting."""

        async with self._lock:
            now = self._clock()
            self._discard_expired(now)
            if now < self._blocked_until or len(self._requests) >= self.limit:
                return False
            self._requests.append(now)
            return True

    async def defer(self, seconds: float | None = None) -> None:
        """Block new requests after a 429 response."""

        async with self._lock:
            now = self._clock()
            self._discard_expired(now)
            if seconds is None:
                if self._requests:
                    seconds = max(
                        0.0,
                        self._requests[0] + self.window_seconds - now,
                    )
                else:
                    seconds = self.window_seconds
            self._blocked_until = max(self._blocked_until, now + seconds)


mojang_rate_limiter = MojangRateLimiter()

_resource_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_component_cache: OrderedDict[tuple[str, str], Image.Image] = OrderedDict()
_http_client: httpx.AsyncClient | None = None


def _lock_for(key: str) -> asyncio.Lock:
    lock = _resource_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _resource_locks[key] = lock
    return lock


def _normalize_component_key(key: object) -> str:
    return str(key).removeprefix("minecraft:")


def _normalize_item_id(item_id: str) -> str:
    value = item_id.strip().lower()
    if not value:
        return "minecraft:barrier"
    if ":" not in value:
        return f"minecraft:{value}"
    return value


FALLBACK_ICON_SPEC = ItemIconSpec("minecraft:barrier", {})


def _normalize_icon_spec(key: str, icon: object) -> ItemIconSpec:
    if isinstance(icon, str):
        return ItemIconSpec(_normalize_item_id(icon), {})
    if not isinstance(icon, Mapping):
        logger.warning(f"成就图标定义格式无效: {key}")
        return FALLBACK_ICON_SPEC

    item_id = icon.get("id") or icon.get("item") or FALLBACK_ICON_SPEC.item_id
    raw_components = icon.get("components")
    components = (
        {
            _normalize_component_key(component_key): component_value
            for component_key, component_value in raw_components.items()
        }
        if isinstance(raw_components, Mapping)
        else {}
    )
    return ItemIconSpec(_normalize_item_id(str(item_id)), components)


# Normalize once at import; advancement lookups are on every rendered card.
icon_specs = {
    key: _normalize_icon_spec(key, icon) for key, icon in _raw_advancements.items()
}
del _raw_advancements


def get_icon_spec(key: str | None) -> ItemIconSpec:
    """Resolve an advancement key into a normalized item specification."""

    if not key:
        return FALLBACK_ICON_SPEC
    spec = icon_specs.get(key)
    if spec is None:
        logger.warning(f"找不到对应的成就图标键: {key}")
        return FALLBACK_ICON_SPEC
    return spec


def _item_filename(item_id: str) -> str:
    path = _normalize_item_id(item_id).split(":", maxsplit=1)[-1]
    safe_path = re.sub(r"[^a-z0-9_./-]", "_", path)
    return f"{safe_path.replace('/', '_').upper()}.png"


def _light_filename(block_state: object) -> str:
    if not isinstance(block_state, Mapping):
        return "light.png"

    raw_level = block_state.get("level")
    if isinstance(raw_level, bool) or not isinstance(raw_level, (int, str)):
        return "light.png"
    try:
        level = int(raw_level)
    except ValueError:
        return "light.png"
    if level not in LIGHT_LEVELS:
        return "light.png"
    return f"light_{level:02d}.png"


def _decode_image(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        return source.convert("RGBA")


def _read_cached_image(path: Path) -> Image.Image | None:
    try:
        return _decode_image(path.read_bytes())
    except (OSError, UnidentifiedImageError, ValueError):
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_bytes(data)
    temp_path.replace(path)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write(
        path,
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
    )


def _get_http_client() -> httpx.AsyncClient:
    global _http_client  # noqa: PLW0603

    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=HTTP_MAX_CONNECTIONS,
                max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS,
            ),
        )
    return _http_client


@get_driver().on_shutdown
async def _close_http_client() -> None:
    global _http_client  # noqa: PLW0603

    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


async def _download_bytes(url: str) -> bytes | None:
    try:
        response = await _get_http_client().get(url)
    except httpx.HTTPError as error:
        logger.warning(f"下载资源失败: {url}, {error}")
        return None
    if response.status_code == httpx.codes.OK:
        return response.content
    logger.warning(f"下载资源失败: {url}, HTTP {response.status_code}")
    return None


async def _load_cached_image(
    cache_path: Path,
    url: str,
    lock_key: str,
    *,
    minimum_size: tuple[int, int] | None = None,
) -> Image.Image | None:
    async with _lock_for(lock_key):
        image = _read_cached_image(cache_path)
        if image is not None and (
            minimum_size is None
            or (image.width >= minimum_size[0] and image.height >= minimum_size[1])
        ):
            return image

        data = await _download_bytes(url)
        if data is None:
            return None
        try:
            image = _decode_image(data)
        except (UnidentifiedImageError, OSError, ValueError):
            logger.warning(f"下载到的资源不是有效图片: {url}")
            return None
        if minimum_size is not None and (
            image.width < minimum_size[0] or image.height < minimum_size[1]
        ):
            logger.warning(f"下载到的资源尺寸无效: {url}")
            return None
        _atomic_write(cache_path, data)
        return image


def _placeholder_icon() -> Image.Image:
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 216, 216), fill=(35, 35, 35, 235))
    draw.line((65, 65, 191, 191), fill=(220, 45, 45, 255), width=28)
    draw.line((191, 65, 65, 191), fill=(220, 45, 45, 255), width=28)
    return image


PLACEHOLDER_ICON = _placeholder_icon()


async def load_base_item_image(
    item_id: str,
    prefix: str = "",
) -> Image.Image:
    """Load and validate a base item render, with a guaranteed fallback."""

    filename = _item_filename(item_id)
    cache_path = CACHE_DIR / filename
    url = f"{prefix}{ITEM_ASSET_ROOT}{filename}"
    image = await _load_cached_image(cache_path, url, f"item:{filename}")
    return image if image is not None else PLACEHOLDER_ICON.copy()


async def load_light_item_image(
    block_state: object,
    prefix: str = "",
) -> Image.Image:
    """Load a Light icon variant selected by its block-state level."""

    filename = _light_filename(block_state)
    cache_path = CACHE_DIR / "light_assets" / LIGHT_ASSET_VERSION / filename
    url = f"{prefix}{LIGHT_ASSET_ROOT}{filename}"
    image = await _load_cached_image(
        cache_path,
        url,
        f"light:{LIGHT_ASSET_VERSION}:{filename}",
    )
    return image if image is not None else PLACEHOLDER_ICON.copy()


def _normalize_texture_path(texture_path: str) -> str | None:
    value = texture_path.strip().lower().replace("\\", "/")
    value = value.removeprefix("minecraft:").removeprefix("textures/")
    if not value.endswith(".png"):
        value = f"{value}.png"
    if not re.fullmatch(r"[a-z0-9_./-]+\.png", value):
        return None
    if value.startswith("/") or ".." in value.split("/"):
        return None
    return value


def get_component_asset_version() -> str:
    """Return the currently active vanilla component texture version."""

    return plugin_config.component_asset_version


async def load_component_texture(
    texture_path: str,
    prefix: str = "",
) -> Image.Image | None:
    """Load a raw vanilla texture used by an item component renderer.

    Cached images are shared as immutable source textures. Callers must not mutate
    them in place.
    """

    normalized_path = _normalize_texture_path(texture_path)
    if normalized_path is None:
        logger.warning(f"无效的组件纹理路径: {texture_path}")
        return None

    version = get_component_asset_version()
    cache_key = (version, normalized_path)
    cached = _component_cache.get(cache_key)
    if cached is not None:
        _component_cache.move_to_end(cache_key)
        return cached

    cache_path = CACHE_DIR / "component_assets" / version / Path(normalized_path)
    component_asset_root = (
        "https://raw.githubusercontent.com/InventivetalentDev/minecraft-assets/"
        f"{version}/assets/minecraft/textures/"
    )
    url = f"{prefix}{component_asset_root}{normalized_path}"
    image = await _load_cached_image(
        cache_path,
        url,
        f"component:{version}:{normalized_path}",
    )
    if image is None:
        return None
    _component_cache[cache_key] = image
    _component_cache.move_to_end(cache_key)
    if len(_component_cache) > COMPONENT_CACHE_SIZE:
        _component_cache.popitem(last=False)
    return image


def normalize_profile_uuid(value: object) -> str | None:
    """Normalize UUID strings or Minecraft's four-signed-int representation."""

    if isinstance(value, (list, tuple)) and len(value) == PROFILE_UUID_INT_PARTS:
        try:
            hex_value = "".join(f"{int(part) & 0xFFFFFFFF:08x}" for part in value)
            return uuid.UUID(hex=hex_value).hex
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return uuid.UUID(value).hex
        except ValueError:
            return None
    return None


def _decode_texture_property(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, validate=True)
        payload = json.loads(decoded.decode("utf-8"))
        url = payload["textures"]["SKIN"]["url"]
    except (ValueError, UnicodeDecodeError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return _validate_skin_url(url)


def _validate_skin_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        return None
    if (parts.hostname or "").lower() != "textures.minecraft.net":
        return None
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))


def _texture_url_from_properties(properties: object) -> str | None:
    if isinstance(properties, Mapping):
        properties = [properties]
    if not isinstance(properties, list):
        return None
    for prop in properties:
        if not isinstance(prop, Mapping) or prop.get("name") != "textures":
            continue
        url = _decode_texture_property(prop.get("value"))
        if url is not None:
            return url
    return None


def _read_json_cache(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _cache_is_fresh(record: Mapping[str, Any], ttl: float) -> bool:
    fetched_at = record.get("fetched_at")
    return isinstance(fetched_at, (int, float)) and time.time() - fetched_at < ttl


def _profile_cache_path(kind: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()
    return CACHE_DIR / "profiles" / f"{kind}-{digest}.json"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                (retry_at - datetime.now(tz=timezone.utc)).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            return None


async def _request_mojang_json(  # noqa: PLR0911
    url: str,
) -> tuple[dict[str, Any] | None, str]:
    if not await mojang_rate_limiter.acquire():
        return None, "limited"
    try:
        response = await _get_http_client().get(url)
    except httpx.HTTPError as error:
        logger.warning(f"Mojang API 请求失败: {url}, {error}")
        return None, "error"

    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        await mojang_rate_limiter.defer(_retry_after_seconds(response))
        logger.warning("Mojang API 已限流，玩家头将使用缓存或默认图标")
        return None, "limited"
    if response.status_code in {httpx.codes.NO_CONTENT, httpx.codes.NOT_FOUND}:
        return None, "missing"
    if response.status_code != httpx.codes.OK:
        logger.warning(f"Mojang API 请求失败: {url}, HTTP {response.status_code}")
        return None, "error"
    try:
        value = response.json()
    except json.JSONDecodeError:
        return None, "error"
    if not isinstance(value, dict):
        return None, "error"
    return value, "ok"


def _profile_cache_state(
    path: Path,
    field: str,
    normalize: Callable[[object], str | None],
) -> tuple[str | None, bool, bool]:
    record = _read_json_cache(path)
    if record is None:
        return None, False, False
    missing = record.get("missing") is True
    ttl = NEGATIVE_CACHE_TTL if missing else PROFILE_CACHE_TTL
    return normalize(record.get(field)), _cache_is_fresh(record, ttl), missing


def _write_profile_cache(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_json(path, {**value, "fetched_at": time.time()})


async def _resolve_name_to_uuid(name: str) -> str | None:
    normalized_name = name.lower()
    cache_path = _profile_cache_path("name", normalized_name)
    lock = _lock_for(f"profile-name:{normalized_name}")
    async with lock:
        stale_uuid, fresh, missing = _profile_cache_state(
            cache_path,
            "uuid",
            normalize_profile_uuid,
        )
        if fresh:
            return None if missing else stale_uuid

        payload, status = await _request_mojang_json(
            MOJANG_NAME_URL.format(name=name),
        )
        if status == "ok" and payload is not None:
            resolved_uuid = normalize_profile_uuid(payload.get("id"))
            if resolved_uuid is not None:
                _write_profile_cache(cache_path, {"uuid": resolved_uuid})
                return resolved_uuid
        elif status == "missing":
            _write_profile_cache(cache_path, {"missing": True})
            return None
        return stale_uuid


async def _resolve_uuid_to_texture(profile_uuid: str) -> str | None:
    cache_path = _profile_cache_path("uuid", profile_uuid)
    lock = _lock_for(f"profile-uuid:{profile_uuid}")
    async with lock:
        stale_url, fresh, missing = _profile_cache_state(
            cache_path,
            "texture_url",
            _validate_skin_url,
        )
        if fresh:
            return None if missing else stale_url

        payload, status = await _request_mojang_json(
            MOJANG_PROFILE_URL.format(uuid=profile_uuid),
        )
        if status == "ok" and payload is not None:
            texture_url = _texture_url_from_properties(payload.get("properties"))
            if texture_url is not None:
                _write_profile_cache(cache_path, {"texture_url": texture_url})
                return texture_url
        elif status == "missing":
            _write_profile_cache(cache_path, {"missing": True})
            return None
        return stale_url


async def load_skin_image(texture_url: str) -> Image.Image | None:
    """Download and permanently cache a content-addressed Minecraft skin."""

    validated_url = _validate_skin_url(texture_url)
    if validated_url is None:
        return None
    digest = hashlib.sha256(validated_url.encode()).hexdigest()
    cache_path = CACHE_DIR / "skins" / f"{digest}.png"
    return await _load_cached_image(
        cache_path,
        validated_url,
        f"skin:{digest}",
        minimum_size=(MIN_SKIN_WIDTH, MIN_SKIN_HEIGHT),
    )


async def resolve_player_skin(profile: object) -> Image.Image | None:
    """Resolve a player-head component into a skin without blocking on limits."""

    texture_url: str | None = None
    profile_uuid: str | None = None
    profile_name: str | None = None

    if isinstance(profile, str):
        profile_name = profile if PLAYER_NAME_RE.fullmatch(profile) else None
    elif isinstance(profile, Mapping):
        texture_url = _texture_url_from_properties(profile.get("properties"))
        profile_uuid = normalize_profile_uuid(profile.get("id"))
        raw_name = profile.get("name")
        if isinstance(raw_name, str) and PLAYER_NAME_RE.fullmatch(raw_name):
            profile_name = raw_name
    else:
        return None

    if texture_url is not None:
        skin = await load_skin_image(texture_url)
        if skin is not None:
            return skin

    if profile_uuid is None and profile_name is not None:
        profile_uuid = await _resolve_name_to_uuid(profile_name)
    if profile_uuid is None:
        return None

    resolved_url = await _resolve_uuid_to_texture(profile_uuid)
    if resolved_url is None:
        return None
    return await load_skin_image(resolved_url)


__all__ = [
    "ItemIconSpec",
    "MojangRateLimiter",
    "get_component_asset_version",
    "get_icon_spec",
    "load_base_item_image",
    "load_component_texture",
    "load_light_item_image",
    "load_skin_image",
    "normalize_profile_uuid",
    "resolve_player_skin",
]
