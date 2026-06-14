"""
Shared command argument parser for /download and /rssfeed.

Supported flags (all optional except -title):
  -title   <Show Title>
  -replace <original:replacement>   (repeatable)
  -avoid    <keyword1,keyword2,...>  (repeatable, comma-separated within each flag)
  -noseason                          (flag, no value — forces episode-only filename)

Examples:
  /download <url> -title "Diamond no Ace" -replace "Act II Second Season:S04"
  /rssfeed <url> -title "Mission: Yozakura Family" -avoid "REPACK,v2"
  /rssfeed <url> -title "Show" -replace "Part 2:S02" -avoid "REPACK" -avoid "v0"

Parsing rules:
  • Flags are detected by the leading dash+word pattern.
  • Values extend until the next flag or end of string.
  • -replace values are split on the FIRST colon only, so colons in the
    replacement string are safe: "Act II:S04" → ("Act II", "S04")
  • -avoid values are split on commas, each keyword stripped and lowercased
    for case-insensitive matching.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class ParsedArgs:
    source: Optional[str]                  # magnet / URL / empty
    title: Optional[str]
    replacements: List[Tuple[str, str]]    # [(original, replacement), ...]
    avoid_keywords: List[str]              # lowercased, stripped
    no_season: bool = False                # -noseason flag


# Matches a flag name and captures everything until the next flag or EOL
# Matches value-bearing flags
_FLAG_RE = re.compile(
    r'-(?P<flag>title|replace|avoid)\s+(?P<value>.+?)(?=\s+-(?:title|replace|avoid|noseason)(?:\s|$)|\s*$)',
    re.IGNORECASE | re.DOTALL,
)
# Matches standalone -noseason flag (no value)
_NOSEASON_RE = re.compile(r'(?i)(?:^|\s)-noseason(?:\s|$)')


def parse_args(args_text: str) -> ParsedArgs:
    """
    Parse everything after the command name.
    Returns a ParsedArgs with all extracted values.
    """
    replacements:   List[Tuple[str, str]] = []
    avoid_keywords: List[str]             = []
    title:          Optional[str]         = None

    # Strip all flags out; what remains (if anything) is the source URL/magnet
    remaining = args_text

    for m in _FLAG_RE.finditer(args_text):
        flag  = m.group("flag").lower()
        value = m.group("value").strip()

        if flag == "title":
            title = value

        elif flag == "replace":
            # Split on the FIRST colon only
            if ":" in value:
                original, replacement = value.split(":", 1)
                replacements.append((original.strip(), replacement.strip()))
            # silently ignore malformed -replace (no colon)

        elif flag == "avoid":
            # Split on commas, strip, lowercase each keyword
            for kw in value.split(","):
                kw = kw.strip()
                if kw:
                    avoid_keywords.append(kw.lower())

        # Remove this flag+value from remaining to isolate the source
        remaining = remaining.replace(m.group(0), "")

    source = remaining.strip() or None

    no_season = bool(_NOSEASON_RE.search(args_text))
    # Remove -noseason from remaining so it isn't treated as source
    remaining = _NOSEASON_RE.sub(' ', remaining).strip()
    source = remaining or None

    return ParsedArgs(
        source=source,
        title=title,
        replacements=replacements,
        avoid_keywords=avoid_keywords,
        no_season=no_season,
    )


def apply_replacements(filename: str, replacements: List[Tuple[str, str]]) -> str:
    """
    Apply all (original → replacement) substitutions to a filename.
    Case-sensitive. Applied in order.
    Used on the raw downloaded filename BEFORE season/episode extraction.
    """
    for original, replacement in replacements:
        filename = filename.replace(original, replacement)
    return filename


def should_avoid(entry_title: str, avoid_keywords: List[str]) -> bool:
    """
    Return True if the entry title contains any of the avoid keywords.
    Case-insensitive.
    """
    lower = entry_title.lower()
    return any(kw in lower for kw in avoid_keywords)
