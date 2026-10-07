from __future__ import annotations

"""Internet Archive (archive.org) scraper plugin for mov-cli v4.

This module provides an integration with Internet Archive's media catalog,
enabling searching, interactive collection discovery, and streaming of
public video content (movies, TV shows, and anime series) with optional user authentication.
"""

import re
import time
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


class ArchiveScraper(Scraper):
    """mov-cli scraper for browsing, discovering, and streaming from archive.org.

    Supports:
        - Curated interactive collection browsing (Anime, Cartoons, Sci-Fi, Classics).
        - Direct collection filtering with search keywords (e.g., 'collection:anime-series evangelion').
        - Broad Lucene full-text searches with wildcard and exclusion support.
        - Optional authenticated session integration for restricted/borrowed items.
        - In-memory result caching and optimized field querying to minimize network traffic.
    """

    # Top curated collections for interactive catalog mode
    CURATED_COLLECTIONS: List[Tuple[str, str]] = [
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

    # Video stream format preference in descending order of compatibility
    PREFERRED_FORMATS: Tuple[str, ...] = (
        "h.264",
        "MPEG4",
        "Matroska",
        "WebM",
        "Ogg Video",
        "512Kb MPEG4",
    )

    # Valid video file extension suffixes
    VIDEO_EXTENSIONS: Tuple[str, ...] = (
        ".mp4",
        ".mkv",
        ".webm",
        ".avi",
        ".ogv",
        ".mov",
        ".m4v",
    )

    def __init__(
        self,
        config: Config,
        http_client: HTTPClient,
        options: Optional[ScraperOptionsT] = None,
    ) -> None:
        """Initialize the Archive scraper and optional authentication.

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
        """Search Internet Archive for video content or launch interactive catalog.

        Args:
            query: User search string, empty string for catalog mode, or
                'collection:<name> [keywords]' for targeted collection searches.
            limit: Maximum number of search results to return (None uses scraper default).

        Yields:
            Metadata: Validated mov-cli Metadata objects for matching media items.

        Raises:
            InternalPluginError: If the Internet Archive API is unreachable or fails.
        """
        actual_limit = _validate_limit(limit, default=self.default_limit)
        clean_query = query.strip() if query else ""

        # Check in-memory search cache to reduce API round-trips
        cache_key = f"{clean_query}:{actual_limit}"
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
                    questions = [
                        inquirer.List(
                            "collection",
                            message="Archive.org Catalog - Select a Collection to browse",
                            choices=self.CURATED_COLLECTIONS,
                        )
                    ]
                    answer = inquirer.prompt(questions)
                    if not answer:
                        return  # User aborted with Escape or Ctrl+C

                    selected_collection = _sanitize_collection_id(answer.get("collection", ""))

                    # Secondary interactive keyword prompt inside chosen collection
                    search_q = [
                        inquirer.Text(
                            "term",
                            message=f"Search within '{selected_collection}' (Leave empty to list popular items)",
                        )
                    ]
                    term_ans = inquirer.prompt(search_q)
                    term = term_ans.get("term", "").strip() if term_ans else ""

                except (KeyboardInterrupt, EOFError):
                    return  # Gracefully exit on user interrupt

                if term:
                    # Escape raw quotes to prevent query breakage
                    safe_term = term.replace('"', '\\"')
                    search_query = f"({safe_term}) AND mediatype:movies AND collection:{selected_collection}"
                else:
                    search_query = f"mediatype:movies AND collection:{selected_collection}"

                results = internetarchive.search_items(
                    search_query,
                    sorts=["downloads desc"],
                    fields=["identifier", "title", "year"],
                )

            elif clean_query.startswith("collection:"):
                # ---------------------------------------------------------
                # 2. Direct Collection Filter Mode (e.g. 'collection:anime haruhi')
                # ---------------------------------------------------------
                parts = clean_query.split(" ", 1)
                raw_collection = parts[0].split("collection:", 1)[1]
                collection_id = _sanitize_collection_id(raw_collection)

                if len(parts) > 1 and parts[1].strip():
                    sub_keyword = parts[1].strip().replace('"', '\\"')
                    search_query = f"({sub_keyword}) AND mediatype:movies AND collection:{collection_id}"
                else:
                    search_query = f"mediatype:movies AND collection:{collection_id}"

                results = internetarchive.search_items(
                    search_query,
                    sorts=["downloads desc"],
                    fields=["identifier", "title", "year"],
                )

            else:
                # ---------------------------------------------------------
                # 3. Broad Full-Text Search
                # ---------------------------------------------------------
                safe_query = clean_query.replace('"', '\\"')
                search_query = f"({safe_query}) AND mediatype:movies"
                results = internetarchive.search_items(
                    search_query,
                    fields=["identifier", "title", "year"],
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

                meta = Metadata(
                    id=item_id,
                    title=title,
                    type=MetadataType.SINGLE,
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

        Internet Archive items are treated as standalone video packages;
        returns a single playable stream key.

        Args:
            metadata: Metadata object for the item.

        Returns:
            Dictionary mapping episode structure ({None: 1}).
        """
        return {None: 1}

    def scrape(self, metadata: Metadata, episode: EpisodeSelector) -> Single | Multi:
        """Resolve and extract a playable video stream URL from the item.

        Searches the files associated with the item on Archive.org, selecting
        the highest quality supported format (h.264, MPEG4, Matroska, etc.).

        Args:
            metadata: Metadata object of the selected item.
            episode: Episode selector (unused for standalone archive items).

        Returns:
            Single: mov-cli Single stream instance with verified video URL.

        Raises:
            InternalPluginError: If item files cannot be retrieved, item is unavailable,
                or no valid video stream is found.
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

        video_url: Optional[str] = None

        # 1. First pass: Search for preferred formats in defined priority order
        for preferred_fmt in self.PREFERRED_FORMATS:
            for f in files:
                fmt = getattr(f, "format", "") or ""
                if fmt.lower() == preferred_fmt.lower():
                    url = getattr(f, "url", None)
                    if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                        video_url = url
                        break
            if video_url:
                break

        # 2. Fallback pass: Check by common video file extensions
        if not video_url:
            for f in files:
                name = (getattr(f, "name", "") or "").lower()
                if any(name.endswith(ext) for ext in self.VIDEO_EXTENSIONS):
                    url = getattr(f, "url", None)
                    if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                        video_url = url
                        break

        if not video_url:
            raise InternalPluginError(
                f"No playable video format found for '{metadata.title}' (ID: {item_id})."
            )

        # Cache resolved video stream URL
        self._item_cache.set(item_id, video_url)

        return Single(
            url=video_url,
            title=metadata.title,
            year=metadata.year,
        )
