"""Session credential encryption.

The signed session cookie keeps a tampered client from forging credentials,
but it does **not** keep them secret — anyone who reads the cookie reads
the password. We layer Fernet on top so the password ciphertext sits in
the cookie instead of the plaintext.

The Fernet key is derived from ``settings.session_secret`` so operators
don't have to manage two secrets. The derivation is a one-shot SHA-256 →
URL-safe base64, which is what Fernet expects (32 bytes).

If the operator rotates ``SESSION_SECRET``, every existing
encrypted password becomes garbage; ``decrypt_password`` returns ``None``,
and the session-credential layer falls back to env-configured creds. That's
the desired behavior — invalidating in-flight sessions on secret rotation.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def _derive_fernet_key(secret: str) -> bytes:
    """Derive a 32-byte URL-safe base64 Fernet key from the session secret."""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_password(secret: str, password: str) -> str:
    """Encrypt ``password`` for storage in the session cookie."""
    fernet = Fernet(_derive_fernet_key(secret))
    token = fernet.encrypt(password.encode("utf-8"))
    return token.decode("ascii")


def decrypt_password(secret: str, ciphertext: str) -> str | None:
    """Decrypt a previously-encrypted password.

    Returns ``None`` if the ciphertext was forged, the secret rotated, or
    the input isn't valid base64 — in any of those cases the calling code
    should treat it as "no session creds" and fall back to env auth.
    """
    fernet = Fernet(_derive_fernet_key(secret))
    try:
        return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
