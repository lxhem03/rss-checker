"""
Helpers that bridge user settings → runtime behaviour.

  get_rename_template(settings)  → (with_season_fmt, no_season_fmt)
  get_patterns(settings)         → merged list of (pattern, group_names) tuples
  get_dump_channels(settings)    → list[int]
  get_upload_channels(settings)  → list[int]
  forward_to_pm(settings)        → bool
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from config import (
    SEASON_EPISODE_PATTERNS,
    FILENAME_TEMPLATE,
    FILENAME_TEMPLATE_NO_SEASON,
)

logger = logging.getLogger(__name__)


def get_rename_templates(settings: Dict[str, Any]) -> Tuple[str, str]:
    """
    Returns (with_season_template, no_season_template).

    User can store a single template string.  We support two special
    placeholder forms:
      • Standard:  {title} - S{season:02d}E{episode:02d}.mkv
      • No-season: {title} - E{episode:02d}.mkv

    If the user's template contains {season}, it is used as the
    with_season template and the no_season variant just drops the
    season portion.  If no custom template is set, fall back to config.
    """
    tmpl: Optional[str] = settings.get("rename_template")
    if not tmpl:
        return FILENAME_TEMPLATE, FILENAME_TEMPLATE_NO_SEASON

    # Make sure it ends in .mkv (defensive)
    if not tmpl.lower().endswith(".mkv"):
        tmpl = tmpl + ".mkv"

    if "{season" in tmpl:
        # Build a no-season variant by removing the season placeholder
        # e.g. "{title} - S{season:02d}E{episode:02d}" → "{title} - E{episode:02d}"
        no_season = re.sub(r"\s*[Ss]\{season[^}]*\}", "", tmpl).strip(" -_")
        return tmpl, no_season

    # Template has no {season} — use as-is for both (season just won't appear)
    return tmpl, tmpl


def get_patterns(settings: Dict[str, Any]) -> list:
    """
    Merge user-defined custom_patterns (stored as raw regex strings)
    with the global SEASON_EPISODE_PATTERNS from config.

    User patterns are prepended so they take priority.

    Each user pattern string may be in one of two forms:
      "PATTERN"                    → episode-only  (one capture group)
      "PATTERN|season,episode"     → two capture groups, pipe-separated spec

    Returns a list in the same format as SEASON_EPISODE_PATTERNS.
    """
    compiled: list = []

    raw_patterns: List[str] = settings.get("custom_patterns", [])
    for raw in raw_patterns:
        try:
            if "|" in raw:
                pattern_str, spec = raw.split("|", 1)
                group_names = tuple(g.strip() for g in spec.split(","))
            else:
                pattern_str = raw
                group_names = ("episode",)

            compiled.append((re.compile(pattern_str), group_names))
        except re.error as exc:
            logger.warning("Skipping invalid custom pattern '%s': %s", raw, exc)

    return compiled + list(SEASON_EPISODE_PATTERNS)


def get_dump_channels(settings: Dict[str, Any]) -> List[int]:
    return settings.get("dump_channels") or []


def get_upload_channels(settings: Dict[str, Any]) -> List[int]:
    return settings.get("upload_channels") or []


def forward_to_pm(settings: Dict[str, Any]) -> bool:
    return bool(settings.get("forward_to_pm", True))
