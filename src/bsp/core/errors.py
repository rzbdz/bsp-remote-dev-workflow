"""Error hierarchy. Every front-end (REST, CLI, AI tools) maps these the same way."""

from __future__ import annotations

from typing import Any


class BspError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, hint: str | None = None, details: Any = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        if self.details is not None:
            d["details"] = self.details
        return d


class NotFound(BspError):
    status = 404
    code = "not_found"


class InvalidRequest(BspError):
    status = 400
    code = "invalid_request"


class Forbidden(BspError):
    status = 403
    code = "forbidden"


class Cancelled(BspError):
    """A job stopped on request (the board is fine; the run was let go)."""
    status = 409
    code = "cancelled"


class Conflict(BspError):
    status = 409
    code = "conflict"


class HardwareError(BspError):
    status = 502
    code = "hardware_error"


class HardwareTimeout(HardwareError):
    status = 504
    code = "hardware_timeout"
