"""Session recorder.

Attached to the event bus as a lossless sink for the lifetime of a session.
Everything that happens lands in three places:

* ``timeline.sqlite`` — searchable, correlatable, batched
* ``events.jsonl.zst`` — the complete append-only stream
* ``audit.jsonl.zst`` — authorization, ESTOP and denial records, flushed
  immediately because those are exactly the records you need after something
  went wrong and the power went out
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fielddeck.capture.storage import AppendLog, SessionLayout, sha256_file
from fielddeck.capture.timeline import Timeline
from fielddeck.common.events import Event
from fielddeck.common.logging import get_logger
from fielddeck.common.models import CaptureArtifact, ClientSource, Session, SessionMark
from fielddeck.common.timebase import TimeAnchor, Timestamp

__all__ = ["SessionRecorder"]

_log = get_logger("fielddeck.capture.recorder")


class SessionRecorder:
    """Owns one session's on-disk state."""

    def __init__(self, session: Session, layout: SessionLayout, anchor: TimeAnchor) -> None:
        self.session = session
        self.layout = layout
        self.anchor = anchor
        self.timeline = Timeline(layout.timeline_db)
        self._events = AppendLog(layout.events_log).open()
        self._audit = AppendLog(layout.audit_log).open()
        self._closed = False
        self.timeline.set_meta("session_id", session.id)
        self.timeline.set_meta("anchor", anchor.as_dict())
        self.timeline.set_meta("started_utc_ns", session.started_utc_ns)

    @property
    def session_id(self) -> str:
        return self.session.id

    @property
    def root(self) -> Path:
        return self.layout.root

    # -- event sink --------------------------------------------------------

    def on_event(self, event: Event) -> None:
        """Bus sink.  Never raises: a recorder fault must not stop the daemon."""
        if self._closed:
            return
        try:
            record = event.model_dump(mode="json")
            self.timeline.add_event(event)
            self._events.write(record)
            if event.is_audit:
                self._audit.write(record)
                self._audit.flush()
                self.timeline.flush()
        except Exception:
            _log.exception("failed to record event", extra={"session": self.session.id})

    # -- marks and notes ---------------------------------------------------

    def mark(
        self, label: str, *, source: ClientSource = ClientSource.FDCTL, note: str | None = None
    ) -> SessionMark:
        ts = Timestamp.now()
        entry = SessionMark(
            label=label,
            monotonic_ns=ts.monotonic_ns,
            utc_ns=ts.utc_ns,
            source=source,
            note=note,
        )
        self.session.marks.append(entry)
        self.timeline.add_mark(entry)
        self.write_session_json()
        return entry

    def note(self, text: str) -> None:
        self.session.notes.append(text)
        self.write_session_json()

    def measurement(
        self,
        *,
        quantity: str,
        value: float,
        device_id: str | None = None,
        unit: str | None = None,
        timestamp: Timestamp | None = None,
    ) -> None:
        ts = timestamp or Timestamp.now()
        self.timeline.add_measurement(
            monotonic_ns=ts.monotonic_ns,
            utc_ns=ts.utc_ns,
            quantity=quantity,
            value=value,
            device_id=device_id,
            unit=unit,
        )

    # -- artifacts ---------------------------------------------------------

    def capture_path(self, kind: str, stem: str, suffix: str) -> Path:
        """A fresh, non-colliding path inside the session for raw capture."""
        return self.layout.next_filename(kind, stem, suffix)

    def add_artifact(
        self,
        path: Path,
        *,
        kind: str,
        media_type: str = "application/octet-stream",
        device_id: str | None = None,
        raw: bool = True,
        source_artifact_ids: list[str] | None = None,
        producer: str | None = None,
        producer_version: str | None = None,
        producer_config: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CaptureArtifact:
        """Register a file with the session and hash it for integrity.

        Derived artifacts must name their sources and the tool that made
        them, so a decoded CSV can always be traced back to the raw capture.
        """
        ts = Timestamp.now()
        artifact = CaptureArtifact(
            artifact_id=f"art-{secrets.token_hex(5)}",
            session_id=self.session.id,
            relative_path=self.layout.relative(path),
            kind=kind,
            media_type=media_type,
            size_bytes=path.stat().st_size if path.exists() else 0,
            sha256=sha256_file(path) if path.exists() else None,
            created_monotonic_ns=ts.monotonic_ns,
            created_utc_ns=ts.utc_ns,
            device_id=device_id,
            raw=raw,
            source_artifact_ids=list(source_artifact_ids or []),
            producer=producer,
            producer_version=producer_version,
            producer_config=dict(producer_config or {}),
            metadata=dict(metadata or {}),
        )
        self.timeline.add_artifact(artifact)
        return artifact

    # -- persistence -------------------------------------------------------

    def write_session_json(self) -> None:
        self.layout.session_json.write_text(
            self.session.model_dump_json(indent=2), encoding="utf-8"
        )

    def summary(self) -> dict[str, Any]:
        return {
            "session": self.session.model_dump(mode="json"),
            "timeline": self.timeline.summary(),
            "path": str(self.layout.root),
        }

    def flush(self) -> None:
        self.timeline.flush()
        self._events.flush()
        self._audit.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.write_session_json()
        finally:
            self.timeline.close()
            self._events.close()
            self._audit.close()
