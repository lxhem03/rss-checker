"""
Extracts season and episode numbers from a filename.

Supports two formats for SEASON_EPISODE_PATTERNS entries:

  1. Plain re.Pattern  (original format, named groups: season / episode)
     re.compile(r'[Ss](?P<season>\d{1,2})[Ee](?P<episode>\d{2,3})')

  2. Tuple of (re.Pattern, group_name_tuple)  (Bruno's new format)
     (re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})'), ('season', 'episode'))
     (re.compile(r'...(\d{1,3})'),                ('episode',))

Strategy:
  1. Try each pattern in SEASON_EPISODE_PATTERNS in order.
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


def _match_pattern(entry, text: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Try a single SEASON_EPISODE_PATTERNS entry against *text*.

    Returns (season, episode) where either may be None.
    Returns (None, None) if no match.
    """
    # ── Tuple format: (compiled_pattern, ('season', 'episode'))  ──────────
    if isinstance(entry, tuple):
        pattern, group_names = entry
        m = pattern.search(text)
        if not m:
            return None, None

        groups = m.groups()
        season = None
        episode = None

        for i, name in enumerate(group_names):
            if i >= len(groups):
                break
            val = groups[i]
            if val is None:
                continue
            try:
                if name == "season":
                    season = int(val)
                elif name == "episode":
                    episode = int(val)
            except ValueError:
                pass

        return season, episode

    # ── Plain re.Pattern with named groups ────────────────────────────────
    m = entry.search(text)
    if not m:
        return None, None

    gd = m.groupdict()
    season  = int(gd["season"])  if gd.get("season")  else None
    episode = int(gd["episode"]) if gd.get("episode") else None
    return season, episode


def extract_season_episode(filename: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Returns (season, episode).  season may be None if not found.
    """
    base = os.path.splitext(os.path.basename(filename))[0]

    for entry in SEASON_EPISODE_PATTERNS:
        season, episode = _match_pattern(entry, base)
        if episode is not None:
            logger.debug(
                "Pattern matched — season=%s episode=%s  file='%s'",
                season, episode, base,
            )
            return season, episode

    # ── Fallback: anitopy ────────────────────────────────────────────────
    try:
        parsed = anitopy.parse(base)
        ep_raw  = parsed.get("episode_number")
        sea_raw = parsed.get("anime_season")
        episode = int(ep_raw)  if ep_raw  else None
        season  = int(sea_raw) if sea_raw else None
        if episode is not None:
            logger.debug("anitopy matched — season=%s episode=%s", season, episode)
            return season, episode
    except Exception as exc:
        logger.warning("anitopy parse error for '%s': %s", base, exc)

    logger.warning("Could not extract episode from '%s'", base)
    return None, None


def build_filename(title: str, filename: str) -> str:
    """Build the final renamed filename."""
    season, episode = extract_season_episode(filename)

    if episode is None:
        stem = os.path.splitext(os.path.basename(filename))[0]
        return f"{title} - {stem}.mkv"

    if season is not None:
        return FILENAME_TEMPLATE.format(title=title, season=season, episode=episode)

    return FILENAME_TEMPLATE_NO_SEASON.format(title=title, episode=episode)
