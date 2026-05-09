"""Utilities for prompt string manipulation."""

import re
import unicodedata
from pathlib import Path


# ---------------------------------------------------------------------------
# Text safety: clean hidden/invisible characters and prevent tokenization failures
# ---------------------------------------------------------------------------

def clean_text(text: str, keep_newlines: bool = True) -> str:
    """Remove invisible/hidden characters from text.

    Targets:
    - Zero-width characters (\u200b, \u200c, \u200d, \ufeff, \u2060, etc.)
    - PDF/Word hidden format characters
    - Most C0/C1 control characters
    - Soft hyphens, directional marks, override chars
    """
    if not isinstance(text, str):
        text = str(text)

    # Step 1: Remove zero-width and format characters explicitly
    text = re.sub(
        r"[\u200b\u200c\u200d\u2060\u00ad\ufeff"
        r"\u200e\u200f\u202a-\u202e\u2066-\u2069]",
        "",
        text,
    )

    # Step 2: Remove control characters (C0/C1), optionally keep \\n\\r\\t
    if keep_newlines:
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    else:
        text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text)

    # Step 3: Normalize Unicode (NFC) to collapse spoofed glyphs
    text = unicodedata.normalize("NFC", text)

    # Step 4: Strip leading/trailing whitespace artifacts
    return text.strip()


def _strip_surrogates(text: str) -> str:
    """Remove lone surrogate code points."""
    return "".join(ch for ch in text if not (0xD800 <= ord(ch) <= 0xDFFF))


def _strip_noncharacters(text: str) -> str:
    """Remove Unicode noncharacters."""

    def _keep(ch: str) -> bool:
        cp = ord(ch)
        if 0xFDD0 <= cp <= 0xFDEF:
            return False
        if (cp & 0xFFFF) in (0xFFFE, 0xFFFF):
            return False
        return True

    return "".join(ch for ch in text if _keep(ch))


def _strip_pua(text: str) -> str:
    """Remove Private Use Area code points."""

    def _keep(ch: str) -> bool:
        cp = ord(ch)
        if 0xE000 <= cp <= 0xF8FF:
            return False
        if 0xF0000 <= cp <= 0xFFFFD:
            return False
        if 0x100000 <= cp <= 0x10FFFD:
            return False
        return True

    return "".join(ch for ch in text if _keep(ch))


def _strip_replacement_chars(text: str) -> str:
    """Remove Unicode replacement characters (sign of prior encoding corruption)."""
    return text.replace("\ufffd", "")


def _dedupe_repeats(text: str, max_repeat: int = 100) -> str:
    """Collapse runs of a single character longer than *max_repeat*."""
    if max_repeat <= 0:
        return text
    return re.sub(r"(.)\1{" + str(max_repeat) + r",}", lambda m: m.group(1) * max_repeat, text)


# Factored out the common suffix so the alternation is shorter and the regex
# engine only compiles/evaluates the character class once.
_PATH_RE = re.compile(
    r"""
    (?<![\w/\\:.])          # not preceded by word char, slash, colon, or dot
    (?P<path>
        (?: / | \.{1,2}/ | ~/ | [A-Za-z]:[\\/] | [\w.-]+[\\/] )
        (?:
            [^\s?#<>\"'`|{},;:!?)\]]+ [\\/]
            |
            [^\s?#<>\"'`|{},;:!?)\]]+ \s+ [^\s?#<>\"'`|{},;:!?)\]]+ [\\/]
        )*
        (?:
            [^\s?#<>\"'`|{},;:!?)\]]++ (?!\s+(?![a-z]+\b)[^\s?#<>\"'`|{},;:!?)\]]+)
            |
            [^\s?#<>\"'`|{},;:!?)\]]+ \s+ (?! [a-z]+ \b ) [^\s?#<>\"'`|{},;:!?)\]]+
        )?
    )
    """,
    re.VERBOSE,
)

_NON_PATH_RE = re.compile(
    r"^\d+/\d+$|"              # pure fraction
    r"^\d{4}/\d{1,2}/\d{1,2}$|"  # ISO date
    r"^\d{1,2}/\d{1,2}/\d{4}$"   # US date
)

