"""
pfv_config.py — ~/.pfvconfig workspace registry

Maintains a JSON index of known PFV workspace directories so that the GUI
and CLI can offer a "recent/known workspaces" picker without scanning the
filesystem.

Each workspace entry is a local directory path.  The workspace itself holds
its repo connection details in .pfv/session.json (see pfv_session.py).

Schema  (~/.pfvconfig):
    {
        "workspaces": [
            {
                "path":        "/absolute/path/to/workspace",
                "name":        "My Film",          # optional display label
                "last_opened": "2026-06-03T09:00:00Z"
            },
            ...
        ]
    }

Usage
-----
    from pfv_config import PFVConfig

    cfg = PFVConfig()
    cfg.register("/path/to/workspace", name="My Film")
    cfg.touch("/path/to/workspace")   # update last_opened timestamp
    cfg.save()

    for ws in cfg.workspaces:
        print(ws["path"], ws.get("name"))
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".pfvconfig"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PFVConfig:
    """Read/write ~/.pfvconfig as a registry of known workspace directories."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_CONFIG_PATH
        self._data: dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}
        data.setdefault("workspaces", [])
        return data

    def save(self) -> None:
        """Persist current state to disk."""
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
            f.write("\n")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def workspaces(self) -> list[dict]:
        """Live list of workspace records (mutable — call save() to persist)."""
        return self._data["workspaces"]

    def register(self, path: str | Path, name: str | None = None) -> dict:
        """
        Add a workspace entry for *path* if not already present.
        Returns the (possibly existing) record.
        """
        key = str(Path(path).resolve())
        for ws in self.workspaces:
            if str(Path(ws["path"]).resolve()) == key:
                return ws

        record: dict = {"path": key, "last_opened": _now_iso()}
        if name:
            record["name"] = name
        self.workspaces.append(record)
        return record

    def touch(self, path: str | Path) -> bool:
        """Update last_opened for *path*. Returns True if found."""
        key = str(Path(path).resolve())
        for ws in self.workspaces:
            if str(Path(ws["path"]).resolve()) == key:
                ws["last_opened"] = _now_iso()
                return True
        return False

    def unregister(self, path: str | Path) -> bool:
        """Remove the entry for *path*. Returns True if one was removed."""
        key = str(Path(path).resolve())
        before = len(self.workspaces)
        self._data["workspaces"] = [
            ws for ws in self.workspaces
            if str(Path(ws["path"]).resolve()) != key
        ]
        return len(self._data["workspaces"]) < before

    def find(self, path: str | Path) -> dict | None:
        """Return the record for *path*, or None."""
        key = str(Path(path).resolve())
        for ws in self.workspaces:
            if str(Path(ws["path"]).resolve()) == key:
                return ws
        return None

    def sorted_by_recent(self) -> list[dict]:
        """Return workspaces sorted most-recently-opened first."""
        return sorted(
            self.workspaces,
            key=lambda ws: ws.get("last_opened", ""),
            reverse=True,
        )
