"""MC→QQ 敏感词的规范化、同音匹配与文本替换。"""

from __future__ import annotations

import os
import threading
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

_Token = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _NormalizedText:
    literal: str
    source_indices: tuple[int, ...] | None


@dataclass(frozen=True, slots=True)
class _WordPattern:
    literal: str
    has_han: bool
    replacement: str
    order: int


@dataclass(frozen=True, slots=True)
class _PhoneticPattern:
    pattern: _WordPattern
    tokens: tuple[_Token, ...]


@dataclass(frozen=True, slots=True)
class _Match:
    source_start: int
    source_end: int
    pattern_length: int
    exact: bool
    replacement: str
    order: int


_pinyin_style: Any | None = None
_lazy_pinyin: Any | None = None
_pinyin_import_lock = threading.Lock()
_HAN_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF),
    (0x2A700, 0x2B73F),
    (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF),
    (0x2CEB0, 0x2EBEF),
    (0x2F800, 0x2FA1F),
    (0x30000, 0x3134F),
    (0x31350, 0x323AF),
)


def _load_pinyin_api() -> tuple[Any, Any]:
    """首次需要同音匹配时才加载拼音词典，避免无需求时常驻内存。"""
    global _lazy_pinyin, _pinyin_style  # noqa: PLW0603

    if _lazy_pinyin is not None and _pinyin_style is not None:
        return _pinyin_style, _lazy_pinyin

    with _pinyin_import_lock:
        if _lazy_pinyin is None or _pinyin_style is None:
            # 保留词组词典以保证多音字上下文，只禁止复制大型单字词典。
            os.environ.setdefault("PYPINYIN_NO_DICT_COPY", "true")
            from pypinyin import Style, lazy_pinyin

            _pinyin_style = Style
            _lazy_pinyin = lazy_pinyin
    return _pinyin_style, _lazy_pinyin


def _is_han(character: str) -> bool:
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _HAN_RANGES)


def _contains_han(text: str) -> bool:
    if text.isascii():
        return False
    return any(_is_han(character) for character in text)


def _to_match_tokens(literal: str) -> tuple[_Token, ...]:
    """为汉字生成带声调拼音令牌，非汉字保留为独立的字面令牌。"""
    style, lazy_pinyin = _load_pinyin_api()
    tokens: list[_Token] = []
    cursor = 0
    while cursor < len(literal):
        if not _is_han(literal[cursor]):
            tokens.append(("literal", literal[cursor]))
            cursor += 1
            continue

        run_end = cursor + 1
        while run_end < len(literal) and _is_han(literal[run_end]):
            run_end += 1
        han_run = literal[cursor:run_end]
        syllables = lazy_pinyin(
            han_run,
            style=style.TONE3,
            strict=True,
            neutral_tone_with_five=True,
            tone_sandhi=False,
        )
        # 正常情况下每个汉字对应一个音节；逐字回退避免异常词典数据破坏位置映射。
        if len(syllables) != len(han_run):
            syllables = [
                lazy_pinyin(
                    character,
                    style=style.TONE3,
                    strict=True,
                    neutral_tone_with_five=True,
                    tone_sandhi=False,
                )[0]
                for character in han_run
            ]
        tokens.extend(("han", syllable.casefold()) for syllable in syllables)
        cursor = run_end
    return tuple(tokens)


def _normalize_text(text: str, *, keep_source_indices: bool) -> _NormalizedText:
    """规范化文本；替换模式才保存规范化字符到原文位置的映射。"""
    if text.isascii() and not any(character.isspace() for character in text):
        return _NormalizedText(
            literal=text.casefold(),
            # 空元组表示规范化位置与原文位置完全相同，避免为常见 ASCII
            # 消息分配逐字符位置表；None 则表示 block 模式无需位置。
            source_indices=() if keep_source_indices else None,
        )

    normalized_characters: list[str] = []
    source_indices: list[int] | None = [] if keep_source_indices else None
    for source_index, character in enumerate(text):
        # 按原始码点规范化，才能让兼容字符展开后仍准确映射回原文。
        normalized_piece = unicodedata.normalize("NFKC", character).casefold()
        for normalized_character in normalized_piece:
            if normalized_character.isspace():
                continue
            normalized_characters.append(normalized_character)
            if source_indices is not None:
                source_indices.append(source_index)

    return _NormalizedText(
        literal="".join(normalized_characters),
        source_indices=(tuple(source_indices) if source_indices is not None else None),
    )


