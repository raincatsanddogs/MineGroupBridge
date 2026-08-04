from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal, TypeAlias
from urllib.parse import quote, unquote, urlsplit

MediaType: TypeAlias = Literal["image", "audio", "video"]
MediaProtocol: TypeAlias = Literal["CICode", "ChatUpgrade"]

GROUP_MENTION_PATTERN = re.compile(
    r" @[^\[\]\r\n]*?\[(?P<member_id>[^\s\[\]]+)\]"
)
TOKEN_PATTERN = re.compile(
    r"(?P<mention> @[^\[\]\r\n]*?\[(?P<member_id>[^\s\[\]]+)\])"
    r"|(?P<bracket>\[\[(?P<tag>CICode|ChatUpgrade)"
    r"(?P<arguments>,[^\]\r\n]*)?\]\])",
    re.IGNORECASE,
)
MINECRAFT_FORMAT_PATTERN = re.compile(r"[&§].")
VALID_MEDIA_TYPES = frozenset({"image", "audio", "video"})
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
PRIVATE_URI_PART_COUNT = 2


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str


@dataclass(frozen=True, slots=True)
class MentionPart:
    member_id: str
    raw: str


@dataclass(frozen=True, slots=True)
class MediaPart:
    media_type: MediaType
    url: str
    name: str | None
    protocol: MediaProtocol
    raw_fallback: str
    name_values: tuple[str, ...] = ()
    name_spans: tuple[tuple[int, int], ...] = ()


MessagePart: TypeAlias = TextPart | MentionPart | MediaPart


@dataclass(frozen=True, slots=True)
class _ParsedAttributes:
    values: dict[str, str]
    name_values: tuple[str, ...]
    name_spans: tuple[tuple[int, int], ...]


def clean_minecraft_formatting(text: str) -> str:
    """移除文本中的 Minecraft 色码；调用方负责先保护媒体 URL。"""
    return MINECRAFT_FORMAT_PATTERN.sub("", text)


def _append_text(parts: list[MessagePart], text: str) -> None:
    if not text:
        return
    if parts and isinstance(parts[-1], TextPart):
        parts[-1] = TextPart(parts[-1].text + text)
    else:
        parts.append(TextPart(text))


def _parse_attributes(raw: str) -> _ParsedAttributes:
    inner = raw[2:-2]
    comma_index = inner.find(",")
    if comma_index < 0:
        return _ParsedAttributes({}, (), ())

    values: dict[str, str] = {}
    name_values: list[str] = []
    name_spans: list[tuple[int, int]] = []
    attributes_start = 2 + comma_index + 1
    cursor = attributes_start
    raw_end = len(raw) - 2

    while cursor <= raw_end:
        next_comma = raw.find(",", cursor, raw_end)
        segment_end = raw_end if next_comma < 0 else next_comma
        segment = raw[cursor:segment_end]
        equals_index = segment.find("=")
        if equals_index >= 0:
            key = segment[:equals_index].strip().lower()
            raw_value = segment[equals_index + 1 :]
            leading = len(raw_value) - len(raw_value.lstrip())
            trailing = len(raw_value) - len(raw_value.rstrip())
            value_start = cursor + equals_index + 1 + leading
            value_end = segment_end - trailing
            value = raw[value_start:value_end]
            if key:
                values[key] = value
                if key == "name":
                    name_values.append(value)
                    name_spans.append((value_start, value_end))
        if next_comma < 0:
            break
        cursor = next_comma + 1

    return _ParsedAttributes(values, tuple(name_values), tuple(name_spans))


def _valid_http_url(url: str) -> bool:
    if CONTROL_CHARACTER_PATTERN.search(url):
        return False
    parsed = urlsplit(url)
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
    )


