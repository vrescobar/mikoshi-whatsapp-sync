"""Runtime config loaded from environment variables / .env."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    database_url: str
    exports_dir: Path
    media_store: Path
    schema_path: Path
    watch_interval: int

    @classmethod
    def from_env(cls) -> "Config":
        try:
            return cls(
                database_url=os.environ["DATABASE_URL"],
                exports_dir=Path(os.environ["EXPORTS_DIR"]),
                media_store=Path(os.environ["MEDIA_STORE"]),
                schema_path=Path(os.environ["SCHEMA_PATH"]),
                watch_interval=int(os.environ.get("WATCH_INTERVAL", "300")),
            )
        except KeyError as e:
            raise RuntimeError(f"Missing required env var: {e}") from None
