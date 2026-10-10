from __future__ import annotations

"""ArchiveScraper implementation for mov-cli v4.

Orchestrates search queries, interactive catalog browsing, multi-episode playlist
navigation, and media streaming from the Internet Archive (archive.org).
"""

from typing import TYPE_CHECKING, Optional, Dict, Generator, Any, List

if TYPE_CHECKING:
    from mov_cli import Config
    from mov_cli.http_client import HTTPClient
    from mov_cli.scraper import ScraperOptionsT

from mov_cli import Single, Multi, Metadata, MetadataType
from mov_cli.scraper import Scraper
from mov_cli.utils import EpisodeSelector
from mov_cli.errors import InternalPluginError
import internetarchive

from .config import (
    CURATED_VIDEO_COLLECTIONS,
    CURATED_AUDIO_COLLECTIONS,
    CURATED_COMBINED_COLLECTIONS,
    DEFAULT_SEARCH_CACHE_TTL,
    DEFAULT_ITEM_CACHE_TTL,
)
from .cache import LocalDiskCache
from .media import (
    validate_limit,
    clean_title,
    clean_year,
    build_image_url,
    sanitize_collection_id,
    parse_media_mode_and_query,
    build_mediatype_filter,
    has_potential_media,
)
from .resolver import MediaResolver

__all__ = ("ArchiveScraper", )


