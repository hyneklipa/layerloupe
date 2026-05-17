"""Tiny humanizers - bytes → ``"1.5 MB"``, datetime → ``"3 weeks ago"``.

Self-contained (no ``humanize`` lib dep). Tuned for what the manifest info
panel actually shows: layer sizes (KB-GB) and image creation timestamps
(seconds ago through years ago).
"""

from __future__ import annotations

from datetime import UTC, datetime

_BYTE_UNITS: tuple[tuple[str, int], ...] = (
    ("TB", 1024**4),
    ("GB", 1024**3),
    ("MB", 1024**2),
    ("KB", 1024),
)


def human_size(num_bytes: int | None) -> str:
    """Format a byte count as ``"1.5 MB"``.

    ``None`` and 0 collapse to ``"0 B"`` - useful for schema 1 layers that
    don't carry size info. Uses binary (1 KB = 1024 B) since that's what
    Docker / ``docker images`` reports.
    """
    if num_bytes is None or num_bytes <= 0:
        return "0 B"
    for unit, divisor in _BYTE_UNITS:
        if num_bytes >= divisor:
            value = num_bytes / divisor
            # 1 decimal up to 999.9, no decimals for 1000+ to keep widths sane.
            return f"{value:.1f} {unit}" if value < 1000 else f"{value:.0f} {unit}"
    return f"{num_bytes} B"


def human_time(when: datetime | None, *, now: datetime | None = None) -> str:
    """Format a timestamp as ``"3 weeks ago"`` / ``"in 2 days"``.

    ``None`` collapses to ``"unknown"``. Naive datetimes are assumed UTC,
    which matches what registries return and what Pydantic gives us for
    ``ImageConfig.created`` parsed from RFC 3339 strings.
    """
    if when is None:
        return "unknown"
    reference = now if now is not None else datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    delta = reference - when
    seconds = int(delta.total_seconds())
    future = seconds < 0
    seconds = abs(seconds)

    def _format(value: int, unit: str) -> str:
        plural = "" if value == 1 else "s"
        return f"in {value} {unit}{plural}" if future else f"{value} {unit}{plural} ago"

    if seconds < 45:
        return "just now" if not future else "in a moment"
    if seconds < 90:
        return _format(1, "minute")
    minutes = seconds // 60
    if minutes < 45:
        return _format(minutes, "minute")
    if minutes < 90:
        return _format(1, "hour")
    hours = minutes // 60
    if hours < 24:
        return _format(hours, "hour")
    if hours < 36:
        return _format(1, "day")
    days = hours // 24
    if days < 14:
        return _format(days, "day")
    if days < 60:
        weeks = days // 7
        return _format(weeks, "week")
    if days < 365:
        months = days // 30
        return _format(months, "month")
    years = days // 365
    return _format(years, "year")
