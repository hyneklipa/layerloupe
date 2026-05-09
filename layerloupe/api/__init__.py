"""FastAPI routers for the public REST API.

Each module declares an :class:`APIRouter` covering a logical slice of the
API surface; :func:`layerloupe.main` mounts them together. Splitting by file
keeps the routes browsable and the dependency wiring local to its concern.
"""

from layerloupe.api import auth, manifests, repositories, system

__all__ = ["auth", "manifests", "repositories", "system"]
