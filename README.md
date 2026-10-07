# Peel File Versions (PFV)

**Version 0.1** · Python 3.10+

PFV is a versioning system for large binary files. Each version is a numbered slot in a per-file directory; sidecar files carry the hash, metadata, lock state, and tag for that slot. No index, no database — the structure is readable with ordinary filesystem or cloud-storage tools.

---

## 1. Design overview

A versioned file is a **directory** named after the original file, including its extension. Every version occupies a numbered **slot** inside that directory. Slot `3` for `hero.psd` is just the file `hero.psd/3`. Sidecar files — `hero.psd/3.meta`, `hero.psd/3.sha`, `hero.psd/3.lock`, etc. — carry structured information about that slot.

```
repo/
  hero.psd/           ← versioned-file directory (vdir)
    .repo.meta        ← repo-level metadata (JSON)
    HEAD              ← current working version number (plain text)
    latest            ← highest committed version number (plain text)
    1                 ← binary content, version 1
    1.meta            ← {"author","timestamp","message","size",...}
    1.sha             ← "sha256:<hex>\n"
    2
    2.meta
    2.sha
    2.tag             ← "approved"
    2.lock            ← present only while locked: {"holder","acquired",...}
    3.link            ← path or URI pointing to content stored elsewhere
    3.meta
    3.sha
```

### Four independent layers

```
┌─────────────────────────────────────────────────────┐
│                   pfv_cli.py                        │  command-line interface
├─────────────────────────────────────────────────────┤
│                     pfv.py                          │  core versioning logic
├────────────────────────┬────────────────────────────┤
│    pfv_storage.py      │      pfv_state.py          │  pluggable I/O
│  (repo: where files    │  (workspace: checkout      │
│   are stored)          │   tracking state)          │
├────────────────────────┴────────────────────────────┤
│                pfv_credentials.py                   │  encrypted credential store
└─────────────────────────────────────────────────────┘
```

**`pfv_storage`** handles all repo I/O — reading and writing versioned files and their sidecars. Swap the backend to change where repos live (local disk, S3, etc.) without changing any other code.

**`pfv_state`** tracks which files are currently checked out to which local paths, and records the SHA at checkout time. This is the data that enables conflict detection at check-in. It is entirely separate from repo storage; a team could keep repos on S3 and checkout state in SQLite locally.

**`pfv_credentials`** stores encrypted credentials for storage backends in `.pfv/credentials.json`, so access keys never appear in shell history or scripts.

### Version lifecycle

```
init()                    create the vdir
  │
commit() ──────────────→  upload binary, write .sha .meta [.tag], advance latest + HEAD
  │
checkout() / tracked_checkout()
  │         │
  │         └──→  write checkout record to state backend
  │                  (version + sha_at_checkout + who + when)
  │
  │   [edit the file locally]
  │
checkin()
  ├── load checkout record
  ├── compare repo's current latest sha to sha_at_checkout
  │     if different → raise RepoConflict
  └── commit() → remove checkout record
```

### Atomicity

On a local filesystem all writes use a temp-file + atomic rename, so a crash mid-write never leaves a partial file visible to readers. On S3, `put_object` is atomic from the reader's perspective by design; large files use multipart upload with the final key only appearing after all parts are assembled.

---

## 2. Repository structure on disk

### Slot file extensions

| File | Content | Notes |
|---|---|---|
| `N` | binary content | the versioned file itself |
| `N.meta` | JSON object | author, timestamp, message, size, custom fields |
| `N.sha` | `sha256:<hex>\n` | one line per algorithm; only sha256 currently written |
| `N.lock` | JSON object | present ↔ locked; remove to unlock |
| `N.link` | text path or URI | replaces binary; `.sha` still holds hash of the target |
| `N.delta` | binary diff | replaces binary; delta format recorded in `.meta` |
| `N.tag` | plain text label | human-readable alias for this version |

### Root files

| File | Content |
|---|---|
| `HEAD` | integer — version last checked out or committed |
| `latest` | integer — highest committed version number |
| `.repo.meta` | JSON — name, owner, description, created, retention |
| `.lock` | JSON — repo-level lock (blocks `commit`) |

### Workspace state (`.pfv/`)

The `.pfv/` directory lives at the **work-tree root** (the directory where checked-out files live), not inside the versioned-file directory.

