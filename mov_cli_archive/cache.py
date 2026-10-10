from __future__ import annotations

"""Persistent local disk cache with TTL expiration for mov-cli-archive.

Provides cross-process local disk caching to prevent redundant Archive.org network
requests and bandwidth waste across separate mov-cli invocations.
"""

import json
import os
import re
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Tuple

from .config import DEFAULT_CACHE_DIR, DEFAULT_SEARCH_CACHE_TTL, DEFAULT_ITEM_CACHE_TTL


def _sanitize_key(key: str) -> str:
    """Convert an arbitrary string into a filesystem-safe filename key."""
    # Replace non-alphanumeric characters with underscores
    safe = re.sub(r"[^\w\-\.]", "_", key)
    # Truncate if excessively long
    if len(safe) > 128:
        import hashlib
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        safe = f"{safe[:100]}_{digest}"
    return safe


class LocalDiskCache:
    """Thread-safe and process-safe persistent disk cache with TTL expiration.

    Attributes:
        cache_dir (Path): Base directory on disk for cached JSON blobs.
        namespace (str): Sub-namespace folder (e.g. 'searches', 'items').
        default_ttl (int): Default duration in seconds before items expire.
    """

    def __init__(
        self,
        namespace: str = "default",
        cache_dir: Optional[Path] = None,
        default_ttl: int = 3600,
        enabled: bool = True,
    ) -> None:
        """Initialize the disk cache.

        Args:
            namespace: Sub-namespace directory name.
            cache_dir: Base directory (defaults to XDG cache ~/.cache/mov-cli-archive).
            default_ttl: Default entry expiration in seconds.
            enabled: Boolean flag to enable or disable caching.
        """
        self.namespace = namespace
        self.base_dir = cache_dir or DEFAULT_CACHE_DIR
        self.cache_dir = self.base_dir / namespace
        self.default_ttl = default_ttl
        self.enabled = enabled
        self._lock = Lock()
        self._memory_cache: Dict[str, Tuple[float, Any]] = {}

        if self.enabled:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def get(self, key: str) -> Optional[Any]:
        """Retrieve value for key from memory or disk if unexpired.

        Args:
            key: Lookup key.

        Returns:
            Cached data or None if missing or expired.
        """
        if not self.enabled:
            return None

        safe_key = _sanitize_key(key)
        now = time.time()

        with self._lock:
            # 1. Fast path: In-memory cache
            if safe_key in self._memory_cache:
                expiry, val = self._memory_cache[safe_key]
                if now < expiry:
                    return val
                del self._memory_cache[safe_key]

            # 2. Disk path
            cache_file = self.cache_dir / f"{safe_key}.json"
            if not cache_file.is_file():
                return None

            try:
                with cache_file.open("r", encoding="utf-8") as f:
                    payload = json.load(f)

                expires_at = payload.get("expires_at", 0)
                if now >= expires_at:
                    try:
                        cache_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return None

                data = payload.get("data")
                self._memory_cache[safe_key] = (expires_at, data)
                return data
            except (json.JSONDecodeError, OSError):
                try:
                    cache_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Store value in memory and on disk with TTL expiration.

        Args:
            key: Lookup key.
            value: JSON-serializable data to cache.
            ttl: Time-to-live in seconds (defaults to self.default_ttl).
        """
        if not self.enabled:
            return

        safe_key = _sanitize_key(key)
        item_ttl = ttl if ttl is not None else self.default_ttl
        now = time.time()
        expires_at = now + item_ttl

        with self._lock:
            self._memory_cache[safe_key] = (expires_at, value)

            payload = {
                "created_at": now,
                "expires_at": expires_at,
                "data": value,
            }

            cache_file = self.cache_dir / f"{safe_key}.json"
            temp_file = self.cache_dir / f"{safe_key}.tmp.{os.getpid()}"

            try:
                with temp_file.open("w", encoding="utf-8") as f:
                    json.dump(payload, f)
                temp_file.replace(cache_file)
            except (OSError, TypeError):
                try:
                    if temp_file.exists():
                        temp_file.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear_expired(self) -> int:
        """Clean up expired cache files from disk.

        Returns:
            Number of expired cache files removed.
        """
        if not self.enabled or not self.cache_dir.is_dir():
            return 0

        removed = 0
        now = time.time()

        with self._lock:
            try:
                for entry in self.cache_dir.glob("*.json"):
                    try:
                        with entry.open("r", encoding="utf-8") as f:
                            payload = json.load(f)
                        if now >= payload.get("expires_at", 0):
                            entry.unlink(missing_ok=True)
                            removed += 1
                    except (json.JSONDecodeError, OSError):
                        entry.unlink(missing_ok=True)
                        removed += 1
            except OSError:
                pass

        return removed
