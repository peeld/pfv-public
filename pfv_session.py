"""
pfv_session.py — per-workspace session manager

Reads and writes .pfv/session.json inside a workspace directory.
A session describes one or more named repo connections for the workspace,
so a single working directory can target multiple storage backends
(e.g. a local cache and an S3 primary).

Schema  (<workspace>/.pfv/session.json):
    {
        "description": "Main film project",
        "owner":       "al",
        "repos": [
            {
                "name":         "primary",
                "storage":      "s3://my-bucket/pfv/my-film",
                "cred_profile": "s3-prod",   # optional
                "region":       "us-east-1", # optional S3 kwargs
                "endpoint_url": "...",        # optional
                "default":      true
            },
            {
                "name":    "local-cache",
                "storage": "/Volumes/NAS/pfv/my-film"
            }
        ]
    }

Usage
-----
    from pfv_session import PFVSession

    # Load (or create) the session for a workspace
    session = PFVSession.load("/path/to/workspace")

    # Add / update a repo entry
    session.add_repo("primary", "s3://bucket/prefix", cred_profile="prod", default=True)
    session.add_repo("local",   "/mnt/nas/repo")

    session.save()

    # Read back
    repo = session.default_repo        # the repo marked default=True (or first)
    repo = session.get_repo("local")   # by name
    print(repo["storage"])

    # Remove
    session.remove_repo("local")
    session.save()
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Name of the session file inside .pfv/
SESSION_FILENAME = "session.json"


class PFVSession:
    """
    Manages the .pfv/session.json file for a workspace directory.

    All mutations are in-memory until save() is called.
    """

    def __init__(self, work_tree: Path, data: dict[str, Any]) -> None:
        self.work_tree = work_tree
        self._data = data

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, work_tree: str | Path) -> "PFVSession":
        """
        Load session from <work_tree>/.pfv/session.json.
        Returns an empty session (not yet saved) if the file does not exist.
        """
        work_tree = Path(work_tree).resolve()
        path = work_tree / ".pfv" / SESSION_FILENAME
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}
        data.setdefault("repos", [])
        return cls(work_tree, data)

    @classmethod
    def exists(cls, work_tree: str | Path) -> bool:
        """Return True if a session file already exists for this workspace."""
        path = Path(work_tree).resolve() / ".pfv" / SESSION_FILENAME
        return path.exists()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self.work_tree / ".pfv" / SESSION_FILENAME

    def save(self) -> None:
        """Write session to disk, creating .pfv/ if needed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
            f.write("\n")

    # ------------------------------------------------------------------
    # Metadata fields
    # ------------------------------------------------------------------

    @property
    def description(self) -> str | None:
        return self._data.get("description")

    @description.setter
    def description(self, value: str | None) -> None:
        if value:
            self._data["description"] = value
        else:
            self._data.pop("description", None)

    @property
    def owner(self) -> str | None:
        return self._data.get("owner")

    @owner.setter
    def owner(self, value: str | None) -> None:
        if value:
            self._data["owner"] = value
        else:
            self._data.pop("owner", None)

    # ------------------------------------------------------------------
    # Repo list access
    # ------------------------------------------------------------------

    @property
    def repos(self) -> list[dict]:
        """Live list of repo records (mutable — call save() to persist)."""
        return self._data["repos"]

    @property
    def default_repo(self) -> dict | None:
        """
        Return the repo marked default=True, or the first repo if none is
        explicitly flagged, or None if the list is empty.
        """
        for repo in self.repos:
            if repo.get("default"):
                return repo
        return self.repos[0] if self.repos else None

    def get_repo(self, name: str) -> dict | None:
        """Return the repo with the given name, or None."""
        for repo in self.repos:
            if repo.get("name") == name:
                return repo
        return None

    def add_repo(
        self,
        name: str,
        storage: str,
        *,
        cred_profile: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        default: bool = False,
        **extra: Any,
    ) -> dict:
        """
        Add or replace the repo entry with *name*.

        If default=True, the default flag is cleared from all other repos
        before setting it on this one.

        Returns the new/updated record.
        """
        # Clear existing entry with same name
        self._data["repos"] = [r for r in self.repos if r.get("name") != name]

        record: dict = {"name": name, "storage": storage}
        if cred_profile:
            record["cred_profile"] = cred_profile
        if region:
            record["region"] = region
        if endpoint_url:
            record["endpoint_url"] = endpoint_url
        record.update(extra)

        if default:
            # Demote any existing default
            for r in self.repos:
                r.pop("default", None)
            record["default"] = True

        self._data["repos"].append(record)
        return record

    def remove_repo(self, name: str) -> bool:
        """Remove the repo with *name*. Returns True if one was removed."""
        before = len(self.repos)
        self._data["repos"] = [r for r in self.repos if r.get("name") != name]
        removed = len(self._data["repos"]) < before

        # If we removed the default, promote the first remaining repo
        if removed and not any(r.get("default") for r in self.repos):
            if self.repos:
                self.repos[0]["default"] = True

        return removed

    def set_default(self, name: str) -> bool:
        """Mark repo *name* as the default. Returns True if found."""
        for r in self.repos:
            r.pop("default", None)
        repo = self.get_repo(name)
        if repo is not None:
            repo["default"] = True
            return True
        return False

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def storage_kwargs(self, name: str | None = None) -> tuple[str, dict]:
        """
        Return (storage_location, kwargs) suitable for passing to
        pfv_storage.open_storage() for the named repo (or the default).

        Raises KeyError if the requested repo does not exist.
        """
        repo = self.get_repo(name) if name else self.default_repo
        if repo is None:
            raise KeyError(f"No repo named {name!r}" if name else "No repos defined in session")

        location = repo["storage"]
        kwargs: dict = {}
        if repo.get("cred_profile"):
            kwargs["cred_profile"] = repo["cred_profile"]
        if repo.get("region"):
            kwargs["region"] = repo["region"]
        if repo.get("endpoint_url"):
            kwargs["endpoint_url"] = repo["endpoint_url"]
        return location, kwargs