class ArchiveScraper(Scraper):
    """mov-cli scraper for browsing, discovering, and streaming from archive.org.

    Features:
        - Multi-format streaming: Video (Movies, TV, Anime Packs) and Audio (Music, OTR, Podcasts).
        - Multi-episode series & playlist navigation: Iterate through batch packs (e.g. animepacks)
          or audio albums with automatic episode selection and sequential playback.
        - Targeted media selection: Search video, audio, or both simultaneously via CLI flags or config.
        - Curated interactive collection browsing across Video and Audio libraries.
        - Direct collection filtering with search keywords (e.g., 'collection:animepacks naruto').
        - Broad Lucene full-text searches with wildcard and exclusion support.
        - Explicit media filtering: Automatically discards non-streamable .rar/.zip packages and HTML files.
        - Persistent local disk cache: Avoids redundant bandwidth usage and repeated network queries.
        - Concurrent stream resolution: Uses thread-pool concurrency for fast multi-episode indexing.
        - Optional authenticated session integration for restricted/borrowed items.
        - Terminal image previews via fzf and chafa integration.
    """

    def __init__(
        self,
        config: Config,
        http_client: HTTPClient,
        options: Optional[ScraperOptionsT] = None,
    ) -> None:
        """Initialize the Archive scraper, options, caches, and authentication.

        Args:
            config: mov-cli configuration instance.
            http_client: mov-cli HTTP client instance.
            options: Scraper-specific options loaded from config.toml.

        Raises:
            InternalPluginError: If user credentials fail during configuration.
        """
        super().__init__(config, http_client, options)

        opts = self.options or {}

        # Cache configuration
        cache_enabled = bool(opts.get("cache_enabled", True))
        search_ttl = int(opts.get("search_cache_ttl", DEFAULT_SEARCH_CACHE_TTL))
        item_ttl = int(opts.get("item_cache_ttl", DEFAULT_ITEM_CACHE_TTL))

        self._search_cache = LocalDiskCache(
            namespace="searches",
            default_ttl=search_ttl,
            enabled=cache_enabled,
        )
        self._item_cache = LocalDiskCache(
            namespace="items",
            default_ttl=item_ttl,
            enabled=cache_enabled,
        )

        # File and media resolver with thread concurrency
        max_workers = int(opts.get("max_workers", 8))
        self.resolver = MediaResolver(cache=self._item_cache, max_workers=max_workers)

        # User options
        self.username: Optional[str] = opts.get("username")
        self.password: Optional[str] = opts.get("password")
        self.default_limit: Optional[int] = validate_limit(opts.get("limit", 100), default=100)
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
                targeted collection filter (e.g. 'collection:animepacks naruto').
            limit: Maximum number of search results to return (None uses scraper default).

        Yields:
            Metadata: Validated mov-cli Metadata objects for matching media items.

        Raises:
            InternalPluginError: If the Internet Archive API is unreachable or fails.
        """
        actual_limit = validate_limit(limit, default=self.default_limit)
        clean_query = query.strip() if query else ""

        # Parse media mode prefix if present (e.g. 'audio:', 'video:', 'both:', 'type:audio')
        media_mode, clean_query = parse_media_mode_and_query(clean_query, self.default_media_type)

        # Check persistent local disk cache
        cache_key = f"{media_mode}_{clean_query}_{actual_limit}"
        cached_data = self._search_cache.get(cache_key)
        if cached_data is not None and isinstance(cached_data, list):
            for item in cached_data:
                yield Metadata(
                    id=item["id"],
                    title=item["title"],
                    type=MetadataType.MULTI if item.get("is_multi", True) else MetadataType.SINGLE,
                    image_url=item.get("image_url"),
                    year=item.get("year"),
                )
            return

        collected_items: List[Dict[str, Any]] = []

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
                        col_choices = CURATED_AUDIO_COLLECTIONS
                    elif media_mode == "both":
                        col_choices = CURATED_COMBINED_COLLECTIONS
                    else:
                        col_choices = CURATED_VIDEO_COLLECTIONS

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
                        selected_collection = sanitize_collection_id(raw_col)

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

                mediatype_filter = build_mediatype_filter(media_mode)

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
                collection_id = sanitize_collection_id(raw_collection)
                mediatype_filter = build_mediatype_filter(media_mode)

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
                mediatype_filter = build_mediatype_filter(media_mode)
                search_query = f"({safe_query}) AND {mediatype_filter}"
                results = internetarchive.search_items(
                    search_query,
                    fields=["identifier", "title", "year", "mediatype", "collection", "format"],
                )

            count = 0
            for item in results:
                if actual_limit is not None and count >= actual_limit:
                    break

                # Explicitly filter out items that only contain compressed archives (.rar, .zip) or web pages
                raw_formats = item.get("format")
                if raw_formats and not has_potential_media(raw_formats):
                    continue

                raw_id = item.get("identifier")
                if not raw_id or not isinstance(raw_id, str):
                    continue

                item_id = raw_id.strip()
                title = clean_title(item.get("title"), fallback_id=item_id)
                year = clean_year(item.get("year"))
                image_url = build_image_url(item_id) if self.fetch_images else None

                item_mediatype = (item.get("mediatype") or "").lower()

                # In 'both' mode, prepend media badge to title for immediate clarity
                if media_mode in ("both", "all"):
                    if item_mediatype in ("audio", "etree"):
                        title = f"[Audio] {title}"
                    else:
                        title = f"[Video] {title}"
                elif media_mode == "audio":
                    title = f"[Audio] {title}"

                # Mark as MULTI to allow mov-cli to dynamically handle playlists or single movies
                meta = Metadata(
                    id=item_id,
                    title=title,
                    type=MetadataType.MULTI,
                    image_url=image_url,
                    year=year,
                )

                collected_items.append({
                    "id": item_id,
                    "title": title,
                    "is_multi": True,
                    "image_url": image_url,
                    "year": year,
                })

                yield meta
                count += 1

            # Save search results to persistent local disk cache
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
            files = self.resolver.resolve_item_files(str(metadata.id).strip())
            total = len(files)
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
        ep_index = max(0, (episode.episode - 1)) if episode and hasattr(episode, "episode") and episode.episode else 0

        stream_url, label = self.resolver.resolve_stream_url(item_id, ep_index)

        return Single(
            url=stream_url,
            title=f"{metadata.title}{label}",
            year=metadata.year,
        )
