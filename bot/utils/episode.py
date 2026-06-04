"""
Season/episode extraction + filename building.

  extract_season_episode(filename, user_settings=None)
  build_filename(title, filename, user_settings=None)

When user_settings is supplied, custom_patterns from the DB are prepended
to the global pattern list and the user's rename_template is used.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

import anitopy

from config import SEASON_EPISODE_PATTERNS, FILENAME_TEMPLATE, FILENAME_TEMPLATE_NO_SEASON

logger = logging.getLogger(__name__)


def _match_pattern(entry, text: str) -> Tuple[Optional[int], Optional[int]]:
    if isinstance(entry, tuple):
        pattern, group_names = entry
        m = pattern.search(text)
        if not m:
            return None, None
        groups = m.groups()
        season = episode = None
        for i, name in enumerate(group_names):
            if i >= len(groups) or groups[i] is None:
                continue
            try:
                v = int(groups[i])
                if name == "season":
                    season = v
                elif name == "episode":
                    episode = v
            except ValueError:
                pass
        return season, episode

    # Plain re.Pattern with named groups
    m = entry.search(text)
    if not m:
        return None, None
    gd = m.groupdict()
    season  = int(gd["season"])  if gd.get("season")  else None
    episode = int(gd["episode"]) if gd.get("episode") else None
    return season, episode


def _get_patterns(user_settings: Optional[Dict[str, Any]]) -> list:
    """Return merged pattern list: user custom patterns first, then global."""
    if not user_settings:
        return list(SEASON_EPISODE_PATTERNS)

    from bot.utils.user_settings import get_patterns
    return get_patterns(user_settings)


def extract_season_episode(
    filename: str,
    user_settings: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[int], Optional[int]]:
    base     = os.path.splitext(os.path.basename(filename))[0]
    patterns = _get_patterns(user_settings)

    for entry in patterns:
        season, episode = _match_pattern(entry, base)
        if episode is not None:
            logger.debug("Pattern matched — S=%s E=%s file='%s'", season, episode, base)
            return season, episode

    # Fallback: anitopy
    try:
        parsed  = anitopy.parse(base)
        ep_raw  = parsed.get("episode_number")
        sea_raw = parsed.get("anime_season")
        episode = int(ep_raw)  if ep_raw  else None
        season  = int(sea_raw) if sea_raw else None
        if episode is not None:
            logger.debug("anitopy matched — S=%s E=%s", season, episode)
            return season, episode
    except Exception as exc:
        logger.warning("anitopy parse error for '%s': %s", base, exc)

    logger.warning("Could not extract episode from '%s'", base)
    return None, None


def build_filename(
    title: str,
    filename: str,
    user_settings: Optional[Dict[str, Any]] = None,
) -> str:
    season, episode = extract_season_episode(filename, user_settings)

    # Resolve templates
    if user_settings and user_settings.get("rename_template"):
        from bot.utils.user_settings import get_rename_templates
        tmpl_season, tmpl_no_season = get_rename_templates(user_settings)
    else:
        tmpl_season    = FILENAME_TEMPLATE
        tmpl_no_season = FILENAME_TEMPLATE_NO_SEASON

    if episode is None:
        stem = os.path.splitext(os.path.basename(filename))[0]
        return f"{title} - {stem}.mkv"

    if season is not None:
        try:
            return tmpl_season.format(title=title, season=season, episode=episode)
        except (KeyError, ValueError):
            pass

    try:
        return tmpl_no_season.format(title=title, episode=episode)
    except (KeyError, ValueError):
        return f"{title} - E{episode:02d}.mkv"
