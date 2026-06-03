"""
Extracts season and episode numbers from a filename.

Strategy:
1. Try each SEASON_EPISODE_PATTERNS in order.
2. If all fail, fall back to anitopy.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional, Tuple

import anitopy

from config import SEASON_EPISODE_PATTERNS, FILENAME_TEMPLATE, FILENAME_TEMPLATE_NO_SEASON

logger = logging.getLogger(__name__)


def extract_season_episode(filename: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Returns (season, episode).  season may be None if not found.
    """
    base = os.path.splitext(os.path.basename(filename))[0]

    for pattern in SEASON_EPISODE_PATTERNS:
        m = pattern.search(base)
        if m:
            groups = m.groupdict()
            season = int(groups["season"]) if groups.get("season") else None
            episode = int(groups["episode"]) if groups.get("episode") else None
            if episode is not None:
                logger.debug("Regex matched — season=%s episode=%s (%s)", season, episode, pattern.pattern)
                return season, episode

    # ── Fallback: anitopy ────────────────────────────────────────────────
    try:
        parsed = anitopy.parse(base)
        episode_str = parsed.get("episode_number")
        season_str = parsed.get("anime_season")
        episode = int(episode_str) if episode_str else None
        season = int(season_str) if season_str else None
        if episode is not None:
            logger.debug("anitopy matched — season=%s episode=%s", season, episode)
            return season, episode
    except Exception as exc:
        logger.warning("anitopy parse error: %s", exc)

    return None, None


def build_filename(title: str, filename: str) -> str:
    """
    Build the final renamed filename from the original downloaded filename.
    """
    season, episode = extract_season_episode(filename)

    if episode is None:
        # Cannot determine episode; use original stem + title prefix
        stem = os.path.splitext(os.path.basename(filename))[0]
        return f"{title} - {stem}.mkv"

    if season is not None:
        return FILENAME_TEMPLATE.format(title=title, season=season, episode=episode)

    return FILENAME_TEMPLATE_NO_SEASON.format(title=title, episode=episode)