| File | Content |
|---|---|
| `.pfv/meta.json` | checkout state (JSON backend) |
| `.pfv/meta.db` | checkout state (SQLite backend) |
| `.pfv/credentials.json` | encrypted credential profiles (chmod 600) |

---

## 3. Module map

| Module | Responsibility |
|---|---|
| `pfv.py` | Versioning operations: init, commit, checkout, checkin, log, verify, prune, flatten, lock/unlock, status, abandon |
| `pfv_storage.py` | `StorageBackend` ABC + `LocalBackend`, `S3Backend`, stubs for Google Drive and Azure; `open_storage()` factory |
| `pfv_state.py` | `StateBackend` ABC + `JSONBackend`, `SQLiteBackend`, `RedisBackend`; `open_backend()` factory |
| `pfv_credentials.py` | `CredentialStore` — PBKDF2+Fernet encrypted profiles in `.pfv/credentials.json` |
| `pfv_cli.py` | Argparse CLI wrapping all of the above |
| `pfv_deletion.py`, `pfv_delete_cli.py` | Non-destructive delete / rename / undelete markers, and their CLI |
| `pfv_session.py` | `PFVSession`: a workspace's repo links (`.pfv/session.json`) |
| `pfv_config.py` | `PFVConfig`: recently used workspaces (`~/.pfvconfig`) |

---

## 4. Core library — `pfv.py`

All public functions accept `storage` as either a `StorageBackend` instance, a location string / `Path` (passed to `open_storage()`), or `None` (defaults to `LocalBackend(".")`).

### `init`

```python
init(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
    name: str | None = None,
    owner: str | None = None,
    description: str | None = None,
) -> str
```

Create a new versioned-file directory named `vdir` in `storage`. Writes `HEAD`, `latest` (both `0`), and `.repo.meta`. Safe to call on an existing directory — only creates files that don't already exist. Returns `vdir`.

```python
from pfv import init
from pfv_storage import open_storage

store = open_storage("s3://my-bucket/repos")
init("render.exr", storage=store, owner="alice", description="Main comp")
```

---

### `commit`

```python
commit(
    vdir: str,
    src: Path,
    storage: str | Path | StorageBackend | None = None,
    author: str | None = None,
    message: str | None = None,
    tag: str | None = None,
    extra_meta: dict | None = None,
) -> int
```

Upload the local file `src` to `storage` as the next version of `vdir`. Writes the binary, `.sha` (SHA-256 of the source file), and `.meta`. Optionally writes `.tag`. Advances `latest` and `HEAD`. Returns the new version number.

Raises `SlotLocked` if a repo-level lock exists. Raises `NotAPFVDirectory` if `vdir` has not been initialised. Raises `FileNotFoundError` if `src` does not exist.

Tag uniqueness is advisory — a warning is printed if the tag is already used by another version, but the commit proceeds.

```python
from pfv import commit
from pathlib import Path

v = commit("render.exr", Path("output/frame001.exr"),
           storage=store, author="alice", message="Colour grade pass 2",
           tag="approved", extra_meta={"shot": "010", "dept": "comp"})
# v == 3
```

---

### `log`

```python
log(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
) -> RepoInfo
```

Return a `RepoInfo` describing the repository. Reads every slot's sidecar files. See [Data class reference](#10-data-class-reference) for the fields available on `RepoInfo` and `SlotInfo`.

```python
info = log("render.exr", storage=store)
for slot in reversed(info.slots):
    print(f"v{slot.version}  {slot.meta.get('message', '')}  [{slot.content_type}]")
```

---

### `checkout`

```python
checkout(
    vdir: str,
    version: int | str,
    dest: Path,
    storage: str | Path | StorageBackend | None = None,
) -> Path
```

Download `version` from `vdir` to the local file `dest`. `version` may be:

- an integer (`3`)
- `"latest"` — the highest committed version
- `"HEAD"` — the version last checked out or committed
- a tag name (`"approved"`)

Updates `HEAD` to the resolved version number. Returns `dest`. Does not write a checkout record; use `tracked_checkout` if you need conflict detection later.

```python
checkout("render.exr", "approved", Path("work/render.exr"), storage=store)
```

---

### `tracked_checkout`

```python
tracked_checkout(
    vdir: str,
    version: int | str,
    dest: Path,
    storage: str | Path | StorageBackend | None = None,
    work_tree: Path | None = None,
    state_backend: str = "json",
    **state_kwargs,
) -> tuple[Path, int]
```