_NON_PATH_RE_MATCH = _NON_PATH_RE.match
_TRAILING_PUNCTUATION = ". , ; : ! ? ) ] }".replace(" ", "")


class _Replacer:
    """Callable replacement helper to avoid re-creating a function on every call."""

    __slots__ = ("text", "text_len", "non_path_match", "trailing_punct", "_code_ranges")

    def __init__(self, text: str) -> None:
        self.text = text
        self.text_len = len(text)
        self.non_path_match = _NON_PATH_RE_MATCH
        self.trailing_punct = _TRAILING_PUNCTUATION
        # Pre-compute markdown fenced code-block ranges (``` … ```).
        self._code_ranges: list[tuple[int, int]] = []
        pos = 0
        while True:
            start = text.find("```", pos)
            if start == -1:
                break
            end = text.find("```", start + 3)
            if end == -1:
                self._code_ranges.append((start, len(text)))
                break
            self._code_ranges.append((start, end + 3))
            pos = end + 3

    def __call__(self, m: re.Match[str]) -> str:
        raw = m.group("path")
        raw_start, raw_end = m.span("path")
        text = self.text
        text_len = self.text_len

        # Inside a markdown fenced code block – leave as-is.
        for code_start, code_end in self._code_ranges:
            if code_start <= raw_start < code_end:
                return raw

        # Already inside quotes, backticks, or bracket pairs – leave as-is.
        if raw_start > 0 and raw_end < text_len:
            prev, nxt = text[raw_start - 1], text[raw_end]
            if prev == nxt and prev in "'\"`":
                return raw
            if (prev, nxt) in (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">")):
                return raw

        # Strip trailing punctuation – fast-path when unnecessary.
        trailing_punct = self.trailing_punct
        if raw and raw[-1] in trailing_punct:
            stripped = raw.rstrip(trailing_punct)
            trailing = raw[len(stripped) :]
            path = stripped
        else:
            path = raw
            trailing = ""

        # The regex guarantees a path separator in raw, and rstrip cannot
        # remove separators, so we only need the length check here.
        if len(path) < 2:
            return raw
        if "://" in path:
            return raw
        if self.non_path_match(path):
            return raw
        if not Path(path).exists():
            return raw

        return f"`{path}`{trailing}"


def escape_file_paths(
    text: str,
    *,
    max_chars: int = 0,
    max_repeat: int = 100,
    truncate_msg: str = "",
) -> str:
    """Detect legal file paths in *text* and wrap each one in backticks,
    then sanitize the result to prevent ``tokenization failed`` errors.

    Paths that are already wrapped in quotes or backticks are left untouched.
    URLs, pure fractions and bare dates are ignored.
    """
    if not isinstance(text, str):
        text = str(text)

    # Escape file paths
    if "/" in text or "\\" in text:
        text = _PATH_RE.sub(_Replacer(text), text)

    # Sanitize for tokenizer
    text = _strip_surrogates(text)
    text = _strip_noncharacters(text)
    text = _strip_pua(text)
    text = _strip_replacement_chars(text)
    text = clean_text(text, keep_newlines=True)
    text = _dedupe_repeats(text, max_repeat=max_repeat)

    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
        if truncate_msg:
            if len(truncate_msg) < max_chars:
                text = text[: max_chars - len(truncate_msg)] + truncate_msg

    return text.strip()


# ---- helpers for text cleaning ----

_FULLWIDTH_SPACE = "\u3000"
_FULLWIDTH_START = 0xFF01
_FULLWIDTH_END = 0xFF5E
_FULLWIDTH_OFFSET = 0xFEE0

_EMOJI_RE = re.compile(
    r"["
    r"\U0001F600-\U0001F64F"
    r"\U0001F300-\U0001F5FF"
    r"\U0001F680-\U0001F6FF"
    r"\U0001F700-\U0001F77F"
    r"\U0001F780-\U0001F7FF"
    r"\U0001F800-\U0001F8FF"
    r"\U0001F900-\U0001F9FF"
    r"\U0001FA00-\U0001FA6F"
    r"\U0001FA70-\U0001FAFF"
    r"\U00002702-\U000027B0"
    r"\U000024C2-\U0001F251"
    r"]+"
)

