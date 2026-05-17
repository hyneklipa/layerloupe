"""Generic loader for ``*_FILE`` environment variables.

LayerLoupe follows the Docker official image convention (used by
``POSTGRES_PASSWORD_FILE``, ``MARIADB_PASSWORD_FILE`` and friends): a
secret can be supplied either inline in an env var, or via a separate
env var ending in ``_FILE`` that points to a path containing the value.

This module is the single point where we resolve the two sources to one
value and warn on misconfiguration (both set, file missing, file empty,
…). The threat model assumption is that the ``*_FILE`` path comes from a
sealed channel - Docker secrets, Kubernetes Secret volume mount, Vault
agent injector - so the file contents are plaintext. Inline env values
typically are *not* plaintext (they're hashes, for the
``ADMIN_PASSWORD_HASH`` use case), but this loader itself doesn't care
- it returns whatever string was supplied. Callers decide how to
interpret it.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SecretFileError(ValueError):
    """Raised when a ``*_FILE`` env var is set but its target is unreadable / empty."""


def resolve_secret(
    value: str | None,
    file_path: str | Path | None,
    *,
    name: str = "secret",
) -> str | None:
    """Return the effective value of a secret given inline + file sources.

    Args:
        value: The direct env value (e.g. ``ADMIN_PASSWORD_HASH``). May
            be ``None`` if the env var isn't set.
        file_path: The ``*_FILE`` env value (e.g.
            ``ADMIN_PASSWORD_FILE``). May be ``None``.
        name: Logical name used in warning / error messages - e.g.
            ``"ADMIN_PASSWORD"``. Used only for diagnostics.

    Returns:
        - ``None`` when neither source is set.
        - The contents of ``file_path`` (stripped of one trailing CR/LF)
          when only the file source is set.
        - ``value`` when only the inline source is set.
        - The contents of ``file_path`` with a logged warning when both
          are set (file wins - it's the more secure source, and an
          operator who supplied both probably added FILE later and
          forgot to drop the inline value).

    Raises:
        SecretFileError: when ``file_path`` is set but the file can't be
            read or its contents are empty.
    """
    if file_path is not None and value is not None:
        logger.warning(
            "Both %s and %s_FILE are set - using file value, ignoring inline.",
            name,
            name,
        )
    if file_path is not None:
        return _read_secret_file(Path(file_path), name=name)
    return value


def _read_secret_file(path: Path, *, name: str) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretFileError(f"Could not read {name}_FILE at {path}: {exc}") from exc
    # Trim a single trailing newline (``echo $secret > file`` convention).
    # We don't ``.strip()`` because a secret could legitimately end with
    # whitespace - rare, but breaking it silently would be worse than
    # reading what the operator actually put there.
    if raw.endswith("\r\n"):
        raw = raw[:-2]
    elif raw.endswith("\n") or raw.endswith("\r"):
        raw = raw[:-1]
    if not raw:
        raise SecretFileError(f"{name}_FILE at {path} is empty")
    return raw
