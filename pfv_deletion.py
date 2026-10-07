"""
pfv_deletion.py — File deletion and renaming support for PFV

Extends PFV with explicit deletion and renaming operations that create
marker files (.deleted, .renamed) to signal changes to the file identity.

This complements the existing delete_version() which tombstones a specific
version. These operations mark files as deleted/renamed in the working file tree.

Operations
----------
  mark_deleted(vdir, storage, author, message, reason)
    → Creates a new version with a .deleted marker
    → Indicates the logical file has been removed
    → New version number is returned

  mark_renamed(vdir, new_name, storage, author, message, reason)
    → Creates a new version with a .renamed marker
    → Contains the new file name/path
    → Returns the new version number

  undelete(vdir, storage, author, message)
    → Reverses a deletion by creating a new unmarked version
    → Requires a previous deleted version to restore from
    → Returns the new version number

File format
-----------
  {vdir}/N.deleted
    Empty marker file indicating version N is deleted.
    .meta still contains deletion details (author, timestamp, reason).

  {vdir}/N.renamed
    Text file containing the new file name/path.
    .meta contains renaming details (author, old_name, new_name).

Usage
-----
  from pfv_deletion import mark_deleted, mark_renamed, undelete
  from pfv import log
  from pfv_storage import open_storage

  store = open_storage(".")

  # Mark file as deleted
  new_ver = mark_deleted("project.mp4", storage=store,
                         author="alice",
                         message="Project archived",
                         reason="Completed Q1 2024")

  # Check deletion in history
  info = log("project.mp4", store)
  print(f"Latest version {info.latest} is deleted: {info.slots[-1].is_deleted}")

  # Mark file as renamed
  new_ver = mark_renamed("old_name.psd", "new_name.psd",
                         storage=store,
                         author="bob",
                         message="Conform to naming standard")

  # Check rename in history
  info = log("new_name.psd", store)
  # old_name.psd would have a .renamed marker in its history

  # Undelete
  new_ver = undelete("project.mp4", storage=store,
                     author="alice",
                     message="Restoring Q1 project")
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Optional, Any
from datetime import datetime, timezone

# Import from pfv core
try:
    from pfv import (
        _default_storage, _is_pfv_vdir, _slot_key, _read_slot,
        _now_iso, _resolve_version, PFVError, NotAPFVDirectory,
        SlotLocked, commit
    )
    from pfv_storage import StorageBackend
except ImportError as e:
    raise ImportError(
        "pfv_deletion requires pfv.py and pfv_storage.py in Python path"
    ) from e


# ---------------------------------------------------------------------------
# Deletion support
# ---------------------------------------------------------------------------

def mark_deleted(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
    author: str | None = None,
    message: str | None = None,
    reason: str | None = None,
    extra_meta: dict | None = None,
) -> int:
    """
    Create a new version marked as deleted without removing old versions.

    Creates a .deleted marker file to signal the logical file is deleted,
    while preserving history. The old binary/link/delta content is NOT deleted.

    Parameters
    ----------
    vdir : str
        Versioned file directory name
    storage : StorageBackend or path
        Storage location
    author : str
        Author of the deletion (default: current user)
    message : str
        Deletion message (e.g., "File no longer needed")
    reason : str
        Longer explanation (optional)
    extra_meta : dict
        Additional metadata to store

    Returns
    -------
    int
        New version number with .deleted marker

    Raises
    ------
    NotAPFVDirectory
        If vdir is not a valid PFV directory
    """
    store = _default_storage(storage)

    if not _is_pfv_vdir(vdir, store):
        raise NotAPFVDirectory(f"{vdir!r} in {store}")

    # Get current latest version
    latest = store.read_int(f"{vdir}/latest")
    new_version = latest + 1

    # Create deletion marker (empty file)
    deleted_key = _slot_key(vdir, new_version, "deleted")
    store.write_bytes(deleted_key, b"")

    # Write metadata
    author = author or os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
    meta = extra_meta.copy() if extra_meta else {}
    meta.update({
        "author": author,
        "committed_at": _now_iso(),
        "message": message or "(deleted)",
        "deleted": True,
        "deleted_at": _now_iso(),
        "deleted_by": author,
        "host": socket.gethostname(),
    })
    if reason:
        meta["deletion_reason"] = reason

    store.write_json(_slot_key(vdir, new_version, "meta"), meta)

    # Update latest
    store.write_int(f"{vdir}/latest", new_version)

    return new_version


def undelete(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
    author: str | None = None,
    message: str | None = None,
    restore_version: int | str | None = None,
    extra_meta: dict | None = None,
) -> int:
    """
    Restore a deleted file by creating a new unmarked version.

    Requires finding a previous non-deleted version to clone from.
    If restore_version is not specified, uses the version just before deletion.

    Parameters
    ----------
    vdir : str
        Versioned file directory
    storage : StorageBackend or path
        Storage location
    author : str
        Author of the restoration
    message : str
        Restoration message
    restore_version : int or str
        Version to restore from (default: version before deletion)
    extra_meta : dict
        Additional metadata

    Returns
    -------
    int
        New version number with restored content

    Raises
    ------
    PFVError
        If no valid version can be found to restore
    """
    store = _default_storage(storage)

    if not _is_pfv_vdir(vdir, store):
        raise NotAPFVDirectory(f"{vdir!r} in {store}")

    # Get current latest
    latest = store.read_int(f"{vdir}/latest")
    latest_slot = _read_slot(vdir, latest, store)

    if not latest_slot.is_deleted:
        raise PFVError(f"Version {latest} is not deleted; cannot undelete")

    # Find source version to restore from
    if restore_version is not None:
        src_version = _resolve_version(vdir, restore_version, store)
    else:
        # Use the version before the current deleted one
        if latest <= 1:
            raise PFVError("Cannot undelete: no previous version available")
        src_version = latest - 1

    src_slot = _read_slot(vdir, src_version, store)

    if src_slot.is_deleted:
        raise PFVError(f"Version {src_version} is also deleted; cannot use as restore source")

    # Create new version by copying content from source
    new_version = latest + 1

    # Copy binary/link/delta from source
    for ext in ("", "link", "delta"):
        src_key = _slot_key(vdir, src_version, ext) if ext else _slot_key(vdir, src_version)
        dst_key = _slot_key(vdir, new_version, ext) if ext else _slot_key(vdir, new_version)

        if store.exists(src_key):
            store.copy(src_key, dst_key)

    # Copy SHA hashes
    src_sha_key = _slot_key(vdir, src_version, "sha")
    dst_sha_key = _slot_key(vdir, new_version, "sha")
    if store.exists(src_sha_key):
        store.copy(src_sha_key, dst_sha_key)

    # Write new metadata (unmarked)
    author = author or os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
    meta = extra_meta.copy() if extra_meta else {}
    meta.update({
        "author": author,
        "committed_at": _now_iso(),
        "message": message or "(restored from deletion)",
        "restored_from_version": src_version,
        "restored_at": _now_iso(),
        "restored_by": author,
        "host": socket.gethostname(),
    })

    store.write_json(_slot_key(vdir, new_version, "meta"), meta)

    # Update latest
    store.write_int(f"{vdir}/latest", new_version)

    return new_version


# ---------------------------------------------------------------------------
# Rename support
# ---------------------------------------------------------------------------

def mark_renamed(
    vdir: str,
    new_name: str,
    storage: str | Path | StorageBackend | None = None,
    author: str | None = None,
    message: str | None = None,
    extra_meta: dict | None = None,
) -> int:
    """
    Create a new version marked as renamed.

    Creates a .renamed marker file containing the new name/path.
    The old binary/link/delta is NOT deleted; you may need to manually
    handle the old vdir or leave it for history.

    Parameters
    ----------
    vdir : str
        Current versioned file directory name
    new_name : str
        New file name/path (e.g., "new_project.mp4")
    storage : StorageBackend or path
        Storage location
    author : str
        Author of the rename
    message : str
        Rename message
    extra_meta : dict
        Additional metadata

    Returns
    -------
    int
        New version number in the old vdir with .renamed marker

    Notes
    -----
    The physical directory is NOT renamed; you should:
    1. Call mark_renamed() on old vdir
    2. Initialize new vdir with init()
    3. Copy latest non-renamed version to new vdir
    4. Both old and new vdir maintain separate version histories
    5. The .renamed marker is the audit trail
    """
    store = _default_storage(storage)

    if not _is_pfv_vdir(vdir, store):
        raise NotAPFVDirectory(f"{vdir!r} in {store}")

    # Get current latest
    latest = store.read_int(f"{vdir}/latest")
    new_version = latest + 1

    # Create .renamed marker with new name as content
    renamed_key = _slot_key(vdir, new_version, "renamed")
    store.write_text(renamed_key, new_name)

    # Write metadata
    author = author or os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
    meta = extra_meta.copy() if extra_meta else {}
    meta.update({
        "author": author,
        "committed_at": _now_iso(),
        "message": message or f"Renamed to {new_name}",
        "renamed": True,
        "old_name": vdir,
        "new_name": new_name,
        "renamed_at": _now_iso(),
        "renamed_by": author,
        "host": socket.gethostname(),
    })

    store.write_json(_slot_key(vdir, new_version, "meta"), meta)

    # Update latest in OLD vdir (marks it as renamed)
    store.write_int(f"{vdir}/latest", new_version)

    return new_version


def migrate_renamed(
    old_vdir: str,
    new_vdir: str,
    storage: str | Path | StorageBackend | None = None,
    author: str | None = None,
    message: str | None = None,
) -> int:
    """
    Complete a rename operation by migrating content to new vdir.

    Steps:
    1. Calls mark_renamed() on old vdir (if not already done)
    2. Initializes new vdir
    3. Copies latest version from old to new
    4. Returns new version in new vdir

    Parameters
    ----------
    old_vdir : str
        Original versioned file directory
    new_vdir : str
        New versioned file directory
    storage : StorageBackend or path
        Storage location
    author : str
        Author of the migration
    message : str
        Rename message

    Returns
    -------
    int
        New version number in the new vdir
    """
    from pfv import init

    store = _default_storage(storage)

    if not _is_pfv_vdir(old_vdir, store):
        raise NotAPFVDirectory(f"{old_vdir!r} in {store}")

    author = author or os.environ.get("USER", os.environ.get("USERNAME", "unknown"))

    # Mark old as renamed
    mark_renamed(old_vdir, new_vdir, storage=store, author=author,
                 message=message or f"Renamed to {new_vdir}")

    # Initialize new vdir if needed
    if not _is_pfv_vdir(new_vdir, store):
        init(new_vdir, storage=store)

    # Get latest version from old
    old_latest = store.read_int(f"{old_vdir}/latest")
    old_slot = _read_slot(old_vdir, old_latest, store)

    # Skip if old version is deleted or already renamed
    if old_slot.is_deleted or old_slot.meta.get("renamed"):
        # Find last good version
        for v in range(old_latest - 1, 0, -1):
            slot = _read_slot(old_vdir, v, store)
            if not slot.is_deleted and not slot.meta.get("renamed"):
                old_latest = v
                break

    # Copy latest version from old to new
    new_latest = store.read_int(f"{new_vdir}/latest")
    new_version = new_latest + 1

    # Copy content
    for ext in ("", "link", "delta"):
        old_key = _slot_key(old_vdir, old_latest, ext) if ext else _slot_key(old_vdir, old_latest)
        new_key = _slot_key(new_vdir, new_version, ext) if ext else _slot_key(new_vdir, new_version)
        if store.exists(old_key):
            store.copy(old_key, new_key)

    # Copy SHA
    old_sha = _slot_key(old_vdir, old_latest, "sha")
    new_sha = _slot_key(new_vdir, new_version, "sha")
    if store.exists(old_sha):
        store.copy(old_sha, new_sha)

    # Copy metadata with migration note
    old_meta = old_slot.meta.copy()
    old_meta.update({
        "migrated_from": old_vdir,
        "migrated_at": _now_iso(),
        "migrated_by": author,
    })

    store.write_json(_slot_key(new_vdir, new_version, "meta"), old_meta)

    # Update new vdir latest
    store.write_int(f"{new_vdir}/latest", new_version)

    return new_version


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def is_deleted(vdir: str, version: int | None = None,
               storage: str | Path | StorageBackend | None = None) -> bool:
    """Check if a version (or latest) is marked as deleted."""
    store = _default_storage(storage)

    if not _is_pfv_vdir(vdir, store):
        return False

    if version is None:
        version = store.read_int(f"{vdir}/latest")

    slot = _read_slot(vdir, version, store)
    return slot.is_deleted


def is_renamed(vdir: str, version: int | None = None,
               storage: str | Path | StorageBackend | None = None) -> bool:
    """Check if a version (or latest) has a rename marker."""
    store = _default_storage(storage)

    if not _is_pfv_vdir(vdir, store):
        return False

    if version is None:
        version = store.read_int(f"{vdir}/latest")

    renamed_key = _slot_key(vdir, version, "renamed")
    return store.exists(renamed_key)


def get_renamed_to(vdir: str, version: int | None = None,
                   storage: str | Path | StorageBackend | None = None) -> str | None:
    """Return new name if version is renamed, else None."""
    store = _default_storage(storage)

    if not _is_pfv_vdir(vdir, store):
        return None

    if version is None:
        version = store.read_int(f"{vdir}/latest")

    renamed_key = _slot_key(vdir, version, "renamed")
    if store.exists(renamed_key):
        return store.read_text(renamed_key).strip()
    return None


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

def list_deleted_files(
    storage: str | Path | StorageBackend | None = None,
) -> list[dict]:
    """
    List all vdirs where the latest version is marked deleted.

    Returns list of dicts with: vdir, version, author, timestamp, reason
    """
    store = _default_storage(storage)
    results = []

    try:
        keys = store.list_keys()
        vdirs = set()
        for key in keys:
            if key.endswith("/latest"):
                vdir = key.replace("/latest", "")
                vdirs.add(vdir)

        for vdir in sorted(vdirs):
            if _is_pfv_vdir(vdir, store):
                latest = store.read_int(f"{vdir}/latest")
                slot = _read_slot(vdir, latest, store)

                if slot.is_deleted:
                    results.append({
                        "vdir": vdir,
                        "version": latest,
                        "author": slot.meta.get("deleted_by", "unknown"),
                        "deleted_at": slot.meta.get("deleted_at", ""),
                        "reason": slot.meta.get("deletion_reason", ""),
                        "message": slot.meta.get("message", ""),
                    })
    except Exception:
        pass

    return results


def list_renamed_files(
    storage: str | Path | StorageBackend | None = None,
) -> list[dict]:
    """
    List all vdirs where the latest version has a rename marker.

    Returns list of dicts with: old_vdir, new_name, version, author, timestamp
    """
    store = _default_storage(storage)
    results = []

    try:
        keys = store.list_keys()
        vdirs = set()
        for key in keys:
            if key.endswith("/latest"):
                vdir = key.replace("/latest", "")
                vdirs.add(vdir)

        for vdir in sorted(vdirs):
            if _is_pfv_vdir(vdir, store):
                latest = store.read_int(f"{vdir}/latest")
                renamed_key = _slot_key(vdir, latest, "renamed")

                if store.exists(renamed_key):
                    new_name = store.read_text(renamed_key).strip()
                    slot = _read_slot(vdir, latest, store)
                    results.append({
                        "old_vdir": vdir,
                        "new_name": new_name,
                        "version": latest,
                        "author": slot.meta.get("renamed_by", "unknown"),
                        "renamed_at": slot.meta.get("renamed_at", ""),
                        "message": slot.meta.get("message", ""),
                    })
    except Exception:
        pass

    return results
