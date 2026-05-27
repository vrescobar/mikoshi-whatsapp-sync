"""iPhone backup source — the existing flow factored into a Source.

Reads MIKOSHI_BACKUP_DIR/extracted/ChatStorage.sqlite (the decrypt
phase's output). Media lives under ``extracted/`` and the existing
``build_attachments_index`` walks the tree to resolve ZMEDIALOCALPATH
to a real file.
"""
from __future__ import annotations

import os
from pathlib import Path

from .base import Source


class IphoneBackupSource(Source):
    name = "iphone_backup"

    def __init__(self, backup_dir: Path | None = None) -> None:
        self._backup_dir = backup_dir

    def _root(self) -> Path | None:
        if self._backup_dir is not None:
            return self._backup_dir
        env = os.environ.get("MIKOSHI_BACKUP_DIR")
        return Path(env) if env else None

    def db_path(self) -> Path:
        root = self._root()
        if root is None:
            # extract_messages.py expects an absolute path; surface a
            # placeholder so is_available() can answer False without
            # raising. Callers must check is_available() first.
            return Path("/nonexistent/MIKOSHI_BACKUP_DIR_unset")
        return root / "extracted" / "ChatStorage.sqlite"

    def media_root(self) -> Path | None:
        root = self._root()
        if root is None:
            return None
        return root / "extracted"

    def is_available(self) -> bool:
        return self.db_path().exists()