def _ordered_words(
    words: Collection[str],
    replacements: Mapping[str, str],
) -> tuple[str, ...]:
    """映射按声明顺序优先，未映射的旧词库使用稳定字典序。"""
    mapped_words = [
        word for word in replacements if isinstance(word, str) and word.strip()
    ]
    mapped_word_set = set(mapped_words)
    plain_words = sorted(
        word
        for word in words
        if isinstance(word, str) and word.strip() and word not in mapped_word_set
    )
    return *mapped_words, *plain_words


def _compile_patterns(
    ordered_words: tuple[str, ...],
    replacement_items: tuple[tuple[str, str], ...],
    default_replacement: str,
) -> tuple[_WordPattern, ...]:
    replacement_map = dict(replacement_items)
    patterns: list[_WordPattern] = []
    for order, word in enumerate(ordered_words):
        normalized = _normalize_text(word, keep_source_indices=False)
        if not normalized.literal:
            continue
        patterns.append(
            _WordPattern(
                literal=normalized.literal,
                has_han=_contains_han(normalized.literal),
                replacement=replacement_map.get(word, default_replacement),
                order=order,
            )
        )
    return tuple(patterns)


def _build_exact_index(
    patterns: tuple[_WordPattern, ...],
) -> Mapping[str, tuple[_WordPattern, ...]]:
    buckets: dict[str, list[_WordPattern]] = {}
    for pattern in patterns:
        buckets.setdefault(pattern.literal[0], []).append(pattern)
    return MappingProxyType({key: tuple(values) for key, values in buckets.items()})


@dataclass(slots=True)
class _LazyPhoneticIndex:
    """线程安全地延迟构建中文拼音索引。"""

    patterns: tuple[_WordPattern, ...]
    _index: dict[_Token, tuple[_PhoneticPattern, ...]] | None = field(
        default=None,
        init=False,
    )
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def get(self) -> dict[_Token, tuple[_PhoneticPattern, ...]]:
        cached = self._index
        if cached is not None:
            return cached

        with self._lock:
            cached = self._index
            if cached is not None:
                return cached

            buckets: dict[_Token, list[_PhoneticPattern]] = {}
            for pattern in self.patterns:
                tokens = _to_match_tokens(pattern.literal)
                phonetic_pattern = _PhoneticPattern(pattern=pattern, tokens=tokens)
                buckets.setdefault(tokens[0], []).append(phonetic_pattern)
            cached = {key: tuple(values) for key, values in buckets.items()}
            self._index = cached
            return cached


def _source_span(
    normalized_text: _NormalizedText,
    start: int,
    end: int,
) -> tuple[int, int]:
    source_indices = normalized_text.source_indices
    if source_indices is None:  # pragma: no cover - 仅替换模式调用
        msg = "replace mode requires source indices"
        raise RuntimeError(msg)
    if not source_indices:
        return start, end
    return source_indices[start], source_indices[end - 1] + 1


def _append_match(  # noqa: PLR0913
    matches: list[_Match],
    normalized_text: _NormalizedText,
    pattern: _WordPattern,
    start: int,
    end: int,
    *,
    exact: bool,
) -> None:
    source_start, source_end = _source_span(normalized_text, start, end)
    matches.append(
        _Match(
            source_start=source_start,
            source_end=source_end,
            pattern_length=len(pattern.literal),
            exact=exact,
            replacement=pattern.replacement,
            order=pattern.order,
        )
    )


