"""Encrypted, server-side storage for connected OAuth accounts."""
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

STORE = Path("data/connections.enc")


def _fernet():
    secret = os.getenv("OAUTH_ENCRYPTION_SECRET") or os.getenv("APP_PASSWORD")
    if not secret:
        raise RuntimeError("Set OAUTH_ENCRYPTION_SECRET (or APP_PASSWORD) to encrypt connected-account tokens.")
    key = base64.urlsafe_b64encode(hashlib.sha256(("forge-oauth-v1:" + secret).encode()).digest())
    return Fernet(key)


def load_connections():
    if not STORE.exists():
        return {}
    try:
        return json.loads(_fernet().decrypt(STORE.read_bytes()))
    except InvalidToken as exc:
        raise RuntimeError("Stored OAuth credentials cannot be decrypted. Keep OAUTH_ENCRYPTION_SECRET stable.") from exc


def save_connections(data):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_bytes(_fernet().encrypt(json.dumps(data).encode()))
    try:
        STORE.chmod(0o600)
    except OSError:
        pass


def get_access_token(provider):
    return load_connections().get(provider, {}).get("access_token", "")
