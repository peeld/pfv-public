#!/usr/bin/env python3
"""
pfv — Peel File Versions
Filesystem-based versioning for large binary files.

Storage is specified with --storage (default: current directory).
  --storage .                          local filesystem (default)
  --storage /mnt/repos                 local path
  --storage s3://bucket/prefix         Amazon S3
  --storage s3:  --cred-profile s3-prod   S3 via saved credential profile
  --s3-region / --s3-profile / --s3-endpoint for explicit S3 config

Commands
--------
  Repo management
    init      <vdir> [--owner NAME] [--desc TEXT]
    commit    <vdir> <file> [-m MSG] [--author NAME] [--tag LABEL]
    log       <vdir>
    info      <vdir> <version>
    verify    <vdir>
    prune     <vdir> --keep N
    delete    <vdir> <version>
    flatten   <vdir> <dest_dir> [--prefix PREFIX]

  Working copy
    checkout  <vdir> <version> <dest> [--track]
    checkin   <dest> [-m MSG] [--author NAME] [--tag LABEL]
    status    [--work-tree DIR]
    abandon   <dest>

  Locking
    lock      <vdir> [--version V] [--holder NAME] [--reason TEXT] [--expires ISO]
    unlock    <vdir> [--version V]

  Credential management
    credentials set    <profile> [--passphrase PW]  (interactive if omitted)
    credentials get    <profile> [--passphrase PW]  (prints field names only)
    credentials list
    credentials delete <profile> [--passphrase PW]
    credentials test   <profile> [--passphrase PW]  (opens backend and pings it)
"""

import argparse
import getpass
import json
import sys
from pathlib import Path

import pfv
from pfv_storage import open_storage
from pfv_credentials import CredentialStore, find_credential_store


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BLUE   = "\033[34m"
DIM    = "\033[2m"

def _use_color() -> bool:
    return sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"{code}{text}{RESET}" if _use_color() else text

def _ok(msg: str)     -> None: print(_c(GREEN,  "✓ ") + msg)
def _err(msg: str)    -> None: print(_c(RED,    "✗ ") + msg, file=sys.stderr)
def _warn(msg: str)   -> None: print(_c(YELLOW, "⚠ ") + msg)
def _info(msg: str)   -> None: print(_c(BLUE,   "· ") + msg)
def _header(msg: str) -> None: print(_c(BOLD, msg))
def _dim(msg: str)    -> str:  return _c(DIM, msg)

