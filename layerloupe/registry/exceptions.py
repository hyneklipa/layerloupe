"""Typed exceptions for registry interactions."""

from __future__ import annotations


class RegistryError(Exception):
    """Base class for registry-related errors."""


class RegistryConnectionError(RegistryError):
    """Network failure: timeout, DNS, TLS, connection refused, etc."""


class RegistryHTTPError(RegistryError):
    """Registry returned a non-2xx response.

    Carries the status code and (a snippet of) the response body so that
    higher layers can map specific 4xx codes to user-facing messages
    (404 → "not found", 401 → "auth required", etc.).
    """

    def __init__(self, status_code: int, message: str, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body

    def __repr__(self) -> str:
        return f"RegistryHTTPError(status_code={self.status_code}, message={self.args[0]!r})"
