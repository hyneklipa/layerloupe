"""LayerLoupe package metadata.

Single source of truth for the version is ``pyproject.toml``;
:func:`importlib.metadata.version` reads it from the installed
``.dist-info/METADATA`` so we don't have to keep a literal in sync.

The ``PackageNotFoundError`` fallback covers the rare case of running
straight from a source checkout without ``uv sync`` / ``pip install``
having installed the package first (e.g. some IDE inspectors).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("layerloupe")
except PackageNotFoundError:  # pragma: no cover - uninstalled dev tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]