def _human_size(n) -> str:
    try: n = int(n)
    except (ValueError, TypeError): return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def _format_slot(slot: pfv.SlotInfo, head: int, latest: int) -> str:
    markers = []
    if slot.version == head:   markers.append(_c(CYAN,   "HEAD"))
    if slot.version == latest: markers.append(_c(GREEN,  "latest"))
    if slot.is_locked:         markers.append(_c(YELLOW, "LOCKED"))
    if slot.is_deleted:        markers.append(_c(RED,    "DELETED"))

    marker_str  = f"  [{', '.join(markers)}]" if markers else ""
    tag_str     = f"  tag={_c(CYAN, slot.tag)}" if slot.tag else ""
    content_str = _c(DIM, f"  [{slot.content_type}]")

    meta_parts = []
    if slot.meta.get("author"):    meta_parts.append(slot.meta["author"])
    if slot.meta.get("timestamp"): meta_parts.append(slot.meta["timestamp"])
    if slot.meta.get("size"):      meta_parts.append(_human_size(slot.meta["size"]))
    meta_str = _dim("  " + "  ·  ".join(meta_parts)) if meta_parts else ""

    lines = [f"  v{slot.version}{marker_str}{tag_str}{content_str}"]
    if slot.meta.get("message"): lines.append(f"    {slot.meta['message']}")
    if meta_str:                 lines.append(f"    {meta_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Storage + credential helpers
# ---------------------------------------------------------------------------

def _storage(args: argparse.Namespace):
    """Build a StorageBackend from parsed CLI flags."""
    location = getattr(args, "storage", ".")
    kwargs: dict = {}

    # Explicit S3 flags override everything
    if getattr(args, "s3_region",   None): kwargs["region"]       = args.s3_region
    if getattr(args, "s3_profile",  None): kwargs["profile"]      = args.s3_profile
    if getattr(args, "s3_endpoint", None): kwargs["endpoint_url"] = args.s3_endpoint

    cred_profile = getattr(args, "cred_profile", None)
    work_tree    = Path(getattr(args, "work_tree", None) or ".")

    return open_storage(
        location,
        cred_profile=cred_profile,
        work_tree=work_tree,
        passphrase=getattr(args, "cred_passphrase", None),
        **kwargs,
    )


def _cred_store(args: argparse.Namespace) -> CredentialStore:
    """Return the CredentialStore for the current work-tree."""
    work_tree = Path(getattr(args, "work_tree", None) or ".")
    pfv_dir = work_tree / ".pfv"
    return CredentialStore(pfv_dir / "credentials.json")


# ---------------------------------------------------------------------------
# Repo command handlers
# ---------------------------------------------------------------------------

def cmd_init(args):
    store = _storage(args)
    vdir  = pfv.init(args.vdir, storage=store, owner=args.owner, description=args.desc)
    _ok(f"Initialised {vdir!r}  [{store}]")
    return 0


def cmd_import_dir(args):
    """
    Import every file in a source directory tree as a new PFV repo.

    Each file becomes its own vdir (keyed by its relative path).  A
    .pfvignore / .pfvignore.txt in the source root controls exclusions.
    Writes .pfv/session.json into the source directory and registers
    the workspace in ~/.pfvconfig.
    """
    from pfv_config import PFVConfig
    from pfv_session import PFVSession

    store      = _storage(args)
    source_dir = Path(args.source_dir).resolve()

    if not source_dir.is_dir():
        _err(f"Not a directory: {source_dir}"); return 1

    _info(f"Importing {source_dir}  →  {store}")

    def _progress(rel_path: str, vdir: str, version: int) -> None:
        print(f"  {_c(DIM, rel_path)}  {_c(GREEN, f'v{version}')}")

    try:
        created = pfv.import_dir(
            source_dir,
            storage=store,
            owner=getattr(args, "owner", None),
            description=getattr(args, "desc", None),
            author=getattr(args, "author", None),
            message=getattr(args, "message", None),
            progress_cb=_progress,
        )
    except NotADirectoryError as e:
        _err(str(e)); return 1
    except pfv.PFVError as e:
        _err(str(e)); return 1

    if not created:
        _warn("No files imported (directory empty or all excluded by .pfvignore)")
        return 0

    _ok(f"Imported {len(created)} file(s) from {source_dir.name!r}")

    # Write .pfv/session.json into the workspace
    storage_str = getattr(args, "storage", ".").strip()
    repo_name   = getattr(args, "repo_name", None) or "primary"
    session = PFVSession.load(source_dir)
    session.description = getattr(args, "desc", None)
    session.owner       = getattr(args, "owner", None)
    session.add_repo(repo_name, storage_str, default=True)
    session.save()
    _ok(f"Session written to {session.path}")

    # Register the workspace (not the repo) in ~/.pfvconfig
    cfg = PFVConfig()
    cfg.register(source_dir, name=getattr(args, "repo_name", None) or source_dir.name)
    cfg.save()
    _info(f"Workspace registered in {cfg.path}")

    return 0


def cmd_session(args):
    """Router for the 'session' sub-subcommand."""
    from pfv_session import PFVSession

    action    = args.session_action
    work_tree = Path(getattr(args, "work_tree", None) or ".").resolve()

    # ---- init ----
    if action == "init":
        session = PFVSession.load(work_tree)
        if getattr(args, "desc", None):
            session.description = args.desc
        if getattr(args, "owner", None):
            session.owner = args.owner
        session.save()
        _ok(f"Session initialised  [{session.path}]")
        return 0

    # ---- list ----
    if action == "list":
        session = PFVSession.load(work_tree)
        if not session.repos:
            _info("No repos defined. Use: pfv session add-repo <name> <storage>")
            return 0
        _header(f"\nWorkspace: {work_tree}")
        if session.description:
            print(f"  {_c(DIM, session.description)}")
        print()
        for repo in session.repos:
            marker = _c(GREEN, " ★") if repo.get("default") else "  "
            cred   = f"  cred={repo['cred_profile']}" if repo.get("cred_profile") else ""
            print(f"{marker} {_c(CYAN, repo['name'])}  {repo['storage']}{cred}")
        print()
        return 0

    # ---- add-repo ----
    if action == "add-repo":
        session  = PFVSession.load(work_tree)
        is_first = len(session.repos) == 0
        session.add_repo(
            args.repo_name,
            args.storage_loc,
            cred_profile=getattr(args, "cred_profile", None),
            region=getattr(args, "region", None),
            endpoint_url=getattr(args, "endpoint_url", None),
            default=getattr(args, "default", False) or is_first,
        )
        session.save()
        _ok(f"Added repo {args.repo_name!r} → {args.storage_loc}  [{session.path}]")
        return 0

    # ---- remove-repo ----
    if action == "remove-repo":
        session = PFVSession.load(work_tree)
        removed = session.remove_repo(args.repo_name)
        if removed:
            session.save()
            _ok(f"Removed repo {args.repo_name!r}")
        else:
            _warn(f"Repo {args.repo_name!r} not found")
        return 0

    # ---- set-default ----
    if action == "set-default":
        session = PFVSession.load(work_tree)
        if session.set_default(args.repo_name):
            session.save()
            _ok(f"Default repo set to {args.repo_name!r}")
        else:
            _err(f"Repo {args.repo_name!r} not found"); return 1
        return 0

    _err(f"Unknown session action: {action}"); return 1


def cmd_commit(args):
    store = _storage(args)
    try:
        extra   = json.loads(args.extra_meta) if getattr(args, "extra_meta", None) else None
        version = pfv.commit(
            args.vdir, Path(args.file), storage=store,
            author=args.author, message=args.message, tag=args.tag,
            extra_meta=extra,
        )
        tag_str = f"  [{args.tag}]" if args.tag else ""
        _ok(f"Committed v{version}{tag_str}  [{store}]")
        return 0
    except (pfv.SlotLocked, pfv.NotAPFVDirectory, FileNotFoundError) as e:
        _err(str(e)); return 1


def cmd_log(args):
    store = _storage(args)
    try:
        info = pfv.log(args.vdir, storage=store)
    except pfv.NotAPFVDirectory as e:
        _err(str(e)); return 1

    _header(f"\n{info.name}  —  {len(info.slots)} version(s)  [{store}]")
    rm = info.repo_meta
    if rm:
        parts = []
        if rm.get("owner"):       parts.append(f"owner: {rm['owner']}")
        if rm.get("description"): parts.append(rm["description"])
        if rm.get("created"):     parts.append(f"created: {rm['created']}")
        if parts: print(_dim("  " + "  ·  ".join(parts)))

    print()
    if not info.slots:
        print(_dim("  (no versions yet)"))
    else:
        for slot in reversed(info.slots):
            print(_format_slot(slot, info.head, info.latest))
    print()
    return 0


def cmd_info(args):
    store = _storage(args)
    try:
        info    = pfv.log(args.vdir, storage=store)
        version = pfv._resolve_version(args.vdir, args.version, store)
        matches = [s for s in info.slots if s.version == version]
        if not matches:
            _err(f"Version {args.version} not found"); return 1
        slot = matches[0]
        _header(f"\n{info.name}  v{slot.version}  [{store}]")
        print(f"  Content : {slot.content_type}")
        print(f"  Locked  : {slot.is_locked}")
        print(f"  Deleted : {slot.is_deleted}")
        if slot.tag:    print(f"  Tag     : {slot.tag}")
        if slot.hashes:
            print("  Hashes  :")
            for h in slot.hashes: print(f"    {h}")
        if slot.meta:
            print("  Meta    :")
            for k, v in slot.meta.items(): print(f"    {k}: {v}")
        if slot.is_locked and slot.lock:
            print("  Lock    :")
            for k, v in slot.lock.items(): print(f"    {k}: {v}")
        print()
        return 0
    except (pfv.NotAPFVDirectory, pfv.VersionNotFound) as e:
        _err(str(e)); return 1


def cmd_checkout(args):
    store = _storage(args)
    if getattr(args, "track", False):
        try:
            dest, version = pfv.tracked_checkout(
                args.vdir, args.version, Path(args.dest), storage=store,
                work_tree=Path(args.work_tree) if args.work_tree else None,
                state_backend=args.state_backend,
            )
            _ok(f"Checked out v{version} → {dest}  (tracked)")
            return 0
        except (pfv.NotAPFVDirectory, pfv.VersionNotFound,
                pfv.NoContentInSlot, FileNotFoundError) as e:
            _err(str(e)); return 1
    else:
        try:
            dest = pfv.checkout(args.vdir, args.version, Path(args.dest), storage=store)
            _ok(f"Restored v{args.version} → {dest}")
            return 0
        except (pfv.NotAPFVDirectory, pfv.VersionNotFound,
                pfv.NoContentInSlot, FileNotFoundError) as e:
            _err(str(e)); return 1


def cmd_checkin(args):
    store = _storage(args)
    try:
        extra = json.loads(args.extra_meta) if getattr(args, "extra_meta", None) else None
        new_version = pfv.checkin(
            Path(args.dest), storage=store,
            work_tree=Path(args.work_tree) if args.work_tree else None,
            author=args.author, message=args.message, tag=args.tag,
            extra_meta=extra, state_backend=args.state_backend,
        )
        _ok(f"Checked in → v{new_version}")
        return 0
    except pfv.RepoConflict as e:
        _err("Conflict — repo changed while you had the file checked out:")
        print(f"  Checked out : v{e.checked_out_version}  sha {e.checked_out_sha[:16]}…")
        print(f"  Repo now    : v{e.current_version}  sha {(e.current_sha or '?')[:16]}…")
        print("  Resolve manually, then use 'commit' to push your version.")
        return 1
    except (pfv.PFVError, FileNotFoundError) as e:
        _err(str(e)); return 1


def cmd_status(args):
    store     = _storage(args)
    work_tree = Path(getattr(args, "work_tree", None) or ".")
    try:
        entries = pfv.status(work_tree, storage=store, state_backend=args.state_backend)
    except Exception as e:
        _err(str(e)); return 1

    if not entries:
        print(_dim("No tracked checkouts in this work tree.")); return 0

    _header(f"\n{len(entries)} tracked checkout(s)  [{work_tree}]\n")
    for e in entries:
        dest_name = Path(e["dest"]).name
        flags = []
        if e["conflict"]:        flags.append(_c(RED,    "CONFLICT"))
        elif e["repo_moved"]:    flags.append(_c(YELLOW, "REPO MOVED"))
        elif e["file_modified"]: flags.append(_c(CYAN,   "MODIFIED"))
        else:                    flags.append(_c(GREEN,  "clean"))

        print(f"  {_c(BOLD, dest_name)}  ←  {e['vdir']} v{e['version']}  {' '.join(flags)}")
        print(_dim(f"    checked out {e['checked_out_at']}  by {e['checked_out_by']}@{e['host']}"))
        if e["file_modified"]:
            print(_dim(f"    file sha   : {(e['working_sha'] or '')[:16]}…  (checkout: {e['sha_at_checkout'][:16]}…)"))
        if e["repo_moved"]:
            print(_dim(f"    repo now   : v{e['repo_version']}  sha {(e['repo_sha'] or '?')[:16]}…"))
        print()
    return 0


def cmd_abandon(args):
    removed = pfv.abandon(
        Path(args.dest),
        work_tree=Path(args.work_tree) if args.work_tree else None,
        state_backend=args.state_backend,
    )
    if removed: _ok(f"Abandoned checkout record for {args.dest}")
    else:        print(_dim(f"No tracked checkout found for {args.dest}"))
    return 0


def cmd_lock(args):
    store = _storage(args)
    try:
        version  = int(args.version) if args.version else None
        lock_key = pfv.lock(
            args.vdir, storage=store, version=version,
            holder=args.holder, reason=args.reason, expires=args.expires,
        )
        target = f"version {version}" if version else "repository"
        _ok(f"Locked {target}  [{lock_key}]")
        return 0
    except pfv.NotAPFVDirectory as e:
        _err(str(e)); return 1


def cmd_unlock(args):
    store   = _storage(args)
    version = int(args.version) if args.version else None
    removed = pfv.unlock(args.vdir, storage=store, version=version)
    target  = f"version {version}" if version else "repository"
    if removed: _ok(f"Unlocked {target}")
    else:        print(_dim(f"No lock found on {target}"))
    return 0


def cmd_delete(args):
    store = _storage(args)
    try:
        pfv.delete_version(args.vdir, args.version, storage=store)
        _ok(f"Tombstoned v{args.version}  (meta/sha preserved)")
        return 0
    except (pfv.SlotLocked, pfv.NotAPFVDirectory) as e:
        _err(str(e)); return 1


def cmd_flatten(args):
    store = _storage(args)
    try:
        written = pfv.flatten(args.vdir, Path(args.dest_dir), storage=store,
                              prefix=args.prefix)
        _ok(f"Flattened {len(written)} version(s) → {args.dest_dir}/")
        for p in written: print(f"  {p.name}")
        return 0
    except pfv.NotAPFVDirectory as e:
        _err(str(e)); return 1


def cmd_verify(args):
    store = _storage(args)
    try:
        errors = pfv.verify(args.vdir, storage=store)
    except pfv.NotAPFVDirectory as e:
        _err(str(e)); return 1

    if errors:
        _err(f"Verification failed ({len(errors)} error(s)):")
        for e in errors: print(f"  {_c(RED, '·')} {e}")
        return 1
    _ok("All hashes verified OK")
    return 0


def cmd_prune(args):
    store = _storage(args)
    try:
        pruned = pfv.prune(args.vdir, keep=args.keep, storage=store)
        if pruned: _ok(f"Pruned {len(pruned)} version(s): {pruned}")
        else:       print(_dim("Nothing to prune"))
        return 0
    except pfv.NotAPFVDirectory as e:
        _err(str(e)); return 1


# ---------------------------------------------------------------------------
# Credential command handlers
# ---------------------------------------------------------------------------

# Field names we never echo, even as "[set]" markers
_SENSITIVE = {"aws_secret_access_key", "aws_session_token", "account_key",
              "password", "token", "secret"}

# Known profile fields grouped by backend for the interactive wizard
_S3_FIELDS = [
    ("backend",               "Backend type",              "s3",   False),
    ("bucket",                "S3 bucket name",            "",     False),
    ("prefix",                "Key prefix (optional)",     "",     False),
    ("region",                "AWS region",                "",     False),
    ("aws_access_key_id",     "AWS access key ID",         "",     False),
    ("aws_secret_access_key", "AWS secret access key",     "",     True),
    ("aws_session_token",     "AWS session token (opt.)",  "",     True),
    ("endpoint_url",          "Custom endpoint URL (opt.)", "",    False),
    ("profile",               "AWS credentials profile",   "",     False),
]

_GDRIVE_FIELDS = [
    ("backend",          "Backend type",               "gdrive", False),
    ("folder_id",        "Google Drive folder ID",      "",      False),
    ("credentials_file", "Path to credentials JSON",   "",       False),
    ("token_file",       "Path to OAuth token file",   "",       False),
]

_AZURE_FIELDS = [
    ("backend",           "Backend type",              "azure", False),
    ("container",         "Azure container name",       "",     False),
    ("prefix",            "Blob prefix (optional)",     "",     False),
    ("connection_string", "Connection string",          "",     True),
    ("account_name",      "Storage account name",       "",     False),
    ("account_key",       "Storage account key",        "",     True),
]

_BACKEND_FIELDS = {
    "s3":     _S3_FIELDS,
    "gdrive": _GDRIVE_FIELDS,
    "azure":  _AZURE_FIELDS,
}


def _interactive_credential_wizard(existing: dict | None = None) -> dict:
    """Prompt the user for credential fields, guided by backend choice."""
    print()
    if existing:
        backend = existing.get("backend", "")
        print(f"Editing profile  (current backend: {backend or 'unset'})")
        print("Press Enter to keep existing value.\n")
    else:
        print("Select backend:")
        print("  1) s3       Amazon S3 (or compatible)")
        print("  2) gdrive   Google Drive  (stub — not yet implemented)")
        print("  3) azure    Azure Blob Storage  (stub — not yet implemented)")
        choice = input("Backend [1]: ").strip() or "1"
        backend = {"1": "s3", "2": "gdrive", "3": "azure"}.get(choice, "s3")
        print()

    fields = _BACKEND_FIELDS.get(backend, _S3_FIELDS)
    result: dict = {"backend": backend}

    for key, label, default, is_secret in fields:
        if key == "backend":
            continue
        current = existing.get(key, "") if existing else default
        if is_secret:
            display_current = "[set]" if current else "[empty]"
            prompt = f"  {label} {display_current}: "
            val = getpass.getpass(prompt)
            if not val and current:
                val = current  # keep existing
        else:
            prompt = f"  {label}"
            if current:
                prompt += f" [{current}]"
            prompt += ": "
            val = input(prompt).strip() or current

        if val:
            result[key] = val

    return result


def cmd_credentials(args):
    """Router for the 'credentials' sub-subcommand."""
    action = args.cred_action

    # Resolve work-tree and credential store
    work_tree  = Path(getattr(args, "work_tree", None) or ".")
    cred_store = CredentialStore(work_tree / ".pfv" / "credentials.json")
    passphrase = getattr(args, "cred_passphrase", None)

    # ---- list ----
    if action == "list":
        profiles = cred_store.list_profiles()
        if not profiles:
            print(_dim("No credential profiles stored."))
            _info(f"Add one with: pfv credentials set <profile>  [{work_tree / '.pfv'}]")
        else:
            _header(f"\n{len(profiles)} credential profile(s)  [{work_tree / '.pfv'}]\n")
            for name in profiles:
                print(f"  {_c(CYAN, name)}")
            print()
        return 0

    # ---- set ----
    if action == "set":
        profile = args.cred_profile_name

        # Load existing if present (for editing)
        existing = None
        if cred_store.exists(profile):
            try:
                pw = passphrase or getpass.getpass(
                    f"Passphrase to read existing profile {profile!r} (Enter to skip): "
                )
                if pw:
                    existing = cred_store.get(profile, passphrase=pw)
                    passphrase = pw  # reuse same passphrase for saving
            except (ValueError, KeyboardInterrupt):
                existing = None

        # Use --json if supplied, otherwise wizard
        if getattr(args, "cred_json", None):
            try:
                credentials = json.loads(args.cred_json)
            except json.JSONDecodeError as e:
                _err(f"Invalid JSON: {e}"); return 1
        else:
            try:
                credentials = _interactive_credential_wizard(existing)
            except KeyboardInterrupt:
                print("\nAborted.")
                return 1

        # Confirm passphrase (prompt if not given or reusing existing)
        if not passphrase:
            passphrase = CredentialStore._prompt_new()

        cred_store.set(profile, credentials, passphrase=passphrase)
        _ok(f"Saved credential profile {profile!r}  [{cred_store.path}]")
        return 0

    # ---- get ----
    if action == "get":
        profile = args.cred_profile_name
        try:
            pw   = passphrase or getpass.getpass(f"Passphrase for {profile!r}: ")
            data = cred_store.get(profile, passphrase=pw)
        except KeyError:
            _err(f"Profile {profile!r} not found"); return 1
        except ValueError as e:
            _err(str(e)); return 1

        _header(f"\nProfile: {profile}\n")
        for k, v in data.items():
            if k.lower() in _SENSITIVE:
                print(f"  {k}: {_c(DIM, '[hidden]')}")
            else:
                print(f"  {k}: {v}")
        print()
        return 0

    # ---- delete ----
    if action == "delete":
        profile = args.cred_profile_name
        try:
            pw      = passphrase or getpass.getpass(f"Passphrase for {profile!r}: ")
            removed = cred_store.delete(profile, passphrase=pw)
            if removed: _ok(f"Deleted profile {profile!r}")
            else:        _warn(f"Profile {profile!r} not found")
        except ValueError as e:
            _err(str(e)); return 1
        return 0

    # ---- test ----
    if action == "test":
        profile = args.cred_profile_name
        try:
            pw   = passphrase or getpass.getpass(f"Passphrase for {profile!r}: ")
            data = cred_store.get(profile, passphrase=pw)
        except (KeyError, ValueError) as e:
            _err(str(e)); return 1

        backend = data.get("backend", "")
        if backend == "s3":
            bucket = data.get("bucket", "")
            prefix = data.get("prefix", "")
            loc    = f"s3://{bucket}/{prefix}" if bucket else "s3:"
        elif backend == "gdrive":
            loc = f"gdrive://{data.get('folder_id', '')}"
        elif backend == "azure":
            loc = f"azure://{data.get('container', '')}"
        else:
            _err(f"Unknown backend {backend!r} in profile"); return 1

        _info(f"Connecting to {loc} using profile {profile!r}…")
        try:
            store = open_storage(loc, cred_profile=profile,
                                 work_tree=work_tree, passphrase=pw)
            # Minimal ping: list keys with an empty prefix
            keys = store.list_keys("")
            _ok(f"Connection OK  ({len(keys)} top-level keys found)  [{store}]")
            store.close()
        except Exception as e:
            _err(f"Connection failed: {e}"); return 1
        return 0

    _err(f"Unknown credentials action: {action}"); return 1


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _add_storage_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("storage")
    g.add_argument("--storage", default=".",
                   help="Repo location: local path, s3://bucket/prefix, "
                        "gdrive://folder-id, azure://container, "
                        "or scheme-only (s3:) with --cred-profile  (default: .)")
    g.add_argument("--cred-profile", metavar="PROFILE",
                   help="Load storage credentials from .pfv/credentials.json profile")
    g.add_argument("--cred-passphrase", metavar="PW",
                   help="Passphrase for credential profile (prompted if omitted)")
    g.add_argument("--s3-region",   metavar="REGION",  help="AWS region")
    g.add_argument("--s3-profile",  metavar="PROFILE", help="AWS credentials profile")
    g.add_argument("--s3-endpoint", metavar="URL",     help="Custom S3 endpoint URL")


def _add_state_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--work-tree",
                   help="Work-tree root for checkout state (default: dest's parent)")
    p.add_argument("--state-backend", default="json",
                   choices=["json", "sqlite", "redis"],
                   help="Checkout state backend (default: json)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pfv",
        description="Peel File Versions — versioning for large binary files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # ---- import-dir ----
    p = sub.add_parser(
        "import-dir",
        help="Import an existing directory tree into a new PFV repo",
        description=(
            "Walk SOURCE_DIR, create a versioned-file repo (vdir) for every file, "
            "and record the storage location in ~/.pfvconfig.  "
            "Place a .pfvignore or .pfvignore.txt in SOURCE_DIR to exclude files "
            "(one fnmatch pattern per line, # for comments)."
        ),
    )
    p.add_argument("source_dir", metavar="SOURCE_DIR",
                   help="Directory to import")
    p.add_argument("--name",   dest="repo_name", metavar="NAME",
                   help="Human-readable repo name for ~/.pfvconfig (default: dir name)")
    p.add_argument("--owner",  help="Owner name stored in each vdir's metadata")
    p.add_argument("--desc",   help="Description stored in metadata and ~/.pfvconfig")
    p.add_argument("--author", help="Author for the initial commit of every file")
    p.add_argument("--message", "-m",
                   help="Commit message for the initial version (default: 'Imported from …')")
    _add_storage_args(p)

    # ---- init ----
    p = sub.add_parser("init", help="Initialise a PFV versioned-file directory")
    p.add_argument("vdir")
    p.add_argument("--owner"); p.add_argument("--desc")
    _add_storage_args(p)

    # ---- commit ----
    p = sub.add_parser("commit", help="Add a new version from a local file")
    p.add_argument("vdir"); p.add_argument("file")
    p.add_argument("--author"); p.add_argument("--message", "-m")
    p.add_argument("--tag"); p.add_argument("--extra-meta")
    _add_storage_args(p)

    # ---- log ----
    p = sub.add_parser("log", help="Show version history")
    p.add_argument("vdir")
    _add_storage_args(p)

    # ---- info ----
    p = sub.add_parser("info", help="Show detailed info for a specific version")
    p.add_argument("vdir"); p.add_argument("version")
    _add_storage_args(p)

    # ---- checkout ----
    p = sub.add_parser("checkout", help="Download a version to a local file")
    p.add_argument("vdir"); p.add_argument("version"); p.add_argument("dest")
    p.add_argument("--track", action="store_true",
                   help="Record checkout for conflict detection on checkin")
    _add_storage_args(p); _add_state_args(p)

    # ---- checkin ----
    p = sub.add_parser("checkin", help="Commit a tracked file back (with conflict check)")
    p.add_argument("dest")
    p.add_argument("--author"); p.add_argument("--message", "-m")
    p.add_argument("--tag"); p.add_argument("--extra-meta")
    _add_storage_args(p); _add_state_args(p)

    # ---- status ----
    p = sub.add_parser("status", help="Show tracked checkout status")
    _add_storage_args(p); _add_state_args(p)
    p.set_defaults(work_tree=".")

    # ---- abandon ----
    p = sub.add_parser("abandon", help="Drop a checkout record without committing")
    p.add_argument("dest")
    _add_storage_args(p); _add_state_args(p)

    # ---- lock ----
    p = sub.add_parser("lock", help="Lock a repo or version")
    p.add_argument("vdir")
    p.add_argument("--version"); p.add_argument("--holder")
    p.add_argument("--reason");  p.add_argument("--expires")
    _add_storage_args(p)

    # ---- unlock ----
    p = sub.add_parser("unlock", help="Remove a lock")
    p.add_argument("vdir"); p.add_argument("--version")
    _add_storage_args(p)

    # ---- delete ----
    p = sub.add_parser("delete", help="Tombstone a version")
    p.add_argument("vdir"); p.add_argument("version")
    _add_storage_args(p)

    # ---- flatten ----
    p = sub.add_parser("flatten", help="Export all versions as plain local files")
    p.add_argument("vdir"); p.add_argument("dest_dir"); p.add_argument("--prefix")
    _add_storage_args(p)

    # ---- verify ----
    p = sub.add_parser("verify", help="Verify SHA hashes of all binary slots")
    p.add_argument("vdir")
    _add_storage_args(p)

    # ---- prune ----
    p = sub.add_parser("prune", help="Tombstone oldest versions beyond --keep count")
    p.add_argument("vdir"); p.add_argument("--keep", type=int, required=True)
    _add_storage_args(p)

    # ---- credentials ----
    p_creds = sub.add_parser("credentials", aliases=["creds"],
                              help="Manage encrypted credential profiles in .pfv/")
    cred_sub = p_creds.add_subparsers(dest="cred_action", metavar="<action>")
    cred_sub.required = True

    # credentials set
    cs = cred_sub.add_parser("set", help="Save a credential profile (interactive wizard)")
    cs.add_argument("cred_profile_name", metavar="profile")
    cs.add_argument("--passphrase", dest="cred_passphrase", metavar="PW",
                    help="Encryption passphrase (prompted if omitted)")
    cs.add_argument("--json", dest="cred_json", metavar="JSON",
                    help="Supply credentials as a JSON string instead of using the wizard")
    cs.add_argument("--work-tree", default=".")

    # credentials get
    cg = cred_sub.add_parser("get", help="Show stored profile fields (secrets hidden)")
    cg.add_argument("cred_profile_name", metavar="profile")
    cg.add_argument("--passphrase", dest="cred_passphrase", metavar="PW")
    cg.add_argument("--work-tree", default=".")

    # credentials list
    cl = cred_sub.add_parser("list", help="List stored profile names")
    cl.add_argument("--work-tree", default=".")

    # credentials delete
    cd = cred_sub.add_parser("delete", help="Delete a credential profile")
    cd.add_argument("cred_profile_name", metavar="profile")
    cd.add_argument("--passphrase", dest="cred_passphrase", metavar="PW")
    cd.add_argument("--work-tree", default=".")

    # credentials test
    ct = cred_sub.add_parser("test", help="Test a credential profile by opening the backend")
    ct.add_argument("cred_profile_name", metavar="profile")
    ct.add_argument("--passphrase", dest="cred_passphrase", metavar="PW")
    ct.add_argument("--work-tree", default=".")


    # ---- session ----
    p_sess = sub.add_parser("session", help="Manage the workspace session (.pfv/session.json)")
    sess_sub = p_sess.add_subparsers(dest="session_action", metavar="<action>")
    sess_sub.required = True

    si = sess_sub.add_parser("init", help="Create or update session metadata")
    si.add_argument("--work-tree", default="."); si.add_argument("--owner"); si.add_argument("--desc")

    sl = sess_sub.add_parser("list", help="List repos defined in the session")
    sl.add_argument("--work-tree", default=".")

    sa = sess_sub.add_parser("add-repo", help="Add or replace a repo in the session")
    sa.add_argument("repo_name",   metavar="NAME")
    sa.add_argument("storage_loc", metavar="STORAGE")
    sa.add_argument("--cred-profile", metavar="PROFILE")
    sa.add_argument("--region",       metavar="REGION")
    sa.add_argument("--endpoint-url", metavar="URL")
    sa.add_argument("--default", action="store_true")
    sa.add_argument("--work-tree", default=".")

    sr = sess_sub.add_parser("remove-repo", help="Remove a repo from the session")
    sr.add_argument("repo_name", metavar="NAME"); sr.add_argument("--work-tree", default=".")

    sd = sess_sub.add_parser("set-default", help="Set the default repo")
    sd.add_argument("repo_name", metavar="NAME"); sd.add_argument("--work-tree", default=".")

    return parser


COMMANDS = {
    "import-dir":  cmd_import_dir,
    "init":        cmd_init,
    "commit":      cmd_commit,
    "log":         cmd_log,
    "info":        cmd_info,
    "checkout":    cmd_checkout,
    "checkin":     cmd_checkin,
    "status":      cmd_status,
    "abandon":     cmd_abandon,
    "lock":        cmd_lock,
    "unlock":      cmd_unlock,
    "delete":      cmd_delete,
    "flatten":     cmd_flatten,
    "verify":      cmd_verify,
    "prune":       cmd_prune,
    "session":     cmd_session,
    "credentials": cmd_credentials,
    "creds":       cmd_credentials,
}


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()
    handler = COMMANDS.get(args.command)
    if not handler:
        parser.print_help(); sys.exit(1)
    sys.exit(handler(args) or 0)


if __name__ == "__main__":
    main()