Like `checkout`, but records the operation in the work-tree's state backend so that `checkin` can later compare the repo's current SHA against the SHA at checkout time. `work_tree` defaults to `dest.parent`. Returns `(dest, resolved_version_number)`.

```python
dest, ver = tracked_checkout(
    "render.exr", "latest", Path("work/render.exr"),
    storage=store, work_tree=Path("work/"),
)
# work/.pfv/meta.json now records: version=ver, sha_at_checkout=<hex>
```

---

### `checkin`

```python
checkin(
    dest: Path,
    storage: str | Path | StorageBackend | None = None,
    work_tree: Path | None = None,
    author: str | None = None,
    message: str | None = None,
    tag: str | None = None,
    extra_meta: dict | None = None,
    state_backend: str = "json",
    **state_kwargs,
) -> int
```

Commit the local file `dest` back to its PFV repo. Before committing:

1. Loads the checkout record for `dest` from the state backend.
2. Reads the current `latest` version and its SHA from the repo.
3. If either the version number or the SHA differs from what was recorded at checkout time, raises `RepoConflict` — someone else has committed since this file was checked out.

On success, calls `commit`, removes the checkout record, and returns the new version number.

```python
try:
    new_ver = checkin(Path("work/render.exr"), storage=store,
                      work_tree=Path("work/"), author="alice",
                      message="Post-grade tweak")
except RepoConflict as e:
    print(f"Conflict: checked out v{e.checked_out_version}, repo is now v{e.current_version}")
    # Resolve manually, then call commit() directly
```

---

### `status`

```python
status(
    work_tree: Path,
    storage: str | Path | StorageBackend | None = None,
    state_backend: str = "json",
    **state_kwargs,
) -> list[dict]
```

Return a list of status dicts for every file tracked in `work_tree`. Each dict contains:

| Key | Type | Meaning |
|---|---|---|
| `dest` | `str` | absolute path of the working copy |
| `vdir` | `str` | logical name of the versioned-file directory |
| `version` | `int` | version that was checked out |
| `sha_at_checkout` | `str` | SHA-256 hex recorded at checkout |
| `repo_version` | `int` | current `latest` in the repo |
| `repo_sha` | `str` | SHA-256 of the current latest version |
| `working_sha` | `str \| None` | SHA-256 of the file on disk right now |
| `file_modified` | `bool` | working copy differs from checkout SHA |
| `repo_moved` | `bool` | repo has a newer version since checkout |
| `conflict` | `bool` | both `file_modified` and `repo_moved` are true |
| `checked_out_at` | `str` | ISO-8601 UTC timestamp |
| `checked_out_by` | `str` | username |
| `host` | `str` | hostname |

---

### `abandon`

```python
abandon(
    dest: Path,
    work_tree: Path | None = None,
    state_backend: str = "json",
    **state_kwargs,
) -> bool
```

Remove the checkout record for `dest` without committing. Returns `True` if a record was found and removed. Use this to discard local edits or to clear a record after resolving a conflict manually.

---

### `lock`

```python
lock(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
    version: int | None = None,
    holder: str | None = None,
    reason: str | None = None,
    expires: str | None = None,
) -> str
```

Create a lock. If `version` is `None`, the lock is repo-level (blocks all `commit` calls). If `version` is an integer or version string, only that slot is locked (blocks `delete_version`). Returns the storage key of the created `.lock` object.

Locks are stored as JSON objects: `{"holder", "acquired", "host", "reason"?, "expires"?}`. The **presence** of the file is the lock; removing it with `unlock()` or `storage.delete()` releases it.

---

### `unlock`

```python
unlock(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
    version: int | None = None,
) -> bool
```

Remove a lock. `version=None` removes the repo-level lock; an integer or version string removes a slot-level lock. Returns `True` if a lock was present and removed.

---

### `delete_version`

```python
delete_version(
    vdir: str,
    version: int | str,
    storage: str | Path | StorageBackend | None = None,
) -> None
```

Tombstone a version: delete the binary, `.link`, and `.delta` files, and set `"deleted": true` in `.meta`. The `.meta` and `.sha` files are preserved for audit purposes. A tombstoned version cannot be checked out but still appears in `log()` output marked `DELETED`.

Raises `SlotLocked` if the slot has a `.lock` file.

---

### `flatten`

```python
flatten(
    vdir: str,
    dest_dir: Path,
    storage: str | Path | StorageBackend | None = None,
    prefix: str | None = None,
) -> list[Path]
```

