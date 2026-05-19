"""
Secret encryption for the connection store.

Connection credentials — Airflow usernames, passwords, API tokens — are
encrypted at rest with Fernet (symmetric AES), the same scheme Airflow
itself uses for connection passwords. The SQLite file never holds them
in plaintext.

The key comes from the FRONTFLOW_SECRET_KEY environment variable.
When it isn't set (local dev), a key is generated once and persisted to
`secret.key` beside the SQLite database — stable across restarts within
a dev environment, but never committed to source. Production deployments
should set the env var explicitly so the key is managed deliberately.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

_log = logging.getLogger("workflow")

# The environment variable the encryption key is read from.
_ENV_KEY = "FRONTFLOW_SECRET_KEY"


def _data_dir() -> Path:
    """The directory frontflow keeps local server state in — the
    encryption key, and the SQLite database when one is used. When the
    database is Postgres (no DB_PATH), this is just the key's home."""
    db = os.environ.get("DB_PATH")
    if db:
        return Path(db).resolve().parent
    home = os.environ.get("FRONTFLOW_HOME")
    base = Path(home) if home else Path.home() / ".frontflow"
    return base.resolve()


def _load_key() -> bytes:
    env = os.environ.get(_ENV_KEY)
    if env:
        return env.encode()
    key_path = _data_dir() / "secret.key"
    if key_path.exists():
        return key_path.read_bytes()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    _log.warning(
        "%s not set — generated a development key at %s. "
        "Set %s explicitly for production deployments.",
        _ENV_KEY, key_path, _ENV_KEY,
    )
    return key


_fernet = Fernet(_load_key())


def encrypt_secret(payload: dict[str, Any]) -> str:
    """Encrypt a JSON-serializable credential payload to an opaque token."""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return _fernet.encrypt(raw).decode()


def decrypt_secret(token: str) -> dict[str, Any]:
    """Reverse of encrypt_secret — recover the credential payload."""
    raw = _fernet.decrypt(token.encode())
    return json.loads(raw)
