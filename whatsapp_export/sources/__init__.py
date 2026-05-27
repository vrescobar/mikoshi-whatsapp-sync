"""Sources of WhatsApp message data on this Mac.

Two sources are first-class today:

- ``iphone_backup`` — the decrypted ChatStorage.sqlite produced by the
  existing backup-and-decrypt pipeline. Slow to refresh (requires the
  iPhone + a full backup + decrypt) but covers the full message history
  the iPhone has ever held, including media files on disk.
- ``mac_live`` — the live ChatStorage.sqlite that WhatsApp Multi-Device
  keeps under ``~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/``.
  Always fresh (the Catalyst app writes to it continuously) but its
  history horizon is when the Mac was linked, and most media exists only
  as thumbnails / cloud-fetch metadata.

Both sources speak the same iOS Core Data schema (ZWAMESSAGE/
ZWACHATSESSION/ZWAMEDIAITEM), so ``extract_messages`` works against
either with no code change — the source object just hands over its
``db_path()`` and ``media_root()``.

Cross-source dedup is the reconciler's job; see ``reconciler.py``.
"""
from .base import Source, SourceNotAvailable
from .iphone_backup import IphoneBackupSource
from .mac_live import MacLiveSource


def available_sources() -> list[Source]:
    """Return the subset of registered sources that can read on this Mac."""
    candidates: list[Source] = [IphoneBackupSource(), MacLiveSource()]
    return [s for s in candidates if s.is_available()]


def get_source(name: str) -> Source:
    """Look up a source by name; raises ``KeyError`` if unknown."""
    by_name = {s.name: s for s in [IphoneBackupSource(), MacLiveSource()]}
    return by_name[name]


__all__ = [
    "Source",
    "SourceNotAvailable",
    "IphoneBackupSource",
    "MacLiveSource",
    "available_sources",
    "get_source",
]
