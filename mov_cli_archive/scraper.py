from __future__ import annotations

"""Internet Archive (archive.org) scraper plugin for mov-cli v4.

This module provides an integration with Internet Archive's media catalog,
enabling searching, interactive collection discovery, multi-episode playlist navigation
(e.g., animepacks, series, albums), and streaming of public video content
(movies, TV shows, anime) and audio content (music, podcasts, old-time radio, audiobooks),
with optional user authentication and terminal image previews.
"""

import re
import time
import urllib.parse
from threading import Lock
from typing import TYPE_CHECKING, Optional, Dict, Generator, Any, List, Tuple

if TYPE_CHECKING:
    from mov_cli import Config
    from mov_cli.http_client import HTTPClient
    from mov_cli.scraper import ScraperOptionsT

from mov_cli import Single, Multi, Metadata, MetadataType
from mov_cli.scraper import Scraper
from mov_cli.utils import EpisodeSelector
from mov_cli.errors import InternalPluginError
import internetarchive

__all__ = ("ArchiveScraper", )


def _natural_sort_key(s: str) -> List[Any]:
    """Split string into text and numeric chunks for natural human sorting.

    Ensures 'episode 2' comes before 'episode 10'.

    Args:
        s: Raw file name or title string.

    Returns:
        List of alphanumeric tokens for natural sorting.
    """
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


class _TTLCache:
    """Thread-safe in-memory cache with Time-To-Live (TTL) expiration.

    Attributes:
        ttl_seconds (int): Duration in seconds before an entry expires.
        max_size (int): Maximum number of entries stored before evicting oldest.
    """

    def __init__(self, ttl_seconds: int = 300, max_size: int = 128) -> None:
        """Initialize the TTL cache.

        Args:
            ttl_seconds: Cache validity duration in seconds (default: 300s / 5 min).
            max_size: Maximum entries capacity (default: 128).
        """
        self._ttl_seconds = ttl_seconds
        self._max_size = max_size
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._lock = Lock()

    def get(self, key: str) -> Optional[Any]:
        """Retrieve an entry from the cache if not expired.

        Args:
            key: Unique lookup key.

        Returns:
            Cached value if present and unexpired; otherwise None.
        """
        with self._lock:
            if key not in self._store:
                return None
            timestamp, value = self._store[key]
            if time.time() - timestamp > self._ttl_seconds:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        """Store an entry in the cache.

        Args:
            key: Unique lookup key.
            value: Data to cache.
        """
        with self._lock:
            # Evict oldest entry if maximum capacity is exceeded
            if len(self._store) >= self._max_size:
                oldest_key = min(self._store, key=lambda k: self._store[k][0], default=None)
                if oldest_key:
                    del self._store[oldest_key]
            self._store[key] = (time.time(), value)


def _validate_limit(limit_val: Any, default: int = 100) -> Optional[int]:
    """Validate and normalize the result count limit.

    Args:
        limit_val: Raw limit value from configuration or caller.
        default: Fallback default limit if limit_val is invalid or None.

    Returns:
        Integer limit (positive number) or None representing an unlimited query.
    """
    if limit_val is None:
        return default

    try:
        parsed = int(limit_val)
        if parsed <= 0:
            return None  # 0 or negative signals unlimited results
        return parsed
    except (ValueError, TypeError):
        return default


def _clean_title(raw_title: Any, fallback_id: str) -> str:
    """Validate and clean media title text.

    Args:
        raw_title: Title received from Internet Archive metadata.
        fallback_id: Item identifier to use if title is absent or empty.

    Returns:
        Sanitized, non-empty title string.
    """
    if isinstance(raw_title, str) and raw_title.strip():
        # Collapse excessive whitespace and strip trailing noise
        cleaned = re.sub(r"\s+", " ", raw_title).strip()
        return cleaned if cleaned else fallback_id
    return str(fallback_id)


