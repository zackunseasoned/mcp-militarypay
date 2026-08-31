"""SQLite connection handling and schema application.

Ingest and serve are deliberately separate: parsing happens once at refresh
time, and every MCP tool call afterwards is a parameterized SELECT with no
HTML/CSV parsing in the request path.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

SCHEMA_VERSION = "1"

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_DB_ENV_VAR = "MILITARYPAY_DB"


def project_root() -> Path | None:
    """The source checkout this package was imported from, if it is one.

    parents[2] is the repository root for a source tree or an editable install,
    but the parent of site-packages for a normal `pip install`, where writing a
    database would land in the Python library tree. The pyproject.toml check
    distinguishes them.
    """
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() else None


def data_dir() -> Path:
    """Where generated data belongs: the checkout if there is one, else the
    user's home directory."""
    root = project_root()
    return root / "data" if root else Path.home() / ".mcp-militarypay"


def default_db_path() -> Path:
    """Where the database lives unless overridden by $MILITARYPAY_DB."""
    override = os.environ.get(DEFAULT_DB_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return data_dir() / "militarypay.sqlite3"


def utcnow() -> str:
    """Current time as an ISO 8601 UTC string, used for every *_at column."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | os.PathLike[str] | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the rate database.

    With read_only=True the connection is opened via a file: URI in ro mode, so
    a serving process physically cannot write. Raises if the file is missing.
    """
    path = Path(db_path) if db_path is not None else default_db_path()

    if read_only:
        if not path.exists():
            raise FileNotFoundError(
                f"rate database not found at {path}. Run the ingest first: "
                f"python -m mcp_militarypay.cli ingest --all"
            )
        # A path containing '#' or '?' would otherwise be read as a URI
        # fragment or query, silently opening a different (empty) database.
        conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")

    conn.row_factory = sqlite3.Row
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create every table/index if absent. Idempotent."""
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def open_for_ingest(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open read-write with the schema guaranteed present."""
    conn = connect(db_path)
    apply_schema(conn)
    return conn


def log_fetch(
    conn: sqlite3.Connection,
    *,
    source: str,
    url: str,
    ok: bool,
    row_count: int | None = None,
    notes: str | None = None,
    fetched_at: str | None = None,
) -> None:
    """Record a fetch attempt. Row counts here are how a silently-changed page
    layout gets noticed on the next refresh."""
    conn.execute(
        "INSERT INTO source_fetch_log(source, url, fetched_at, row_count, ok, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source, url, fetched_at or utcnow(), row_count, 1 if ok else 0, notes),
    )
