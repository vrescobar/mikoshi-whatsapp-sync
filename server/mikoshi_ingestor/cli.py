"""CLI entrypoint for the Mikoshi ingestor."""

import logging
import shutil
import sys
import time
from pathlib import Path

import click
import psycopg

from .config import Config
from .ingestor import ingest_export

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mikoshi.cli")


def _pending_exports(exports_dir: Path) -> list[Path]:
    """JSON files not yet processed (i.e. still in the top-level exports_dir)."""
    if not exports_dir.exists():
        return []
    return sorted(exports_dir.glob("whatsapp_export_*.json"))


def _archive(export_path: Path, target_subdir: str):
    target = export_path.parent / target_subdir
    target.mkdir(parents=True, exist_ok=True)
    dest = target / export_path.name
    if dest.exists():
        dest = target / f"{export_path.stem}_{int(time.time())}.json"
    shutil.move(str(export_path), str(dest))
    logger.info("Archived %s -> %s", export_path.name, dest)


@click.group()
def cli():
    pass


@cli.command()
@click.option("--once", is_flag=True, help="Process current pending exports and exit.")
@click.option("--watch", is_flag=True, help="Loop forever, polling every WATCH_INTERVAL.")
@click.option("--file", "file_arg", type=click.Path(path_type=Path),
              help="Ingest a single file (debugging).")
def ingest(once, watch, file_arg):
    """Ingest pending WhatsApp export JSONs into PostgreSQL."""
    cfg = Config.from_env()

    if not (once or watch or file_arg):
        click.echo("Specify one of: --once, --watch, --file", err=True)
        sys.exit(2)

    cfg.media_store.mkdir(parents=True, exist_ok=True)

    if file_arg:
        _run_batch(cfg, [file_arg])
        return

    if once:
        _run_batch(cfg, _pending_exports(cfg.exports_dir))
        return

    # watch
    logger.info("Watching %s (every %ds)", cfg.exports_dir, cfg.watch_interval)
    while True:
        pending = _pending_exports(cfg.exports_dir)
        if pending:
            _run_batch(cfg, pending)
        time.sleep(cfg.watch_interval)


def _run_batch(cfg: Config, files: list[Path]):
    if not files:
        logger.info("Nothing to ingest.")
        return

    logger.info("Ingesting %d file(s)", len(files))
    with psycopg.connect(cfg.database_url, autocommit=False) as conn:
        for f in files:
            result = ingest_export(f, cfg.schema_path, cfg.media_store, conn)
            logger.info(
                "%s: status=%s chats=%d msgs=%d attachments=%d err=%s",
                f.name,
                result.status,
                result.chats_seen,
                result.messages_upserted,
                result.attachments_stored,
                result.error or "-",
            )
            if result.status == "ok":
                _archive(f, "processed")
            else:
                _archive(f, "quarantine")


@cli.command()
def stats():
    """Show ingestion stats from the DB."""
    cfg = Config.from_env()
    with psycopg.connect(cfg.database_url) as conn:
        for row in conn.execute(
            """
            SELECT
              (SELECT count(*) FROM chats)                AS chats,
              (SELECT count(*) FROM messages)             AS messages,
              (SELECT count(*) FROM attachments)          AS attachments,
              (SELECT count(*) FROM ingestion_log
                 WHERE status = 'ok')                     AS successful_runs,
              (SELECT count(*) FROM ingestion_log
                 WHERE status = 'fail')                   AS failed_runs,
              (SELECT max(finished_at) FROM ingestion_log) AS last_run
            """
        ):
            click.echo(
                f"Chats:        {row[0]}\n"
                f"Messages:     {row[1]}\n"
                f"Attachments:  {row[2]}\n"
                f"Successful:   {row[3]}\n"
                f"Failed:       {row[4]}\n"
                f"Last run:     {row[5]}"
            )


if __name__ == "__main__":
    cli()
