from __future__ import annotations

"""Configuration constants, format definitions, and collection registries for mov-cli-archive.

This module defines all static configuration, file extensions, format priorities,
and curated collection registries used across the Archive.org scraper.
"""

import os
from pathlib import Path
from typing import List, Tuple

# -------------------------------------------------------------------------
# Local Cache Configuration
# -------------------------------------------------------------------------

DEFAULT_CACHE_DIR: Path = Path(
    os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
) / "mov-cli-archive"

# Default TTL in seconds (1 hour for search results, 6 hours for item metadata)
DEFAULT_SEARCH_CACHE_TTL: int = 3600
DEFAULT_ITEM_CACHE_TTL: int = 21600

# -------------------------------------------------------------------------
# Curated Collection Registries
# -------------------------------------------------------------------------

CURATED_VIDEO_COLLECTIONS: List[Tuple[str, str]] = [
    ("Feature Films", "feature_films"),
    ("Anime Packs (Batch Releases)", "animepacks"),
    ("Anime (General)", "anime"),
    ("Anime Series", "anime-series"),
    ("Animation & Cartoons", "animationandcartoons"),
    ("Sci-Fi & Horror", "SciFi_Horror"),
    ("Comedy Films", "Comedy_Films"),
    ("Classic Television", "television"),
    ("TV Archive", "tvarchive"),
    ("Short Films", "short_films"),
    ("Silent Films", "silent_films"),
    ("Film Noir", "Film_Noir"),
]

CURATED_AUDIO_COLLECTIONS: List[Tuple[str, str]] = [
    ("Old Time Radio", "oldtimeradio"),
    ("Live Concerts (Etree)", "etree"),
    ("78 RPMs & Cylinder Recordings", "georgeblood"),
    ("Audiobooks & Poetry", "audio_bookspoetry"),
    ("Netlabels (Electronic Music)", "netlabels"),
    ("Podcasts", "podcast"),
    ("Radio Programs & Broadcasts", "radioprograms"),
    ("Community Audio & Music", "audio_music"),
]

CURATED_COMBINED_COLLECTIONS: List[Tuple[str, str]] = [
    ("All Video & Audio (Search All)", "*all*"),
    ("Anime Packs (Batch Releases)", "animepacks"),
    ("Feature Films (Video)", "feature_films"),
    ("Anime (General)", "anime"),
    ("Classic Television (Video)", "television"),
    ("Old Time Radio (Audio)", "oldtimeradio"),
    ("Live Concerts (Audio)", "etree"),
    ("Audiobooks & Poetry (Audio)", "audio_bookspoetry"),
]

# -------------------------------------------------------------------------
# Format & File Extension Definitions
# -------------------------------------------------------------------------

PREFERRED_VIDEO_FORMATS: Tuple[str, ...] = (
    "h.264",
    "MPEG4",
    "Matroska",
    "WebM",
    "Ogg Video",
    "512Kb MPEG4",
)

PREFERRED_AUDIO_FORMATS: Tuple[str, ...] = (
    "VBR MP3",
    "MP3",
    "Flac",
    "128Kbps MP3",
    "Advanced Audio Coding",
    "Ogg Vorbis",
    "AAC",
    "MPEG-4 Audio",
    "Waveform Audio",
)

VIDEO_EXTENSIONS: Tuple[str, ...] = (
    ".mp4",
    ".mkv",
    ".webm",
    ".avi",
    ".ogv",
    ".mov",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".wmv",
)

AUDIO_EXTENSIONS: Tuple[str, ...] = (
    ".mp3",
    ".flac",
    ".ogg",
    ".wav",
    ".m4a",
    ".aac",
    ".opus",
    ".wma",
    ".aiff",
    ".alac",
)

# Explicit archive formats that cannot be streamed over HTTP by players
NON_MEDIA_ARCHIVE_EXTENSIONS: Tuple[str, ...] = (
    ".rar",
    ".zip",
    ".7z",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".iso",
    ".bin",
    ".cue",
)

# Web page indicators that contain markup rather than media
WEB_PAGE_EXTENSIONS: Tuple[str, ...] = (
    ".html",
    ".htm",
    ".xhtml",
    ".xml",
    ".json",
    ".php",
    ".asp",
)

# Internet Archive format labels representing non-media metadata or packaging
NON_MEDIA_FORMATS: Tuple[str, ...] = (
    "archive bittorrent",
    "metadata",
    "item tile",
    "thumbnail",
    "spectrogram",
    "text",
    "html",
    "xml",
    "sqlite",
    "checksums",
    "rar",
    "zip",
    "7z",
    "tar",
    "iso",
    "graphic",
    "single page processed jp2 zip",
    "abyssoft installer",
    "windows executable",
    "chm",
    "djvu",
    "epub",
    "pdf",
)
