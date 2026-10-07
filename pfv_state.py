"""
pfv_state.py — Pluggable checkout-state backends for PFV

The StateBackend ABC defines the interface. Three implementations are provided:
  - JSONBackend      : stores state in .pfv/meta.json  (default, no deps)
  - SQLiteBackend    : stores state in .pfv/meta.db     (stdlib sqlite3)
  - RedisBackend     : stores state in a Redis hash     (requires redis-py)

Usage
-----
from pfv_state import open_backend

# JSON (default)
backend = open_backend(work_tree)

# SQLite
backend = open_backend(work_tree, backend="sqlite")

# Redis
backend = open_backend(work_tree, backend="redis", redis_url="redis://localhost:6379/0")

All backends implement the same four methods:
  upsert(record: CheckoutRecord) -> None
  get(dest: str) -> CheckoutRecord | None
  remove(dest: str) -> bool
  list_all() -> list[CheckoutRecord]

`dest` is always the string form of the absolute destination path, used as
the primary key so a single work-tree can track many checked-out files.
"""

from __future__ import annotations

import json
import os
import socket
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Record type
# ---------------------------------------------------------------------------

@dataclass
class CheckoutRecord:
    """Everything we need to detect a mid-edit repo change on check-in."""
    dest: str               # absolute path of the working copy
    vdir: str               # absolute path of the PFV versioned-file directory
    version: int            # version that was checked out
    sha_at_checkout: str    # sha256 hex of the content at checkout time
    checked_out_at: str     # ISO-8601 UTC timestamp
    checked_out_by: str     # username
    host: str               # hostname
    extra: dict = field(default_factory=dict)  # reserved for future use

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CheckoutRecord":
        known = {f for f in cls.__dataclass_fields__}
        extra = {k: v for k, v in d.items() if k not in known}
        base  = {k: v for k, v in d.items() if k in known and k != "extra"}
        return cls(**base, extra=extra)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class StateBackend(ABC):
    """Interface that all checkout-state backends must implement."""

    @abstractmethod
    def upsert(self, record: CheckoutRecord) -> None:
        """Insert or replace the record for record.dest."""

    @abstractmethod
    def get(self, dest: str) -> CheckoutRecord | None:
        """Return the record for *dest*, or None if not tracked."""

    @abstractmethod
    def remove(self, dest: str) -> bool:
        """Delete the record for *dest*. Returns True if it existed."""

    @abstractmethod
    def list_all(self) -> list[CheckoutRecord]:
        """Return all tracked records."""

    def close(self) -> None:
        """Optional teardown (e.g. close DB connection)."""


# ---------------------------------------------------------------------------
# JSON backend
# ---------------------------------------------------------------------------

class JSONBackend(StateBackend):
    """
    Stores checkout state in <work_tree>/.pfv/meta.json.

    The file is a JSON object keyed by absolute dest path:
    {
      "/path/to/file.psd": { ...CheckoutRecord fields... },
      ...
    }
    Writes are atomic (temp-file + rename).
    """

    def __init__(self, work_tree: Path) -> None:
        self._pfv_dir = Path(work_tree) / ".pfv"
        self._pfv_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._pfv_dir / "meta.json"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    # StateBackend interface
    # ------------------------------------------------------------------

    def upsert(self, record: CheckoutRecord) -> None:
        data = self._load()
        data[record.dest] = record.to_dict()
        self._save(data)

    def get(self, dest: str) -> CheckoutRecord | None:
        data = self._load()
        raw = data.get(dest)
        return CheckoutRecord.from_dict(raw) if raw else None

    def remove(self, dest: str) -> bool:
        data = self._load()
        if dest not in data:
            return False
        del data[dest]
        self._save(data)
        return True

    def list_all(self) -> list[CheckoutRecord]:
        return [CheckoutRecord.from_dict(v) for v in self._load().values()]


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------

