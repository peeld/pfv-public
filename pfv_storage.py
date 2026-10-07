"""
pfv_storage.py — Pluggable repo storage backends for Peel File Versions (PFV)

A StorageBackend abstracts all I/O so the same high-level logic works whether
the repo lives on a local filesystem, an S3 bucket, or any other store.

Backends
--------
  LocalBackend        local filesystem (default, no extra deps)
  S3Backend           Amazon S3 via boto3
  GoogleDriveBackend  stub — raises NotImplementedError
  AzureBackend        stub — raises NotImplementedError

Factory
-------
  open_storage(url_or_path, *, cred_profile=None, work_tree=None, passphrase=None, **kwargs)

  url_or_path examples
    "."                             → LocalBackend(".")
    "/mnt/repos/project"           → LocalBackend(...)
    "s3://my-bucket/pfv/project"   → S3Backend(...)
    "gdrive://folder-id"           → GoogleDriveBackend(...)   (stub)
    "azure://container/pfv"        → AzureBackend(...)         (stub)

  Credential profile
    When cred_profile is given, open_storage() loads that profile from
    .pfv/credentials.json and merges its fields into the backend kwargs.
    Explicitly supplied kwargs always override profile values.

    The profile dict should contain fields relevant to the backend:
      s3:     aws_access_key_id, aws_secret_access_key, region, bucket, prefix,
              endpoint_url, profile (AWS named profile)
      gdrive: credentials_file, token_file
      azure:  connection_string, account_name, account_key

Interface
---------
Every backend exposes:
  exists(key)              -> bool
  read_bytes(key)          -> bytes
  write_bytes(key, data)   -> None
  delete(key)              -> None
  list_keys(prefix)        -> list[str]
  download_to(key, local)  -> None
  upload_from(key, local)  -> None
  copy(src_key, dst_key)   -> None
  close()                  -> None

Text/JSON/int helpers are on the base class (no need to override):
  read_text / write_text / read_json / write_json / read_int / write_int
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class StorageBackend(ABC):
    """Abstract interface for PFV repo storage."""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def read_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def list_keys(self, prefix: str = "") -> list[str]: ...

    @abstractmethod
    def download_to(self, key: str, local: Path) -> None:
        """Stream key content to a local file (handles large binaries)."""

    @abstractmethod
    def upload_from(self, key: str, local: Path) -> None:
        """Stream a local file to key (handles large binaries)."""

    @abstractmethod
    def copy(self, src_key: str, dst_key: str) -> None:
        """Duplicate a key within the backend (server-side where possible)."""

    def close(self) -> None:
        """Optional teardown."""

    # ---- text / JSON / int helpers ----

    def read_text(self, key: str) -> str:
        return self.read_bytes(key).decode("utf-8")

    def write_text(self, key: str, text: str) -> None:
        self.write_bytes(key, text.encode("utf-8"))

    def read_json(self, key: str) -> dict:
        return json.loads(self.read_bytes(key))

    def write_json(self, key: str, obj: dict) -> None:
        self.write_bytes(key, (json.dumps(obj, indent=2) + "\n").encode("utf-8"))

    def read_int(self, key: str) -> int:
        return int(self.read_text(key).strip())

    def write_int(self, key: str, n: int) -> None:
        self.write_text(key, str(n))

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


# ---------------------------------------------------------------------------
# LocalBackend
# ---------------------------------------------------------------------------

class LocalBackend(StorageBackend):
    """
    Stores PFV repos on the local filesystem.

    root : directory that contains versioned-file directories.
           Keys are relative paths inside root (e.g. "video.mp4/1.meta").
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root)):
            raise ValueError(f"Key {key!r} escapes root {self.root}")
        return p

    def exists(self, key: str) -> bool:
        return self._p(key).exists()

    def read_bytes(self, key: str) -> bytes:
        return self._p(key).read_bytes()

    def write_bytes(self, key: str, data: bytes) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise

    def delete(self, key: str) -> None:
        p = self._p(key)
        if p.exists():
            p.unlink()

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self._p(prefix) if prefix else self.root
        if not base.exists():
            return []
        # Use as_posix() so keys are always forward-slash separated on all platforms
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in base.rglob("*") if p.is_file()
        )

    def download_to(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        src = self._p(key)
        fd, tmp = tempfile.mkstemp(dir=local.parent)
        try:
            os.close(fd)
            shutil.copy2(src, tmp)
            os.replace(tmp, local)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise

    def upload_from(self, key: str, local: Path) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent)
        try:
            os.close(fd)
            shutil.copy2(local, tmp)
            os.replace(tmp, p)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise

    def copy(self, src_key: str, dst_key: str) -> None:
        dst = self._p(dst_key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._p(src_key), dst)

    def __repr__(self) -> str:
        return f"<LocalBackend root={self.root}>"


