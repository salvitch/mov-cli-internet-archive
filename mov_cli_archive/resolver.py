from __future__ import annotations

"""File processing, media resolution, and network operations for mov-cli-archive.

Provides robust network exception handling with retry support, concurrent file
verification via ThreadPoolExecutor, persistent caching, and stream resolution.
"""

import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import internetarchive
from mov_cli.errors import InternalPluginError

from .cache import LocalDiskCache
from .config import (
    DEFAULT_ITEM_CACHE_TTL,
    PREFERRED_VIDEO_FORMATS,
    PREFERRED_AUDIO_FORMATS,
    VIDEO_EXTENSIONS,
    AUDIO_EXTENSIONS,
)
from .media import (
    natural_sort_key,
    is_streamable_video_file,
    is_streamable_audio_file,
    is_unsupported_archive_file,
    is_web_page_file,
)


def _safe_fetch_item(item_id: str, max_retries: int = 2) -> Any:
    """Fetch an item from Internet Archive with retry and robust error handling.

    Args:
        item_id: Identifier of the item.
        max_retries: Number of retry attempts for transient network failures.

    Returns:
        internetarchive Item object.

    Raises:
        InternalPluginError: If item is missing, dark, or network fails repeatedly.
    """
    last_err: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            item = internetarchive.get_item(item_id)
            if not item.exists:
                raise InternalPluginError(
                    f"Item '{item_id}' was not found on Internet Archive (it may have been removed or made private)."
                )

            if getattr(item, "item_metadata", {}).get("is_dark", False):
                raise InternalPluginError(
                    f"Item '{item_id}' is restricted/dark on Internet Archive."
                )

            return item
        except InternalPluginError:
            raise
        except Exception as exc:
            last_err = exc
            if attempt < max_retries:
                time.sleep(0.5 * (2 ** attempt))  # Exponential backoff
                continue

    raise InternalPluginError(
        f"Network error while connecting to Internet Archive for item '{item_id}': {last_err}"
    ) from last_err