def _resolve_private_media_url(
    url: str,
    template: str | None,
) -> tuple[MediaType, str] | None:
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "chat-upgrade"
        or parsed.netloc.lower() != "media"
        or parsed.query
        or parsed.fragment
    ):
        return None
    path_parts = parsed.path.split("/")
    if (
        len(path_parts) != PRIVATE_URI_PART_COUNT + 1
        or path_parts[0]
    ):
        return None
    raw_type, encoded_media_id = path_parts[1:]
    uri_type = raw_type.lower()
    if uri_type not in VALID_MEDIA_TYPES or not template:
        return None

    media_id = unquote(encoded_media_id)
    if (
        not media_id
        or media_id in {".", ".."}
        or "/" in media_id
        or "\\" in media_id
        or CONTROL_CHARACTER_PATTERN.search(media_id)
    ):
        return None
    resolved = template.format(
        type=uri_type,
        media_id=quote(media_id, safe=""),
    )
    if not _valid_http_url(resolved):
        return None
    return uri_type, resolved  # type: ignore[return-value]


def _parse_media_part(  # noqa: PLR0911
    raw: str,
    tag: str,
    *,
    media_url_template: str | None,
) -> MediaPart | None:
    attributes = _parse_attributes(raw)
    url = attributes.values.get("url")
    if not url:
        return None

    normalized_tag = tag.lower()
    protocol: MediaProtocol = (
        "CICode" if normalized_tag == "cicode" else "ChatUpgrade"
    )
    explicit_type = attributes.values.get("type")
    normalized_explicit_type = explicit_type.lower() if explicit_type else None
    if normalized_explicit_type and normalized_explicit_type not in VALID_MEDIA_TYPES:
        return None

    private_media = _resolve_private_media_url(url, media_url_template)
    if urlsplit(url).scheme.lower() == "chat-upgrade":
        if private_media is None:
            return None
        uri_type, resolved_url = private_media
        if protocol == "CICode":
            if uri_type != "image":
                return None
            media_type: MediaType = "image"
        else:
            if normalized_explicit_type and normalized_explicit_type != uri_type:
                return None
            media_type = uri_type
        url = resolved_url
    else:
        if not _valid_http_url(url):
            return None
        if protocol == "CICode":
            media_type = "image"
        else:
            media_type = normalized_explicit_type or "image"  # type: ignore[assignment]

    return MediaPart(
        media_type=media_type,
        url=url,
        name=attributes.values.get("name"),
        protocol=protocol,
        raw_fallback=raw,
        name_values=attributes.name_values,
        name_spans=attributes.name_spans,
    )


def parse_mc_message(
    text: str,
    *,
    parse_group_mentions: bool,
    parse_rich_media: bool,
    media_url_template: str | None = None,
    media_limit: int = 4,
) -> list[MessagePart]:
    """按原始顺序一次扫描 Minecraft 文本中的 mention 与 bracket 标记。"""
    if not parse_group_mentions and not parse_rich_media:
        return [TextPart(text)] if text else []

    parts: list[MessagePart] = []
    cursor = 0
    media_count = 0
    for match in TOKEN_PATTERN.finditer(text):
        _append_text(parts, text[cursor : match.start()])
        raw = match.group(0)
        if match.group("mention") is not None:
            if parse_group_mentions:
                parts.append(MentionPart(match.group("member_id"), raw))
            else:
                _append_text(parts, raw)
        elif not parse_rich_media or media_count >= media_limit:
            _append_text(parts, raw)
        else:
            media = _parse_media_part(
                raw,
                match.group("tag"),
                media_url_template=media_url_template,
            )
            if media is None:
                _append_text(parts, raw)
            else:
                parts.append(media)
                media_count += 1
        cursor = match.end()
    _append_text(parts, text[cursor:])
    return parts


def replace_media_names(part: MediaPart, names: tuple[str, ...]) -> MediaPart:
    """保留原始 tag 排列，仅替换其中 name 参数的可见值。"""
    if len(names) != len(part.name_spans):
        msg = "filtered media names do not match source spans"
        raise ValueError(msg)
    raw = part.raw_fallback
    for (start, end), name in reversed(
        tuple(zip(part.name_spans, names, strict=True))
    ):
        raw = raw[:start] + name + raw[end:]
    return replace(
        part,
        name=names[-1] if names else None,
        raw_fallback=raw,
        name_values=names,
    )
