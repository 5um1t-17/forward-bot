"""Fernet encryption helpers for securing Telethon session strings."""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from bot.config import config


class SessionCrypto:
    """Encrypts/decrypts session strings with a Fernet key.

    The key can be supplied via the SESSION_ENCRYPTION_KEY env var. If missing,
    a random key is generated and persisted to SESSION_KEY_FILE (0600) so that
    previously stored sessions remain decryptable across restarts.
    """

    def __init__(self, key: str | None = None) -> None:
        self._fernet = Fernet(self._resolve_key(key).encode())

    def _resolve_key(self, key: str | None) -> str:
        if key:
            return key
        if os.path.exists(config.SESSION_KEY_FILE):
            with open(config.SESSION_KEY_FILE, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        generated = Fernet.generate_key().decode()
        os.makedirs(os.path.dirname(config.SESSION_KEY_FILE) or ".", exist_ok=True)
        fd = os.open(config.SESSION_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(generated)
        return generated

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Failed to decrypt session (invalid key or corrupted data)") from exc
