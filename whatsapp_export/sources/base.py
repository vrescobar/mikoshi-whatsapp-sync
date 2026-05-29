"""Source abstraction — the minimum surface every WhatsApp data source
must implement so extraction and reconciliation can stay source-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


# Per-process memoization of expensive COUNT queries, keyed by
# (db_path, mtime_ns). Cleared implicitly when the DB is touched
# (different mtime → cache miss).
_COUNT_CACHE: dict[tuple[str, int], tuple[int, int]] = {}


class SourceNotAvailable(RuntimeError):
    """Raised when a source's underlying DB / files aren't reachable on
    this Mac (e.g. WhatsApp Desktop is uninstalled, or no iPhone backup
    has been produced yet)."""


@dataclass(frozen=True)
class SourceSnapshot:
    """Read-only snapshot of a source's current state at probe time.

    The ``mtime_iso`` is what the user sees in the TUI's Sources rows —
    "fresh as of HH:MM". ``message_count`` is the row count in
    ZWAMESSAGE; ``media_with_local_path`` is the count of media items
    whose bytes actually live on disk (the rest are cloud-fetch
    metadata).
    """
    name: str
    db_path: Path
    mtime_iso: str
    message_count: int
    media_with_local_path: int


class Source(ABC):
    """Read-only handle to a ChatStorage.sqlite plus the directory
    layout that holds its media."""

    name: str

    @abstractmethod
    def is_available(self) -> bool:
        """Cheap check: does the source's DB exist + can we read it?
        Must not raise — used in the TUI header refresh path."""

    @abstractmethod
    def db_path(self) -> Path:
        """Absolute path to the ChatStorage.sqlite for this source.
        Implementations that need to copy from a live location should
        do so eagerly and return the copy path."""

    @abstractmethod
    def media_root(self) -> Path | None:
        """Directory containing media files referenced by ZMEDIALOCALPATH.
        Returns ``None`` when the source has no local media bytes (e.g.
        the Mac live DB is mostly thumbnails/cloud-fetch metadata)."""

    def snapshot(self) -> SourceSnapshot:
        """Produce a SourceSnapshot. Default implementation reads the
        DB stat + a couple of cheap COUNT queries; sources can override
        when they have a cheaper probe.

        COUNT(*) over ZWAMESSAGE on a million-row DB is the long pole
        for the TUI header — the values can't change without the file's
        mtime changing, so we memoize keyed by (path, mtime_ns) and
        only re-query when the DB was actually touched.
        """
        import sqlite3
        from datetime import datetime, timezone
        path = self.db_path()
        if not path.exists():
            raise SourceNotAvailable(f"{self.name}: {path} not found")
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        cache_key = (str(path), stat.st_mtime_ns)
        cached = _COUNT_CACHE.get(cache_key)
        if cached is not None:
            msg_count, media_count = cached
        else:
            with sqlite3.connect(self._readonly_uri(path), uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                msg_count = cur.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0]
                try:
                    media_count = cur.execute(
                        "SELECT COUNT(*) FROM ZWAMEDIAITEM WHERE ZMEDIALOCALPATH IS NOT NULL"
                    ).fetchone()[0]
                except sqlite3.OperationalError:
                    # Some test fixtures don't carry ZWAMEDIAITEM at all.
                    media_count = 0
            _COUNT_CACHE[cache_key] = (int(msg_count or 0), int(media_count or 0))
            # Drop stale entries for the same path on a different mtime —
            # keeps the cache from growing unbounded across edits.
            for k in list(_COUNT_CACHE.keys()):
                if k[0] == str(path) and k != cache_key:
                    _COUNT_CACHE.pop(k, None)
        return SourceSnapshot(
            name=self.name,
            db_path=path,
            mtime_iso=mtime.isoformat(timespec="seconds"),
            message_count=int(msg_count or 0),
            media_with_local_path=int(media_count or 0),
        )

    def _readonly_uri(self, path: Path) -> str:
        """SQLite read-only URI. ``immutable=1`` skips locking — safe
        when reading a DB that another process is actively writing to,
        and gives us a stable snapshot via the OS page cache."""
        return f"file:{path}?mode=ro&immutable=1"
