"""Tests for :mod:`layerloupe.utils.humanize`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from layerloupe.utils import human_size, human_time
from layerloupe.utils.humanize import _BYTE_UNITS  # noqa: F401  (presence check)

# -- human_size -----------------------------------------------------------


@pytest.mark.parametrize(
    ("num", "expected"),
    [
        (None, "0 B"),
        (0, "0 B"),
        (-5, "0 B"),
        (500, "500 B"),
        (1024, "1.0 KB"),
        (1500, "1.5 KB"),
        (1024 * 1024, "1.0 MB"),
        (int(1.5 * 1024 * 1024), "1.5 MB"),
        (1024**3, "1.0 GB"),
        (int(2.7 * 1024**3), "2.7 GB"),
        (1024**4, "1.0 TB"),
    ],
)
def test_human_size(num: int | None, expected: str) -> None:
    assert human_size(num) == expected


def test_human_size_uses_largest_fitting_unit() -> None:
    """Walking up the unit ladder happens at every 1024x boundary."""
    # 1024 GB = 1 TB.
    assert human_size(1024 * 1024**3) == "1.0 TB"
    # 1500 GB rolls up to TB (1.46), since the loop picks the largest fitting unit.
    assert human_size(1500 * 1024**3) == "1.5 TB"


# -- human_time -----------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=10), "just now"),
        (timedelta(seconds=60), "1 minute ago"),
        (timedelta(minutes=5), "5 minutes ago"),
        (timedelta(minutes=44), "44 minutes ago"),
        (timedelta(minutes=60), "1 hour ago"),
        (timedelta(hours=5), "5 hours ago"),
        (timedelta(hours=24), "1 day ago"),
        (timedelta(days=3), "3 days ago"),
        (timedelta(days=14), "2 weeks ago"),
        (timedelta(days=60), "2 months ago"),
        (timedelta(days=365), "1 year ago"),
        (timedelta(days=365 * 3), "3 years ago"),
    ],
)
def test_human_time_past(delta: timedelta, expected: str) -> None:
    when = _now() - delta
    assert human_time(when, now=_now()) == expected


def test_human_time_future() -> None:
    when = _now() + timedelta(days=2)
    assert human_time(when, now=_now()) == "in 2 days"


def test_human_time_naive_datetime_assumed_utc() -> None:
    """Pydantic gives us tz-naive datetimes when registries return ISO-Z strings."""
    when = datetime(2026, 5, 5, 12, 0, 0)  # naive
    assert "ago" in human_time(when, now=_now())


def test_human_time_unknown_returns_unknown() -> None:
    assert human_time(None) == "unknown"