# ---------------------------------------------------------------------------
# S3Backend
# ---------------------------------------------------------------------------

class S3Backend(StorageBackend):
    """
    Stores PFV repos in Amazon S3.

    All keys are stored under s3://<bucket>/<prefix>/<key>.
    Requires: pip install boto3

    Constructor parameters (all optional except bucket)
    ---------------------------------------------------
    bucket          : S3 bucket name
    prefix          : key prefix within the bucket (no trailing slash)
    region          : AWS region
    profile         : AWS credentials profile name (~/.aws/credentials)
    aws_access_key_id     : explicit AWS access key (overrides env / profile)
    aws_secret_access_key : explicit AWS secret key (overrides env / profile)
    aws_session_token     : explicit session token (for temporary credentials)
    endpoint_url    : custom endpoint (e.g. http://localhost:9000 for MinIO)
    chunk_size      : multipart chunk size in bytes (default 8 MB)
    """

    DEFAULT_CHUNK = 8 * 1024 * 1024

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
        profile: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
        endpoint_url: str | None = None,
        chunk_size: int = DEFAULT_CHUNK,
        **_ignored: Any,   # absorb unknown credential profile fields gracefully
    ) -> None:
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError as e:
            raise ImportError("S3Backend requires boto3: pip install boto3") from e

        self._ClientError = ClientError
        self.bucket     = bucket
        self.prefix     = prefix.strip("/")
        self.chunk_size = chunk_size

        session_kwargs: dict = {}
        if profile:
            session_kwargs["profile_name"] = profile
        if aws_access_key_id:
            session_kwargs["aws_access_key_id"] = aws_access_key_id
        if aws_secret_access_key:
            session_kwargs["aws_secret_access_key"] = aws_secret_access_key
        if aws_session_token:
            session_kwargs["aws_session_token"] = aws_session_token
        if region:
            session_kwargs["region_name"] = region

        session = boto3.Session(**session_kwargs)

        client_kwargs: dict = {}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url

        self._s3 = session.client("s3", **client_kwargs)

    def _full_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._full_key(key))
            return True
        except self._ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def read_bytes(self, key: str) -> bytes:
        resp = self._s3.get_object(Bucket=self.bucket, Key=self._full_key(key))
        return resp["Body"].read()

    def write_bytes(self, key: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=self._full_key(key), Body=data)

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=self._full_key(key))

    def list_keys(self, prefix: str = "") -> list[str]:
        full_prefix = (
            self._full_key(prefix) if prefix
            else (self.prefix + "/" if self.prefix else "")
        )
        paginator = self._s3.get_paginator("list_objects_v2")
        strip = (self.prefix + "/") if self.prefix else ""
        keys = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                if strip and k.startswith(strip):
                    k = k[len(strip):]
                keys.append(k)
        return sorted(keys)

    def download_to(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=local.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                self._s3.download_fileobj(
                    self.bucket, self._full_key(key), f,
                    Config=self._transfer_config(),
                )
            os.replace(tmp, local)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise

    def upload_from(self, key: str, local: Path) -> None:
        with open(local, "rb") as f:
            self._s3.upload_fileobj(
                f, self.bucket, self._full_key(key),
                Config=self._transfer_config(),
            )

    def copy(self, src_key: str, dst_key: str) -> None:
        self._s3.copy_object(
            Bucket=self.bucket,
            CopySource={"Bucket": self.bucket, "Key": self._full_key(src_key)},
            Key=self._full_key(dst_key),
        )

    def _transfer_config(self):
        from boto3.s3.transfer import TransferConfig
        return TransferConfig(multipart_chunksize=self.chunk_size)

    def close(self) -> None:
        self._s3.close()

    def __repr__(self) -> str:
        suffix = f"/{self.prefix}" if self.prefix else ""
        return f"<S3Backend s3://{self.bucket}{suffix}>"


# ---------------------------------------------------------------------------
# GoogleDriveBackend (stub)
# ---------------------------------------------------------------------------

class GoogleDriveBackend(StorageBackend):
    """
    Stub for Google Drive storage.

    To implement: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
    Credential profile fields: credentials_file, token_file
    """

    def __init__(self, folder_id: str, credentials_file: str | None = None,
                 token_file: str | None = None, **_: Any) -> None:
        self.folder_id        = folder_id
        self.credentials_file = credentials_file
        self.token_file       = token_file

    def _nyi(self, name: str):
        raise NotImplementedError(
            f"GoogleDriveBackend.{name} is not yet implemented. "
            "See pfv_storage.py for the interface to implement."
        )

    def exists(self, key: str) -> bool:           self._nyi("exists")
    def read_bytes(self, key: str) -> bytes:       self._nyi("read_bytes")
    def write_bytes(self, key: str, data: bytes):  self._nyi("write_bytes")
    def delete(self, key: str):                    self._nyi("delete")
    def list_keys(self, prefix: str = ""):         self._nyi("list_keys")
    def download_to(self, key: str, local: Path):  self._nyi("download_to")
    def upload_from(self, key: str, local: Path):  self._nyi("upload_from")
    def copy(self, src: str, dst: str):            self._nyi("copy")

    def __repr__(self) -> str:
        return f"<GoogleDriveBackend folder_id={self.folder_id}>"


# ---------------------------------------------------------------------------
# AzureBackend (stub)
# ---------------------------------------------------------------------------

class AzureBackend(StorageBackend):
    """
    Stub for Azure Blob Storage.

    To implement: pip install azure-storage-blob
    Credential profile fields: connection_string, account_name, account_key
    """

    def __init__(self, container: str, prefix: str = "",
                 connection_string: str | None = None,
                 account_name: str | None = None,
                 account_key: str | None = None,
                 **_: Any) -> None:
        self.container         = container
        self.prefix            = prefix
        self.connection_string = connection_string
        self.account_name      = account_name
        self.account_key       = account_key

    def _nyi(self, name: str):
        raise NotImplementedError(
            f"AzureBackend.{name} is not yet implemented. "
            "See pfv_storage.py for the interface to implement."
        )

    def exists(self, key: str) -> bool:           self._nyi("exists")
    def read_bytes(self, key: str) -> bytes:       self._nyi("read_bytes")
    def write_bytes(self, key: str, data: bytes):  self._nyi("write_bytes")
    def delete(self, key: str):                    self._nyi("delete")
    def list_keys(self, prefix: str = ""):         self._nyi("list_keys")
    def download_to(self, key: str, local: Path):  self._nyi("download_to")
    def upload_from(self, key: str, local: Path):  self._nyi("upload_from")
    def copy(self, src: str, dst: str):            self._nyi("copy")

    def __repr__(self) -> str:
        return f"<AzureBackend container={self.container} prefix={self.prefix!r}>"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def open_storage(
    location: str | Path,
    *,
    cred_profile: str | None = None,
    work_tree: Path | None = None,
    passphrase: str | None = None,
    **kwargs: Any,
) -> StorageBackend:
    """
    Parse *location* and return an appropriate StorageBackend.

    Credential profiles
    -------------------
    If *cred_profile* is given, the named profile is loaded from
    .pfv/credentials.json (searching upward from *work_tree* or cwd).
    The profile fields are merged into *kwargs*, with explicit *kwargs*
    taking precedence.

    If the profile contains a "backend" and/or "bucket"/"prefix" field,
    those will be used to construct the URL if *location* is omitted or
    just a scheme hint like "s3:".

    location formats
    ----------------
    Local path (str or Path):          "."  "/mnt/repos"  "~/pfv"
    S3:                                "s3://bucket/optional/prefix"
    Google Drive (stub):               "gdrive://folder-id"
    Azure Blob Storage (stub):         "azure://container/optional/prefix"

    S3 extra kwargs (or via credential profile)
    -------------------------------------------
    region, profile (AWS profile), aws_access_key_id, aws_secret_access_key,
    aws_session_token, endpoint_url, chunk_size

    Examples
    --------
    >>> store = open_storage(".")
    >>> store = open_storage("s3://my-bucket/pfv", region="us-east-1")
    >>> store = open_storage("s3://my-bucket/pfv",
    ...     aws_access_key_id="AKIA...", aws_secret_access_key="...")
    >>> store = open_storage("s3://my-bucket/pfv",
    ...     cred_profile="s3-prod", work_tree=Path("."))
    >>> # Location inferred entirely from the credential profile:
    >>> store = open_storage("s3:", cred_profile="s3-prod")
    """
    # --- load credential profile if requested ---
    if cred_profile:
        from pfv_credentials import load_profile
        try:
            profile_data = load_profile(cred_profile, work_tree=work_tree,
                                        passphrase=passphrase)
        except KeyError:
            raise KeyError(
                f"Credential profile {cred_profile!r} not found. "
                "Run: pfv credentials set <profile>"
            )
        # Explicit kwargs win over profile values
        merged = {**profile_data, **kwargs}
        kwargs = merged

    loc = str(location)

    # --- S3 ---
    if loc.startswith("s3://") or loc == "s3:":
        if loc.startswith("s3://"):
            rest   = loc[5:]
            parts  = rest.split("/", 1)
            bucket = parts[0]
            prefix = parts[1] if len(parts) > 1 else ""
        else:
            # Location is just "s3:" — pull bucket/prefix from credential profile
            bucket = kwargs.pop("bucket", "")
            prefix = kwargs.pop("prefix", "")
            if not bucket:
                raise ValueError(
                    "No bucket specified. Either use s3://bucket/prefix or "
                    "include 'bucket' in the credential profile."
                )
        # prefix from profile is lower priority than URL prefix
        if "prefix" in kwargs and not prefix:
            prefix = kwargs.pop("prefix")
        else:
            kwargs.pop("prefix", None)
        return S3Backend(bucket=bucket, prefix=prefix, **kwargs)

    # --- Google Drive ---
    if loc.startswith("gdrive://") or loc == "gdrive:":
        folder_id = loc[9:] if loc.startswith("gdrive://") else kwargs.pop("folder_id", "")
        return GoogleDriveBackend(folder_id=folder_id, **kwargs)

    # --- Azure ---
    if loc.startswith("azure://") or loc == "azure:":
        if loc.startswith("azure://"):
            rest      = loc[8:]
            parts     = rest.split("/", 1)
            container = parts[0]
            prefix    = parts[1] if len(parts) > 1 else ""
        else:
            container = kwargs.pop("container", "")
            prefix    = kwargs.pop("prefix", "")
        return AzureBackend(container=container, prefix=prefix, **kwargs)

    # --- Local filesystem ---
    return LocalBackend(Path(loc).expanduser())