class MediaResolver:
    """Handles file extraction, deduplication, concurrent verification, and stream resolution.

    Attributes:
        cache (LocalDiskCache): Persistent local disk cache for resolved item files.
        executor (ThreadPoolExecutor): Thread pool for concurrent operations.
    """

    def __init__(self, cache: Optional[LocalDiskCache] = None, max_workers: int = 8) -> None:
        """Initialize the media resolver.

        Args:
            cache: Optional persistent cache instance.
            max_workers: Maximum threads for concurrent file processing.
        """
        self.cache = cache or LocalDiskCache(namespace="items", default_ttl=DEFAULT_ITEM_CACHE_TTL)
        self.max_workers = max_workers

    def resolve_item_files(self, item_id: str) -> List[Dict[str, Any]]:
        """Resolve, deduplicate, and naturally sort all playable media files for an item.

        Results are cached to local disk with TTL expiration to eliminate redundant
        Archive.org network queries when hopping between episodes.

        Args:
            item_id: Internet Archive item identifier.

        Returns:
            List of dictionaries containing file metadata:
            `{'name': str, 'format': str, 'url': str, 'size': Optional[int]}`

        Raises:
            InternalPluginError: If the item lacks streamable media.
        """
        clean_id = item_id.strip()

        # Check local disk cache first
        cached = self.cache.get(clean_id)
        if cached is not None and isinstance(cached, list):
            return cached

        item = _safe_fetch_item(clean_id)

        try:
            raw_files = list(item.get_files())
        except Exception as exc:
            raise InternalPluginError(
                f"Failed to read file list for Internet Archive item '{clean_id}': {exc}"
            ) from exc

        if not raw_files:
            raise InternalPluginError(
                f"No files are associated with Internet Archive item '{clean_id}'."
            )

        # 1. Check for video files
        video_files = [f for f in raw_files if is_streamable_video_file(f.name)]

        if video_files:
            # Prefer original files to avoid duplicate .ia.mp4 derivatives
            orig_videos = [f for f in video_files if getattr(f, "source", None) == "original"]
            if orig_videos:
                candidates = orig_videos
            else:
                import re
                seen_bases = set()
                candidates = []
                for f in video_files:
                    base = re.sub(r"\.ia\.(mp4|mkv|webm)$", "", f.name.lower())
                    if base not in seen_bases:
                        seen_bases.add(base)
                        candidates.append(f)
        else:
            # 2. Check for audio files
            audio_files = [f for f in raw_files if is_streamable_audio_file(f.name)]

            if not audio_files:
                # No streamable video or audio files. Provide explicit diagnostic reasons
                archive_files = [f.name for f in raw_files if is_unsupported_archive_file(f.name)]
                html_files = [f.name for f in raw_files if is_web_page_file(f.name)]

                if archive_files:
                    raise InternalPluginError(
                        f"Item '{clean_id}' only contains compressed archive packages ({', '.join(archive_files[:2])}) "
                        "which cannot be directly streamed. Direct media files (.mp4/.mp3) are required."
                    )
                elif html_files:
                    raise InternalPluginError(
                        f"Item '{clean_id}' is a web page archive (.html) with no playable audio or video streams."
                    )
                else:
                    sample_files = [f.name for f in raw_files[:3]]
                    raise InternalPluginError(
                        f"No streamable video or audio files found in item '{clean_id}' (contains: {', '.join(sample_files)})."
                    )

            orig_audio = [f for f in audio_files if getattr(f, "source", None) == "original"]
            audio_candidates = orig_audio if orig_audio else audio_files

            # Deduplicate multiple audio formats (e.g., FLAC and MP3 for the same track)
            import re
            seen_bases = set()
            candidates = []
            for f in audio_candidates:
                base = re.sub(r"\.(mp3|flac|ogg|wav|m4a|aac|opus|wma|aiff|alac)$", "", f.name.lower())
                if base not in seen_bases:
                    seen_bases.add(base)
                    candidates.append(f)

        # Naturally sort candidates (Episode 1, Episode 2, ... Episode 10)
        sorted_files = sorted(candidates, key=lambda f: natural_sort_key(f.name))

        # Concurrently build and verify serialized file records
        records = self._build_file_records_concurrently(clean_id, sorted_files)

        # Store in persistent local disk cache
        self.cache.set(clean_id, records)
        return records

    def _build_file_records_concurrently(
        self,
        item_id: str,
        files: List[Any],
    ) -> List[Dict[str, Any]]:
        """Construct verified file metadata records using a thread pool.

        Args:
            item_id: Internet Archive item identifier.
            files: List of File objects.

        Returns:
            List of serializable file dicts.
        """
        def process_file(f: Any) -> Dict[str, Any]:
            url = getattr(f, "url", None)
            if not url:
                encoded = urllib.parse.quote(f.name)
                url = f"https://archive.org/download/{item_id}/{encoded}"

            return {
                "name": f.name,
                "format": getattr(f, "format", "") or "",
                "url": url,
                "size": getattr(f, "size", None),
            }

        if len(files) <= 1:
            return [process_file(f) for f in files]

        records: List[Tuple[int, Dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(files))) as pool:
            future_to_idx = {pool.submit(process_file, f): idx for idx, f in enumerate(files)}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    res = future.result()
                    records.append((idx, res))
                except Exception:
                    # Fallback to direct synchronous evaluation on error
                    f = files[idx]
                    records.append((idx, process_file(f)))

        records.sort(key=lambda x: x[0])
        return [r[1] for r in records]

    def resolve_stream_url(
        self,
        item_id: str,
        episode_index: int = 0,
    ) -> Tuple[str, str]:
        """Resolve direct stream URL and clean display label for a specific episode or track.

        Args:
            item_id: Internet Archive item identifier.
            episode_index: 0-indexed episode number.

        Returns:
            Tuple of (stream_url, episode_display_title_suffix).

        Raises:
            InternalPluginError: If resolution fails.
        """
        files = self.resolve_item_files(item_id)
        if not files:
            raise InternalPluginError(f"No streamable media files resolved for item '{item_id}'.")

        idx = max(0, min(episode_index, len(files) - 1))
        target = files[idx]

        url = target.get("url")
        if not url:
            encoded = urllib.parse.quote(target["name"])
            url = f"https://archive.org/download/{item_id}/{encoded}"

        if len(files) > 1:
            clean_name = target["name"].rsplit("/", 1)[-1]
            label = f" [Ep {idx + 1}/{len(files)}: {clean_name}]"
        else:
            label = ""

        return url, label
