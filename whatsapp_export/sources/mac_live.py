"""Mac live source — the Catalyst WhatsApp app's live ChatStorage.

WhatsApp ships on macOS as both a native Electron-style app (bundle
``desktop.WhatsApp``, LevelDB-backed, mostly dormant on this Mac) and
as the iOS app via Mac Catalyst (bundle ``net.whatsapp.WhatsApp``).
The Catalyst app shares its data with the iPhone via WhatsApp
Multi-Device, writing the same iOS Core Data schema we already parse.

The DB lives at::

    ~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite

It's a *live* file — the Catalyst app may be writing to it at any
moment. We never open it read-write; instead we use SQLite's
``mode=ro&immutable=1`` URI flag, which:

- skips all locking (we don't compete with the live writer);
- treats the file as effectively immutable for the duration of the
  connection (page cache acts as a stable snapshot).

This is exactly the "mirror a live DB you don't own" use case
``immutable=1`` exists for.

Note on media: ZWAMEDIAITEM in the live DB has hundreds of thousands
of rows but only ~1% have ``ZMEDIALOCALPATH`` populated — the rest are
thumbnails / cloud-fetch metadata. So ``media_root()`` exists and the
share group container does have a ``Media/`` subtree, but in practice
most attachment bytes resolve via the iPhone backup. The reconciler
handles this by preferring the source whose attachment actually exists
on disk.
"""
from __future__ import annotations

from pathlib import Path

from .base import Source


GROUP_CONTAINER = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.net.whatsapp.WhatsApp.shared"
)


class MacLiveSource(Source):
    name = "mac_live"

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else GROUP_CONTAINER

    def db_path(self) -> Path:
        return self._root / "ChatStorage.sqlite"

    def media_root(self) -> Path | None:
        # The Catalyst app stores media under the share group container
        # at ``Media/...`` (same layout the decrypter lays out the
        # iPhone backup at, conveniently). Most files are absent — that
        # is what the reconciler's attachment-provenance rule handles.
        if not self._root.exists():
            return None
        return self._root

    def is_available(self) -> bool:
        return self.db_path().exists()