Download all non-deleted versions from `vdir` to plain files in `dest_dir`. The output filename format is `<base>_v<N>[_<tag>].<ext>` — for example, versions 2 and 3 of `hero.psd` with tag `approved` on v2 would produce `hero_v2_approved.psd` and `hero_v3.psd`. Returns a list of local Paths written.

Useful for delivering a snapshot of all versions to someone who shouldn't need PFV tooling to access the files.

---

### `verify`

```python
verify(
    vdir: str,
    storage: str | Path | StorageBackend | None = None,
) -> list[str]
```

Re-hash every binary slot and compare against the stored `.sha` file. Returns a list of error strings — one per mismatch or missing hash. An empty list means all hashes are valid. For remote backends, each binary is downloaded to a temporary file for hashing.

---

### `prune`

```python
prune(
    vdir: str,
    keep: int,
    storage: str | Path | StorageBackend | None = None,
) -> list[int]
```

Tombstone the oldest versions, keeping the `keep` most recent non-deleted, non-locked binary slots. Returns a list of version numbers pruned. Locked versions are skipped and do not count toward the `keep` total.

---

## 5. Storage backends — `pfv_storage.py`

### `StorageBackend` (ABC)

All repo I/O goes through this interface. Eight methods must be implemented; text, JSON, and integer helpers are provided on the base class.

**Required methods**

| Method | Signature | Notes |
|---|---|---|
| `exists` | `(key: str) -> bool` | |
| `read_bytes` | `(key: str) -> bytes` | |
| `write_bytes` | `(key: str, data: bytes) -> None` | atomic where possible |
| `delete` | `(key: str) -> None` | |
| `list_keys` | `(prefix: str = "") -> list[str]` | all keys under prefix, sorted |
| `download_to` | `(key: str, local: Path) -> None` | stream to local file |
| `upload_from` | `(key: str, local: Path) -> None` | stream from local file |
| `copy` | `(src_key: str, dst_key: str) -> None` | server-side where possible |
| `close` | `() -> None` | optional teardown |

**Derived helpers (base class)**

| Method | Notes |
|---|---|
| `read_text(key)` | decodes UTF-8 |
| `write_text(key, text)` | encodes UTF-8 |
| `read_json(key)` | parses JSON |
| `write_json(key, obj)` | serialises JSON with indent=2 |
| `read_int(key)` | reads and strips a plain integer |
| `write_int(key, n)` | writes `str(n)` |

Keys are always forward-slash-separated relative paths within the repo root, e.g. `"render.exr/3.meta"`. Keys must not start with `/` and must not use `..` to escape the root.

---

### `LocalBackend`

```python
LocalBackend(root: str | Path)
```

Stores repos on the local filesystem. `root` is the directory that contains versioned-file directories. All writes are atomic (temp-file + rename). Path traversal is blocked — a key that resolves outside `root` raises `ValueError`.

---

### `S3Backend`

```python
S3Backend(
    bucket: str,
    prefix: str = "",
    region: str | None = None,
    profile: str | None = None,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    aws_session_token: str | None = None,
    endpoint_url: str | None = None,
    chunk_size: int = 8_388_608,   # 8 MB
)
```

Stores repos in Amazon S3 (or any S3-compatible store — MinIO, LocalStack, Backblaze B2, etc.). All keys are stored under `s3://<bucket>/<prefix>/<key>`. Large file transfers use multipart upload/download. `copy()` uses server-side copy.

Credential resolution follows the standard boto3 chain: explicit constructor arguments → environment variables (`AWS_ACCESS_KEY_ID` etc.) → `~/.aws/credentials` profile → instance role. Explicit keys passed as constructor arguments take precedence over all.

Requires: `pip install boto3`

---

### `GoogleDriveBackend` (stub)

```python
GoogleDriveBackend(
    folder_id: str,
    credentials_file: str | None = None,
    token_file: str | None = None,
)
```

All methods raise `NotImplementedError`. The constructor signature and the credential profile fields (`credentials_file`, `token_file`) are defined, ready to be implemented using `google-api-python-client`.

---

### `AzureBackend` (stub)

```python
AzureBackend(
    container: str,
    prefix: str = "",
    connection_string: str | None = None,
    account_name: str | None = None,
    account_key: str | None = None,
)
```

All methods raise `NotImplementedError`. Ready to be implemented using `azure-storage-blob`.

---

### `open_storage`

