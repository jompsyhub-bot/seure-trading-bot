from __future__ import annotations

import hmac
from cryptography.fernet import Fernet


class CredentialVault:
    """Encrypts optional credentials; never log or pass these through chat."""
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        return self._fernet.decrypt(ciphertext).decode()


def secure_equals(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(candidate.encode(), expected.encode())