def _find_exact_matches(
    normalized_text: _NormalizedText,
    exact_index: Mapping[str, tuple[_WordPattern, ...]],
    *,
    block: bool,
) -> list[_Match] | None:
    matches: list[_Match] = []
    literal = normalized_text.literal
    for start, first_character in enumerate(literal):
        for pattern in exact_index.get(first_character, ()):
            end = start + len(pattern.literal)
            if end > len(literal) or not literal.startswith(pattern.literal, start):
                continue
            if block:
                return None
            _append_match(
                matches,
                normalized_text,
                pattern,
                start,
                end,
                exact=True,
            )
    return matches


def _find_phonetic_matches(
    normalized_text: _NormalizedText,
    lazy_index: _LazyPhoneticIndex,
    *,
    block: bool,
) -> list[_Match] | None:
    tokens = _to_match_tokens(normalized_text.literal)
    phonetic_index = lazy_index.get()
    matches: list[_Match] = []
    for start, first_token in enumerate(tokens):
        for phonetic_pattern in phonetic_index.get(first_token, ()):
            pattern = phonetic_pattern.pattern
            end = start + len(phonetic_pattern.tokens)
            if end > len(tokens):
                continue
            if tokens[start:end] != phonetic_pattern.tokens:
                continue
            # 精确候选已由字面索引收集，避免同一词产生重复候选。
            if normalized_text.literal.startswith(pattern.literal, start):
                continue
            if block:
                return None
            _append_match(
                matches,
                normalized_text,
                pattern,
                start,
                end,
                exact=False,
            )
    return matches


def _select_matches(matches: list[_Match], source_length: int) -> list[_Match]:
    """按最长、精确、声明顺序选择互不重叠的原文区间。"""
    ranked_matches = sorted(
        matches,
        key=lambda match: (
            -match.pattern_length,
            -int(match.exact),
            match.order,
            match.source_start,
            match.source_end,
        ),
    )
    occupied = bytearray(source_length)
    selected: list[_Match] = []
    for match in ranked_matches:
        if any(
            occupied[index] for index in range(match.source_start, match.source_end)
        ):
            continue
        selected.append(match)
        for index in range(match.source_start, match.source_end):
            occupied[index] = 1
    return sorted(selected, key=lambda match: match.source_start)


@dataclass(frozen=True, slots=True)
class SensitiveWordFilter:
    """不可变的精确索引与仅初始化一次的拼音索引。"""

    patterns: tuple[_WordPattern, ...]
    exact_index: Mapping[str, tuple[_WordPattern, ...]]
    mode: str
    phonetic_index: _LazyPhoneticIndex | None

    @classmethod
    def build(
        cls,
        words: Collection[str],
        replacements: Mapping[str, str],
        *,
        mode: str,
        default_replacement: str,
        prewarm_phonetic: bool = False,
    ) -> SensitiveWordFilter:
        ordered_words = _ordered_words(words, replacements)
        replacement_items = tuple(
            (word, replacement)
            for word, replacement in replacements.items()
            if isinstance(word, str) and word.strip() and isinstance(replacement, str)
        )
        patterns = _compile_patterns(
            ordered_words,
            replacement_items,
            default_replacement,
        )
        han_patterns = tuple(pattern for pattern in patterns if pattern.has_han)
        phonetic_index = _LazyPhoneticIndex(han_patterns) if han_patterns else None
        matcher = cls(
            patterns=patterns,
            exact_index=_build_exact_index(patterns),
            mode=mode,
            phonetic_index=phonetic_index,
        )
        if prewarm_phonetic and phonetic_index is not None:
            phonetic_index.get()
        return matcher

    def filter(self, text: str) -> str | None:
        if not text or not self.patterns:
            return text

        block = self.mode == "block"
        normalized_text = _normalize_text(text, keep_source_indices=not block)
        if not normalized_text.literal:
            return text

        exact_matches = _find_exact_matches(
            normalized_text,
            self.exact_index,
            block=block,
        )
        if exact_matches is None:
            return None

        matches = exact_matches
        if self.phonetic_index is not None and _contains_han(normalized_text.literal):
            phonetic_matches = _find_phonetic_matches(
                normalized_text,
                self.phonetic_index,
                block=block,
            )
            if phonetic_matches is None:
                return None
            matches.extend(phonetic_matches)

        if not matches:
            return text

        selected = _select_matches(matches, len(text))
        pieces: list[str] = []
        cursor = 0
        for match in selected:
            pieces.append(text[cursor : match.source_start])
            pieces.append(match.replacement)
            cursor = match.source_end
        pieces.append(text[cursor:])
        return "".join(pieces)