```python
open_storage(
    location: str | Path,
    *,
    cred_profile: str | None = None,
    work_tree: Path | None = None,
    passphrase: str | None = None,
    **kwargs,
) -> StorageBackend
```

Factory that parses `location` and returns the appropriate backend.

| Location format | Backend |
|---|---|
| `"."` / `"/path"` / `"~/path"` | `LocalBackend` |
| `"s3://bucket"` / `"s3://bucket/prefix"` | `S3Backend` |
| `"s3:"` (scheme only) | `S3Backend` — bucket/prefix read from credential profile |
| `"gdrive://folder-id"` | `GoogleDriveBackend` (stub) |
| `"azure://container"` / `"azure://container/prefix"` | `AzureBackend` (stub) |

If `cred_profile` is given, the named profile is loaded from `.pfv/credentials.json` (searching upward from `work_tree` or cwd). The profile fields are merged into `kwargs`, with explicit `kwargs` taking precedence. This is how you can avoid putting credentials in source code or shell history:

```python
store = open_storage("s3:", cred_profile="s3-prod", work_tree=Path("."))
```

---

## 6. Checkout state backends — `pfv_state.py`

These store the per-file checkout records that enable conflict detection.

### `StateBackend` (ABC)

| Method | Signature | Notes |
|---|---|---|
| `upsert` | `(record: CheckoutRecord) -> None` | insert or replace by `dest` key |
| `get` | `(dest: str) -> CheckoutRecord \| None` | |
| `remove` | `(dest: str) -> bool` | returns `True` if record existed |
| `list_all` | `() -> list[CheckoutRecord]` | |
| `close` | `() -> None` | optional |

### `CheckoutRecord`

```python
@dataclass
class CheckoutRecord:
    dest:             str    # absolute path of working copy
    vdir:             str    # versioned-file directory logical name
    version:          int    # version number at checkout
    sha_at_checkout:  str    # sha256 hex at checkout time
    checked_out_at:   str    # ISO-8601 UTC
    checked_out_by:   str    # username ($USER / $USERNAME)
    host:             str    # hostname
    extra:            dict   # reserved
```

### Implementations

| Class | Storage | Extra deps |
|---|---|---|
| `JSONBackend` | `.pfv/meta.json` | none (default) |
| `SQLiteBackend` | `.pfv/meta.db` | none (stdlib `sqlite3`) |
| `RedisBackend` | Redis hash + set | `pip install redis` |

### `open_backend`

```python
open_backend(
    work_tree: Path,
    backend: str = "json",
    **kwargs,
) -> StateBackend
```

`backend` is one of `"json"`, `"sqlite"`, or `"redis"`. For Redis, pass `redis_url="redis://host:6379/0"`. Returns an initialised `StateBackend`.

---

## 7. Credential store — `pfv_credentials.py`

### Security design

Credentials are stored in `.pfv/credentials.json`. The file is written with `chmod 600` (owner read/write only on POSIX; a no-op on Windows). The encryption scheme:

- **Key derivation:** PBKDF2-HMAC-SHA256, 600 000 iterations (OWASP 2023 recommendation), 32-byte random salt stored in the file
- **Encryption:** Fernet (AES-128-CBC + HMAC-SHA256)
- **Per-profile:** each profile is independently encrypted; you can have multiple profiles with different passphrases in the same file
- **Passphrase never stored:** only the derived key is held in memory during a session

### `CredentialStore`

```python
CredentialStore(path: str | Path)
```

Manages profiles in a single JSON file at `path`.

```python
store = CredentialStore(".pfv/credentials.json")
```

**`set(profile, credentials, passphrase=None)`**

Save `credentials` (a plain dict) under `profile`, encrypted with `passphrase`. If `passphrase` is `None`, prompts interactively with confirmation. If the profile already exists, it is overwritten (requires supplying the passphrase to read the existing data first when called from the CLI wizard).

```python
store.set("s3-prod", {
    "backend":               "s3",
    "bucket":                "my-bucket",
    "prefix":                "pfv/project",
    "region":                "us-east-1",
    "aws_access_key_id":     "AKIA...",
    "aws_secret_access_key": "...",
}, passphrase="hunter2")
```

**`get(profile, passphrase=None) -> dict`**

Decrypt and return the credentials dict. Raises `KeyError` if the profile does not exist. Raises `ValueError` if the passphrase is wrong.

**`list_profiles() -> list[str]`**

Return sorted profile names. No decryption needed.

