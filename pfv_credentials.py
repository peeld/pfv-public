"""
pfv_credentials.py — Encrypted credential store for Peel File Versions (PFV)

Credentials are stored in <work_tree>/.pfv/credentials.json as Fernet-encrypted
blobs, one per named profile.  The encryption key is derived from a passphrase
using PBKDF2-HMAC-SHA256 with a per-file random salt.

File layout (.pfv/credentials.json)
-------------------------------------
{
  "kdf": {
    "algorithm": "pbkdf2-hmac-sha256",
    "iterations": 600000,
    "salt": "<base64>"
  },
  "profiles": {
    "s3-prod": "<fernet-token-base64>",
    "s3-dev":  "<fernet-token-base64>"
  }
}

Each decrypted profile is a plain JSON object, e.g.:
{
  "backend":           "s3",
  "aws_access_key_id": "AKIA...",
  "aws_secret_access_key": "...",
  "region":            "us-east-1",
  "bucket":            "my-bucket",
  "prefix":            "pfv/project"
}

Security notes
--------------
- The file is chmod 600 on creation (owner read/write only).
- Passphrases are never stored; only the derived key is held in memory during
  a session and discarded afterwards.
- The salt is stored in plaintext in the file — this is safe; the salt's
  purpose is to prevent rainbow-table attacks on the passphrase, not to be
  a secret.
- 600 000 PBKDF2 iterations follow OWASP 2023 recommendations for HMAC-SHA256.

Usage
-----
    from pfv_credentials import CredentialStore

    store = CredentialStore(".pfv/credentials.json")

    # Save a profile (prompts for passphrase if none given)
    store.set("s3-prod", {
        "backend": "s3",
        "aws_access_key_id": "AKIA...",
        "aws_secret_access_key": "...",
        "region": "us-east-1",
        "bucket": "my-bucket",
        "prefix": "pfv/project",
    }, passphrase="hunter2")

    # Load a profile
    creds = store.get("s3-prod", passphrase="hunter2")

    # List profile names (no decryption needed)
    names = store.list_profiles()

    # Delete a profile
    store.delete("s3-prod", passphrase="hunter2")
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import stat
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Crypto helpers (all via stdlib + cryptography package)
# ---------------------------------------------------------------------------

_ITERATIONS = 600_000
_KEY_LEN    = 32   # 256-bit key for Fernet (which uses AES-128-CBC internally
                   # but accepts a 32-byte URL-safe base64 key)


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a 32-byte Fernet key from a passphrase + salt."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LEN,
        salt=salt,
        iterations=_ITERATIONS,
    )
    raw = kdf.derive(passphrase.encode("utf-8"))
    # Fernet expects URL-safe base64-encoded 32 bytes
    return base64.urlsafe_b64encode(raw)


def _encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.fernet import Fernet
    return Fernet(key).encrypt(data)


def _decrypt(token: bytes, key: bytes) -> bytes:
    from cryptography.fernet import Fernet, InvalidToken
    try:
        return Fernet(key).decrypt(token)
    except InvalidToken:
        raise ValueError("Decryption failed — wrong passphrase?")


# ---------------------------------------------------------------------------
# CredentialStore
# ---------------------------------------------------------------------------

class CredentialStore:
    """
    Manages named credential profiles in a single encrypted JSON file.

    Parameters
    ----------
    path : Path to the credentials file (e.g. Path(".pfv/credentials.json")).
           The parent directory is created if needed.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set(
        self,
        profile: str,
        credentials: dict[str, Any],
        passphrase: str | None = None,
    ) -> None:
        """
        Store *credentials* under *profile*, encrypting with *passphrase*.
        If passphrase is None, prompts interactively (with confirmation).
        """
        passphrase = passphrase or self._prompt_new()
        doc = self._load_doc()
        salt = self._get_or_create_salt(doc)
        key  = _derive_key(passphrase, salt)

        plaintext = json.dumps(credentials, indent=2).encode("utf-8")
        token = _encrypt(plaintext, key)

        doc["profiles"][profile] = base64.urlsafe_b64encode(token).decode("ascii")
        self._save_doc(doc)

    def get(
        self,
        profile: str,
        passphrase: str | None = None,
    ) -> dict[str, Any]:
        """
        Decrypt and return the credentials for *profile*.
        Raises KeyError if the profile doesn't exist.
        Raises ValueError on wrong passphrase.
        """
        doc = self._load_doc()
        if profile not in doc["profiles"]:
            raise KeyError(f"Credential profile {profile!r} not found")

        passphrase = passphrase or self._prompt_existing(profile)
        salt = base64.urlsafe_b64decode(doc["kdf"]["salt"])
        key  = _derive_key(passphrase, salt)

        token     = base64.urlsafe_b64decode(doc["profiles"][profile])
        plaintext = _decrypt(token, key)
        return json.loads(plaintext)

    def list_profiles(self) -> list[str]:
        """Return names of all stored profiles (no decryption needed)."""
        doc = self._load_doc()
        return sorted(doc["profiles"].keys())

    def delete(self, profile: str, passphrase: str | None = None) -> bool:
        """
        Remove *profile*.  Requires correct passphrase to prevent casual
        deletion by someone who found the file but doesn't know the passphrase.
        Returns True if the profile existed.
        """
        doc = self._load_doc()
        if profile not in doc["profiles"]:
            return False

        # Verify passphrase by decrypting — raises ValueError on mismatch
        passphrase = passphrase or self._prompt_existing(profile)
        salt = base64.urlsafe_b64decode(doc["kdf"]["salt"])
        key  = _derive_key(passphrase, salt)
        token = base64.urlsafe_b64decode(doc["profiles"][profile])
        _decrypt(token, key)  # raises on wrong passphrase

        del doc["profiles"][profile]
        self._save_doc(doc)
        return True

    def exists(self, profile: str) -> bool:
        """Return True if a profile with this name is stored."""
        return profile in self._load_doc()["profiles"]

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _load_doc(self) -> dict:
        if not self.path.exists():
            return {
                "kdf": {
                    "algorithm": "pbkdf2-hmac-sha256",
                    "iterations": _ITERATIONS,
                    "salt": None,
                },
                "profiles": {},
            }
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Corrupt credentials file {self.path}: {e}") from e

    def _save_doc(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        # Restrict to owner-only before moving into place
        try:
            tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # Windows — chmod is a no-op
        os.replace(tmp, self.path)
        # Ensure the final file is also 600
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def _get_or_create_salt(self, doc: dict) -> bytes:
        """Return existing salt or create a fresh one."""
        if doc["kdf"].get("salt"):
            return base64.urlsafe_b64decode(doc["kdf"]["salt"])
        salt = os.urandom(32)
        doc["kdf"]["salt"] = base64.urlsafe_b64encode(salt).decode("ascii")
        doc["kdf"]["iterations"] = _ITERATIONS
        return salt

    # ------------------------------------------------------------------
    # Interactive passphrase prompts
    # ------------------------------------------------------------------

    @staticmethod
    def _prompt_new() -> str:
        while True:
            pw  = getpass.getpass("New credentials passphrase: ")
            pw2 = getpass.getpass("Confirm passphrase: ")
            if pw == pw2:
                return pw
            print("Passphrases do not match — try again.")

    @staticmethod
    def _prompt_existing(profile: str) -> str:
        return getpass.getpass(f"Passphrase for profile {profile!r}: ")


# ---------------------------------------------------------------------------
# Convenience: find the nearest .pfv/credentials.json by walking up
# ---------------------------------------------------------------------------

def find_credential_store(start: Path | None = None) -> CredentialStore:
    """
    Walk up from *start* (default: cwd) looking for a .pfv/ directory.
    Returns a CredentialStore pointing at the first one found, or one
    anchored at cwd/.pfv/ if none is found (it will be created on first write).
    """
    here = Path(start or ".").resolve()
    for parent in [here, *here.parents]:
        candidate = parent / ".pfv" / "credentials.json"
        if candidate.exists():
            return CredentialStore(candidate)
    # Default: cwd
    return CredentialStore(here / ".pfv" / "credentials.json")


def load_profile(
    profile: str,
    work_tree: Path | None = None,
    passphrase: str | None = None,
) -> dict[str, Any]:
    """
    Shortcut: find the credential store and return the decrypted profile dict.
    Useful from pfv_storage.open_storage().
    """
    store = find_credential_store(work_tree)
    return store.get(profile, passphrase=passphrase)
