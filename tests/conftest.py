"""Shared test fixtures: JSON loader for manifest fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load and parse a JSON fixture by basename (without extension)."""
    path = FIXTURE_DIR / f"{name}.json"
    with path.open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


def load_fixture_bytes(name: str) -> bytes:
    """Load a fixture's raw bytes (for digest verification, etc.)."""
    return (FIXTURE_DIR / f"{name}.json").read_bytes()


@pytest.fixture
def manifest_v2() -> dict[str, Any]:
    return load_fixture("manifest_v2")


@pytest.fixture
def manifest_oci() -> dict[str, Any]:
    return load_fixture("manifest_oci")


@pytest.fixture
def manifest_index() -> dict[str, Any]:
    return load_fixture("manifest_index")


@pytest.fixture
def manifest_docker_list() -> dict[str, Any]:
    return load_fixture("manifest_docker_list")


@pytest.fixture
def manifest_v1() -> dict[str, Any]:
    return load_fixture("manifest_v1")


@pytest.fixture
def image_config() -> dict[str, Any]:
    return load_fixture("image_config")