**`delete(profile, passphrase=None) -> bool`**

Remove a profile. Requires the correct passphrase (to prevent casual deletion by someone who found the file but cannot decrypt it). Returns `True` if the profile existed.

**`exists(profile) -> bool`**

Return whether a profile is stored, without decryption.

### `find_credential_store`

```python
find_credential_store(start: Path | None = None) -> CredentialStore
```

Walk up from `start` (default: cwd) looking for a `.pfv/` directory and return a `CredentialStore` pointing at the first `credentials.json` found. Falls back to `cwd/.pfv/credentials.json` if none is found.

### `load_profile`

```python
load_profile(
    profile: str,
    work_tree: Path | None = None,
    passphrase: str | None = None,
) -> dict
```

Convenience wrapper: find the credential store, decrypt, and return the profile dict.

### Credential profile fields by backend

**S3 / S3-compatible**

| Field | Required | Notes |
|---|---|---|
| `backend` | yes | `"s3"` |
| `bucket` | yes | S3 bucket name |
| `prefix` | no | key prefix within the bucket |
| `region` | no | AWS region |
| `aws_access_key_id` | no | explicit access key |
| `aws_secret_access_key` | no | explicit secret key |
| `aws_session_token` | no | temporary session token |
| `endpoint_url` | no | custom endpoint (MinIO, LocalStack, etc.) |
| `profile` | no | AWS named credentials profile |

**Google Drive (stub)**

| Field | Required | Notes |
|---|---|---|
| `backend` | yes | `"gdrive"` |
| `folder_id` | yes | Drive folder ID |
| `credentials_file` | no | path to OAuth2 credentials JSON |
| `token_file` | no | path to cached token file |

**Azure (stub)**

| Field | Required | Notes |
|---|---|---|
| `backend` | yes | `"azure"` |
| `container` | yes | blob container name |
| `prefix` | no | blob name prefix |
| `connection_string` | no | Azure storage connection string |
| `account_name` | no | storage account name |
| `account_key` | no | storage account key |

---

## 8. Command-line interface — `pfv_cli.py`

All commands accept `--storage`, `--cred-profile`, and `--cred-passphrase`. S3-specific flags (`--s3-region`, `--s3-profile`, `--s3-endpoint`) are available on every command too and override values from a credential profile.

### Global storage flags

| Flag | Default | Notes |
|---|---|---|
| `--storage LOCATION` | `"."` | local path, `s3://bucket/prefix`, `gdrive://id`, `azure://container`, or scheme-only like `s3:` |
| `--cred-profile NAME` | none | load credentials from `.pfv/credentials.json` |
| `--cred-passphrase PW` | prompted | passphrase for credential profile |
| `--s3-region REGION` | none | overrides profile value |
| `--s3-profile PROFILE` | none | AWS credentials profile |
| `--s3-endpoint URL` | none | custom S3 endpoint |

### Repo commands

```
pfv init    <vdir> [--owner NAME] [--desc TEXT]
pfv commit  <vdir> <file> [-m MSG] [--author NAME] [--tag LABEL] [--extra-meta JSON]
pfv log     <vdir>
pfv info    <vdir> <version>
pfv verify  <vdir>
pfv prune   <vdir> --keep N
pfv delete  <vdir> <version>
pfv flatten <vdir> <dest_dir> [--prefix PREFIX]
```

### Working copy commands

```
pfv checkout <vdir> <version> <dest> [--track] [--work-tree DIR] [--state-backend B]
pfv checkin  <dest> [-m MSG] [--author NAME] [--tag LABEL] [--work-tree DIR] [--state-backend B]
pfv status   [--work-tree DIR] [--state-backend B]
pfv abandon  <dest> [--work-tree DIR] [--state-backend B]
```

`--state-backend` is one of `json` (default), `sqlite`, or `redis`.

### Lock commands

```
pfv lock   <vdir> [--version V] [--holder NAME] [--reason TEXT] [--expires ISO8601]
pfv unlock <vdir> [--version V]
```

Omit `--version` for a repo-level lock that blocks all commits.

### Credential commands

```
pfv credentials set    <profile> [--passphrase PW] [--json JSON] [--work-tree DIR]
pfv credentials get    <profile> [--passphrase PW] [--work-tree DIR]
pfv credentials list   [--work-tree DIR]
pfv credentials delete <profile> [--passphrase PW] [--work-tree DIR]
pfv credentials test   <profile> [--passphrase PW] [--work-tree DIR]
```

