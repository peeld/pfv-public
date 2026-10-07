"""
pfv_app.py — backend facade for the PFV C++ app

The C++ GUI (src/ at the repo root) embeds Python and calls into this module
only, through dispatch(name, args_json) -> result_json. Everything crossing
the boundary is JSON, so C++ never touches Python objects, and every call can
run on any thread (C++ takes the GIL around it).

    dispatch("history", '{"vdir": "a.psd"}')
    -> '{"ok": true, "result": {"latest": 3, ...}}'
    -> '{"ok": false, "error": "...", "traceback": "..."}'

State is the open workspace (work tree) and storage, set by open_workspace()
and connect*(). Each command reads them once at the start, so a command
running on a worker thread keeps using the storage it started with even if
the GUI connects to another one meanwhile.

Formatting (sizes, dates, status icons) is the GUI's job: results carry raw
values.
"""

from __future__ import annotations

import getpass
import json
import os
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

import pfv
from pfv_config import PFVConfig
from pfv_session import PFVSession
from pfv_storage import open_storage

try:
    import pfv_deletion
    HAS_DELETION_SUPPORT = True
except ImportError:
    pfv_deletion = None
    HAS_DELETION_SUPPORT = False

# Keys the S3 dialog can pass to open_storage() that are secrets. They're used
# for the connection but never written to session.json (storage_kwargs() never
# read them back anyway).
_SECRET_KWARGS = ("aws_access_key_id", "aws_secret_access_key")

_lock = threading.Lock()
_work_tree: Path | None = None
_storage = None
_location = ""


def _author() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def _current():
    """(work_tree, storage) as of now."""
    with _lock:
        return _work_tree, _storage


def _need_storage():
    work_tree, storage = _current()
    if storage is None:
        raise RuntimeError("No repo connected")
    return work_tree, storage


def _need_work_tree() -> Path:
    work_tree, _ = _current()
    if work_tree is None:
        raise RuntimeError("No workspace open")
    return work_tree


def _vdirs(storage) -> list[str]:
    return sorted({k[: -len("/latest")] for k in storage.list_keys() if k.endswith("/latest")})


def _slot_dict(slot) -> dict:
    meta = slot.meta or {}
    return {
        "version": slot.version,
        "content_type": slot.content_type,
        "is_locked": slot.is_locked,
        "is_deleted": slot.is_deleted,
        "tag": slot.tag or "",
        "author": meta.get("author", ""),
        "message": meta.get("message", ""),
        "has_meta": bool(slot.meta),
    }


# ---------------------------------------------------------------------------
# Workspaces and repo links
# ---------------------------------------------------------------------------

def info() -> dict:
    """What the GUI needs to know about this backend."""
    return {"deletion": HAS_DELETION_SUPPORT}


def workspaces() -> list[dict]:
    """Recent workspaces from ~/.pfvconfig, most recent first."""
    try:
        items = PFVConfig().sorted_by_recent()
    except Exception:
        return []
    return [{"name": ws.get("name") or ws["path"], "path": ws["path"]} for ws in items]


def init_workspace(path: str) -> dict:
    """Create <path>/.pfv/session.json (and <path>) if missing.
    Returns {"created": bool}."""
    p = Path(path)
    session_file = p / ".pfv" / "session.json"
    if session_file.exists():
        return {"created": False}
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text('{"repos": []}\n', encoding="utf-8")
    return {"created": True}


def open_workspace(path: str) -> dict:
    """Make *path* the current work tree and record it in ~/.pfvconfig.
    Disconnects storage; the GUI connects next. Returns the resolved path."""
    global _work_tree, _storage, _location
    work_tree = Path(path).resolve()
    try:
        cfg = PFVConfig()
        if not cfg.touch(work_tree):
            cfg.register(work_tree)
        cfg.save()
    except Exception:
        pass
    with _lock:
        _work_tree, _storage, _location = work_tree, None, ""
    return {"path": str(work_tree)}


def repos() -> list[dict]:
    """Repo links in the current workspace's session.json."""
    work_tree, _ = _current()
    if work_tree is None:
        return []
    try:
        session = PFVSession.load(work_tree)
    except Exception:
        return []
    return [
        {"name": r.get("name", r["storage"]), "storage": r["storage"], "default": bool(r.get("default"))}
        for r in session.repos
    ]