_PUNCT_CHARS = r"!?.。，,、;；:：…~～·\"\"''（）()【】\[\]{}《》<>「」『』〖〗｛｝［］\\|｜—–―"
_REPEAT_PUNCT_RE = re.compile(
    r"([" + _PUNCT_CHARS + r"])"
    r"[" + _PUNCT_CHARS + r"]{2,}"
)

_ZW_CHARS = "".join(
    chr(c)
    for c in (
        0x200B, 0x200C, 0x200D, 0xFEFF, 0x2060, 0x00AD,
        0x034F, 0x180B, 0x180C, 0x180D,
        0xFE00, 0xFE01, 0xFE02, 0xFE03, 0xFE04, 0xFE05,
        0xFE06, 0xFE07, 0xFE08, 0xFE09, 0xFE0A, 0xFE0B,
        0xFE0C, 0xFE0D, 0xFE0E, 0xFE0F,
    )
)


def _extract_code(text: str) -> tuple[str, list[str]]:
    """Extract markdown fenced code blocks and inline code into placeholders."""
    placeholders: list[str] = []
    counter = 0

    def _repl(m: re.Match[str]) -> str:
        nonlocal counter
        placeholders.append(m.group(0))
        result = f"\x00{counter:08d}\x00"
        counter += 1
        return result

    text = re.sub(r"```[\s\S]*?```", _repl, text)
    text = re.sub(r"`[^`]*`", _repl, text)
    return text, placeholders


def _restore_code(text: str, placeholders: list[str]) -> str:
    """Restore placeholders to original code blocks."""
    for i, ph in enumerate(placeholders):
        text = text.replace(f"\x00{i:08d}\x00", ph, 1)
    return text


def remove_redundant_whitespace(text: str) -> str:
    """删除多余空格、换行、制表符，将连续空白压缩为单个空格。

    示例: ``  你好  世界  `` → ``你好 世界``
    Markdown 代码块及行内代码保留原样。
    """
    text, placeholders = _extract_code(text)
    text = re.sub(r"[\s]+", " ", text).strip()
    return _restore_code(text, placeholders)


def normalize_encoding(text: str) -> str:
    """全角转半角、繁简转换、Unicode 规范化。

    示例: ``ＡＩ`` → ``AI``
    Markdown 代码块及行内代码保留原样。
    """
    text, placeholders = _extract_code(text)

    # Unicode NFKC normalization
    text = unicodedata.normalize("NFKC", text)

    # Full-width to half-width for remaining chars
    def _fw2hw(s: str) -> str:
        result: list[str] = []
        for ch in s:
            code = ord(ch)
            if _FULLWIDTH_START <= code <= _FULLWIDTH_END:
                result.append(chr(code - _FULLWIDTH_OFFSET))
            elif ch == _FULLWIDTH_SPACE:
                result.append(" ")
            else:
                result.append(ch)
        return "".join(result)

    text = _fw2hw(text)

    # Traditional to Simplified (optional)
    try:
        import opencc

        converter = opencc.OpenCC("t2s")
        text = converter.convert(text)
    except ImportError:
        pass

    return _restore_code(text, placeholders)


def remove_meaningless_symbols(text: str) -> str:
    """清理 emoji、特殊符号、重复标点。

    示例: ``!!!???`` → ``!``
    Markdown 代码块及行内代码保留原样。
    """
    text, placeholders = _extract_code(text)

    # Remove zero-width / invisible chars
    trans = str.maketrans("", "", _ZW_CHARS)
    text = text.translate(trans)

    # Remove emoji
    text = _EMOJI_RE.sub("", text)

    # Deduplicate repeated punctuation (3+ identical -> 1)
    text = _REPEAT_PUNCT_RE.sub(r"\1", text)

    return _restore_code(text, placeholders)


def normalize_case(text: str, mode: str = "lower") -> str:
    """根据场景统一大小写（代码保留原样）。

    mode: ``'lower'`` 全小写, ``'title'`` 首字母大写。
    Markdown 代码块及行内代码保留原样。
    """
    text, placeholders = _extract_code(text)
    if mode == "lower":
        text = text.lower()
    elif mode == "title":
        text = text.title()
    return _restore_code(text, placeholders)