`pfv creds` is an alias for `pfv credentials`.

`credentials set` opens an interactive wizard unless `--json` is supplied. `credentials get` prints all field names and values, with secrets shown as `[hidden]`. `credentials test` opens the backend described by the profile and performs a minimal ping (listing top-level keys).

### Version specifiers

Wherever a command accepts `<version>`, you may pass:

- a plain integer (`3`)
- `latest` — the highest committed version
- `HEAD` — the version last checked out or committed
- a tag name (`approved`)

---

## 9. Exception reference

All exceptions inherit from `PFVError`.

| Exception | Raised when |
|---|---|
| `PFVError` | Base class; catch this to handle any PFV error |
| `NotAPFVDirectory` | `vdir` has not been initialised (no `latest` file found in storage) |
| `VersionNotFound` | Version number, tag, or specifier resolves to nothing; or the slot is a deleted tombstone |
| `SlotLocked` | `commit()` when a repo-level `.lock` exists; `delete_version()` when the slot has a `.lock` |
| `NoContentInSlot` | `checkout()` on a slot with no binary, `.link`, or `.delta` |
| `RepoConflict` | `checkin()` when the repo has advanced since the file was checked out |

`RepoConflict` carries extra attributes:

```python
e.checked_out_version   # int — version at checkout
e.current_version       # int — current latest in repo
e.checked_out_sha       # str — sha256 hex at checkout
e.current_sha           # str — sha256 hex of current latest
```

---

## 10. Data class reference

### `RepoInfo`

Returned by `log()`.

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | same as `vdir` |
| `vdir` | `str` | logical name (e.g. `"render.exr"`) |
| `storage` | `StorageBackend` | the backend used to read this info |
| `latest` | `int` | highest committed version number |
| `head` | `int` | version last checked out or committed |
| `slots` | `list[SlotInfo]` | one entry per version, ordered 1 → latest |
| `repo_meta` | `dict` | contents of `.repo.meta` |

### `SlotInfo`

One entry per version slot in `RepoInfo.slots`.

| Field | Type | Notes |
|---|---|---|
| `version` | `int` | slot number |
| `has_binary` | `bool` | binary content file present |
| `has_link` | `bool` | `.link` file present |
| `has_delta` | `bool` | `.delta` file present |
| `has_meta` | `bool` | `.meta` file present |
| `has_sha` | `bool` | `.sha` file present |
| `is_locked` | `bool` | `.lock` file present |
| `is_deleted` | `bool` | `deleted` field in `.meta` is `true` |
| `tag` | `str \| None` | contents of `.tag`, or `None` |
| `meta` | `dict` | parsed contents of `.meta` |
| `lock` | `dict` | parsed contents of `.lock`, or `{}` |
| `hashes` | `list[str]` | lines from `.sha` (e.g. `["sha256:abc…"]`) |
| `content_type` | `str` (property) | `"binary"`, `"link"`, `"delta"`, or `"none"` |

---

## 11. Dependency summary

| Package | Required for | Install |
|---|---|---|
| `cryptography` | credential encryption | `pip install cryptography` |
| `boto3` | S3 storage backend | `pip install boto3` |
| `redis` | Redis state backend | `pip install redis` |
| stdlib `sqlite3` | SQLite state backend | included with Python |
| `google-api-python-client` | Google Drive backend (stub) | `pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib` |
| `azure-storage-blob` | Azure backend (stub) | `pip install azure-storage-blob` |

Minimum for local use only: no third-party packages needed beyond the standard library (the credential store requires `cryptography` if you want to save credentials).

---

## 12. Extending PFV

### Adding a storage backend

1. Subclass `StorageBackend` in `pfv_storage.py`.
2. Implement the eight abstract methods.
3. Add a URL scheme to `open_storage()`.

The only contract is: `write_bytes` followed by `read_bytes` on the same key returns the same bytes. Everything else in PFV follows from that.

### Adding a state backend

1. Subclass `StateBackend` in `pfv_state.py`.
2. Implement `upsert`, `get`, `remove`, `list_all`.
3. Add a case to `open_backend()`.

### Adding credential profile fields

The credential dict is an arbitrary JSON object. Add fields to the relevant `_*_FIELDS` list in `pfv_cli.py` to include them in the interactive wizard, and handle them in the backend constructor (unknown keys are absorbed by `**_ignored` in `S3Backend`).