def connect(location: str, kwargs: dict | None = None) -> dict:
    """Open storage at *location* (local path or s3://...)."""
    global _storage, _location
    storage = open_storage(location, **(kwargs or {}))
    with _lock:
        _storage, _location = storage, location
    return {"location": location}


def connect_repo(name: str | None = None) -> dict:
    """Connect to the named repo link (default: the session's default).
    Returns {"location": ...}, or {"location": None} if the session has no
    default repo."""
    work_tree = _need_work_tree()
    session = PFVSession.load(work_tree)
    if name is None and session.default_repo is None:
        return {"location": None}
    location, kwargs = session.storage_kwargs(name)
    return connect(location, kwargs)


def add_repo(name: str, location: str, kwargs: dict | None = None) -> dict:
    """Save a repo link to session.json (the first one becomes the default)."""
    work_tree = _need_work_tree()
    session = PFVSession.load(work_tree)
    if not name:
        name = location.rstrip("/").split("/")[-1] or "repo"
    keep = {k: v for k, v in (kwargs or {}).items() if k not in _SECRET_KWARGS}
    session.add_repo(name, location, default=len(session.repos) == 0, **keep)
    session.save()
    return {"name": name}


def remove_repo(name: str) -> dict:
    work_tree = _need_work_tree()
    session = PFVSession.load(work_tree)
    removed = session.remove_repo(name)
    session.save()
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def repo_view() -> dict:
    """Every vdir in storage with its latest version and checkout status.
    Per-vdir failures are returned in "errors" rather than failing the call."""
    work_tree, storage = _need_storage()
    vdirs = _vdirs(storage)
    stat_entries = pfv.status(work_tree, storage=storage) if (work_tree and vdirs) else []
    by_vdir: dict[str, dict] = {}
    for entry in stat_entries:
        by_vdir.setdefault(entry["vdir"], entry)

    files, errors = [], []
    for vdir in vdirs:
        try:
            repo_info = pfv.log(vdir, storage=storage)
            latest = next((s for s in repo_info.slots if s.version == repo_info.latest), None)
            meta = (latest.meta if latest else None) or {}
            st = by_vdir.get(vdir) or {}
            files.append({
                "vdir": vdir,
                "version": repo_info.latest,
                "size": meta.get("size", 0) or 0,
                # commit() writes "timestamp"; "committed_at" is what the old GUI read
                "committed_at": meta.get("timestamp") or meta.get("committed_at", ""),
                "is_locked": bool(latest and latest.is_locked),
                "conflict": bool(st.get("conflict")),
                "file_modified": bool(st.get("file_modified")),
                "repo_moved": bool(st.get("repo_moved")),
            })
        except Exception as e:
            errors.append({"vdir": vdir, "error": str(e)})
    return {"files": files, "errors": errors}


def workspace_view() -> dict:
    """Files under the work tree (hidden files and folders skipped), each
    flagged with whether a vdir of the same relative path exists."""
    work_tree, storage = _current()
    if work_tree is None or not work_tree.is_dir():
        raise RuntimeError("No valid work-tree directory set")
    known: set[str] = set()
    if storage is not None:
        try:
            known = set(_vdirs(storage))
        except Exception:
            pass
    files = []
    for dirpath, dirnames, filenames in os.walk(work_tree):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            abs_path = Path(dirpath) / filename
            rel_path = abs_path.relative_to(work_tree).as_posix()
            st = abs_path.stat()
            files.append({
                "rel_path": rel_path,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "in_repo": rel_path in known,
            })
    return {"files": files}


def history(vdir: str) -> dict:
    _, storage = _need_storage()
    repo_info = pfv.log(vdir, storage=storage)
    return {
        "vdir": vdir,
        "latest": repo_info.latest,
        "head": repo_info.head,
        "slots": [_slot_dict(s) for s in repo_info.slots],
    }


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def checkout(vdir: str, version: str, dest: str) -> dict:
    work_tree, storage = _need_storage()
    ver: int | str = int(version) if str(version).isdigit() else version
    path, resolved = pfv.tracked_checkout(vdir, ver, Path(dest), storage=storage, work_tree=work_tree)
    return {"dest": str(path), "version": resolved}


