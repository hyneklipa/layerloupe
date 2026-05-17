"""Smoke tests for ``scripts/hash-password.py``.

The interactive path uses ``getpass`` which is awkward to drive in a
test (it bypasses ``sys.stdin``). We exercise the piped path - which
is also the path that matters in CI / automated provisioning - and
the structural properties of the file (exists, executable, shebang).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import bcrypt

SCRIPT = Path(__file__).parent.parent / "scripts" / "hash-password.py"


def test_script_exists() -> None:
    assert SCRIPT.exists()


def test_script_is_executable() -> None:
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/hash-password.py must be chmod +x"


def test_script_has_python_shebang() -> None:
    """The user runs it via ``uv run`` typically, but the shebang lets
    a direct ``./scripts/hash-password.py`` invocation work too."""
    first_line = SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!") and "python" in first_line


def test_piped_password_produces_verifiable_bcrypt_hash() -> None:
    """``echo -n 'hunter2' | hash-password.py`` → bcrypt hash on stdout."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="hunter2",
        capture_output=True,
        text=True,
        check=True,
        cwd=SCRIPT.parent.parent,
        env={**os.environ, "PYTHONPATH": str(SCRIPT.parent.parent)},
    )
    stdout = result.stdout.strip()
    assert stdout.startswith("$2b$"), f"not a bcrypt hash: {stdout!r}"
    # Verify the hash against the original plaintext - this is the
    # only contract the script needs to honor.
    assert bcrypt.checkpw(b"hunter2", stdout.encode("ascii"))


def test_piped_password_strips_one_trailing_newline() -> None:
    """``echo "hunter2" | hash-password.py`` (no ``-n``) appends one LF;
    the script strips it so the resulting hash verifies the password
    the operator actually typed."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="hunter2\n",
        capture_output=True,
        text=True,
        check=True,
        cwd=SCRIPT.parent.parent,
        env={**os.environ, "PYTHONPATH": str(SCRIPT.parent.parent)},
    )
    stdout = result.stdout.strip()
    assert bcrypt.checkpw(b"hunter2", stdout.encode("ascii"))
    # The trailing LF must NOT have been hashed in.
    assert not bcrypt.checkpw(b"hunter2\n", stdout.encode("ascii"))


def test_piped_empty_input_fails_with_clear_error() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="",
        capture_output=True,
        text=True,
        check=False,
        cwd=SCRIPT.parent.parent,
        env={**os.environ, "PYTHONPATH": str(SCRIPT.parent.parent)},
    )
    assert result.returncode == 2
    assert "empty" in result.stderr.lower()


def test_piped_password_with_special_chars() -> None:
    """Operator-chosen passwords often have shell metacharacters or
    unicode - make sure the script preserves them byte-for-byte."""
    plaintext = "p@$$w%rd:über!#$\n with spaces"
    # Strip the LF on input ourselves (one trailing newline gets eaten
    # by the script too, so we only feed plain content).
    cleaned = plaintext.replace("\n", "")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=cleaned,
        capture_output=True,
        text=True,
        check=True,
        cwd=SCRIPT.parent.parent,
        env={**os.environ, "PYTHONPATH": str(SCRIPT.parent.parent)},
    )
    stdout = result.stdout.strip()
    assert bcrypt.checkpw(cleaned.encode("utf-8"), stdout.encode("ascii"))
