"""Fernet symmetric encryption for OAuth tokens at rest.

Key comes from Settings.fernet_key (file-backed, separate from the DB — T12).
Never log plaintext tokens or the key.
"""
from __future__ import annotations

from cryptography.fernet import Fernet

from app.config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().fernet_key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
