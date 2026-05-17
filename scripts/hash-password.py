#!/usr/bin/env python3
"""Generate a bcrypt hash of a password for ``ADMIN_PASSWORD_HASH``.

Usage:

    uv run scripts/hash-password.py
    Password: ********
    Confirm:  ********
    $2b$12$abc...xyz

Or pipe a single line in:

    echo -n "hunter2" | uv run scripts/hash-password.py
    $2b$12$abc...xyz

The interactive path uses ``getpass`` so the password never echoes to
the terminal and never lands in shell history. The piped path is for
scripted bootstrapping (CI provisioning, automated deploy templates)
- the trailing newline on a typical ``echo`` is stripped, but a real
trailing newline character in the password itself can't survive a
piped read; use the interactive form if your password ends with
whitespace.

The output is exactly one line: the bcrypt hash. Copy it into
``ADMIN_PASSWORD_HASH=`` in your ``.env`` (or your deploy template's
env block) and never the plaintext.
"""

from __future__ import annotations

import getpass
import sys

from layerloupe.auth.env_provider import hash_password


def _read_password() -> str:
    """Prompt for the password twice when interactive; trust stdin otherwise.

    Interactive flow rejects empty input and a mismatch - better to
    fail loud than to ship a hash of an empty string. Piped flow
    accepts whatever's on stdin verbatim (minus exactly one trailing
    newline), so a tool feeding from a secret manager isn't second-guessed.
    """
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.endswith("\r\n"):
            raw = raw[:-2]
        elif raw.endswith("\n") or raw.endswith("\r"):
            raw = raw[:-1]
        if not raw:
            print("error: empty password on stdin", file=sys.stderr)
            sys.exit(2)
        return raw

    password = getpass.getpass("Password: ")
    if not password:
        print("error: empty password", file=sys.stderr)
        sys.exit(2)
    confirm = getpass.getpass("Confirm:  ")
    if password != confirm:
        print("error: passwords do not match", file=sys.stderr)
        sys.exit(2)
    return password


def main() -> int:
    password = _read_password()
    hashed = hash_password(password)
    print(hashed)
    if sys.stdin.isatty():
        # Interactive flow: the operator is about to paste this into a
        # ``.env`` file. Compose's parser interprets ``$NAME`` segments
        # as variable references, which truncates a bcrypt hash silently.
        # Surface the wrapping recipe here so they don't have to find
        # the README for it.
        print(
            "\nFor Docker Compose .env files, paste with single quotes:\n"
            f"  ADMIN_PASSWORD_HASH='{hashed}'\n"
            "(Single quotes disable Compose's variable interpolation;"
            " double quotes don't.)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