def abandon(vdir: str) -> dict:
    """Drop every checkout record of *vdir* in the work tree."""
    work_tree, storage = _need_storage()
    entries = [s for s in pfv.status(work_tree, storage=storage) if s["vdir"] == vdir]
    for entry in entries:
        pfv.abandon(Path(entry["dest"]), work_tree=work_tree)
    return {"dests": [e["dest"] for e in entries]}


def commit_new(rel_path: str) -> dict:
    """Commit an untracked work-tree file as a new vdir named *rel_path*."""
    work_tree, storage = _need_storage()
    src = (work_tree or Path(".")) / rel_path
    if not src.exists():
        raise FileNotFoundError(f"Cannot find {src}")
    author = _author()
    pfv.init(rel_path, storage=storage, owner=author)  # commit() needs the vdir to exist
    v = pfv.commit(rel_path, src, storage=storage, author=author, message="Initial commit via GUI")
    return {"version": v}


def _need_deletion():
    if not HAS_DELETION_SUPPORT:
        raise RuntimeError("Deletion support (pfv_deletion) is not available")


def delete(vdir: str, message: str, reason: str = "") -> dict:
    _need_deletion()
    _, storage = _need_storage()
    v = pfv_deletion.mark_deleted(vdir, storage=storage, author=_author(), message=message, reason=reason)
    return {"version": v}


def rename(vdir: str, new_name: str, message: str) -> dict:
    _need_deletion()
    _, storage = _need_storage()
    v = pfv_deletion.mark_renamed(vdir, new_name, storage=storage, author=_author(), message=message)
    return {"version": v}


def is_deleted(vdir: str) -> dict:
    _need_deletion()
    _, storage = _need_storage()
    return {"deleted": pfv_deletion.is_deleted(vdir, storage=storage)}


def undelete(vdir: str, message: str, restore_version: int | None = None) -> dict:
    _need_deletion()
    _, storage = _need_storage()
    v = pfv_deletion.undelete(vdir, storage=storage, author=_author(), message=message,
                              restore_version=restore_version)
    return {"version": v}


# ---------------------------------------------------------------------------
# Sync: the GUI calls sync_plan() once, then checkin() per modified file, so
# it can report progress between calls.
# ---------------------------------------------------------------------------

def sync_plan() -> dict:
    work_tree, storage = _need_storage()
    statuses = pfv.status(work_tree, storage=storage)
    conflicts = [s["dest"] for s in statuses if s.get("conflict")]
    modified = [s["dest"] for s in statuses if s.get("file_modified") and not s.get("conflict")]
    repo_moved = [s["dest"] for s in statuses if s.get("repo_moved") and not s.get("conflict")]
    return {"tracked": len(statuses), "conflicts": conflicts, "modified": modified, "repo_moved": repo_moved}


def checkin(dest: str) -> dict:
    work_tree, storage = _need_storage()
    v = pfv.checkin(Path(dest), storage=storage, work_tree=work_tree)
    return {"version": v}


# ---------------------------------------------------------------------------
# Entry point for C++
# ---------------------------------------------------------------------------

_COMMANDS: dict[str, Callable[..., Any]] = {
    f.__name__: f
    for f in (
        info, workspaces, init_workspace, open_workspace, repos, connect, connect_repo,
        add_repo, remove_repo, repo_view, workspace_view, history, checkout, abandon,
        commit_new, delete, rename, is_deleted, undelete, sync_plan, checkin,
    )
}


def dispatch(name: str, args_json: str) -> str:
    """Run command *name* with keyword arguments from *args_json*. Never
    raises: failures come back as {"ok": false, "error", "traceback"}."""
    try:
        fn = _COMMANDS.get(name)
        if fn is None:
            raise KeyError(f"Unknown PFV command {name!r}")
        args = json.loads(args_json) if args_json else {}
        return json.dumps({"ok": True, "result": fn(**args)}, default=str)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e) or type(e).__name__,
                           "traceback": traceback.format_exc()})