@dataclass(frozen=True, slots=True)
class SensitiveWordRuntimeSnapshot:
    """一次性发布的词库数据和匹配器，消息路径只读取一个对象引用。"""

    words: frozenset[str]
    replacements: tuple[tuple[str, str], ...]
    matcher: SensitiveWordFilter


def build_sensitive_runtime(
    words: Collection[str],
    replacements: Mapping[str, str],
    *,
    mode: str,
    default_replacement: str,
    prewarm_phonetic: bool = False,
) -> SensitiveWordRuntimeSnapshot:
    valid_replacements = tuple(
        (word, replacement)
        for word, replacement in replacements.items()
        if isinstance(word, str) and word.strip() and isinstance(replacement, str)
    )
    valid_words = frozenset(
        word for word in words if isinstance(word, str) and word.strip()
    ) | frozenset(word for word, _replacement in valid_replacements)
    replacement_map = dict(valid_replacements)
    return SensitiveWordRuntimeSnapshot(
        words=valid_words,
        replacements=valid_replacements,
        matcher=SensitiveWordFilter.build(
            valid_words,
            replacement_map,
            mode=mode,
            default_replacement=default_replacement,
            prewarm_phonetic=prewarm_phonetic,
        ),
    )


_active_runtime = SensitiveWordRuntimeSnapshot(
    words=frozenset(),
    replacements=(),
    matcher=SensitiveWordFilter((), MappingProxyType({}), "replace", None),
)


def publish_sensitive_runtime(snapshot: SensitiveWordRuntimeSnapshot) -> None:
    """用单次引用赋值发布已完整构建的运行时快照。"""
    global _active_runtime  # noqa: PLW0603

    _active_runtime = snapshot


def configure_sensitive_filter(
    words: Collection[str],
    replacements: Mapping[str, str],
    *,
    mode: str,
    default_replacement: str,
    prewarm_phonetic: bool = False,
) -> None:
    """构建并原子发布过滤器，保留给测试和兼容调用方使用。"""
    publish_sensitive_runtime(
        build_sensitive_runtime(
            words,
            replacements,
            mode=mode,
            default_replacement=default_replacement,
            prewarm_phonetic=prewarm_phonetic,
        )
    )


def filter_current_sensitive_text(text: str) -> str | None:
    """使用当前唯一的运行时快照过滤文本。"""
    runtime = _active_runtime
    return runtime.matcher.filter(text)


def filter_sensitive_text(
    text: str,
    words: Collection[str],
    replacements: Mapping[str, str],
    *,
    mode: str,
    default_replacement: str,
) -> str | None:
    """
    过滤一段即将发往 QQ 的文本。

    block 模式命中时返回 None；replace 模式返回替换后的文本。替换结果不会
    再次参与匹配，避免自定义替换文本触发递归过滤。
    """
    return SensitiveWordFilter.build(
        words,
        replacements,
        mode=mode,
        default_replacement=default_replacement,
    ).filter(text)


__all__ = [
    "SensitiveWordFilter",
    "SensitiveWordRuntimeSnapshot",
    "build_sensitive_runtime",
    "configure_sensitive_filter",
    "filter_current_sensitive_text",
    "filter_sensitive_text",
    "publish_sensitive_runtime",
]