def _clean_year(raw_year: Any) -> Optional[str]:
    """Validate and normalize year values into a 4-digit string format.

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


def _build_image_url(identifier: str) -> Optional[str]:
    """Construct a direct thumbnail image URL for an Internet Archive item.

    Internet Archive exposes a dynamic thumbnail service endpoint:
    `https://archive.org/services/img/{identifier}` which automatically serves
    the item's primary thumbnail image for preview tools like fzf and chafa.

    Args:
        identifier: Internet Archive item identifier.

    Returns:
        Standard HTTPS URL to the item's thumbnail image, or None if identifier is empty.
    """
    if not identifier:
        return None
    clean_id = urllib.parse.quote(identifier.strip(), safe="-_.")
    return f"https://archive.org/services/img/{clean_id}"


def _sanitize_collection_id(collection_name: str) -> str:
    """Sanitize collection identifier to prevent malformed Lucene queries.

    Args:
        collection_name: Raw collection identifier from user or config.

    Returns:
        Sanitized alphanumeric and safe-character identifier.
    """
    # Retain only standard identifier characters (alphanumeric, dashes, underscores)
    cleaned = re.sub(r"[^\w\-]", "", collection_name.strip())
    return cleaned if cleaned else "moviesandfilms"


def _parse_media_mode_and_query(raw_query: str, default_mode: str = "video") -> Tuple[str, str]:
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


def _build_mediatype_filter(mode: str) -> str:
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
)


def _has_potential_media(raw_formats: Any) -> bool:
    """Check if an item's format list contains at least one playable media format.

    Filters out items that only consist of compressed archive dumps (.rar, .zip),
    metadata files, or raw HTML pages.

    Args:
        raw_formats: Formats attribute from Internet Archive search result.

    Returns:
        False if all formats are confirmed non-media archives; True otherwise.
    """
    if not raw_formats:
        return True
    if isinstance(raw_formats, str):
        formats = [raw_formats]
    else:
        formats = list(raw_formats)

    return any(f.strip().lower() not in NON_MEDIA_FORMATS for f in formats)


class ArchiveScraper(Scraper):
    """mov-cli scraper for browsing, discovering, and streaming from archive.org.

    Supports:
        - Multi-format streaming: Video (Movies, TV, Anime Packs) and Audio (Music, OTR, Podcasts).
        - Multi-episode series & playlist navigation: Iterate through batch packs (e.g. animepacks)
          or audio albums with automatic episode selection, skipping (next/previous), and track titles.
        - Targeted media selection: Search video, audio, or both simultaneously via CLI flags or config.
        - Curated interactive collection browsing across Video and Audio libraries.
        - Direct collection filtering with search keywords (e.g., 'collection:animepacks naruto').
        - Broad Lucene full-text searches with wildcard and exclusion support.
        - Optional authenticated session integration for restricted/borrowed items.
        - Terminal image previews via fzf and chafa integration.
        - In-memory result caching and optimized field querying to minimize network traffic.
    """

    # Curated video collections for interactive catalog mode
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

    # Curated audio collections for interactive catalog mode
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

    # Curated collections across both Video and Audio
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

    # Stream format preferences in descending order of compatibility
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

    # Valid media file extensions
    VIDEO_EXTENSIONS: Tuple[str, ...] = (
        ".mp4",
        ".mkv",
        ".webm",
        ".avi",
        ".ogv",
        ".mov",
        ".m4v",
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

    def __init__(
        self,
        config: Config,
        http_client: HTTPClient,
        options: Optional[ScraperOptionsT] = None,
    ) -> None:
        """Initialize the Archive scraper, options, and authentication.

        Args:
            config: mov-cli configuration instance.
            http_client: mov-cli HTTP client instance.
            options: Scraper-specific options loaded from config.toml.

        Raises:
            InternalPluginError: If user credentials fail during configuration.
        """
        super().__init__(config, http_client, options)

        self._search_cache = _TTLCache(ttl_seconds=300, max_size=64)
        self._media_files_cache = _TTLCache(ttl_seconds=600, max_size=128)

        # Extract and validate options
        opts = self.options or {}
        self.username: Optional[str] = opts.get("username")
        self.password: Optional[str] = opts.get("password")
        self.default_limit: Optional[int] = _validate_limit(opts.get("limit", 100), default=100)
        self.fetch_images: bool = bool(opts.get("fetch_images", True))

        # Default media type: 'video', 'audio', or 'both'
        raw_media_type = str(opts.get("media_type", "video")).lower().strip()
        if raw_media_type in ("video", "audio", "both", "all"):
            self.default_media_type: str = "both" if raw_media_type == "all" else raw_media_type
        else:
            self.default_media_type = "video"

        # Configure session authentication if credentials are provided
        if self.username and self.password:
            try:
                internetarchive.configure(self.username, self.password)
            except Exception as exc:
                raise InternalPluginError(
                    f"Failed to authenticate with Internet Archive: {exc}"
                ) from exc

    def _resolve_playable_media_files(self, item_id: str) -> List[Any]:
        """Fetch, filter, deduplicate, and naturally sort all playable media files in an item.

        For series and pack collections (like animepacks), this isolates distinct
        episodes or audio tracks in numerical sequence, ignoring duplicate derivative files.

        Args:
            item_id: Internet Archive item identifier.

        Returns:
            List of IA File objects sorted naturally by episode/track order.

        Raises:
            InternalPluginError: If the item does not exist or has no media.
        """
        cached = self._media_files_cache.get(item_id)
        if cached is not None:
            return cached

        try:
            item = internetarchive.get_item(item_id)
            if not item.exists:
                raise InternalPluginError(
                    f"Item '{item_id}' was not found on Internet Archive (may have been deleted or made private)."
                )

            if getattr(item, "item_metadata", {}).get("is_dark", False):
                raise InternalPluginError(
                    f"Item '{item_id}' is restricted/dark on Internet Archive."
                )

            files = list(item.get_files())
        except Exception as exc:
            if isinstance(exc, InternalPluginError):
                raise
            raise InternalPluginError(
                f"Failed to fetch item files from Internet Archive for '{item_id}': {exc}"
            ) from exc

        if not files:
            raise InternalPluginError(
                f"No files are associated with Internet Archive item '{item_id}'."
            )

        # 1. First, check if there are video files
        video_files = [
            f for f in files
            if any(f.name.lower().endswith(ext) for ext in self.VIDEO_EXTENSIONS)
        ]

        if video_files:
            # Prefer original files to avoid duplicate .ia.mp4 derivatives
            orig_videos = [f for f in video_files if getattr(f, "source", None) == "original"]
            if orig_videos:
                candidates = orig_videos
            else:
                # Deduplicate derivatives by base name if no originals marked
                seen_bases = set()
                candidates = []
                for f in video_files:
                    base = re.sub(r"\.ia\.(mp4|mkv|webm)$", "", f.name.lower())
                    if base not in seen_bases:
                        seen_bases.add(base)
                        candidates.append(f)
        if not video_files:
            # 2. Check for audio files
            audio_files = [
                f for f in files
                if any(f.name.lower().endswith(ext) for ext in self.AUDIO_EXTENSIONS)
            ]
            if not audio_files:
                # Neither video nor audio files exist. Inspect file types to report clear error
                archive_exts = (".rar", ".zip", ".7z", ".tar", ".gz", ".iso")
                archive_files = [f.name for f in files if any(f.name.lower().endswith(ext) for ext in archive_exts)]
                html_files = [f.name for f in files if f.name.lower().endswith((".html", ".htm"))]

                if archive_files:
                    raise InternalPluginError(
                        f"Item '{item_id}' only contains compressed archive files ({', '.join(archive_files[:2])}) "
                        "which cannot be directly streamed. Unpackable media is required."
                    )
                elif html_files:
                    raise InternalPluginError(
                        f"Item '{item_id}' is a web page archive (.html) with no playable audio or video streams."
                    )
                else:
                    file_names = [f.name for f in files[:3]]
                    raise InternalPluginError(
                        f"No streamable video or audio files found in '{item_id}' (contains: {', '.join(file_names)})."
                    )

            orig_audio = [f for f in audio_files if getattr(f, "source", None) == "original"]
            audio_candidates = orig_audio if orig_audio else audio_files

            # Deduplicate multiple audio formats (e.g. .flac and .mp3 for same track)
            seen_bases = set()
            candidates = []
            for f in audio_candidates:
                base = re.sub(r"\.(mp3|flac|ogg|wav|m4a|aac|opus|wma|aiff|alac)$", "", f.name.lower())
                if base not in seen_bases:
                    seen_bases.add(base)
                    candidates.append(f)

        sorted_files = sorted(candidates, key=lambda f: _natural_sort_key(f.name))
        self._media_files_cache.set(item_id, sorted_files)
        return sorted_files

    def search(
        self,
        query: str,
        limit: int | None = None,
    ) -> Generator[Metadata, Any, None]:
        """Search Internet Archive for video and audio content or open interactive catalog.

        Args:
            query: User search string, empty string for catalog mode, or query with
                media mode prefix (e.g. 'audio:zelda', 'type:both evangelion') or
                targeted collection filter (e.g. 'collection:animepacks naruto').
            limit: Maximum number of search results to return (None uses scraper default).

        Yields:
            Metadata: Validated mov-cli Metadata objects for matching media items.

        Raises:
            InternalPluginError: If the Internet Archive API is unreachable or fails.
        """
        actual_limit = _validate_limit(limit, default=self.default_limit)
        clean_query = query.strip() if query else ""

        # Parse media mode prefix if present (e.g. 'audio:', 'video:', 'both:', 'type:audio')
        media_mode, clean_query = _parse_media_mode_and_query(clean_query, self.default_media_type)

        # Check in-memory search cache to reduce API round-trips
        cache_key = f"{media_mode}:{clean_query}:{actual_limit}"
        cached_results = self._search_cache.get(cache_key)
        if cached_results is not None:
            for item in cached_results:
                yield item
            return

        collected_items: List[Metadata] = []

        try:
            if not clean_query:
                # ---------------------------------------------------------
                # 1. Interactive Catalog Mode
                # ---------------------------------------------------------
                try:
                    import inquirer
                except ImportError as exc:
                    raise InternalPluginError(
                        "The 'inquirer' package is required for interactive catalog mode. "
                        "Please run 'pipx inject mov-cli inquirer' or provide a search query."
                    ) from exc

                try:
                    # Category selection prompt
                    category_prompt = [
                        inquirer.List(
                            "category",
                            message="Archive.org Catalog - Select Media Category",
                            choices=[
                                ("🎬 Video & Movies", "video"),
                                ("🎵 Audio & Music", "audio"),
                                ("🌐 Both (Video & Audio)", "both"),
                            ],
                            default=self.default_media_type,
                        )
                    ]
                    cat_ans = inquirer.prompt(category_prompt)
                    if not cat_ans:
                        return  # User aborted

                    media_mode = cat_ans.get("category", "video")

                    if media_mode == "audio":
                        col_choices = self.CURATED_AUDIO_COLLECTIONS
                    elif media_mode == "both":
                        col_choices = self.CURATED_COMBINED_COLLECTIONS
                    else:
                        col_choices = self.CURATED_VIDEO_COLLECTIONS

                    questions = [
                        inquirer.List(
                            "collection",
                            message=f"Browse {media_mode.title()} Collections",
                            choices=col_choices,
                        )
                    ]
                    answer = inquirer.prompt(questions)
                    if not answer:
                        return  # User aborted

                    raw_col = answer.get("collection", "")

                    if raw_col == "*all*":
                        selected_collection = None
                    else:
                        selected_collection = _sanitize_collection_id(raw_col)

                    col_label = selected_collection if selected_collection else "All Collections"

                    # Secondary interactive keyword prompt inside chosen collection
                    search_q = [
                        inquirer.Text(
                            "term",
                            message=f"Search within '{col_label}' (Leave empty to list popular items)",
                        )
                    ]
                    term_ans = inquirer.prompt(search_q)
                    term = term_ans.get("term", "").strip() if term_ans else ""

                except (KeyboardInterrupt, EOFError):
                    return  # Gracefully exit on user interrupt

                mediatype_filter = _build_mediatype_filter(media_mode)

                query_parts = []
                if term:
                    safe_term = term.replace('"', '\\"')
                    query_parts.append(f"({safe_term})")
                query_parts.append(mediatype_filter)
                if selected_collection:
                    query_parts.append(f"collection:{selected_collection}")

                search_query = " AND ".join(query_parts)

                results = internetarchive.search_items(
                    search_query,
                    sorts=["downloads desc"],
                    fields=["identifier", "title", "year", "mediatype", "collection", "format"],
                )

            elif clean_query.startswith("collection:"):
                # ---------------------------------------------------------
                # 2. Direct Collection Filter Mode (e.g. 'collection:animepacks naruto')
                # ---------------------------------------------------------
                parts = clean_query.split(" ", 1)
                raw_collection = parts[0].split("collection:", 1)[1]
                collection_id = _sanitize_collection_id(raw_collection)
                mediatype_filter = _build_mediatype_filter(media_mode)

                query_parts = [mediatype_filter, f"collection:{collection_id}"]
                if len(parts) > 1 and parts[1].strip():
                    sub_keyword = parts[1].strip().replace('"', '\\"')
                    query_parts.insert(0, f"({sub_keyword})")

                search_query = " AND ".join(query_parts)

                results = internetarchive.search_items(
                    search_query,
                    sorts=["downloads desc"],
                    fields=["identifier", "title", "year", "mediatype", "collection", "format"],
                )

            else:
                # ---------------------------------------------------------
                # 3. Broad Full-Text Search
                # ---------------------------------------------------------
                safe_query = clean_query.replace('"', '\\"')
                mediatype_filter = _build_mediatype_filter(media_mode)
                search_query = f"({safe_query}) AND {mediatype_filter}"
                results = internetarchive.search_items(
                    search_query,
                    fields=["identifier", "title", "year", "mediatype", "collection", "format"],
                )

            count = 0
            for item in results:
                if actual_limit is not None and count >= actual_limit:
                    break

                # Filter out items that only contain compressed archive packages (.rar, .zip) or web pages
                raw_formats = item.get("format")
                if raw_formats and not _has_potential_media(raw_formats):
                    continue

                raw_id = item.get("identifier")
                if not raw_id or not isinstance(raw_id, str):
                    continue

                item_id = raw_id.strip()
                title = _clean_title(item.get("title"), fallback_id=item_id)
                year = _clean_year(item.get("year"))
                image_url = _build_image_url(item_id) if self.fetch_images else None

                item_mediatype = (item.get("mediatype") or "").lower()

                # In 'both' mode, prepend media badge to title for immediate clarity
                if media_mode in ("both", "all"):
                    if item_mediatype in ("audio", "etree"):
                        title = f"[Audio] {title}"
                    else:
                        title = f"[Video] {title}"
                elif media_mode == "audio":
                    title = f"[Audio] {title}"

                # Mark as MULTI so mov-cli calls scrape_episodes on selection.
                # If scrape_episodes resolves to 1 file, it plays directly as standalone;
                # If multiple files exist (anime packs, series, albums), it opens episode selection.
                meta = Metadata(
                    id=item_id,
                    title=title,
                    type=MetadataType.MULTI,
                    image_url=image_url,
                    year=year,
                )

                collected_items.append(meta)
                yield meta
                count += 1

            # Cache completed search result set
            self._search_cache.set(cache_key, collected_items)

        except Exception as exc:
            if isinstance(exc, InternalPluginError):
                raise
            raise InternalPluginError(
                f"Internet Archive search failed: {exc}"
            ) from exc

    def scrape_episodes(self, metadata: Metadata) -> Dict[Optional[int], int]:
        """Map available episodes or tracks for multi-file items.

        Resolves playable video or audio files in the Archive item.
        If the item contains multiple distinct episodes or tracks (e.g. animepacks),
        returns `{1: episode_count}` allowing full episode navigation.
        If only 1 file is present, returns `{None: 1}` to play immediately as a single stream.

        Args:
            metadata: Metadata object for the item.

        Returns:
            Dictionary mapping season to episode count, or `{None: 1}` for single files.
        """
        if not metadata or not metadata.id:
            return {None: 1}

        try:
            media_files = self._resolve_playable_media_files(str(metadata.id).strip())
            total = len(media_files)
            if total > 1:
                return {1: total}
            return {None: 1}
        except Exception:
            return {None: 1}

    def scrape(self, metadata: Metadata, episode: EpisodeSelector) -> Single | Multi:
        """Resolve and extract a playable video or audio stream URL from the item.

        If the item has multiple files (e.g. animepacks batch or music album),
        selects the specific episode or track corresponding to `episode.episode`.

        Args:
            metadata: Metadata object of the selected item.
            episode: Episode selector containing the target episode number (1-indexed).

        Returns:
            Single: mov-cli Single stream instance with verified media URL.

        Raises:
            InternalPluginError: If item files cannot be retrieved, item is unavailable,
                or no valid video/audio stream is found.
        """
        if not metadata or not metadata.id:
            raise InternalPluginError("Invalid media selection: missing item identifier.")

        item_id = str(metadata.id).strip()
        media_files = self._resolve_playable_media_files(item_id)

        if not media_files:
            raise InternalPluginError(
                f"No playable video or audio format found for '{metadata.title}' (ID: {item_id})."
            )

        # Resolve requested episode index (1-indexed from EpisodeSelector)
        ep_idx = 0
        if episode and hasattr(episode, "episode") and episode.episode:
            ep_idx = max(0, episode.episode - 1)

        if ep_idx >= len(media_files):
            ep_idx = 0

        target_file = media_files[ep_idx]
        url = getattr(target_file, "url", None)

        if not url:
            encoded_name = urllib.parse.quote(target_file.name)
            url = f"https://archive.org/download/{item_id}/{encoded_name}"

        # Clean display title with episode details if multiple files exist
        if len(media_files) > 1:
            clean_filename = target_file.name.rsplit("/", 1)[-1]
            title = f"{metadata.title} [Ep {ep_idx + 1}/{len(media_files)}: {clean_filename}]"
        else:
            title = metadata.title

        return Single(
            url=url,
            title=title,
            year=metadata.year,
        )
