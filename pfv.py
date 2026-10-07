"""
pfv.py — Peel File Versions core library

Implements the PFV 0.1 spec: filesystem-based versioning for large binary files.

All repo I/O is routed through a StorageBackend (from pfv_storage.py), so the
same logic works whether the repo is on a local filesystem, S3, or any other
backend.  The caller either passes a StorageBackend instance explicitly, or lets
the functions construct one from a location string via open_storage().

Quick start
-----------
    from pfv import init, commit, checkout, log
    from pfv_storage import open_storage

    store = open_storage(".")                       # local
    store = open_storage("s3://my-bucket/repos")    # S3

    init("video.mp4", storage=store, owner="alice")
    commit("video.mp4", Path("render.mp4"), storage=store, message="v1")
    checkout("video.mp4", "latest", Path("working.mp4"), storage=store)
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import socket
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pfv_storage import LocalBackend, StorageBackend, open_storage


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PFVError(Exception):
    """Base error for all PFV operations."""

class NotAPFVDirectory(PFVError):
    """Path is not a valid PFV versioned-file directory."""

class VersionNotFound(PFVError):
    """Requested version slot does not exist."""

class SlotLocked(PFVError):
    """Operation blocked because a lock file is present."""

class NoContentInSlot(PFVError):
    """Slot has no binary, .link, or .delta content."""

class RepoConflict(PFVError):
    """
    The repo changed between checkout and check-in.
    Attributes:
        checked_out_version : version that was checked out
        current_version     : version that is now latest in the repo
        checked_out_sha     : sha at checkout time
        current_sha         : sha of the current latest version
    """
    def __init__(self, msg: str, checked_out_version: int, current_version: int,
                 checked_out_sha: str, current_sha: str) -> None:
        super().__init__(msg)
        self.checked_out_version = checked_out_version
        self.current_version = current_version
        self.checked_out_sha = checked_out_sha
        self.current_sha = current_sha


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SlotInfo:
    version: int
    has_binary: bool
    has_link: bool
    has_delta: bool
    has_meta: bool
    has_sha: bool
    is_locked: bool
    is_deleted: bool
    tag: Optional[str] = None
    meta: dict = field(default_factory=dict)
    lock: dict = field(default_factory=dict)
    hashes: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        if self.has_binary:
            return "binary"
        if self.has_link:
            return "link"
        if self.has_delta:
            return "delta"
        return "none"


@dataclass
class RepoInfo:
    name: str
    vdir: str               # logical name (e.g. "video.mp4")
    storage: StorageBackend
    latest: int
    head: int
    slots: list[SlotInfo]
    repo_meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers — all storage-aware
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slot_key(vdir: str, version: int, ext: str = "") -> str:
    """Build a storage key for a version slot file."""
    name = str(version) + (f".{ext}" if ext else "")
    return f"{vdir}/{name}"


def _is_pfv_vdir(vdir: str, storage: StorageBackend) -> bool:
    return storage.exists(f"{vdir}/latest")


def _read_slot(vdir: str, version: int, storage: StorageBackend) -> SlotInfo:
    """Read all sidecar files for a slot and return a SlotInfo."""
    def key(ext=""):
        return _slot_key(vdir, version, ext)

    has_binary = storage.exists(key())
    has_link   = storage.exists(key("link"))
    has_delta  = storage.exists(key("delta"))
    has_meta   = storage.exists(key("meta"))
    has_sha    = storage.exists(key("sha"))
    is_locked  = storage.exists(key("lock"))
    has_tag    = storage.exists(key("tag"))

    meta: dict = {}
    if has_meta:
        try:
            meta = storage.read_json(key("meta"))
        except (json.JSONDecodeError, Exception):
            meta = {}

    lock: dict = {}
    if is_locked:
        try:
            lock = storage.read_json(key("lock"))
        except (json.JSONDecodeError, Exception):
            lock = {}

    hashes: list[str] = []
    if has_sha:
        try:
            hashes = [l.strip() for l in storage.read_text(key("sha")).splitlines() if l.strip()]
        except Exception:
            pass

    tag: Optional[str] = None
    if has_tag:
        try:
            tag = storage.read_text(key("tag")).strip()
        except Exception:
            pass

    return SlotInfo(
        version=version,
        has_binary=has_binary,
        has_link=has_link,
        has_delta=has_delta,
        has_meta=has_meta,
        has_sha=has_sha,
        is_locked=is_locked,
        is_deleted=meta.get("deleted", False),
        tag=tag,
        meta=meta,
        lock=lock,
        hashes=hashes,
    )


def _sha256_path(p: Path, chunk: int = 1 << 20) -> str:
    """Hash a local file."""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _default_storage(location: str | Path | StorageBackend | None) -> StorageBackend:
    """Coerce a location argument to a StorageBackend."""
    if location is None:
        return LocalBackend(Path("."))
    if isinstance(location, StorageBackend):
        return location
    return open_storage(location)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
    name: str | None = None,
    owner: str | None = None,
    description: str | None = None,
) -> str:
    """
    Initialise a PFV versioned-file directory named *vdir* in *storage*.

    Parameters
    ----------
    vdir    : logical name, e.g. "my-video.mp4"
    storage : StorageBackend, location string/Path, or None (→ LocalBackend("."))

    Returns *vdir*.
    """
    store = _default_storage(storage)

    if not store.exists(f"{vdir}/latest"):
        store.write_int(f"{vdir}/latest", 0)
    if not store.exists(f"{vdir}/HEAD"):
        store.write_int(f"{vdir}/HEAD", 0)

    if not store.exists(f"{vdir}/.repo.meta"):
        repo_meta: dict = {"name": name or vdir, "created": _now_iso()}
        if owner:
            repo_meta["owner"] = owner
        if description:
            repo_meta["description"] = description
        store.write_json(f"{vdir}/.repo.meta", repo_meta)

    return vdir


def commit(
    vdir: str,
    src: Path,
    storage: str | Path | StorageBackend | None = None,
    author: str | None = None,
    message: str | None = None,
    tag: str | None = None,
    extra_meta: dict | None = None,
) -> int:
    """
    Upload *src* to *storage* as the next version of *vdir*.
    Returns the new version number.
    """
    store = _default_storage(storage)
    src = Path(src)

    if not _is_pfv_vdir(vdir, store):
        raise NotAPFVDirectory(f"{vdir!r} in {store}")
    if not src.is_file():
        raise FileNotFoundError(src)

    # Repo-level lock check
    if store.exists(f"{vdir}/.lock"):
        try:
            info = store.read_json(f"{vdir}/.lock")
        except Exception:
            info = {}
        raise SlotLocked(f"Repository locked by {info.get('holder', 'unknown')}")

    latest = store.read_int(f"{vdir}/latest")
    version = latest + 1

    # Tag uniqueness warning
    if tag:
        for v in range(1, latest + 1):
            tag_key = _slot_key(vdir, v, "tag")
            if store.exists(tag_key):
                try:
                    existing = store.read_text(tag_key).strip()
                    if existing == tag:
                        print(f"Warning: tag '{tag}' already used by version {v}")
                except Exception:
                    pass

    # Upload binary
    binary_key = _slot_key(vdir, version)
    store.upload_from(binary_key, src)

    # Hash the source file (local hash before upload, matches content)
    digest = _sha256_path(src)
    store.write_text(_slot_key(vdir, version, "sha"), f"sha256:{digest}\n")

    # Meta
    meta: dict = {
        "timestamp": _now_iso(),
        "size": src.stat().st_size,
    }
    if author:
        meta["author"] = author
    if message:
        meta["message"] = message
    if extra_meta:
        meta.update(extra_meta)
    store.write_json(_slot_key(vdir, version, "meta"), meta)

    # Tag
    if tag:
        store.write_text(_slot_key(vdir, version, "tag"), tag)

    # Advance latest and HEAD
    store.write_int(f"{vdir}/latest", version)
    store.write_int(f"{vdir}/HEAD", version)

    return version


def log(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
) -> RepoInfo:
    """Return a RepoInfo describing all version slots in *vdir*."""
    store = _default_storage(storage)

    if not _is_pfv_vdir(vdir, store):
        raise NotAPFVDirectory(f"{vdir!r} in {store}")

    latest = store.read_int(f"{vdir}/latest")
    head   = store.read_int(f"{vdir}/HEAD")

    repo_meta: dict = {}
    if store.exists(f"{vdir}/.repo.meta"):
        try:
            repo_meta = store.read_json(f"{vdir}/.repo.meta")
        except Exception:
            pass

    slots = [_read_slot(vdir, v, store) for v in range(1, latest + 1)]

    return RepoInfo(
        name=vdir,
        vdir=vdir,
        storage=store,
        latest=latest,
        head=head,
        slots=slots,
        repo_meta=repo_meta,
    )


def checkout(
    vdir: str,
    version: int | str,
    dest: Path,
    storage: str | Path | StorageBackend | None = None,
) -> Path:
    """
    Download *version* from *vdir* to *dest* (a local Path).
    *version* may be an integer, "latest", "HEAD", or a tag name.
    Returns dest.
    """
    store = _default_storage(storage)
    dest = Path(dest)

    if not _is_pfv_vdir(vdir, store):
        raise NotAPFVDirectory(f"{vdir!r} in {store}")

    version = _resolve_version(vdir, version, store)
    slot = _read_slot(vdir, version, store)

    if slot.is_deleted:
        raise VersionNotFound(f"Version {version} has been deleted (tombstone only)")

    dest.parent.mkdir(parents=True, exist_ok=True)

    if slot.has_binary:
        store.download_to(_slot_key(vdir, version), dest)
    elif slot.has_link:
        link_text = store.read_text(_slot_key(vdir, version, "link")).strip()
        # .link can be a local path or a remote URI; try local first
        link_path = Path(link_text)
        if link_path.is_file():
            import shutil
            shutil.copy2(link_path, dest)
        else:
            raise FileNotFoundError(
                f".link target not found or not a local path: {link_text!r}. "
                "Remote link targets require manual resolution."
            )
    elif slot.has_delta:
        raise PFVError("Delta reconstruction not implemented; use a full-binary slot")
    else:
        raise NoContentInSlot(f"Version {version} has no restorable content")

    # Update HEAD
    store.write_int(f"{vdir}/HEAD", version)
    return dest


def lock(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
    version: int | None = None,
    holder: str | None = None,
    reason: str | None = None,
    expires: str | None = None,
) -> str:
    """
    Lock *vdir* (version=None → repo-level) or a specific slot.
    Returns the storage key of the created .lock object.
    """
    store = _default_storage(storage)
    if not _is_pfv_vdir(vdir, store):
        raise NotAPFVDirectory(f"{vdir!r} in {store}")

    info: dict = {
        "holder": holder or os.environ.get("USER", os.environ.get("USERNAME", "unknown")),
        "acquired": _now_iso(),
        "host": socket.gethostname(),
    }
    if reason:
        info["reason"] = reason
    if expires:
        info["expires"] = expires

    if version is None:
        lock_key = f"{vdir}/.lock"
    else:
        version = _resolve_version(vdir, version, store)
        lock_key = _slot_key(vdir, version, "lock")

    store.write_json(lock_key, info)
    return lock_key


def unlock(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
    version: int | None = None,
) -> bool:
    """Remove a lock. Returns True if one existed."""
    store = _default_storage(storage)

    if version is None:
        lock_key = f"{vdir}/.lock"
    else:
        version = _resolve_version(vdir, version, store)
        lock_key = _slot_key(vdir, version, "lock")

    if store.exists(lock_key):
        store.delete(lock_key)
        return True
    return False


def delete_version(
    vdir: str,
    version: int | str,
    storage: str | Path | StorageBackend | None = None,
) -> None:
    """
    Tombstone a version: remove binary/link/delta, mark meta as deleted.
    Preserves .meta and .sha for the audit trail.
    """
    store = _default_storage(storage)
    if not _is_pfv_vdir(vdir, store):
        raise NotAPFVDirectory(f"{vdir!r} in {store}")

    version = _resolve_version(vdir, version, store)
    slot = _read_slot(vdir, version, store)

    if slot.is_locked:
        raise SlotLocked(f"Version {version} is locked; unlock before deleting")

    for ext in ("", "link", "delta"):
        key = _slot_key(vdir, version, ext) if ext else _slot_key(vdir, version)
        if store.exists(key):
            store.delete(key)

    meta = slot.meta.copy()
    meta["deleted"] = True
    meta["deleted_at"] = _now_iso()
    store.write_json(_slot_key(vdir, version, "meta"), meta)


def flatten(
    vdir: str,
    dest_dir: Path,
    storage: str | Path | StorageBackend | None = None,
    prefix: str | None = None,
) -> list[Path]:
    """
    Download all non-deleted versions from *vdir* to plain files in *dest_dir*.
    Files are named: <name>_v<N>[_<tag>].<ext>
    Returns list of local Paths written.
    """
    store = _default_storage(storage)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not _is_pfv_vdir(vdir, store):
        raise NotAPFVDirectory(f"{vdir!r} in {store}")

    info = log(vdir, store)

    # Derive base name + extension from vdir name (e.g. "my-video.mp4")
    dir_name = vdir.rstrip("/").split("/")[-1]
    if "." in dir_name:
        dot = dir_name.rfind(".")
        base_name = prefix or dir_name[:dot]
        file_ext  = dir_name[dot:]
    else:
        base_name = prefix or dir_name
        file_ext  = ""

    written = []
    for slot in info.slots:
        if slot.is_deleted:
            continue
        tag_suffix = f"_{slot.tag}" if slot.tag else ""
        out_name = f"{base_name}_v{slot.version}{tag_suffix}{file_ext}"
        out_path = dest_dir / out_name
        try:
            checkout(vdir, slot.version, out_path, store)
            written.append(out_path)
        except (NoContentInSlot, FileNotFoundError) as e:
            print(f"  Warning: skipping v{slot.version} — {e}")

    return written


def verify(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
) -> list[str]:
    """
    Verify SHA hashes for all binary slots.
    Returns a list of error strings; empty list means all OK.

    Note: for remote backends this downloads each binary to verify it.
    """
    store = _default_storage(storage)
    if not _is_pfv_vdir(vdir, store):
        raise NotAPFVDirectory(f"{vdir!r} in {store}")

    info = log(vdir, store)
    errors: list[str] = []

    for slot in info.slots:
        if slot.is_deleted or not slot.has_binary:
            continue
        if not slot.has_sha:
            errors.append(f"v{slot.version}: no .sha file")
            continue

        # Download to a temp file to hash
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            store.download_to(_slot_key(vdir, slot.version), tmp_path)
            actual = _sha256_path(tmp_path)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

        for entry in slot.hashes:
            if entry.startswith("sha256:"):
                expected = entry[len("sha256:"):]
                if actual != expected:
                    errors.append(
                        f"v{slot.version}: SHA256 mismatch "
                        f"(expected {expected[:12]}…, got {actual[:12]}…)"
                    )
                break

    return errors


def prune(
    vdir: str,
    keep: int,
    storage: str | Path | StorageBackend | None = None,
) -> list[int]:
    """
    Tombstone oldest versions beyond *keep* count (unlocked, non-deleted only).
    Returns list of versions pruned.
    """
    store = _default_storage(storage)
    info = log(vdir, store)

    candidates = [
        s for s in info.slots
        if not s.is_deleted and not s.is_locked and s.has_binary
    ]
    to_prune = candidates[: max(0, len(candidates) - keep)]
    pruned = []
    for slot in to_prune:
        delete_version(vdir, slot.version, store)
        pruned.append(slot.version)
    return pruned


# ---------------------------------------------------------------------------
# Tracked checkout / checkin (workspace state via pfv_state backends)
# ---------------------------------------------------------------------------

def tracked_checkout(
    vdir: str,
    version: int | str,
    dest: Path,
    storage: str | Path | StorageBackend | None = None,
    work_tree: Path | None = None,
    state_backend: str = "json",
    **state_kwargs: Any,
) -> tuple[Path, int]:
    """
    Like checkout(), but records the operation in the work-tree's state backend
    so checkin() can detect whether the repo moved on.

    Returns (dest_path, resolved_version_number).
    """
    from pfv_state import open_backend, make_record

    store = _default_storage(storage)
    dest = Path(dest)
    work_tree = Path(work_tree) if work_tree else dest.parent

    resolved = _resolve_version(vdir, version, store)
    checkout(vdir, resolved, dest, store)

    # Get SHA recorded in the repo for this slot
    sha_key = _slot_key(vdir, resolved, "sha")
    sha_at_checkout = ""
    if store.exists(sha_key):
        for line in store.read_text(sha_key).splitlines():
            if line.startswith("sha256:"):
                sha_at_checkout = line[len("sha256:"):]
                break
    if not sha_at_checkout:
        sha_at_checkout = _sha256_path(dest)

    record = make_record(dest, Path(vdir), resolved, sha_at_checkout)
    record.extra["storage_repr"] = repr(store)  # informational; not used for reconstruction

    state = open_backend(work_tree, backend=state_backend, **state_kwargs)
    try:
        state.upsert(record)
    finally:
        state.close()

    return dest, resolved


def checkin(
    dest: Path,
    storage: str | Path | StorageBackend | None = None,
    work_tree: Path | None = None,
    author: str | None = None,
    message: str | None = None,
    tag: str | None = None,
    extra_meta: dict | None = None,
    state_backend: str = "json",
    **state_kwargs: Any,
) -> int:
    """
    Commit the file at *dest* back to its PFV repo after verifying no conflict.
    Raises RepoConflict if the repo moved on since checkout.
    Returns the new version number.
    """
    from pfv_state import open_backend

    store = _default_storage(storage)
    dest = Path(dest).resolve()
    work_tree = Path(work_tree).resolve() if work_tree else dest.parent

    state = open_backend(work_tree, backend=state_backend, **state_kwargs)
    try:
        record = state.get(str(dest))
        if record is None:
            raise PFVError(
                f"{dest} is not tracked. Use tracked_checkout() before checkin()."
            )

        vdir = record.vdir

        if not _is_pfv_vdir(vdir, store):
            raise NotAPFVDirectory(f"{vdir!r} in {store}")

        # Conflict check
        current_latest = store.read_int(f"{vdir}/latest")
        current_slot   = _read_slot(vdir, current_latest, store)

        current_sha = ""
        for entry in current_slot.hashes:
            if entry.startswith("sha256:"):
                current_sha = entry[len("sha256:"):]
                break

        if current_latest != record.version or current_sha != record.sha_at_checkout:
            raise RepoConflict(
                f"Repo conflict: checked out v{record.version} "
                f"(sha {record.sha_at_checkout[:12]}…) but repo is now "
                f"v{current_latest} (sha {current_sha[:12] if current_sha else '?'}…). "
                "Resolve the conflict before checking in.",
                checked_out_version=record.version,
                current_version=current_latest,
                checked_out_sha=record.sha_at_checkout,
                current_sha=current_sha,
            )

        new_version = commit(vdir, dest, store, author=author, message=message,
                             tag=tag, extra_meta=extra_meta)
        state.remove(str(dest))
        return new_version

    finally:
        state.close()


def status(
    work_tree: Path,
    storage: str | Path | StorageBackend | None = None,
    state_backend: str = "json",
    **state_kwargs: Any,
) -> list[dict]:
    """
    Return status dicts for all tracked checkouts under *work_tree*.

    Each dict includes: dest, vdir, version, sha_at_checkout, repo_sha,
    repo_version, working_sha, file_modified, repo_moved, conflict,
    checked_out_at, checked_out_by, host.
    """
    from pfv_state import open_backend

    store = _default_storage(storage)
    work_tree = Path(work_tree).resolve()
    state = open_backend(work_tree, backend=state_backend, **state_kwargs)
    try:
        records = state.list_all()
    finally:
        state.close()

    results = []
    for rec in records:
        dest = Path(rec.dest)
        vdir = rec.vdir

        working_sha = _sha256_path(dest) if dest.exists() else None

        repo_sha = ""
        repo_version = rec.version
        try:
            repo_version = store.read_int(f"{vdir}/latest")
            slot = _read_slot(vdir, repo_version, store)
            for entry in slot.hashes:
                if entry.startswith("sha256:"):
                    repo_sha = entry[len("sha256:"):]
                    break
        except Exception:
            pass

        file_modified = working_sha is not None and working_sha != rec.sha_at_checkout
        repo_moved    = repo_version != rec.version or repo_sha != rec.sha_at_checkout

        results.append({
            "dest":             str(dest),
            "vdir":             vdir,
            "version":          rec.version,
            "sha_at_checkout":  rec.sha_at_checkout,
            "repo_sha":         repo_sha,
            "repo_version":     repo_version,
            "working_sha":      working_sha,
            "file_modified":    file_modified,
            "repo_moved":       repo_moved,
            "conflict":         file_modified and repo_moved,
            "checked_out_at":   rec.checked_out_at,
            "checked_out_by":   rec.checked_out_by,
            "host":             rec.host,
        })

    return results


def abandon(
    dest: Path,
    work_tree: Path | None = None,
    state_backend: str = "json",
    **state_kwargs: Any,
) -> bool:
    """Drop the checkout record for *dest* without committing."""
    from pfv_state import open_backend

    dest = Path(dest).resolve()
    work_tree = Path(work_tree).resolve() if work_tree else dest.parent
    state = open_backend(work_tree, backend=state_backend, **state_kwargs)
    try:
        return state.remove(str(dest))
    finally:
        state.close()


# ---------------------------------------------------------------------------
# Directory import
# ---------------------------------------------------------------------------

# Ignore helpers live in pfv_collection (the layer that walks filesystems)
from pfv_collection import load_pfvignore as _load_pfvignore, is_ignored as _is_ignored


def import_dir(
    source_dir: str | Path,
    storage: str | Path | StorageBackend | None = None,
    owner: str | None = None,
    description: str | None = None,
    author: str | None = None,
    message: str | None = None,
    progress_cb=None,
) -> list[str]:
    """
    Import an existing directory tree into a PFV storage backend.

    Each file in *source_dir* becomes a separate vdir (its relative path
    with path separators replaced by '/').  If `.pfvignore` or
    `.pfvignore.txt` exists in the root of *source_dir* its fnmatch patterns
    are used to exclude files and directories.

    Parameters
    ----------
    source_dir  : root of the directory tree to import
    storage     : StorageBackend, location string/Path, or None (→ LocalBackend("."))
    owner       : stored in each vdir's .repo.meta
    description : stored in each vdir's .repo.meta
    author      : stored in the initial commit's .meta
    message     : commit message for the initial version of every file
    progress_cb : optional callable(rel_path_str, vdir, version) called per file

    Returns a list of vdir names that were created/committed.
    """
    source_dir = Path(source_dir).resolve()
    if not source_dir.is_dir():
        raise NotADirectoryError(f"source_dir is not a directory: {source_dir}")

    store = _default_storage(storage)
    patterns = _load_pfvignore(source_dir)

    created: list[str] = []

    for abs_path in sorted(source_dir.rglob("*")):
        if not abs_path.is_file():
            continue

        rel = abs_path.relative_to(source_dir)

        # Always skip the ignore files themselves
        if rel.name in (".pfvignore", ".pfvignore.txt"):
            continue

        if _is_ignored(rel, patterns):
            continue

        # Use POSIX-style relative path as the vdir name
        vdir = rel.as_posix()

        init(
            vdir,
            storage=store,
            name=vdir,
            owner=owner,
            description=description,
        )
        version = commit(
            vdir,
            abs_path,
            storage=store,
            author=author,
            message=message or f"Imported from {source_dir.name}",
        )

        created.append(vdir)
        if progress_cb:
            progress_cb(str(rel), vdir, version)

    return created


# ---------------------------------------------------------------------------
# Internal: version resolution
# ---------------------------------------------------------------------------

def _resolve_version(vdir: str, version: int | str, storage: StorageBackend) -> int:
    if isinstance(version, int):
        return version

    v = str(version).strip()
    if v == "latest":
        return storage.read_int(f"{vdir}/latest")
    if v == "HEAD":
        return storage.read_int(f"{vdir}/HEAD")
    if v.isdigit():
        return int(v)

    # Tag lookup
    latest = storage.read_int(f"{vdir}/latest")
    for n in range(1, latest + 1):
        tag_key = _slot_key(vdir, n, "tag")
        if storage.exists(tag_key):
            try:
                if storage.read_text(tag_key).strip() == v:
                    return n
            except Exception:
                pass

    raise VersionNotFound(f"Cannot resolve version: {version!r}")
