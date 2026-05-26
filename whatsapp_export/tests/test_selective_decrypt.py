"""
Tests for selective_decrypt.py.

We don't exercise the real iphone_backup_decrypt library here — that would
need a real encrypted backup. Instead we drive selective_decrypt through a
FakeBackup that records the calls it received, which is exactly what we want
to assert against (which files were extracted, which were skipped, which
filter_callback returned what).
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import selective_decrypt as sd  # noqa: E402


IOS_EPOCH_OFFSET = 978307200


def _build_chatstorage(path: Path, *, with_jid: str, media_relpaths: list[str],
                      other_chat_relpaths: list[str] | None = None) -> None:
    """
    Build a minimal ChatStorage.sqlite mirroring the columns selective_decrypt
    reads. Includes one target chat and (optionally) one decoy chat so we can
    assert the filter is actually narrowing.
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ZWACHATSESSION (
            Z_PK INTEGER PRIMARY KEY,
            ZCONTACTJID TEXT,
            ZPARTNERNAME TEXT,
            ZLASTMESSAGEDATE REAL
        );
        CREATE TABLE ZWAMESSAGE (
            Z_PK INTEGER PRIMARY KEY,
            ZCHATSESSION INTEGER,
            ZMESSAGEDATE REAL
        );
        CREATE TABLE ZWAMEDIAITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZMESSAGE INTEGER,
            ZMEDIALOCALPATH TEXT
        );
    """)
    conn.execute(
        "INSERT INTO ZWACHATSESSION VALUES (1, ?, 'Target', 0)",
        (with_jid,),
    )
    msg_pk = 100
    mi_pk = 1000
    for rel in media_relpaths:
        conn.execute("INSERT INTO ZWAMESSAGE VALUES (?, 1, 0)", (msg_pk,))
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (?, ?, ?)",
            (mi_pk, msg_pk, rel),
        )
        msg_pk += 1
        mi_pk += 1
    if other_chat_relpaths:
        conn.execute(
            "INSERT INTO ZWACHATSESSION VALUES (2, 'decoy@s.whatsapp.net', 'Decoy', 0)"
        )
        for rel in other_chat_relpaths:
            conn.execute("INSERT INTO ZWAMESSAGE VALUES (?, 2, 0)", (msg_pk,))
            conn.execute(
                "INSERT INTO ZWAMEDIAITEM VALUES (?, ?, ?)",
                (mi_pk, msg_pk, rel),
            )
            msg_pk += 1
            mi_pk += 1
    # Add a no-media message in target chat so the JOIN drops it.
    conn.execute("INSERT INTO ZWAMESSAGE VALUES (?, 1, 0)", (msg_pk,))
    conn.commit()
    conn.close()


class FakeBackup:
    """
    Stand-in for iphone_backup_decrypt.EncryptedBackup. Records every
    extract_file / extract_files call and pre-populates ChatStorage.sqlite
    when the canonical relative_path is requested.
    """

    def __init__(self, *, chatstorage_seed: Path | None = None,
                 domain_candidates: list[dict] | None = None,
                 already_on_disk: set[str] | None = None):
        self.chatstorage_seed = chatstorage_seed
        # Each candidate dict: {relative_path, domain, file_id, written}
        self.domain_candidates = domain_candidates or []
        self.already_on_disk = already_on_disk or set()
        self.extract_file_calls: list[dict] = []
        self.extract_files_calls: list[dict] = []

    def extract_file(self, *, relative_path, output_filename, domain_like=None):
        self.extract_file_calls.append({
            "relative_path": relative_path,
            "output_filename": output_filename,
            "domain_like": domain_like,
        })
        # If asked for ChatStorage and we have a seed, copy it.
        if "ChatStorage" in str(relative_path) and self.chatstorage_seed:
            Path(output_filename).write_bytes(self.chatstorage_seed.read_bytes())

    def extract_files(self, *, output_folder, domain_like=None,
                      relative_paths_like=None, preserve_folders=False,
                      domain_subfolders=False, incremental=False,
                      filter_callback=None):
        self.extract_files_calls.append({
            "output_folder": output_folder,
            "domain_like": domain_like,
            "filter_callback": bool(filter_callback),
            "incremental": incremental,
        })
        out = Path(output_folder)
        out.mkdir(parents=True, exist_ok=True)
        written = 0
        for i, cand in enumerate(self.domain_candidates):
            keep = True
            if filter_callback is not None:
                keep = filter_callback(
                    n=i, total_files=len(self.domain_candidates),
                    relative_path=cand["relative_path"],
                    domain=cand["domain"],
                    file_id=cand["file_id"],
                )
            if not keep:
                continue
            if incremental and cand["relative_path"] in self.already_on_disk:
                continue
            # Simulate writing the file.
            dest = out / Path(cand["relative_path"]).name
            dest.write_bytes(b"fake-bytes")
            written += 1
        return written


# ─── Tests ─────────────────────────────────────────────────────────────────


class TestListChatMediaRelpaths:
    def test_returns_relpaths_for_chat(self, tmp_path):
        db = tmp_path / "ChatStorage.sqlite"
        _build_chatstorage(
            db, with_jid="alice@s.whatsapp.net",
            media_relpaths=["Media/a.jpg", "Media/b.opus"],
        )
        rels = sd.list_chat_media_relpaths(db, "alice@s.whatsapp.net")
        assert sorted(rels) == ["Media/a.jpg", "Media/b.opus"]

    def test_excludes_other_chats(self, tmp_path):
        db = tmp_path / "ChatStorage.sqlite"
        _build_chatstorage(
            db, with_jid="alice@s.whatsapp.net",
            media_relpaths=["Media/alice.jpg"],
            other_chat_relpaths=["Media/decoy.jpg"],
        )
        rels = sd.list_chat_media_relpaths(db, "alice@s.whatsapp.net")
        assert rels == ["Media/alice.jpg"]

    def test_unknown_jid_raises_with_hint(self, tmp_path):
        db = tmp_path / "ChatStorage.sqlite"
        _build_chatstorage(
            db, with_jid="alice@s.whatsapp.net",
            media_relpaths=[],
        )
        with pytest.raises(ValueError) as exc:
            sd.list_chat_media_relpaths(db, "ghost@s.whatsapp.net")
        # The error should help the user — at minimum it should mention the
        # known JID(s) so they can copy-paste the right one.
        assert "alice@s.whatsapp.net" in str(exc.value)

    def test_chat_without_media_returns_empty_list(self, tmp_path):
        db = tmp_path / "ChatStorage.sqlite"
        _build_chatstorage(
            db, with_jid="texty@s.whatsapp.net", media_relpaths=[],
        )
        assert sd.list_chat_media_relpaths(db, "texty@s.whatsapp.net") == []


class TestDecryptWhatsappFullDomain:
    """3A path: chat_jid=None decrypts the whole domain."""

    def test_calls_extract_files_with_domain(self, tmp_path):
        chatstorage_seed = tmp_path / "src.sqlite"
        _build_chatstorage(chatstorage_seed,
                           with_jid="alice@s.whatsapp.net",
                           media_relpaths=["Media/a.jpg"])

        fake = FakeBackup(
            chatstorage_seed=chatstorage_seed,
            domain_candidates=[
                {"relative_path": "Media/a.jpg", "domain": "x", "file_id": "f1"},
                {"relative_path": "Media/b.jpg", "domain": "x", "file_id": "f2"},
            ],
        )
        stats = sd.decrypt_whatsapp(
            backup_dir=tmp_path / "backup",
            password="x",
            out_dir=tmp_path / "extracted",
            chat_jid=None,
            eb_factory=lambda *_a, **_k: fake,
        )
        # Both candidates extracted (no filter applied for 3A path).
        assert stats.media_decrypted == 2
        # And the call used the shared-domain filter.
        assert fake.extract_files_calls[0]["domain_like"] == \
               sd.WHATSAPP_SHARED_DOMAIN
        # filter_callback should NOT be supplied for the 3A path — that's
        # what makes it a "decrypt everything" operation.
        assert fake.extract_files_calls[0]["filter_callback"] is False


class TestDecryptWhatsappChatScoped:
    """3B path: chat_jid set → only that chat's media."""

    def test_decrypts_only_relpaths_belonging_to_chat(self, tmp_path):
        chatstorage_seed = tmp_path / "src.sqlite"
        _build_chatstorage(
            chatstorage_seed,
            with_jid="alice@s.whatsapp.net",
            media_relpaths=["Media/a.jpg", "Media/b.opus"],
            other_chat_relpaths=["Media/decoy.jpg"],
        )

        fake = FakeBackup(
            chatstorage_seed=chatstorage_seed,
            domain_candidates=[
                {"relative_path": "Media/a.jpg", "domain": "x", "file_id": "f1"},
                {"relative_path": "Media/b.opus", "domain": "x", "file_id": "f2"},
                {"relative_path": "Media/decoy.jpg", "domain": "x", "file_id": "f3"},
                {"relative_path": "Media/unrelated.pdf", "domain": "x", "file_id": "f4"},
            ],
        )

        stats = sd.decrypt_whatsapp(
            backup_dir=tmp_path / "backup",
            password="x",
            out_dir=tmp_path / "extracted",
            chat_jid="alice@s.whatsapp.net",
            eb_factory=lambda *_a, **_k: fake,
        )

        assert stats.chat_jid == "alice@s.whatsapp.net"
        assert stats.media_total_candidates == 2
        assert stats.media_decrypted == 2
        # The decoy + unrelated files must NOT have been written.
        out_media = tmp_path / "extracted" / "media"
        assert (out_media / "a.jpg").exists()
        assert (out_media / "b.opus").exists()
        assert not (out_media / "decoy.jpg").exists()
        assert not (out_media / "unrelated.pdf").exists()

    def test_incremental_skips_already_decrypted_files(self, tmp_path):
        chatstorage_seed = tmp_path / "src.sqlite"
        _build_chatstorage(
            chatstorage_seed,
            with_jid="alice@s.whatsapp.net",
            media_relpaths=["Media/a.jpg", "Media/b.opus"],
        )

        fake = FakeBackup(
            chatstorage_seed=chatstorage_seed,
            domain_candidates=[
                {"relative_path": "Media/a.jpg", "domain": "x", "file_id": "f1"},
                {"relative_path": "Media/b.opus", "domain": "x", "file_id": "f2"},
            ],
            already_on_disk={"Media/a.jpg"},
        )

        stats = sd.decrypt_whatsapp(
            backup_dir=tmp_path / "backup",
            password="x",
            out_dir=tmp_path / "extracted",
            chat_jid="alice@s.whatsapp.net",
            eb_factory=lambda *_a, **_k: fake,
            incremental=True,
        )

        # One was cached (a.jpg), one was decrypted (b.opus).
        assert stats.media_decrypted == 1
        assert stats.media_skipped_cached == 1
        assert stats.media_total_candidates == 2

    def test_chat_with_no_media_returns_empty_stats(self, tmp_path):
        chatstorage_seed = tmp_path / "src.sqlite"
        _build_chatstorage(
            chatstorage_seed,
            with_jid="texty@s.whatsapp.net",
            media_relpaths=[],
        )
        fake = FakeBackup(chatstorage_seed=chatstorage_seed, domain_candidates=[])

        stats = sd.decrypt_whatsapp(
            backup_dir=tmp_path / "backup",
            password="x",
            out_dir=tmp_path / "extracted",
            chat_jid="texty@s.whatsapp.net",
            eb_factory=lambda *_a, **_k: fake,
        )
        assert stats.chatstorage_extracted is True
        assert stats.media_decrypted == 0
        assert stats.media_total_candidates == 0
        # No extract_files call — we short-circuit when there's no media.
        assert fake.extract_files_calls == []

    def test_relpaths_missing_from_manifest_surface_as_warning(self, tmp_path):
        """
        ZMEDIALOCALPATH can list files that the encrypted backup doesn't
        actually contain (eg orphaned references). Those should appear as a
        non-fatal warning, not crash the run.
        """
        chatstorage_seed = tmp_path / "src.sqlite"
        _build_chatstorage(
            chatstorage_seed,
            with_jid="alice@s.whatsapp.net",
            media_relpaths=["Media/a.jpg", "Media/orphan.bin"],
        )

        fake = FakeBackup(
            chatstorage_seed=chatstorage_seed,
            domain_candidates=[
                # Only a.jpg is present in the (faked) Manifest — orphan.bin
                # has no entry, so the filter never gets to see it.
                {"relative_path": "Media/a.jpg", "domain": "x", "file_id": "f1"},
            ],
        )

        stats = sd.decrypt_whatsapp(
            backup_dir=tmp_path / "backup",
            password="x",
            out_dir=tmp_path / "extracted",
            chat_jid="alice@s.whatsapp.net",
            eb_factory=lambda *_a, **_k: fake,
        )
        assert stats.media_decrypted == 1
        # 1 expected, only 1 found → 1 missing recorded
        assert any("orphan" in e for e in stats.errors)