class SQLiteBackend(StateBackend):
    """
    Stores checkout state in <work_tree>/.pfv/meta.db (SQLite).

    The `extra` column holds a JSON blob for forward-compatible extension.
    No third-party dependencies — uses stdlib sqlite3.
    """

    def __init__(self, work_tree: Path) -> None:
        import sqlite3

        pfv_dir = Path(work_tree) / ".pfv"
        pfv_dir.mkdir(parents=True, exist_ok=True)
        db_path = pfv_dir / "meta.db"

        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._bootstrap()

    def _bootstrap(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS checkouts (
                dest              TEXT PRIMARY KEY,
                vdir              TEXT NOT NULL,
                version           INTEGER NOT NULL,
                sha_at_checkout   TEXT NOT NULL,
                checked_out_at    TEXT NOT NULL,
                checked_out_by    TEXT NOT NULL,
                host              TEXT NOT NULL,
                extra             TEXT NOT NULL DEFAULT '{}'
            )
        """)
        self._conn.commit()

    def upsert(self, record: CheckoutRecord) -> None:
        self._conn.execute("""
            INSERT INTO checkouts
                (dest, vdir, version, sha_at_checkout,
                 checked_out_at, checked_out_by, host, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dest) DO UPDATE SET
                vdir            = excluded.vdir,
                version         = excluded.version,
                sha_at_checkout = excluded.sha_at_checkout,
                checked_out_at  = excluded.checked_out_at,
                checked_out_by  = excluded.checked_out_by,
                host            = excluded.host,
                extra           = excluded.extra
        """, (
            record.dest, record.vdir, record.version,
            record.sha_at_checkout, record.checked_out_at,
            record.checked_out_by, record.host,
            json.dumps(record.extra),
        ))
        self._conn.commit()

    def get(self, dest: str) -> CheckoutRecord | None:
        row = self._conn.execute(
            "SELECT * FROM checkouts WHERE dest = ?", (dest,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def remove(self, dest: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM checkouts WHERE dest = ?", (dest,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_all(self) -> list[CheckoutRecord]:
        rows = self._conn.execute("SELECT * FROM checkouts").fetchall()
        return [self._row_to_record(r) for r in rows]

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_record(row: Any) -> CheckoutRecord:
        return CheckoutRecord(
            dest=row["dest"],
            vdir=row["vdir"],
            version=row["version"],
            sha_at_checkout=row["sha_at_checkout"],
            checked_out_at=row["checked_out_at"],
            checked_out_by=row["checked_out_by"],
            host=row["host"],
            extra=json.loads(row["extra"] or "{}"),
        )


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------

class RedisBackend(StateBackend):
    """
    Stores checkout state in Redis hashes.

    Each record is stored as a Redis hash at key:
        pfv:<work_tree_key>:checkout:<dest>

    and an index set at:
        pfv:<work_tree_key>:index

    so list_all() can enumerate without a full SCAN.

    Requires: pip install redis
    """

    def __init__(self, work_tree: Path, redis_url: str = "redis://localhost:6379/0",
                 key_prefix: str | None = None) -> None:
        try:
            import redis  # type: ignore
        except ImportError as e:
            raise ImportError(
                "RedisBackend requires the 'redis' package: pip install redis"
            ) from e

        self._r = redis.from_url(redis_url, decode_responses=True)
        # Use work_tree path as part of the key namespace
        safe = str(Path(work_tree).resolve()).replace("/", "_").replace("\\", "_")
        self._prefix = key_prefix or f"pfv:{safe}"
        self._index_key = f"{self._prefix}:index"

    def _record_key(self, dest: str) -> str:
        safe_dest = dest.replace("/", "_").replace("\\", "_")
        return f"{self._prefix}:checkout:{safe_dest}"

    def upsert(self, record: CheckoutRecord) -> None:
        key = self._record_key(record.dest)
        d = record.to_dict()
        d["extra"] = json.dumps(d["extra"])
        self._r.hset(key, mapping=d)
        self._r.sadd(self._index_key, record.dest)

    def get(self, dest: str) -> CheckoutRecord | None:
        key = self._record_key(dest)
        raw = self._r.hgetall(key)
        if not raw:
            return None
        raw["extra"] = json.loads(raw.get("extra", "{}"))
        raw["version"] = int(raw["version"])
        return CheckoutRecord.from_dict(raw)

    def remove(self, dest: str) -> bool:
        key = self._record_key(dest)
        deleted = self._r.delete(key)
        self._r.srem(self._index_key, dest)
        return deleted > 0

    def list_all(self) -> list[CheckoutRecord]:
        members = self._r.smembers(self._index_key)
        records = []
        for dest in members:
            r = self.get(dest)
            if r is not None:
                records.append(r)
        return records

    def close(self) -> None:
        self._r.close()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def open_backend(
    work_tree: Path,
    backend: str = "json",
    **kwargs: Any,
) -> StateBackend:
    """
    Return an initialised StateBackend for *work_tree*.

    Parameters
    ----------
    work_tree : Path
        Root of the working directory (where .pfv/ will live).
    backend : str
        One of "json" (default), "sqlite", "redis".
    **kwargs
        Passed to the backend constructor.
        For RedisBackend: redis_url="redis://...", key_prefix="..."

    Examples
    --------
    >>> b = open_backend(Path("."))                          # JSON
    >>> b = open_backend(Path("."), backend="sqlite")        # SQLite
    >>> b = open_backend(Path("."), backend="redis",
    ...                  redis_url="redis://localhost:6379/0")
    """
    work_tree = Path(work_tree).resolve()
    name = backend.lower()

    if name == "json":
        return JSONBackend(work_tree)
    elif name == "sqlite":
        return SQLiteBackend(work_tree, **kwargs)
    elif name == "redis":
        return RedisBackend(work_tree, **kwargs)
    else:
        raise ValueError(
            f"Unknown backend {backend!r}. Choose from: json, sqlite, redis"
        )


# ---------------------------------------------------------------------------
# Convenience: build a CheckoutRecord
# ---------------------------------------------------------------------------

def make_record(
    dest: Path,
    vdir: Path,
    version: int,
    sha_at_checkout: str,
) -> CheckoutRecord:
    """Build a CheckoutRecord with current user / host / timestamp."""
    return CheckoutRecord(
        dest=str(dest.resolve()),
        vdir=Path(vdir).as_posix(),  # a storage key, not a filesystem path: never resolve() it
        version=version,
        sha_at_checkout=sha_at_checkout,
        checked_out_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        checked_out_by=os.environ.get("USER", os.environ.get("USERNAME", "unknown")),
        host=socket.gethostname(),
    )
