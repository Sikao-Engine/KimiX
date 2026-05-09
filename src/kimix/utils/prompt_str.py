"""Utilities for prompt string manipulation."""

import re
from pathlib import Path

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

    __slots__ = ("text", "text_len", "non_path_match", "trailing_punct")

    def __init__(self, text: str) -> None:
        self.text = text
        self.text_len = len(text)
        self.non_path_match = _NON_PATH_RE_MATCH
        self.trailing_punct = _TRAILING_PUNCTUATION

    def __call__(self, m: re.Match[str]) -> str:
        raw = m.group("path")
        raw_start, raw_end = m.span("path")
        text = self.text
        text_len = self.text_len

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


def escape_file_paths(text: str) -> str:
    """Detect legal file paths in *text* and wrap each one in backticks.

    Paths that are already wrapped in quotes or backticks are left untouched.
    URLs, pure fractions and bare dates are ignored.
    """
    # Fast-path: every possible path match must contain '/' or '\'.
    if "/" not in text and "\\" not in text:
        return text
    return _PATH_RE.sub(_Replacer(text), text)
