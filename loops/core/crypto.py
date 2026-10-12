"""Fernet encryption for secrets kept at rest (Gmail refresh token, Telethon session)."""

from cryptography.fernet import Fernet

from loops.core.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not set. Generate one with: loops gen-key")
    return Fernet(key.encode())


def encrypt(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt(token: bytes) -> bytes:
    return _fernet().decrypt(token)


def generate_key() -> str:
    return Fernet.generate_key().decode()
