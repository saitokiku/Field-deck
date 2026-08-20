"""Session lifecycle.

A session is the unit of engineering activity: it owns a directory, a
timeline, and every artifact produced while it was open.  Sessions survive a
daemon restart because everything is written as it happens, not at the end.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fielddeck import __version__
from fielddeck.capture.recorder import SessionRecorder
from fielddeck.capture.storage import SessionLayout, compression_available, free_space_mb
from fielddeck.capture.timeline import Timeline
from fielddeck.common.errors import CaptureError, SessionError
from fielddeck.common.events import EventSeverity, EventType, new_event
from fielddeck.common.logging import get_logger
from fielddeck.common.models import ClientSource, Session, SessionState
from fielddeck.common.timebase import TimeAnchor, Timestamp, format_utc_ns

__all__ = ["SessionManager", "slugify"]

_log = get_logger("fielddeck.capture.sessions")
_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG.sub("-", name.strip().lower()).strip("-")
    return slug[:48] or "session"


class SessionManager:
    """Starts, stops and reads sessions.  One active session at a time."""

    def __init__(
        self,
        sessions_dir: Path,
        *,
        publish: Any = None,
        min_free_mb: int = 256,
        simulated: bool = False,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._publish = publish or (lambda event: event)
        self._min_free_mb = min_free_mb
        self._simulated = simulated
        self._recorder: SessionRecorder | None = None
        self._remove_sink: Any = None
        self._bus: Any = None

    def attach_bus(self, bus: Any) -> None:
        """Remember the bus so a recorder can be wired in as a sink on start."""
        self._bus = bus

    # -- state -------------------------------------------------------------

    @property
    def recorder(self) -> SessionRecorder | None:
        return self._recorder

    @property
    def current(self) -> Session | None:
        return self._recorder.session if self._recorder else None

    @property
    def current_id(self) -> str | None:
        return self._recorder.session.id if self._recorder else None

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        name: str,
        *,
        operator: str | None = None,
        source: ClientSource = ClientSource.FDCTL,
        devices: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        if self._recorder is not None:
            raise SessionError(
                f"session {self._recorder.session.id} is already active; stop it first",
                details={"active_session": self._recorder.session.id},
            )
        free = free_space_mb(self.sessions_dir)
        if free < self._min_free_mb:
            raise CaptureError(
                f"only {free:.0f} MB free at {self.sessions_dir}; "
                f"need {self._min_free_mb} MB to start recording",
                details={"free_mb": free, "required_mb": self._min_free_mb},
            )

        ts = Timestamp.now()
        session_id = self._allocate_id(name, ts.utc_ns)
        layout = SessionLayout.create(self.sessions_dir, session_id)
        session = Session(
            id=session_id,
            name=name,
            state=SessionState.ACTIVE,
            operator=operator,
            started_monotonic_ns=ts.monotonic_ns,
            started_utc_ns=ts.utc_ns,
            devices=list(devices or []),
            software={"fielddeck": __version__, "compression": compression_available()},
            simulated=self._simulated,
            metadata=dict(metadata or {}),
        )
        recorder = SessionRecorder(session, layout, TimeAnchor(ts.monotonic_ns, ts.utc_ns))
        recorder.write_session_json()
        self._recorder = recorder
        if self._bus is not None:
            self._remove_sink = self._bus.add_sink(recorder.on_event)

        self._publish(
            new_event(
                EventType.SESSION_STARTED,
                source=source,
                session_id=session_id,
                message=f"session {session_id} started",
                payload={
                    "name": name,
                    "operator": operator,
                    "path": str(layout.root),
                    "simulated": self._simulated,
                },
            )
        )
        _log.info("session started", extra={"session": session_id, "source": str(source)})
        return session

    def stop(self, *, source: ClientSource = ClientSource.FDCTL) -> Session:
        if self._recorder is None:
            raise SessionError("no active session")
        recorder = self._recorder
        session = recorder.session
        ts = Timestamp.now()
        session.state = SessionState.FINALIZING
        session.ended_monotonic_ns = ts.monotonic_ns
        session.ended_utc_ns = ts.utc_ns

        self._publish(
            new_event(
                EventType.SESSION_STOPPED,
                source=source,
                session_id=session.id,
                message=f"session {session.id} stopped after {session.elapsed_s():.1f}s",
                payload={"duration_s": session.elapsed_s()},
            )
        )

        if self._remove_sink is not None:
            self._remove_sink()
            self._remove_sink = None
        session.state = SessionState.CLOSED
        recorder.close()
        self._recorder = None
        _log.info("session stopped", extra={"session": session.id})
        return session

    def _allocate_id(self, name: str, utc_ns: int) -> str:
        date = format_utc_ns(utc_ns)[:10]
        base = f"{date}_{slugify(name)}"
        candidate = base
        index = 2
        while (self.sessions_dir / candidate).exists():
            candidate = f"{base}-{index:02d}"
            index += 1
        return candidate

    # -- reading -----------------------------------------------------------

    def list_sessions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(self.sessions_dir.iterdir(), reverse=True):
            if not path.is_dir() or not (path / "session.json").exists():
                continue
            try:
                data = json.loads((path / "session.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):  # pragma: no cover - corrupt dir
                continue
            out.append(
                {
                    "id": data.get("id", path.name),
                    "name": data.get("name", ""),
                    "state": data.get("state", "CLOSED"),
                    "started_utc_ns": data.get("started_utc_ns"),
                    "ended_utc_ns": data.get("ended_utc_ns"),
                    "simulated": data.get("simulated", False),
                    "active": self.current_id == data.get("id"),
                    "path": str(path),
                }
            )
            if len(out) >= limit:
                break
        return out

    def _layout(self, session_id: str) -> SessionLayout:
        root = (self.sessions_dir / session_id).resolve()
        if not root.is_relative_to(self.sessions_dir.resolve()) or not root.is_dir():
            raise SessionError(f"no such session {session_id!r}", details={"id": session_id})
        return SessionLayout(root)

    def get(self, session_id: str) -> dict[str, Any]:
        layout = self._layout(session_id)
        data = json.loads(layout.session_json.read_text(encoding="utf-8"))
        with Timeline(layout.timeline_db) as timeline:
            data["timeline"] = timeline.summary()
            data["artifacts"] = timeline.artifacts()
        return data

    def events(self, session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        if self._recorder is not None and self._recorder.session_id == session_id:
            self._recorder.flush()
            return [dict(row) for row in self._recorder.timeline.events(**kwargs)]
        layout = self._layout(session_id)
        with Timeline(layout.timeline_db) as timeline:
            return [dict(row) for row in timeline.events(**kwargs)]

    def window(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        if self._recorder is not None and self._recorder.session_id == session_id:
            self._recorder.flush()
            return self._recorder.timeline.window(**kwargs)
        layout = self._layout(session_id)
        with Timeline(layout.timeline_db) as timeline:
            return timeline.window(**kwargs)

    def summary(self, session_id: str) -> dict[str, Any]:
        data = self.get(session_id)
        layout = self._layout(session_id)
        with Timeline(layout.timeline_db) as timeline:
            data["marks"] = timeline.marks()
            data["measurement_quantities"] = sorted(
                {row["quantity"] for row in timeline.measurements(limit=10000)}
            )
        return data

    # -- shutdown ----------------------------------------------------------

    def shutdown(self, *, source: ClientSource = ClientSource.SYSTEM) -> None:
        """Close cleanly.  Never discards a session's data."""
        if self._recorder is not None:
            try:
                self.stop(source=source)
            except Exception:  # noqa: BLE001 - shutdown must close the session no matter what failed
                _log.exception("failed to close session cleanly")
                if self._recorder is not None:
                    self._recorder.close()
                    self._recorder = None


def _severity_order() -> list[str]:  # pragma: no cover - documentation helper
    return [str(level) for level in EventSeverity]
