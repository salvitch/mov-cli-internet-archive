from __future__ import annotations

"""Media type detection, format validation, and query parsing for mov-cli-archive.

This module encapsulates data validation, Lucene query generation, thumbnail URL
construction, and explicit media filtering to prevent unplayable archive dumps
and web page mirrors from breaking streaming playback.
"""

import re
import urllib.parse
from typing import Any, List, Optional, Tuple

from .config import (
    NON_MEDIA_FORMATS,
    NON_MEDIA_ARCHIVE_EXTENSIONS,
    WEB_PAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    AUDIO_EXTENSIONS,
)


def natural_sort_key(s: str) -> List[Any]:
    """Split string into text and numeric chunks for natural human sorting.

    Ensures 'episode 2' sorts before 'episode 10'.

    Args:
        s: Raw file name or title string.

    Returns:
        List of alphanumeric tokens for natural sorting.
    """
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", str(s))]


def validate_limit(limit_val: Any, default: int = 100) -> Optional[int]:
    """Validate and normalize result limits.

    Args:
        limit_val: Raw limit value from configuration or caller.
        default: Fallback default limit if limit_val is invalid or None.

    Returns:
        Positive integer limit, or None representing unlimited results.
    """
    if limit_val is None:
        return default

    try:
        parsed = int(limit_val)
        if parsed <= 0:
            return None  # 0 or negative numbers indicate unlimited
        return parsed
    except (ValueError, TypeError):
        return default


def clean_title(raw_title: Any, fallback_id: str) -> str:
    """Validate and sanitize media title text.

    Args:
        raw_title: Title received from Internet Archive metadata.
        fallback_id: Item identifier to use if title is absent or empty.

    Returns:
        Sanitized, non-empty title string.
    """
    if isinstance(raw_title, str) and raw_title.strip():
        cleaned = re.sub(r"\s+", " ", raw_title).strip()
        return cleaned if cleaned else fallback_id
    return str(fallback_id)


def clean_year(raw_year: Any) -> Optional[str]:
    """Extract and validate a 4-digit year string.

    Args:
        raw_year: Year value (string, integer, or date format like '1999-05-12').

    Returns:
        Four-digit year string (e.g. '1999') if parseable, otherwise None.
    """
    if raw_year is None:
        return None

    str_year = str(raw_year).strip()
    match = re.search(r"\b(18|19|20)\d{2}\b", str_year)
    if match:
        return match.group(0)
    return None


def build_image_url(identifier: str) -> Optional[str]:
    """Construct a direct thumbnail image URL for an Internet Archive item.

    Internet Archive exposes a dynamic thumbnail service endpoint:
    `https://archive.org/services/img/{identifier}` which automatically serves
    the item's primary thumbnail image for preview tools like fzf and chafa.

    Args:
        identifier: Internet Archive item identifier.

    Returns:
        Standard HTTPS URL to the item's thumbnail image, or None if empty.
    """
    if not identifier:
        return None
    clean_id = urllib.parse.quote(identifier.strip(), safe="-_.")
    return f"https://archive.org/services/img/{clean_id}"


def sanitize_collection_id(collection_name: str) -> str:
    """Sanitize collection identifier to prevent malformed Lucene queries.

    Args:
        collection_name: Raw collection identifier from user or config.

    Returns:
        Sanitized alphanumeric and safe-character identifier.
    """
    cleaned = re.sub(r"[^\w\-]", "", collection_name.strip())
    return cleaned if cleaned else "moviesandfilms"


def parse_media_mode_and_query(raw_query: str, default_mode: str = "video") -> Tuple[str, str]:
    """Parse media mode flags from query string.

    Supported syntax:
        - 'audio:<query>', 'type:audio <query>', 'media:audio <query>' -> 'audio'
        - 'video:<query>', 'type:video <query>', 'media:video <query>' -> 'video'
        - 'both:<query>', 'type:both <query>', 'media:both <query>',
          'all:<query>', 'type:all <query>', 'media:all <query>' -> 'both'

    Args:
        raw_query: Raw user query string.
        default_mode: Configured default media mode ('video', 'audio', or 'both').

    Returns:
        Tuple of (resolved_media_mode, cleaned_query_without_flag).
    """
    q = raw_query.strip()
    if not q:
        return default_mode, ""

    prefix_map = [
        ("audio:", "audio"),
        ("video:", "video"),
        ("both:", "both"),
        ("all:", "both"),
        ("type:audio", "audio"),
        ("type:video", "video"),
        ("type:both", "both"),
        ("type:all", "both"),
        ("media:audio", "audio"),
        ("media:video", "video"),
        ("media:both", "both"),
        ("media:all", "both"),
    ]

    for prefix, mode in prefix_map:
        if q.lower().startswith(prefix):
            remainder = q[len(prefix):].strip()
            if remainder.startswith(":"):
                remainder = remainder[1:].strip()
            return mode, remainder

    return default_mode, q


def build_mediatype_filter(mode: str) -> str:
    """Build Archive.org Lucene mediatype filter expression.

    Args:
        mode: Media type mode ('video', 'audio', or 'both').

    Returns:
        Lucene filter expression string.
    """
    if mode == "audio":
        return "(mediatype:audio OR mediatype:etree)"
    elif mode in ("both", "all"):
        return "(mediatype:movies OR mediatype:audio OR mediatype:etree)"
    else:
        return "mediatype:movies"


def has_potential_media(raw_formats: Any) -> bool:
    """Evaluate whether an item's format list contains at least one streamable media format.

    Behavior and Logic:
        Internet Archive allows uploaders to mark any item as `mediatype: audio` or
        `mediatype: movies` regardless of the actual file payload. Many items are
        raw data dumps containing only `.rar`/`.zip` archives, metadata files,
        or archived `.html` web pages.

        This function inspects the `format` metadata list returned by Archive.org's search API.
        If all reported formats belong to known non-media categories (e.g., 'Archive BitTorrent',
        'Metadata', 'HTML', 'RAR', 'ZIP', '7z', 'Text', 'Spectrogram'), it returns False.
        This prevents unplayable entries from appearing in search results and failing on playback.

    Args:
        raw_formats: List or string of format names from Internet Archive item metadata.

    Returns:
        False if every format is confirmed to be non-media; True if potential media exists.
    """
    if not raw_formats:
        return True  # If no format metadata is available, do not prematurely reject

    if isinstance(raw_formats, str):
        formats = [raw_formats]
    else:
        formats = list(raw_formats)

    # Return True if there is at least one format outside the non-media blocklist
    return any(f.strip().lower() not in NON_MEDIA_FORMATS for f in formats)


def is_unsupported_archive_file(filename: str) -> bool:
    """Check if a filename indicates a compressed, non-streamable archive package.

    Args:
        filename: File name or path string.

    Returns:
        True if the file matches compressed archive extensions (.rar, .zip, .7z, etc.).
    """
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in NON_MEDIA_ARCHIVE_EXTENSIONS)


def is_web_page_file(filename: str) -> bool:
    """Check if a filename indicates an HTML or web markup file.

    Args:
        filename: File name or path string.

    Returns:
        True if the file matches HTML/web extensions (.html, .htm, etc.).
    """
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in WEB_PAGE_EXTENSIONS)


def is_streamable_video_file(filename: str) -> bool:
    """Check if a filename indicates a streamable video file."""
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in VIDEO_EXTENSIONS)


def is_streamable_audio_file(filename: str) -> bool:
    """Check if a filename indicates a streamable audio file."""
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in AUDIO_EXTENSIONS)
