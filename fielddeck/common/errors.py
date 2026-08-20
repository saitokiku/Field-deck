"""Structured, actionable FieldDeck errors.

Every error carries a stable machine code, a human sentence, structured
details, and — critically for an instrument — a statement of what was
*preserved* when the operation failed.  An operator who loses a capture
without being told is an operator who stops trusting the device.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable error identifiers.  These cross the RPC boundary; never rename."""

    # Authorization and safety
    PERMISSION_DENIED = "PermissionDenied"
    SAFETY_LIMIT_EXCEEDED = "SafetyLimitExceeded"
    ESTOP_ACTIVE = "EstopActive"
    LEASE_ERROR = "LeaseError"

    # Devices
    DEVICE_NOT_FOUND = "DeviceNotFound"
    DEVICE_DISCONNECTED = "DeviceDisconnected"
    DEVICE_BUSY = "DeviceBusy"
    UNSUPPORTED_CAPABILITY = "UnsupportedCapability"

    # Execution
    ACTION_TIMEOUT = "ActionTimeout"
    ACTION_CANCELLED = "ActionCancelled"
    PROTOCOL_ERROR = "ProtocolError"
    EXTERNAL_TOOL_ERROR = "ExternalToolError"
    CAPTURE_ERROR = "CaptureError"
    RECIPE_ERROR = "RecipeError"

    # Plumbing
    CONFIGURATION_ERROR = "ConfigurationError"
    INVALID_REQUEST = "InvalidRequest"
    UNKNOWN_ACTION = "UnknownAction"
    SESSION_ERROR = "SessionError"
    TRANSPORT_ERROR = "TransportError"
    INTERNAL_ERROR = "InternalError"


class FieldDeckError(Exception):
    """Base class for every error FieldDeck reports to a client."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        preserved: str | None = None,
        code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        #: What survived the failure, in plain language, e.g.
        #: "42 MiB of raw CAN frames were flushed to can/can0-0001.log".
        self.preserved = preserved
        if code is not None:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": str(self.code),
            "message": self.message,
            "details": self.details,
        }
        if self.preserved:
            payload["preserved"] = self.preserved
        return payload

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.preserved:
            return f"{self.message} (preserved: {self.preserved})"
        return self.message


class PermissionDenied(FieldDeckError):
    code = ErrorCode.PERMISSION_DENIED


class SafetyLimitExceeded(FieldDeckError):
    code = ErrorCode.SAFETY_LIMIT_EXCEEDED


class EstopActive(FieldDeckError):
    code = ErrorCode.ESTOP_ACTIVE


class LeaseError(FieldDeckError):
    code = ErrorCode.LEASE_ERROR


class DeviceNotFound(FieldDeckError):
    code = ErrorCode.DEVICE_NOT_FOUND


class DeviceDisconnected(FieldDeckError):
    code = ErrorCode.DEVICE_DISCONNECTED


class DeviceBusy(FieldDeckError):
    code = ErrorCode.DEVICE_BUSY


class UnsupportedCapability(FieldDeckError):
    code = ErrorCode.UNSUPPORTED_CAPABILITY


class ActionTimeout(FieldDeckError):
    code = ErrorCode.ACTION_TIMEOUT


class ActionCancelled(FieldDeckError):
    code = ErrorCode.ACTION_CANCELLED


class ProtocolError(FieldDeckError):
    code = ErrorCode.PROTOCOL_ERROR


class ExternalToolError(FieldDeckError):
    code = ErrorCode.EXTERNAL_TOOL_ERROR


class CaptureError(FieldDeckError):
    code = ErrorCode.CAPTURE_ERROR


class RecipeError(FieldDeckError):
    code = ErrorCode.RECIPE_ERROR


class ConfigurationError(FieldDeckError):
    code = ErrorCode.CONFIGURATION_ERROR


class InvalidRequest(FieldDeckError):
    code = ErrorCode.INVALID_REQUEST


class UnknownAction(FieldDeckError):
    code = ErrorCode.UNKNOWN_ACTION


class SessionError(FieldDeckError):
    code = ErrorCode.SESSION_ERROR


class TransportError(FieldDeckError):
    code = ErrorCode.TRANSPORT_ERROR


#: Maps a wire code back to the concrete class so clients can re-raise.
ERROR_CLASSES: dict[str, type[FieldDeckError]] = {
    str(cls.code): cls
    for cls in (
        PermissionDenied,
        SafetyLimitExceeded,
        EstopActive,
        LeaseError,
        DeviceNotFound,
        DeviceDisconnected,
        DeviceBusy,
        UnsupportedCapability,
        ActionTimeout,
        ActionCancelled,
        ProtocolError,
        ExternalToolError,
        CaptureError,
        RecipeError,
        ConfigurationError,
        InvalidRequest,
        UnknownAction,
        SessionError,
        TransportError,
    )
}


def error_from_dict(payload: dict[str, Any]) -> FieldDeckError:
    """Rebuild a typed exception from an RPC error payload."""
    code = str(payload.get("code", ErrorCode.INTERNAL_ERROR))
    cls = ERROR_CLASSES.get(code, FieldDeckError)
    err = cls(
        str(payload.get("message", "unknown error")),
        details=payload.get("details") or {},
        preserved=payload.get("preserved"),
    )
    if cls is FieldDeckError:
        try:
            err.code = ErrorCode(code)
        except ValueError:
            err.code = ErrorCode.INTERNAL_ERROR
    return err


#: Exit codes used by ``fdctl`` so scripts can branch deterministically.
EXIT_CODES: dict[str, int] = {
    "ok": 0,
    "usage": 2,
    str(ErrorCode.PERMISSION_DENIED): 3,
    str(ErrorCode.SAFETY_LIMIT_EXCEEDED): 4,
    str(ErrorCode.ESTOP_ACTIVE): 5,
    str(ErrorCode.DEVICE_NOT_FOUND): 6,
    str(ErrorCode.DEVICE_DISCONNECTED): 6,
    str(ErrorCode.DEVICE_BUSY): 7,
    str(ErrorCode.ACTION_TIMEOUT): 8,
    str(ErrorCode.TRANSPORT_ERROR): 9,
    str(ErrorCode.RECIPE_ERROR): 10,
    "assertion_failed": 11,
}
DEFAULT_ERROR_EXIT = 1
