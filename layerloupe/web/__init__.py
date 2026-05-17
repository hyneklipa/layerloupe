"""HTML routes - Jinja2-rendered server-side UI.

Mirrors :mod:`layerloupe.api` (REST) but renders templates instead of JSON.
The page-level routes hand-render the static shell; htmx fragment routes
power in-place updates inside the three-column layout.
"""

from layerloupe.web import routes

__all__ = ["routes"]
