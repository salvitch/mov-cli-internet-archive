from __future__ import annotations

"""Internet Archive (archive.org) scraper plugin for mov-cli v4.

This module provides an integration with Internet Archive's media catalog,
enabling searching, interactive collection discovery, and streaming of
public video content (movies, TV shows, anime) and audio content (music, podcasts,
old-time radio, audiobooks), with optional user authentication and terminal image previews.
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
            # If remainder started with colon or space, strip again
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


class ArchiveScraper(Scraper):
    """mov-cli scraper for browsing, discovering, and streaming from archive.org.

    Supports:
        - Multi-format streaming: Video (Movies, TV, Anime) and Audio (Music, OTR, Podcasts, Soundtracks).
        - Targeted media selection: Search video, audio, or both simultaneously via CLI flags or config.
        - Curated interactive collection browsing across Video and Audio libraries.
        - Direct collection filtering with search keywords (e.g., 'collection:anime-series evangelion').
        - Broad Lucene full-text searches with wildcard and exclusion support.
        - Optional authenticated session integration for restricted/borrowed items.
        - Terminal image previews via fzf and chafa integration.
        - In-memory result caching and optimized field querying to minimize network traffic.
    """

    # Curated video collections for interactive catalog mode
    CURATED_VIDEO_COLLECTIONS: List[Tuple[str, str]] = [
        ("Feature Films", "feature_films"),
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
        "Ogg Vorbis",
        "AAC",
        "MPEG-4 Audio",
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
        self._item_cache = _TTLCache(ttl_seconds=600, max_size=128)

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

    def search(
        self,
        query: str,
        limit: int | None = None,
    ) -> Generator[Metadata, Any, None]:
        """Search Internet Archive for video and audio content or open interactive catalog.

        Args:
            query: User search string, empty string for catalog mode, or query with
                media mode prefix (e.g. 'audio:zelda', 'type:both evangelion') or
                targeted collection filter (e.g. 'collection:oldtimeradio shadow').
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
                    fields=["identifier", "title", "year", "mediatype"],
                )

            elif clean_query.startswith("collection:"):
                # ---------------------------------------------------------
                # 2. Direct Collection Filter Mode (e.g. 'collection:etree grateful dead')
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
                    fields=["identifier", "title", "year", "mediatype"],
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
                    fields=["identifier", "title", "year", "mediatype"],
                )

            count = 0
            for item in results:
                if actual_limit is not None and count >= actual_limit:
                    break

                # Validate essential identifier
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

                meta = Metadata(
                    id=item_id,
                    title=title,
                    type=MetadataType.SINGLE,
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

    def scrape_episodes(self, metadata: Metadata) -> Dict[int, int] | Dict[None, int]:
        """Map episode availability for the selected item.

        Internet Archive items are treated as standalone media packages;
        returns a single playable stream key.

        Args:
            metadata: Metadata object for the item.

        Returns:
            Dictionary mapping episode structure ({None: 1}).
        """
        return {None: 1}

    def scrape(self, metadata: Metadata, episode: EpisodeSelector) -> Single | Multi:
        """Resolve and extract a playable video or audio stream URL from the item.

        Searches the files associated with the item on Archive.org, selecting
        the highest quality supported format:
            1. Preferred video format (h.264, MPEG4, Matroska, WebM, etc.)
            2. Preferred audio format (VBR MP3, MP3, FLAC, Vorbis, AAC, etc.)
            3. Fallback video file extensions (.mp4, .mkv, .webm, .avi, etc.)
            4. Fallback audio file extensions (.mp3, .flac, .ogg, .wav, etc.)

        Args:
            metadata: Metadata object of the selected item.
            episode: Episode selector (unused for standalone archive items).

        Returns:
            Single: mov-cli Single stream instance with verified media URL.

        Raises:
            InternalPluginError: If item files cannot be retrieved, item is unavailable,
                or no valid video/audio stream is found.
        """
        if not metadata or not metadata.id:
            raise InternalPluginError("Invalid media selection: missing item identifier.")

        item_id = str(metadata.id).strip()

        # Check in-memory item URL cache
        cached_url = self._item_cache.get(item_id)
        if cached_url:
            return Single(
                url=cached_url,
                title=metadata.title,
                year=metadata.year,
            )

        try:
            item = internetarchive.get_item(item_id)
            if not item.exists:
                raise InternalPluginError(
                    f"Item '{item_id}' was not found on Internet Archive (may have been deleted or made private)."
                )

            # Check if item is restricted or dark
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

        media_url: Optional[str] = None

        # 1. Preferred video formats
        for preferred_fmt in self.PREFERRED_VIDEO_FORMATS:
            for f in files:
                fmt = getattr(f, "format", "") or ""
                if fmt.lower() == preferred_fmt.lower():
                    url = getattr(f, "url", None)
                    if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                        media_url = url
                        break
            if media_url:
                break

        # 2. Preferred audio formats
        if not media_url:
            for preferred_fmt in self.PREFERRED_AUDIO_FORMATS:
                for f in files:
                    fmt = getattr(f, "format", "") or ""
                    if fmt.lower() == preferred_fmt.lower():
                        url = getattr(f, "url", None)
                        if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                            media_url = url
                            break
                if media_url:
                    break

        # 3. Fallback video extensions
        if not media_url:
            for f in files:
                name = (getattr(f, "name", "") or "").lower()
                if any(name.endswith(ext) for ext in self.VIDEO_EXTENSIONS):
                    url = getattr(f, "url", None)
                    if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                        media_url = url
                        break

        # 4. Fallback audio extensions
        if not media_url:
            for f in files:
                name = (getattr(f, "name", "") or "").lower()
                if any(name.endswith(ext) for ext in self.AUDIO_EXTENSIONS):
                    url = getattr(f, "url", None)
                    if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                        media_url = url
                        break

        if not media_url:
            raise InternalPluginError(
                f"No playable video or audio format found for '{metadata.title}' (ID: {item_id})."
            )

        # Cache resolved stream URL
        self._item_cache.set(item_id, media_url)

        return Single(
            url=media_url,
            title=metadata.title,
            year=metadata.year,
        )
