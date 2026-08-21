"""Structured logging.

One line of JSON per record, which journald stores verbatim and the LOG tmux
window renders readably.  Credentials never reach the log: a redaction pass
scrubs anything whose key looks like a secret, because the fastest way to
leak an API key is to log a config dict "just for debugging".
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from fielddeck.common.timebase import format_utc_ns, utc_ns

__all__ = ["configure_logging", "get_logger", "redact"]

#: Substrings that mark a value as never-loggable.
_SECRET_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "authorization",
    "credential",
    "private_key",
    "session_key",
)

_REDACTED = "***redacted***"

_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively strip secret-looking values from a structure."""
    if _depth > 6:
        return "<...>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _SECRET_HINTS):
                out[str(key)] = _REDACTED
            else:
                out[str(key)] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class JsonFormatter(logging.Formatter):
    """Renders a record as one JSON object with FieldDeck's standard fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": format_utc_ns(utc_ns()),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRS and not key.startswith("_")
        }
        # redact() checks the key names too, so `extra={"api_key": ...}` is
        # scrubbed rather than only its nested contents.
        payload.update(redact(extras))
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """Compact human format for a terminal or a developer laptop."""

    _FIELDS = ("session", "device", "action", "permission", "source", "request_id", "duration_ms")

    def format(self, record: logging.LogRecord) -> str:
        head = (
            f"{format_utc_ns(utc_ns())} {record.levelname:<8} "
            f"{record.name:<24} {record.getMessage()}"
        )
        extras = [
            f"{name}={redact({name: record.__dict__[name]})[name]}"
            for name in self._FIELDS
            if name in record.__dict__
        ]
        line = head + ("  " + " ".join(extras) if extras else "")
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "INFO", *, json_output: bool | None = None) -> None:
    """Install the FieldDeck log handler on the root logger.

    ``FIELDDECK_LOG_LEVEL`` and ``FIELDDECK_LOG_JSON`` override the arguments
    so a running service can be made chatty without editing config.
    """
    level = os.environ.get("FIELDDECK_LOG_LEVEL", level).upper()
    if json_output is None:
        env = os.environ.get("FIELDDECK_LOG_JSON")
        json_output = (
            env.strip().lower() in {"1", "true", "yes"} if env else not sys.stderr.isatty()
        )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    # Third-party chatter is not what an operator needs at 2am in a plant room.
    for noisy in ("asyncio", "can", "pymodbus", "pyvisa", "markdown_it"):
        logging.getLogger(noisy).setLevel(max(root.level, logging.WARNING))


#: Attribute names ``logging.LogRecord`` sets itself.  Passing any of these
#: through ``extra=`` makes ``Logger.makeRecord`` raise KeyError.
_RESERVED_RECORD_KEYS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class _SafeLogger(logging.Logger):
    """A logger whose ``extra=`` fields cannot take the process down.

    ``logging`` refuses to let ``extra`` shadow a LogRecord attribute and
    raises KeyError if you try.  That turns a structured field with an
    unlucky name — ``module``, ``filename``, ``process`` are all plausible
    things to log about a device — into a crash at the call site.

    An instrument must not die because of what it was asked to write down, so
    colliding keys are renamed rather than rejected.
    """

    def makeRecord(
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        extra: Any = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        if extra:
            collisions = _RESERVED_RECORD_KEYS.intersection(extra)
            if collisions:
                extra = {
                    (f"field_{key}" if key in collisions else key): value
                    for key, value in extra.items()
                }
        return super().makeRecord(name, level, fn, lno, msg, args, exc_info, func, extra, sinfo)


logging.setLoggerClass(_SafeLogger)


def get_logger(name: str) -> logging.Logger:
    """Component logger.  Use dotted names: ``fielddeck.daemon.rpc``."""
    return logging.getLogger(name)
