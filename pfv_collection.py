"""
pfv_collection.py -- Unified abstraction over versioned file collections

VersionedFileCollection is an ABC that presents a common interface for asking
"what named files exist, and what is the current version/sha/meta of each?"
regardless of whether the backing store is a rich multi-version PFV repo or a
flat workspace of checked-out files.

Implementations
---------------
  RepoCollection       wraps a StorageBackend -- discovers vdirs via storage keys
  WorkspaceCollection  wraps a filesystem Path -- discovers files via os.walk

This abstraction decouples RecursiveOperation (pfv_recursive.py) from any
specific storage mechanism, so a single operation implementation works on both
repos and workspaces without modification.

.pfvignore support
------------------
load_pfvignore(dir) and is_ignored(rel, patterns) are public helpers used by
WorkspaceCollection and re-imported by pfv.py (import_dir). Patterns follow
fnmatch syntax and are matched against both the full relative path and each
individual path component, so both "*.tmp" and "build" behave as expected.

Usage
-----
    from pfv_collection import RepoCollection, WorkspaceCollection
    from pfv_storage import open_storage

    col = RepoCollection(open_storage("/mnt/repos"))
    col = WorkspaceCollection(Path("/work"), state=open_backend(Path("/work")))

    for name in col.list_entries():
        print(name, col.get_version(name), col.get_sha(name))
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pfv_state import StateBackend
    from pfv_storage import StorageBackend


# ---------------------------------------------------------------------------
# .pfvignore helpers (used by WorkspaceCollection; re-imported by pfv.py)
# ---------------------------------------------------------------------------

def load_pfvignore(source_dir: Path) -> list[str]:
    """
    Read ignore patterns from .pfvignore or .pfvignore.txt in *source_dir*.
    Returns a list of fnmatch patterns (blank lines and #-comments stripped).
    """
    for name in (".pfvignore", ".pfvignore.txt"):
        p = source_dir / name
        if p.is_file():
            patterns: list[str] = []
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
            return patterns
    return []


def is_ignored(rel: Path, patterns: list[str]) -> bool:
    """
    Return True if *rel* (a relative Path) matches any ignore pattern.
    Patterns are matched against the full relative path string and against
    individual path parts, so both "*.tmp" and "build" work as expected.
    """
    print(rel)
    rel_str = rel.as_posix()
    for pattern in patterns:
        bare = pattern.rstrip("/")  # normalize trailing slash
        if fnmatch.fnmatch(rel_str, bare):
            print("TRUE 1")
            return True
        # Match each component so directory patterns prune entire subtrees
        for part in rel.parts:
            if fnmatch.fnmatch(part, bare):
                print("TRUE 2")
                return True
    print("FALSE")
    return False


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class VersionedFileCollection(ABC):
    """
    Common read interface over any collection of versioned files.

    Both a PFV repo (many versions, rich metadata) and a workspace (one
    version per file, minimal metadata) implement this interface, so
    operations like RecursiveOperation can work on either without changes.
    """

    @abstractmethod
    def list_entries(self) -> list[str]:
        """Return the logical names of all files in the collection (vdir names)."""

    @abstractmethod
    def get_version(self, name: str) -> int:
        """
        Return the current version number for *name*.
        Repos return the latest committed version. Workspaces return the
        checked-out version from the state backend, or 0 if untracked.
        """

    @abstractmethod
    def get_sha(self, name: str) -> str:
        """
        Return the SHA-256 hex digest for the current version of *name*.
        Returns an empty string if the sha is unavailable.
        """

    @abstractmethod
    def get_meta(self, name: str) -> dict:
        """
        Return metadata for the current version of *name*.
        Repos return the parsed .meta JSON. Workspaces return basic file stats.
        Returns an empty dict if no metadata is available.
        """


# ---------------------------------------------------------------------------
# Repo implementation
# ---------------------------------------------------------------------------

class RepoCollection(VersionedFileCollection):
    """
    VersionedFileCollection backed by a PFV StorageBackend.

    Discovers vdirs by scanning for <name>/latest sentinel keys.
    Reads version, sha, and meta from the corresponding slot sidecar files.

    Parameters
    ----------
    storage : StorageBackend
        The repo store to scan.
    prefix : str
        Limit the scan to keys under this prefix (default: all keys).
    """

    def __init__(self, storage: "StorageBackend", prefix: str = "") -> None:
        self._storage = storage
        self._prefix = prefix

    def list_entries(self) -> list[str]:
        # Find all keys that are vdir sentinel files, then strip the suffix
        all_keys = self._storage.list_keys(self._prefix)
        return [k[: -len("/latest")] for k in all_keys if k.endswith("/latest")]

    def get_version(self, name: str) -> int:
        try:
            return self._storage.read_int(f"{name}/latest")
        except Exception:
            return 0

    def get_sha(self, name: str) -> str:
        version = self.get_version(name)
        if version == 0:
            return ""
        sha_key = f"{name}/{version}.sha"
        if not self._storage.exists(sha_key):
            return ""
        try:
            # .sha lines look like "sha256:<hex>" -- return just the hex part
            raw = self._storage.read_text(sha_key).splitlines()[0].strip()
            return raw.split(":", 1)[-1] if ":" in raw else raw
        except Exception:
            return ""

    def get_meta(self, name: str) -> dict:
        version = self.get_version(name)
        if version == 0:
            return {}
        meta_key = f"{name}/{version}.meta"
        if not self._storage.exists(meta_key):
            return {}
        try:
            return self._storage.read_json(meta_key)
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# Workspace implementation
# ---------------------------------------------------------------------------

class WorkspaceCollection(VersionedFileCollection):
    """
    VersionedFileCollection backed by a local filesystem work-tree.

    Discovers entries by walking the work-tree and returning relative paths
    to every regular file, excluding the .pfv metadata directory and any
    paths that match patterns in a .pfvignore file at the work-tree root.

    SHA-256 is computed by hashing the file on disk. Version is looked up
    from the StateBackend if one is supplied, otherwise reported as 0.

    Parameters
    ----------
    work_tree : Path
        Root of the checked-out work-tree.
    state : StateBackend | None
        Optional state backend for resolving version numbers. When None,
        get_version() returns 0 for every entry.
    """

    def __init__(self, work_tree: Path, state: "StateBackend | None" = None) -> None:
        self._work_tree = Path(work_tree).resolve()
        self._state = state
        # Load ignore patterns once at construction time
        self._ignore_patterns: list[str] = load_pfvignore(self._work_tree)

    def list_entries(self) -> list[str]:
        """
        Walk the work-tree and return relative POSIX paths to all regular
        files, excluding:
          - the .pfv metadata directory
          - .pfvignore / .pfvignore.txt themselves
          - any path matching a pattern in .pfvignore
        """
        entries = []
        ignore_filenames = {".pfvignore", ".pfvignore.txt"}
        for dirpath, dirnames, filenames in os.walk(self._work_tree):
            rel_dir = Path(dirpath).relative_to(self._work_tree)

            # Prune ignored and internal directories so os.walk skips their subtrees
            dirnames[:] = [
                d for d in dirnames
                if d != ".pfv"
                and not is_ignored(rel_dir / d, self._ignore_patterns)
            ]

            for fname in filenames:
                if fname in ignore_filenames:
                    continue
                rel = rel_dir / fname
                if is_ignored(rel, self._ignore_patterns):
                    continue
                entries.append(rel.as_posix())  # forward-slash keys, like storage

        return sorted(entries)

    def get_version(self, name: str) -> int:
        """
        Return the checked-out version from the state backend.
        Returns 0 if no state backend is set or the file is untracked.
        """
        if self._state is None:
            return 0
        abs_path = str(self._work_tree / name)
        record = self._state.get(abs_path)
        return record.version if record is not None else 0

    def get_sha(self, name: str) -> str:
        """Hash the file on disk and return the SHA-256 hex digest."""
        file_path = self._work_tree / name
        if not file_path.is_file():
            return ""
        h = hashlib.sha256()
        try:
            with open(file_path, "rb") as fh:
                # Read in chunks to handle large files without loading all into memory
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return ""

    def get_meta(self, name: str) -> dict:
        """Return basic filesystem metadata for the file."""
        file_path = self._work_tree / name
        try:
            stat = file_path.stat()
            return {
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        except OSError:
            return {}
