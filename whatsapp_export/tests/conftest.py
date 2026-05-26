"""Shared fixtures: synthetic ChatStorage.sqlite + decrypted-root with media."""

import sqlite3
import sys
from pathlib import Path

import pytest

# Make extract_messages importable
sys.path.insert(0, str(Path(__file__).parent.parent))


def persist_cursors_like_push(state_file, chats_state):
    """
    Mimic what push_via_api.py does on a successful commit: stamp the
    cursor cache with the server-confirmed values.

    Since extract_messages no longer writes the cache by default (that
    was the silent-drift bug), tests that exercise multi-run cursor
    semantics need to simulate the post-push step explicitly. This
    helper is the post-redesign equivalent of the old
    `save_sync_state(state_file, sync_state)` call.
    """
    import pipeline_state
    committed = {
        jid: {"ts": ts, "external_id": None}
        for jid, ts in (chats_state or {}).items()
        if ts
    }
    pipeline_state.update_cache_from_commit(
        state_file=state_file,
        server_url="http://test",
        push_id="test-push",
        committed_cursors=committed,
    )


IOS_EPOCH_OFFSET = 978307200  # 2001-01-01 UTC


def _ios_ts(year, month, day, hour=12, minute=0):
    """Convert civil date to iOS Core Data timestamp."""
    from datetime import datetime, timezone

    dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    return dt.timestamp() - IOS_EPOCH_OFFSET


@pytest.fixture
def synthetic_db(tmp_path):
    """
    Build a minimal ChatStorage.sqlite with:
      - chat 1: 1-1 with Alice, 3 text messages
      - chat 2: 1-1 with Bob, 2 messages, one with image attachment
      - chat 3: group with 3 members, 2 text + 1 system (group creation) + 1 video msg
    """
    db_path = tmp_path / "ChatStorage.sqlite"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE ZWACHATSESSION (
            Z_PK INTEGER PRIMARY KEY,
            ZCONTACTJID TEXT,
            ZPARTNERNAME TEXT,
            ZLASTMESSAGEDATE REAL
        );
        CREATE TABLE ZWAMESSAGE (
            Z_PK INTEGER PRIMARY KEY,
            ZCHATSESSION INTEGER,
            ZTEXT TEXT,
            ZMESSAGEDATE REAL,
            ZFROMJID TEXT,
            ZTOJID TEXT,
            ZISFROMME INTEGER,
            ZMESSAGETYPE INTEGER,
            ZPUSHNAME TEXT
        );
        CREATE TABLE ZWAMEDIAITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZMESSAGE INTEGER,
            ZMEDIALOCALPATH TEXT,
            ZVCARDSTRING TEXT,
            ZTITLE TEXT,
            ZMEDIASIZE INTEGER
        );
        CREATE TABLE ZWAGROUPMEMBER (
            Z_PK INTEGER PRIMARY KEY,
            ZCHATSESSION INTEGER,
            ZMEMBERJID TEXT,
            ZCONTACTNAME TEXT
        );
        """
    )

    # Chats
    cur.execute(
        "INSERT INTO ZWACHATSESSION VALUES (1, 'alice@s.whatsapp.net', 'Alice', ?)",
        (_ios_ts(2026, 5, 25),),
    )
    cur.execute(
        "INSERT INTO ZWACHATSESSION VALUES (2, 'bob@s.whatsapp.net', 'Bob', ?)",
        (_ios_ts(2026, 5, 25),),
    )
    cur.execute(
        "INSERT INTO ZWACHATSESSION VALUES (3, '12345@g.us', 'Family Group', ?)",
        (_ios_ts(2026, 5, 25),),
    )

    # Alice messages (chat 1)
    cur.execute(
        "INSERT INTO ZWAMESSAGE VALUES (101, 1, 'Hola', ?, 'alice@s.whatsapp.net', 'me@s.whatsapp.net', 0, 0, 'Alice')",
        (_ios_ts(2026, 5, 20),),
    )
    cur.execute(
        "INSERT INTO ZWAMESSAGE VALUES (102, 1, 'Que tal', ?, 'alice@s.whatsapp.net', 'me@s.whatsapp.net', 0, 0, 'Alice')",
        (_ios_ts(2026, 5, 22),),
    )
    cur.execute(
        "INSERT INTO ZWAMESSAGE VALUES (103, 1, 'Bien y tu', ?, 'me@s.whatsapp.net', 'alice@s.whatsapp.net', 1, 0, NULL)",
        (_ios_ts(2026, 5, 24),),
    )

    # Bob messages (chat 2) — message 202 has an image attachment
    cur.execute(
        "INSERT INTO ZWAMESSAGE VALUES (201, 2, 'mira esto', ?, 'bob@s.whatsapp.net', 'me@s.whatsapp.net', 0, 0, 'Bob')",
        (_ios_ts(2026, 5, 21),),
    )
    cur.execute(
        "INSERT INTO ZWAMESSAGE VALUES (202, 2, NULL, ?, 'bob@s.whatsapp.net', 'me@s.whatsapp.net', 0, 1, 'Bob')",
        (_ios_ts(2026, 5, 23),),
    )
    cur.execute(
        "INSERT INTO ZWAMEDIAITEM VALUES (301, 202, 'Media/photo1.jpg', 'image/jpeg', 'photo1.jpg', 1024)"
    )

    # Group messages (chat 3): text + system + video
    cur.execute(
        "INSERT INTO ZWAMESSAGE VALUES (401, 3, NULL, ?, 'alice@s.whatsapp.net', '12345@g.us', 0, 10, 'Alice')",
        (_ios_ts(2026, 5, 18),),
    )  # system: group creation
    cur.execute(
        "INSERT INTO ZWAMESSAGE VALUES (402, 3, 'hola familia', ?, 'alice@s.whatsapp.net', '12345@g.us', 0, 0, 'Alice')",
        (_ios_ts(2026, 5, 19),),
    )
    cur.execute(
        "INSERT INTO ZWAMESSAGE VALUES (403, 3, NULL, ?, 'bob@s.whatsapp.net', '12345@g.us', 0, 2, 'Bob')",
        (_ios_ts(2026, 5, 20),),
    )
    cur.execute(
        "INSERT INTO ZWAMEDIAITEM VALUES (501, 403, 'Media/video1.mp4', 'video/mp4', 'video1.mp4', 9999999)"
    )

    # Group members (chat 3)
    cur.execute("INSERT INTO ZWAGROUPMEMBER VALUES (1, 3, 'alice@s.whatsapp.net', 'Alice')")
    cur.execute("INSERT INTO ZWAGROUPMEMBER VALUES (2, 3, 'bob@s.whatsapp.net', 'Bob')")
    cur.execute("INSERT INTO ZWAGROUPMEMBER VALUES (3, 3, 'me@s.whatsapp.net', 'Me')")

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def extracted_root(tmp_path):
    """Decrypted-backup tree with one small image and one oversize PDF."""
    root = tmp_path / "extracted"
    media = root / "media" / "Media"
    media.mkdir(parents=True)

    # Small image (1KB) — should be kept
    (media / "photo1.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 1020)

    # Oversize file (6MB) — should be filtered for size
    (media / "huge.pdf").write_bytes(b"%PDF-1.7\n" + b"x" * (6 * 1024 * 1024))

    # Fake video file (any size) — filtered by extension
    (media / "video1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp4")

    return root


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / ".sync_state.json"


@pytest.fixture
def output_path(tmp_path):
    return tmp_path / "export.json"


@pytest.fixture
def attachments_dir(tmp_path):
    return tmp_path / "attachments"
