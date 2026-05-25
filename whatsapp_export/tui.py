#!/usr/bin/env python3
"""
Interactive menu for the WhatsApp → Mikoshi pipeline.

Wraps run_pipeline.sh, extract_messages.py, explore_backup.py and
push_via_api.py behind a guided menu so you don't have to remember flags.

Run:  python3 tui.py
"""

import json
import os
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import questionary
    from questionary import Choice
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
except ImportError:
    print("Missing deps. Activate venv first:")
    print("  source .venv/bin/activate && pip install questionary rich")
    sys.exit(1)


SCRIPT_DIR = Path(__file__).parent.resolve()
EXPORTS_DIR = SCRIPT_DIR / "exports"
STATE_FILE = SCRIPT_DIR / ".sync_state.json"
INGEST_CONF = Path(os.environ.get("MIKOSHI_INGEST_CONF", Path.home() / ".mikoshi-ingest.conf"))

console = Console()

IOS_EPOCH = 978307200


# ─── helpers ───────────────────────────────────────────────────────────────

def load_ingest_conf() -> dict:
    """Mirror the bash logic: read KEY=VALUE lines from ~/.mikoshi-ingest.conf.

    Also exports each value to os.environ so that subprocess children
    (explore_backup.py, push_via_api.py, run_pipeline.sh) inherit them
    even when tui.py is launched directly without going through the
    mikoshi-whatsapp.sh wrapper.
    """
    cfg = {}
    if INGEST_CONF.exists():
        for line in INGEST_CONF.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    # Env vars take precedence over file values
    for key in ("MIKOSHI_URL", "MIKOSHI_TOKEN", "MIKOSHI_BACKUP_DIR",
                "MIKOSHI_CLIENT_ID", "KEEP_LOCAL_EXPORTS",
                "MIKOSHI_FAVORITES_FILE"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
        elif cfg.get(key):
            # File-provided value: export so children inherit
            os.environ[key] = cfg[key]
    return cfg


# Eager load on import so the env is set before any subprocess fires
load_ingest_conf()


def get_backup_dir(cfg: dict) -> Path | None:
    val = cfg.get("MIKOSHI_BACKUP_DIR")
    return Path(val) if val else None


def find_existing_chatstorage() -> Path | None:
    """Either freshly-decrypted or kept from a previous run."""
    candidates = [
        SCRIPT_DIR / "temp" / "extracted" / "ChatStorage.sqlite",
    ]
    cfg = load_ingest_conf()
    if backup_dir := get_backup_dir(cfg):
        candidates.append(backup_dir / "extracted" / "ChatStorage.sqlite")
    for c in candidates:
        if c.exists():
            return c
    return None


def list_chats_from_db(db: Path) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT s.ZCONTACTJID as jid, s.ZPARTNERNAME as name,
               s.ZLASTMESSAGEDATE as last_ts,
               COUNT(m.Z_PK) as msg_count
        FROM ZWACHATSESSION s
        LEFT JOIN ZWAMESSAGE m ON m.ZCHATSESSION = s.Z_PK
        WHERE s.ZCONTACTJID IS NOT NULL
        GROUP BY s.Z_PK
        ORDER BY s.ZLASTMESSAGEDATE DESC NULLS LAST
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fmt_ts(ios_ts: float | None) -> str:
    if not ios_ts:
        return "—"
    return datetime.fromtimestamp(ios_ts + IOS_EPOCH, tz=timezone.utc).strftime("%Y-%m-%d")


def run(cmd: list[str], env_extra: dict | None = None) -> int:
    """Run a subprocess inline (its stdout/stderr go to terminal)."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    console.print(f"[dim]$ {' '.join(shlex.quote(c) for c in cmd)}[/]")
    try:
        return subprocess.call(cmd, env=env, cwd=SCRIPT_DIR)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/]")
        return 130


def pause():
    console.print()
    questionary.press_any_key_to_continue("Press any key to return to menu...").ask()


# ─── actions ───────────────────────────────────────────────────────────────

def _dir_size_gb(path: Path, timeout: float = 5.0) -> str:
    """
    Return human-readable size of `path` using `du -sk`. Capped by timeout
    because a 200 GB backup on an external SSD with a slow file system can
    take minutes to walk via Python's stat().
    """
    try:
        result = subprocess.run(
            ["du", "-sk", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return "[dim](du failed)[/]"
        kb = int(result.stdout.split()[0])
        gb = kb / (1024 * 1024)
        if gb >= 1:
            return f"{gb:.1f} GB"
        return f"{kb / 1024:.1f} MB"
    except subprocess.TimeoutExpired:
        return "[dim](still computing — large backup)[/]"
    except Exception as e:
        return f"[dim](error: {e})[/]"


def action_status():
    """Render the pipeline status table.

    Order matters: render text-only fields first, then expensive disk
    measurements (du). With Rich's Live wrapper the user sees progress as
    rows fill in instead of staring at a blank screen.
    """
    from rich.live import Live

    cfg = load_ingest_conf()
    table = Table(title="Pipeline status", show_header=False, box=None)
    table.add_column("Key", style="cyan")
    table.add_column("Value")

    # ── Fast fields (in-memory / single file stat) ─────────────────────────
    table.add_row("Config file",
                  str(INGEST_CONF) + (" ✓" if INGEST_CONF.exists() else " ✗ (missing)"))
    table.add_row("MIKOSHI_URL", cfg.get("MIKOSHI_URL", "[red]not set[/]"))
    table.add_row("MIKOSHI_TOKEN",
                  "[green]set[/]" if cfg.get("MIKOSHI_TOKEN") else "[red]not set[/]")
    bdir = get_backup_dir(cfg)
    table.add_row("MIKOSHI_BACKUP_DIR",
                  str(bdir) if bdir else "[dim](local temp/)[/]")

    db = find_existing_chatstorage()
    table.add_row("Decrypted ChatStorage", str(db) if db else "[dim]none[/]")

    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            table.add_row("Last global sync", state.get("last_global_sync") or "—")
            table.add_row("Chats with cursor", str(len(state.get("chats", {}))))
        except Exception as e:
            table.add_row("Sync state", f"[red]corrupt: {e}[/]")
    else:
        table.add_row("Last global sync", "[dim]never[/]")

    exports = sorted(EXPORTS_DIR.glob("whatsapp_export_*.json"))
    table.add_row("Local exports",
                  f"{len(exports)} files" if exports else "[dim]none[/]")

    try:
        import favorites as _favs
        fav_count = len(_favs.load().get("favorites", []))
        table.add_row("Favorites",
                      f"{fav_count} chat(s)" if fav_count else "[dim]none[/]")
    except Exception:
        pass

    # Render the cheap portion immediately, then live-update with backup sizes.
    udids: list[Path] = []
    if bdir and bdir.exists():
        backup_root = bdir / "backup"
        if backup_root.exists():
            udids = [d for d in backup_root.iterdir()
                     if d.is_dir() and len(d.name) > 20]
            for u in udids:
                table.add_row(f"  Backup {u.name[:12]}", "[dim]measuring…[/]")

    if not udids:
        console.print(table)
        pause()
        return

    # Stream `du` results into the table without re-rendering the whole screen.
    with Live(table, console=console, refresh_per_second=4, transient=False) as live:
        for i, u in enumerate(udids):
            # The backup rows start after the "fixed" rows; locate them by name
            size = _dir_size_gb(u)
            # Rich's Table doesn't expose row updates, so rebuild that row.
            # Simplest: keep a separate index. We know the order of udids.
            row_idx = len(table.rows) - len(udids) + i
            table.columns[1]._cells[row_idx] = size
            live.refresh()

    pause()


def action_verify():
    run(["bash", str(SCRIPT_DIR / "verify_setup.sh")])
    pause()


def action_list_chats():
    db = find_existing_chatstorage()
    if not db:
        console.print("[yellow]No decrypted ChatStorage found.[/]")
        if questionary.confirm(
            "Decrypt now from existing backup (no iPhone needed)?",
            default=True
        ).ask():
            run([sys.executable, str(SCRIPT_DIR / "explore_backup.py"), "list-chats"])
        pause()
        return

    chats = list_chats_from_db(db)
    table = Table(title=f"Chats ({len(chats)} total)", header_style="bold cyan")
    table.add_column("Last msg")
    table.add_column("Msgs", justify="right")
    table.add_column("Kind")
    table.add_column("Name")
    table.add_column("JID", style="dim")

    for c in chats[:80]:
        kind = "group" if (c["jid"] or "").endswith("@g.us") else "1-on-1"
        table.add_row(
            fmt_ts(c["last_ts"]),
            str(c["msg_count"]),
            kind,
            (c["name"] or "—")[:32],
            c["jid"],
        )
    console.print(table)
    if len(chats) > 80:
        console.print(f"[dim](showing top 80 of {len(chats)})[/]")
    pause()


def pick_contact() -> str | None:
    """Either pick from existing DB or type free-form."""
    db = find_existing_chatstorage()

    if db:
        chats = list_chats_from_db(db)
        # Top 50 most recent
        choices = [
            Choice(
                title=f"{fmt_ts(c['last_ts']):<12} {c['msg_count']:>5} msgs  {(c['name'] or '—')[:30]}",
                value=c["name"] or c["jid"],
            )
            for c in chats[:50] if c["name"] or c["jid"]
        ]
        choices.append(Choice(title="✎ Type name/JID manually", value="__manual__"))
        choices.append(Choice(title="← Cancel", value=None))

        pick = questionary.select(
            "Select a contact (or type to filter):",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
        ).ask()
        if pick is None or pick == "__manual__":
            if pick is None:
                return None
        else:
            return pick

    return questionary.text(
        "Contact name (partial match) or JID:",
        validate=lambda x: bool(x.strip()) or "Required",
    ).ask()


def action_full_backup():
    console.print(Panel(
        "[bold yellow]Full pipeline:[/] device backup → decrypt → extract ALL chats → push.\n"
        "First time this can take HOURS (200+ GB if your iPhone is full).\n"
        "Subsequent runs are incremental (minutes).",
        title="⚠  Full backup",
    ))
    if not questionary.confirm("Continue?", default=False).ask():
        return
    skip_remote = not questionary.confirm("Push to Mikoshi at the end?", default=True).ask()
    cmd = ["bash", str(SCRIPT_DIR / "run_pipeline.sh"), "--mode", "full"]
    if skip_remote:
        cmd.append("--skip-remote-sync")
    run(cmd)
    pause()


def action_backup_one_contact():
    contact = pick_contact()
    if not contact:
        return
    skip_remote = not questionary.confirm("Push to Mikoshi at the end?", default=True).ask()
    cmd = ["bash", str(SCRIPT_DIR / "run_pipeline.sh"),
           "--mode", "full-contact", "--contact", contact]
    if skip_remote:
        cmd.append("--skip-remote-sync")
    run(cmd)
    pause()


def action_incremental():
    skip_remote = not questionary.confirm("Push to Mikoshi at the end?", default=True).ask()
    cmd = ["bash", str(SCRIPT_DIR / "run_pipeline.sh")]
    if skip_remote:
        cmd.append("--skip-remote-sync")
    run(cmd)
    pause()


def action_reextract():
    """Run extraction against an already-downloaded backup."""
    if not find_existing_chatstorage():
        cfg = load_ingest_conf()
        bdir = get_backup_dir(cfg)
        if not (bdir and (bdir / "backup").exists()):
            console.print("[red]No existing backup found.[/] Run a full backup first.")
            pause()
            return

    mode = questionary.select(
        "Re-extract mode:",
        choices=[
            Choice("Incremental (new since last sync)", "incremental"),
            Choice("Full (re-process everything from existing backup)", "full"),
            Choice("Just one contact", "full-contact"),
        ],
    ).ask()
    if not mode:
        return

    contact = None
    if mode == "full-contact":
        contact = pick_contact()
        if not contact:
            return

    cmd = [sys.executable, str(SCRIPT_DIR / "explore_backup.py"),
           "extract", "--mode", mode]
    if contact:
        cmd += ["--contact", contact]
    run(cmd)
    pause()


def action_push_existing():
    exports = sorted(EXPORTS_DIR.glob("whatsapp_export_*.json"), reverse=True)
    if not exports:
        console.print("[red]No local exports found in exports/[/]")
        pause()
        return

    cfg = load_ingest_conf()
    if not cfg.get("MIKOSHI_URL") or not cfg.get("MIKOSHI_TOKEN"):
        console.print("[red]MIKOSHI_URL and MIKOSHI_TOKEN are required.[/]")
        console.print(f"Edit {INGEST_CONF}")
        pause()
        return

    choices = []
    for e in exports[:20]:
        try:
            meta = json.loads(e.read_text())
            choices.append(Choice(
                title=f"{e.name}  [{meta.get('mode')}, {meta['stats']['total_messages']} msgs]",
                value=str(e),
            ))
        except Exception:
            choices.append(Choice(title=e.name, value=str(e)))

    pick = questionary.select("Pick an export to push:", choices=choices).ask()
    if not pick:
        return
    run([
        sys.executable, str(SCRIPT_DIR / "push_via_api.py"),
        "--manifest", pick,
        "--attachments-dir", str(EXPORTS_DIR / "attachments"),
    ])
    pause()


def action_sqlite_shell():
    db = find_existing_chatstorage()
    if not db:
        console.print("[yellow]No decrypted ChatStorage. Decrypting now...[/]")
        run([sys.executable, str(SCRIPT_DIR / "explore_backup.py"), "shell"])
        return
    console.print(f"[cyan]Opening sqlite3 against {db}[/]")
    console.print("[dim]Type .quit to return[/]")
    os.execvp("sqlite3", ["sqlite3", str(db)])


def action_run_tests():
    run([sys.executable, "-m", "pytest", "-v"], env_extra=None)
    pause()


def action_edit_config():
    if not INGEST_CONF.exists():
        if not questionary.confirm(f"{INGEST_CONF} doesn't exist. Create with template?", default=True).ask():
            return
        INGEST_CONF.write_text(
            "# Mikoshi WhatsApp pipeline config\n"
            "MIKOSHI_URL=https://your-mikoshi.example.com\n"
            "MIKOSHI_TOKEN=paste-token-here\n"
            "# MIKOSHI_BACKUP_DIR=/Volumes/ExternalSSD/iphone_backup\n"
            "# MIKOSHI_CLIENT_ID=my-mac\n"
            "# KEEP_LOCAL_EXPORTS=5\n"
        )
        INGEST_CONF.chmod(0o600)
    editor = os.environ.get("EDITOR", "nano")
    subprocess.call([editor, str(INGEST_CONF)])


# ─── favorites ─────────────────────────────────────────────────────────────

import favorites as favs


def _pick_chats_multi(prompt: str, source_chats: list[dict], preselect_jids: set[str] | None = None) -> list[dict] | None:
    """questionary.checkbox over chats; returns selected dicts or None on cancel."""
    preselect_jids = preselect_jids or set()
    choices = []
    for c in source_chats:
        if not c.get("jid"):
            continue
        kind = "group" if c["jid"].endswith("@g.us") else "1-on-1"
        title = f"{fmt_ts(c['last_ts']):<12} {c['msg_count']:>5} msgs  [{kind}]  {(c.get('name') or '—')[:30]}"
        choices.append(Choice(title=title, value=c, checked=(c["jid"] in preselect_jids)))

    if not choices:
        console.print("[red]No chats to choose from.[/]")
        return None

    return questionary.checkbox(
        prompt,
        choices=choices,
        use_search_filter=True,
        use_jk_keys=False,
    ).ask()


def _render_favorites_table():
    data = favs.load()
    items = data.get("favorites", [])
    if not items:
        console.print("[yellow]No favorites yet.[/]")
        return

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"chats": {}}
    cursors = state.get("chats", {})

    table = Table(title=f"Favorites ({len(items)})", header_style="bold cyan")
    table.add_column("Name")
    table.add_column("JID", style="dim")
    table.add_column("Last sync")
    table.add_column("Added")
    for f in items:
        table.add_row(
            (f.get("name") or "—")[:32],
            f["jid"],
            (cursors.get(f["jid"]) or "—")[:19],
            (f.get("added_at") or "")[:10],
        )
    console.print(table)


def action_manage_favorites():
    while True:
        console.clear()
        console.print(Panel.fit(
            f"[bold cyan]Favorites[/]   ({favs.path()})\n"
            "[dim]These chats will be synced when you run 'mikoshi-whatsapp.sh sync'[/]"
        ))
        _render_favorites_table()
        console.print()

        choice = questionary.select(
            "Manage favorites:",
            choices=[
                Choice("➕  Add chats", "add"),
                Choice("➖  Remove chats", "remove"),
                Choice("🗑   Clear all", "clear"),
                Choice("🔂  Sync favorites now", "sync_now"),
                Choice("← Back", "back"),
            ],
        ).ask()

        if choice is None or choice == "back":
            return
        if choice == "add":
            db = find_existing_chatstorage()
            if not db:
                console.print("[red]No ChatStorage decrypted yet.[/] "
                              "Run a sync first so we can list your chats.")
                pause()
                continue
            all_chats = list_chats_from_db(db)
            current = {f["jid"] for f in favs.load()["favorites"]}
            picked = _pick_chats_multi(
                "Select chats to add (space to toggle, enter to confirm):",
                all_chats,
                preselect_jids=current,
            )
            if not picked:
                continue
            added = favs.add(
                [{"jid": c["jid"], "name": c.get("name")} for c in picked]
            )
            console.print(f"[green]Added {added} new favorite(s)[/]")
            pause()
        elif choice == "remove":
            data = favs.load()
            if not data["favorites"]:
                console.print("[yellow]No favorites to remove[/]")
                pause()
                continue
            picked = questionary.checkbox(
                "Select favorites to remove:",
                choices=[
                    Choice(f"{(f.get('name') or f['jid'])[:35]}  ({f['jid']})", f["jid"])
                    for f in data["favorites"]
                ],
                use_search_filter=True,
                use_jk_keys=False,
            ).ask()
            if picked:
                n = favs.remove(picked)
                console.print(f"[green]Removed {n} favorite(s)[/]")
                pause()
        elif choice == "clear":
            if questionary.confirm("Clear ALL favorites?", default=False).ask():
                n = favs.clear()
                console.print(f"[green]Cleared {n} favorite(s)[/]")
                pause()
        elif choice == "sync_now":
            action_sync_favorites()


def action_sync_favorites():
    data = favs.load()
    if not data["favorites"]:
        console.print("[yellow]No favorites configured. Add some first.[/]")
        pause()
        return
    skip_remote = not questionary.confirm(
        f"Push to Mikoshi at the end? ({len(data['favorites'])} chat(s))",
        default=True
    ).ask()
    cmd = ["bash", str(SCRIPT_DIR / "run_pipeline.sh"), "--favorites"]
    if skip_remote:
        cmd.append("--skip-remote-sync")
    run(cmd)
    pause()


# ─── main loop ─────────────────────────────────────────────────────────────

ACTIONS = [
    ("📊  Show status / config",                action_status),
    ("✅  Verify setup (run checks)",           action_verify),
    ("📋  List chats from backup",              action_list_chats),
    ("📌  Manage favorites",                    action_manage_favorites),
    ("🔂  Sync favorites now",                  action_sync_favorites),
    ("🔁  Sync — incremental (default)",        action_incremental),
    ("👤  Sync — one contact only",             action_backup_one_contact),
    ("🌍  Sync — full (everything)",            action_full_backup),
    ("♻️   Re-extract from existing backup",    action_reextract),
    ("📤  Push existing export to Mikoshi",     action_push_existing),
    ("🐚  Open sqlite3 shell on ChatStorage",   action_sqlite_shell),
    ("✏️   Edit ~/.mikoshi-ingest.conf",        action_edit_config),
    ("🧪  Run tests",                            action_run_tests),
]


# Sentinel for explicit-Exit so we can distinguish it from ESC/Ctrl+C (None).
# Using None for both made `choice()` get called with a non-callable string
# when use_shortcuts=True returned the shortcut key for the Exit row.
_EXIT_SENTINEL = "__exit__"


def main():
    while True:
        console.clear()
        console.print(Panel.fit(
            "[bold cyan]Mikoshi WhatsApp Pipeline[/]\n"
            "[dim]TUI · ESC or Ctrl+C to exit[/]",
        ))

        choice = questionary.select(
            "What do you want to do?",
            choices=[Choice(title=label, value=fn) for label, fn in ACTIONS]
                    + [Choice(title="🚪  Exit", value=_EXIT_SENTINEL)],
            use_shortcuts=True,
        ).ask()

        # User hit ESC / Ctrl+C, or picked Exit, or somehow got a non-callable.
        if choice is None or choice == _EXIT_SENTINEL:
            break
        if not callable(choice):
            # Defensive: questionary occasionally hands back a shortcut string
            # when use_shortcuts is enabled. Don't crash; warn and re-prompt.
            console.print(f"[yellow]Unexpected selection: {choice!r}[/]")
            continue
        try:
            choice()
        except KeyboardInterrupt:
            console.print("\n[yellow]Action cancelled[/]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[cyan]Bye![/]")
