"""Reusable authenticated encryption for application secrets."""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretKeyError(RuntimeError):
    """Encrypted state cannot be safely read with the supplied master key."""


class SecretBox:
    """Versioned Fernet envelope; the 32-byte master key stays outside SQLite."""

    VERSION = "v1"

    def __init__(self, master_key_file: Path) -> None:
        try:
            key = master_key_file.read_bytes()
        except OSError as error:
            raise SecretKeyError(
                "Master key is unavailable; encrypted data cannot be read."
            ) from error
        if len(key) != 32:
            raise SecretKeyError("Master key must contain exactly 32 bytes.")
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, plaintext: str) -> str:
        return f"{self.VERSION}:{self._fernet.encrypt(plaintext.encode()).decode()}"

    def decrypt(self, envelope: str) -> str:
        version, separator, token = envelope.partition(":")
        if version != self.VERSION or not separator:
            raise SecretKeyError("Unsupported encrypted-secret envelope.")
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as error:
            raise SecretKeyError("Master key is invalid or encrypted data was modified.") from error
