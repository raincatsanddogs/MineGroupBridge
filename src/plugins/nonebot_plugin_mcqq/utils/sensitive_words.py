"""MC→QQ 敏感词的规范化、同音匹配与文本替换。"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from nonebot import logger

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

_Token = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _NormalizedText:
    literal: str
    source_indices: tuple[int, ...] | None


@dataclass(frozen=True, slots=True)
class SensitiveWordRule:
    """单个敏感词对全局模式和替换文本的可选覆盖。"""

    mode: str
    replacement: str | None = None


@dataclass(frozen=True, slots=True)
class _WordPattern:
    literal: str
    has_han: bool
    mode: str
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
    mode: str
    replacement: str
    order: int


@dataclass(frozen=True, slots=True)
class _RegexPattern:
    pattern: str
    mode: str
    compiled: re.Pattern[str]


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
    """规范化文本；需要选择或替换命中时保存规范化字符到原文位置的映射。"""
    if text.isascii() and not any(character.isspace() for character in text):
        return _NormalizedText(
            literal=text.casefold(),
            # 空元组表示规范化位置与原文位置完全相同，避免为常见 ASCII
            # 消息分配逐字符位置表；None 表示纯 block 过滤器无需位置。
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
    rules: Mapping[str, SensitiveWordRule],
) -> tuple[str, ...]:
    """逐词规则、替换映射按声明顺序优先，普通词使用稳定字典序。"""
    ruled_words = [word for word in rules if isinstance(word, str) and word.strip()]
    ruled_word_set = set(ruled_words)
    mapped_words = [
        word
        for word in replacements
        if isinstance(word, str) and word.strip() and word not in ruled_word_set
    ]
    configured_word_set = ruled_word_set | set(mapped_words)
    plain_words = sorted(
        word
        for word in words
        if isinstance(word, str) and word.strip() and word not in configured_word_set
    )
    return *ruled_words, *mapped_words, *plain_words


def _compile_patterns(
    ordered_words: tuple[str, ...],
    replacement_items: tuple[tuple[str, str], ...],
    rule_items: tuple[tuple[str, SensitiveWordRule], ...],
    default_mode: str,
    default_replacement: str,
) -> tuple[_WordPattern, ...]:
    replacement_map = dict(replacement_items)
    rule_map = dict(rule_items)
    patterns: list[_WordPattern] = []
    for order, word in enumerate(ordered_words):
        normalized = _normalize_text(word, keep_source_indices=False)
        if not normalized.literal:
            continue
        rule = rule_map.get(word)
        replacement = replacement_map.get(word, default_replacement)
        if rule is not None and rule.replacement is not None:
            replacement = rule.replacement
        patterns.append(
            _WordPattern(
                literal=normalized.literal,
                has_han=_contains_han(normalized.literal),
                mode=rule.mode if rule is not None else default_mode,
                replacement=replacement,
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
    if source_indices is None:  # pragma: no cover - 仅候选收集路径调用
        msg = "match selection requires source indices"
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
            mode=pattern.mode,
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


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """按原文位置合并重复或重叠区间，保留相邻的独立命中。"""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not merged or start >= merged[-1][1]:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged


def _subtract_spans(
    selected: list[tuple[int, int]],
    deleted: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """从已选区间中扣除删除区间，返回仍可见的非空片段。"""
    remaining: list[tuple[int, int]] = []
    delete_index = 0
    for selected_start, selected_end in selected:
        cursor = selected_start
        while delete_index < len(deleted) and deleted[delete_index][1] <= cursor:
            delete_index += 1

        current_delete = delete_index
        while current_delete < len(deleted):
            delete_start, delete_end = deleted[current_delete]
            if delete_start >= selected_end:
                break
            if delete_start > cursor:
                remaining.append((cursor, min(delete_start, selected_end)))
            cursor = max(cursor, delete_end)
            if cursor >= selected_end:
                break
            current_delete += 1
        if cursor < selected_end:
            remaining.append((cursor, selected_end))
    return remaining


@dataclass(frozen=True, slots=True)
class SensitiveRegexFilter:
    """在普通敏感词处理后执行的不可变正则过滤器。"""

    patterns: tuple[_RegexPattern, ...]

    @classmethod
    def build(
        cls,
        rules: tuple[tuple[str, SensitiveWordRule], ...],
    ) -> SensitiveRegexFilter:
        patterns: list[_RegexPattern] = []
        for pattern, rule in rules:
            if rule.replacement is not None:
                logger.warning(
                    f"敏感词正则规则 {pattern!r} 的 replacement 不生效，已忽略"
                )
            try:
                compiled = re.compile(pattern)
            except re.error as error:
                logger.warning(f"敏感词正则规则 {pattern!r} 无效，已忽略：{error}")
                continue
            patterns.append(_RegexPattern(pattern, rule.mode, compiled))
        return cls(tuple(patterns))

    def filter(self, text: str) -> str:
        if not text or not self.patterns:
            return text

        selected_spans: list[tuple[int, int]] = []
        deleted_spans: list[tuple[int, int]] = []
        for pattern in self.patterns:
            target = selected_spans if pattern.mode == "regsel" else deleted_spans
            target.extend(
                (match.start(), match.end())
                for match in pattern.compiled.finditer(text)
                if match.start() != match.end()
            )

        deleted = _merge_spans(deleted_spans)
        if selected_spans:
            selected = _merge_spans(selected_spans)
            remaining = _subtract_spans(selected, deleted)
            return " ".join(text[start:end] for start, end in remaining)

        if not deleted:
            return text
        pieces: list[str] = []
        cursor = 0
        for start, end in deleted:
            pieces.append(text[cursor:start])
            cursor = end
        pieces.append(text[cursor:])
        return "".join(pieces)


@dataclass(frozen=True, slots=True)
class SensitiveWordFilter:
    """不可变的精确索引与仅初始化一次的拼音索引。"""

    patterns: tuple[_WordPattern, ...]
    exact_index: Mapping[str, tuple[_WordPattern, ...]]
    block_only: bool
    phonetic_index: _LazyPhoneticIndex | None

    @classmethod
    def build(  # noqa: PLR0913
        cls,
        words: Collection[str],
        replacements: Mapping[str, str],
        *,
        mode: str,
        default_replacement: str,
        rules: Mapping[str, SensitiveWordRule] | None = None,
        prewarm_phonetic: bool = False,
    ) -> SensitiveWordFilter:
        valid_rules = {
            word: rule
            for word, rule in (rules or {}).items()
            if isinstance(word, str)
            and word.strip()
            and isinstance(rule, SensitiveWordRule)
            and rule.mode in {"block", "replace"}
            and (rule.replacement is None or isinstance(rule.replacement, str))
        }
        ordered_words = _ordered_words(words, replacements, valid_rules)
        replacement_items = tuple(
            (word, replacement)
            for word, replacement in replacements.items()
            if isinstance(word, str) and word.strip() and isinstance(replacement, str)
        )
        rule_items = tuple(valid_rules.items())
        patterns = _compile_patterns(
            ordered_words,
            replacement_items,
            rule_items,
            mode,
            default_replacement,
        )
        han_patterns = tuple(pattern for pattern in patterns if pattern.has_han)
        phonetic_index = _LazyPhoneticIndex(han_patterns) if han_patterns else None
        matcher = cls(
            patterns=patterns,
            exact_index=_build_exact_index(patterns),
            block_only=bool(patterns)
            and all(pattern.mode == "block" for pattern in patterns),
            phonetic_index=phonetic_index,
        )
        if prewarm_phonetic and phonetic_index is not None:
            phonetic_index.get()
        return matcher

    def filter(self, text: str) -> str | None:  # noqa: PLR0911
        if not text or not self.patterns:
            return text

        normalized_text = _normalize_text(text, keep_source_indices=not self.block_only)
        if not normalized_text.literal:
            return text

        exact_matches = _find_exact_matches(
            normalized_text,
            self.exact_index,
            block=self.block_only,
        )
        if exact_matches is None:
            return None

        matches = exact_matches
        if self.phonetic_index is not None and _contains_han(normalized_text.literal):
            phonetic_matches = _find_phonetic_matches(
                normalized_text,
                self.phonetic_index,
                block=self.block_only,
            )
            if phonetic_matches is None:
                return None
            matches.extend(phonetic_matches)

        if not matches:
            return text

        selected = _select_matches(matches, len(text))
        if any(match.mode == "block" for match in selected):
            return None

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
    rules: tuple[tuple[str, SensitiveWordRule], ...]
    matcher: SensitiveWordFilter
    regex_filter: SensitiveRegexFilter

    def filter_plain_text(self, text: str) -> str | None:
        """先应用普通词规则，再应用纯文本正则规则。"""
        filtered = self.matcher.filter(text)
        if filtered is None:
            return None
        return self.regex_filter.filter(filtered)


def build_sensitive_runtime(  # noqa: PLR0913
    words: Collection[str],
    replacements: Mapping[str, str],
    *,
    mode: str,
    default_replacement: str,
    rules: Mapping[str, SensitiveWordRule] | None = None,
    prewarm_phonetic: bool = False,
) -> SensitiveWordRuntimeSnapshot:
    valid_replacements = tuple(
        (word, replacement)
        for word, replacement in replacements.items()
        if isinstance(word, str) and word.strip() and isinstance(replacement, str)
    )
    candidate_rules = tuple(
        (word, rule)
        for word, rule in (rules or {}).items()
        if isinstance(word, str)
        and word.strip()
        and isinstance(rule, SensitiveWordRule)
        and rule.mode in {"block", "replace", "regdel", "regsel"}
        and (rule.replacement is None or isinstance(rule.replacement, str))
    )
    regex_rule_keys = frozenset(
        word
        for word, rule in candidate_rules
        if rule.mode in {"regdel", "regsel"}
    )
    regex_filter = SensitiveRegexFilter.build(
        tuple(
            (word, rule)
            for word, rule in candidate_rules
            if word in regex_rule_keys
        )
    )
    valid_regex_keys = frozenset(pattern.pattern for pattern in regex_filter.patterns)
    valid_rules = tuple(
        (word, rule)
        for word, rule in candidate_rules
        if word not in regex_rule_keys or word in valid_regex_keys
    )
    literal_rules = tuple(
        (word, rule)
        for word, rule in valid_rules
        if rule.mode in {"block", "replace"}
    )
    literal_replacements = tuple(
        (word, replacement)
        for word, replacement in valid_replacements
        if word not in regex_rule_keys
    )
    valid_words = (
        frozenset(
            word
            for word in words
            if isinstance(word, str) and word.strip() and word not in regex_rule_keys
        )
        | frozenset(word for word, _replacement in literal_replacements)
        | frozenset(word for word, _rule in literal_rules)
    )
    replacement_map = dict(literal_replacements)
    rule_map = dict(literal_rules)
    return SensitiveWordRuntimeSnapshot(
        words=valid_words,
        replacements=literal_replacements,
        rules=valid_rules,
        matcher=SensitiveWordFilter.build(
            valid_words,
            replacement_map,
            mode=mode,
            default_replacement=default_replacement,
            rules=rule_map,
            prewarm_phonetic=prewarm_phonetic,
        ),
        regex_filter=regex_filter,
    )


_active_runtime = SensitiveWordRuntimeSnapshot(
    words=frozenset(),
    replacements=(),
    rules=(),
    matcher=SensitiveWordFilter(
        patterns=(),
        exact_index=MappingProxyType({}),
        block_only=False,
        phonetic_index=None,
    ),
    regex_filter=SensitiveRegexFilter(patterns=()),
)


def publish_sensitive_runtime(snapshot: SensitiveWordRuntimeSnapshot) -> None:
    """用单次引用赋值发布已完整构建的运行时快照。"""
    global _active_runtime  # noqa: PLW0603

    _active_runtime = snapshot


def configure_sensitive_filter(  # noqa: PLR0913
    words: Collection[str],
    replacements: Mapping[str, str],
    *,
    mode: str,
    default_replacement: str,
    rules: Mapping[str, SensitiveWordRule] | None = None,
    prewarm_phonetic: bool = False,
) -> None:
    """构建并原子发布过滤器，保留给测试和兼容调用方使用。"""
    publish_sensitive_runtime(
        build_sensitive_runtime(
            words,
            replacements,
            mode=mode,
            default_replacement=default_replacement,
            rules=rules,
            prewarm_phonetic=prewarm_phonetic,
        )
    )


def filter_current_sensitive_text(text: str) -> str | None:
    """使用当前运行时快照过滤普通词，不执行纯文本正则规则。"""
    runtime = _active_runtime
    return runtime.matcher.filter(text)


def filter_current_sensitive_plain_text(text: str) -> str | None:
    """使用当前运行时快照依次执行普通词和纯文本正则规则。"""
    runtime = _active_runtime
    return runtime.filter_plain_text(text)


def filter_sensitive_text(  # noqa: PLR0913
    text: str,
    words: Collection[str],
    replacements: Mapping[str, str],
    *,
    mode: str,
    default_replacement: str,
    rules: Mapping[str, SensitiveWordRule] | None = None,
) -> str | None:
    """
    过滤一段即将发往 QQ 的文本。

    最终选中的 block 命中返回 None，否则先完成逐词替换，再应用正则规则。
    替换结果不会再次参与普通词匹配，但会作为正则规则的输入。
    """
    runtime = build_sensitive_runtime(
        words,
        replacements,
        mode=mode,
        default_replacement=default_replacement,
        rules=rules,
    )
    return runtime.filter_plain_text(text)


__all__ = [
    "SensitiveWordFilter",
    "SensitiveWordRule",
    "SensitiveWordRuntimeSnapshot",
    "build_sensitive_runtime",
    "configure_sensitive_filter",
    "filter_current_sensitive_plain_text",
    "filter_current_sensitive_text",
    "filter_sensitive_text",
    "publish_sensitive_runtime",
]
