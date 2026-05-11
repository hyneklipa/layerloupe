"""Tests for the generic ``*_FILE`` secret loader."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from layerloupe.config_secrets import SecretFileError, resolve_secret


def test_neither_source_returns_none() -> None:
    assert resolve_secret(None, None) is None


def test_inline_only_returns_value() -> None:
    assert resolve_secret("inline-value", None) == "inline-value"


def test_file_only_returns_file_contents(tmp_path: Path) -> None:
    f = tmp_path / "secret.txt"
    f.write_text("file-value")
    assert resolve_secret(None, f) == "file-value"


def test_file_strips_one_trailing_lf(tmp_path: Path) -> None:
    """``echo $secret > file`` adds a trailing LF — we trim exactly one."""
    f = tmp_path / "secret.txt"
    f.write_text("value-with-trailing-lf\n")
    assert resolve_secret(None, f) == "value-with-trailing-lf"


def test_file_strips_one_trailing_crlf(tmp_path: Path) -> None:
    f = tmp_path / "secret.txt"
    f.write_bytes(b"value-with-crlf\r\n")
    assert resolve_secret(None, f) == "value-with-crlf"


def test_file_strips_only_one_newline(tmp_path: Path) -> None:
    """Two trailing newlines → keep one (don't strip all whitespace)."""
    f = tmp_path / "secret.txt"
    f.write_text("value\n\n")
    assert resolve_secret(None, f) == "value\n"


def test_file_preserves_trailing_space(tmp_path: Path) -> None:
    """A password can legitimately end with whitespace — preserve it."""
    f = tmp_path / "secret.txt"
    f.write_text("value-with-space \n")
    assert resolve_secret(None, f) == "value-with-space "


def test_file_missing_raises(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does-not-exist"
    with pytest.raises(SecretFileError) as excinfo:
        resolve_secret(None, nonexistent, name="ADMIN_PASSWORD")
    assert "ADMIN_PASSWORD_FILE" in str(excinfo.value)
    assert str(nonexistent) in str(excinfo.value)


def test_file_empty_raises(tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("")
    with pytest.raises(SecretFileError) as excinfo:
        resolve_secret(None, f, name="ADMIN_PASSWORD")
    assert "empty" in str(excinfo.value)


def test_file_only_whitespace_after_strip_raises(tmp_path: Path) -> None:
    """File with just a newline → empty after strip → error."""
    f = tmp_path / "newline.txt"
    f.write_text("\n")
    with pytest.raises(SecretFileError):
        resolve_secret(None, f)


def test_both_sources_prefers_file_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    f = tmp_path / "secret.txt"
    f.write_text("file-wins")
    with caplog.at_level(logging.WARNING, logger="layerloupe.config_secrets"):
        result = resolve_secret("inline-loses", f, name="ADMIN_PASSWORD")
    assert result == "file-wins"
    assert any(
        "ADMIN_PASSWORD" in rec.message and "ADMIN_PASSWORD_FILE" in rec.message
        for rec in caplog.records
    )


def test_path_accepts_string(tmp_path: Path) -> None:
    """``file_path`` parameter accepts both ``str`` and ``Path``."""
    f = tmp_path / "secret.txt"
    f.write_text("ok")
    assert resolve_secret(None, str(f)) == "ok"
    assert resolve_secret(None, f) == "ok"
