"""Session lifecycle, and the immutability of what a session records.

A session owns a directory, a timeline and every artifact produced while it
was open.  The rule that matters is the one about raw capture data: once bytes
are written they are never rewritten, re-ordered or tidied up.  A decoder that
disagrees with the raw file is a decoder bug, and you can only discover that
if the raw file is still exactly what came off the wire.

So these tests go looking for the ways a file could be silently replaced: a
second capture with the same name, a re-registered artifact, a session id that
collides with an existing directory, a filename that tries to leave the
session, and a shutdown in the middle of recording.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fielddeck.capture.recorder import SessionRecorder
from fielddeck.capture.sessions import SessionManager, slugify
from fielddeck.capture.storage import (
    AppendLog,
    SessionLayout,
    compression_available,
    read_append_log,
    sha256_file,
)
from fielddeck.common.errors import CaptureError, SessionError
from fielddeck.common.events import EventSeverity, EventType, new_event
from fielddeck.common.models import ClientSource, SessionState
from fielddeck.common.paths import Paths

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_starting_a_session_creates_its_directory_and_metadata(
        self, sessions: SessionManager
    ) -> None:
        session = sessions.start("bench bringup", operator="A. Engineer")

        assert session.state is SessionState.ACTIVE
        assert session.id.endswith("_bench-bringup")
        assert session.operator == "A. Engineer"
        assert session.simulated is True
        assert session.software["fielddeck"]

        root = sessions.sessions_dir / session.id
        assert (root / "session.json").is_file()
        assert (root / "timeline.sqlite").is_file()
        for subdir in SessionLayout.SUBDIRS:
            assert (root / subdir).is_dir()

    def test_only_one_session_may_be_active(self, sessions: SessionManager) -> None:
        first = sessions.start("first")
        with pytest.raises(SessionError) as caught:
            sessions.start("second")
        assert caught.value.details["active_session"] == first.id

    def test_stopping_closes_the_session_and_records_when(
        self, sessions: SessionManager
    ) -> None:
        started = sessions.start("bringup")
        stopped = sessions.stop()

        assert stopped.id == started.id
        assert stopped.state is SessionState.CLOSED
        assert stopped.ended_monotonic_ns is not None
        assert stopped.ended_utc_ns is not None
        assert sessions.current is None
        assert sessions.current_id is None

    def test_stopping_with_no_session_is_an_error_not_a_silent_no_op(
        self, sessions: SessionManager
    ) -> None:
        with pytest.raises(SessionError, match="no active session"):
            sessions.stop()

    def test_a_new_session_after_a_stop_gets_its_own_directory(
        self, sessions: SessionManager
    ) -> None:
        """Same name, same day: the second one must not land on the first."""
        first = sessions.start("repeat test")
        sessions.stop()
        second = sessions.start("repeat test")

        assert second.id != first.id
        assert second.id.endswith("-02")
        assert (sessions.sessions_dir / first.id).is_dir()

    def test_shutdown_closes_an_open_session(self, sessions: SessionManager) -> None:
        session = sessions.start("interrupted")
        sessions.shutdown()

        assert sessions.current is None
        stored = (sessions.sessions_dir / session.id / "session.json").read_text(encoding="utf-8")
        assert '"CLOSED"' in stored

    def test_the_free_space_floor_refuses_to_start_recording(self, paths: Paths) -> None:
        """Better to refuse than to fill the card and lose the timeline with it."""
        paths.ensure()
        manager = SessionManager(paths.sessions_dir, min_free_mb=10**9)
        with pytest.raises(CaptureError) as caught:
            manager.start("no room")
        assert caught.value.details["required_mb"] == 10**9

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Bench Bringup", "bench-bringup"),
            ("  trailing  ", "trailing"),
            ("motor/test #4", "motor-test-4"),
            ("!!!", "session"),
            ("", "session"),
            ("x" * 100, "x" * 48),
        ],
    )
    def test_names_become_predictable_directory_slugs(self, name: str, expected: str) -> None:
        assert slugify(name) == expected


# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------


class TestLayout:
    def test_a_capture_filename_never_collides_with_an_existing_one(
        self, tmp_path: Path
    ) -> None:
        """The mechanism that makes raw captures immutable in the first place."""
        layout = SessionLayout.create(tmp_path, "2026-08-20_bench")

        first = layout.next_filename("can", "can0", ".log")
        first.write_bytes(b"the original frames")
        second = layout.next_filename("can", "can0", ".log")

        assert first.name == "can0-0001.log"
        assert second.name == "can0-0002.log"
        assert first.read_bytes() == b"the original frames"

    def test_a_filename_cannot_escape_the_session(self, tmp_path: Path) -> None:
        """Recipes and MCP callers reach this, so traversal must be impossible."""
        layout = SessionLayout.create(tmp_path, "2026-08-20_bench")
        with pytest.raises(CaptureError, match="escapes the session directory"):
            layout.path_for("can", "../../../etc/passwd")

    def test_an_unknown_capture_kind_is_refused(self, tmp_path: Path) -> None:
        layout = SessionLayout.create(tmp_path, "2026-08-20_bench")
        with pytest.raises(CaptureError) as caught:
            layout.path_for("etc", "passwd")
        assert caught.value.details["kind"] == "etc"

    def test_creating_over_an_existing_session_directory_is_refused(
        self, tmp_path: Path
    ) -> None:
        SessionLayout.create(tmp_path, "2026-08-20_bench")
        with pytest.raises(CaptureError, match="already exists"):
            SessionLayout.create(tmp_path, "2026-08-20_bench")

    def test_relative_paths_are_reported_from_the_session_root(self, tmp_path: Path) -> None:
        layout = SessionLayout.create(tmp_path, "2026-08-20_bench")
        target = layout.path_for("serial", "capture.bin")
        assert layout.relative(target) == "serial/capture.bin"

    def test_the_append_log_extension_matches_the_available_codec(
        self, tmp_path: Path
    ) -> None:
        layout = SessionLayout.create(tmp_path, "2026-08-20_bench")
        suffix = ".zst" if compression_available() == "zstd" else ".gz"
        assert layout.events_log.name == f"events.jsonl{suffix}"
        assert layout.audit_log.name == f"audit.jsonl{suffix}"


class TestAppendLog:
    def test_records_round_trip_through_the_codec(self, tmp_path: Path) -> None:
        path = tmp_path / f"events.jsonl{'.zst' if compression_available() == 'zstd' else '.gz'}"
        with AppendLog(path) as log:
            log.write({"seq": 1, "type": "ESTOP"})
            log.write({"seq": 2, "type": "SAFE_STATE_APPLIED"})
            log.flush()

        assert [record["seq"] for record in read_append_log(path)] == [1, 2]

    def test_writing_to_a_closed_log_is_an_error_not_a_silent_loss(
        self, tmp_path: Path
    ) -> None:
        log = AppendLog(tmp_path / "events.jsonl", compress=False).open()
        log.close()
        with pytest.raises(CaptureError, match="closed"):
            log.write({"seq": 1})

    def test_it_appends_rather_than_truncating(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        with AppendLog(path, compress=False) as log:
            log.write({"seq": 1})
        with AppendLog(path, compress=False) as log:
            log.write({"seq": 2})
        assert [record["seq"] for record in read_append_log(path)] == [1, 2]


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------


class TestRecorder:
    def _recorder(self, sessions: SessionManager) -> SessionRecorder:
        sessions.start("recording")
        recorder = sessions.recorder
        assert recorder is not None
        return recorder

    def test_events_land_in_the_timeline_and_the_append_log(
        self, sessions: SessionManager
    ) -> None:
        recorder = self._recorder(sessions)
        recorder.on_event(new_event(EventType.MEASUREMENT, message="a reading"))
        recorder.flush()

        assert len(recorder.timeline.events()) >= 1
        assert recorder.layout.events_log.exists()

    def test_an_audit_event_is_flushed_immediately(self, sessions: SessionManager) -> None:
        """After a stop and a power cut, this is the file that has to be there."""
        recorder = self._recorder(sessions)
        recorder.on_event(new_event(EventType.ESTOP, message="operator pressed stop"))

        records = [record["type"] for record in read_append_log(recorder.layout.audit_log)]
        assert str(EventType.ESTOP) in records
        # ...and it is in the queryable timeline without waiting for a batch.
        assert any(row["type"] == str(EventType.ESTOP) for row in recorder.timeline.events())

    def test_an_ordinary_event_does_not_reach_the_audit_log(
        self, sessions: SessionManager
    ) -> None:
        recorder = self._recorder(sessions)
        recorder.on_event(new_event(EventType.MEASUREMENT))
        recorder.flush()
        assert list(read_append_log(recorder.layout.audit_log)) == []

    def test_a_recorder_fault_never_propagates_back_into_the_bus(
        self, sessions: SessionManager
    ) -> None:
        """A broken recorder must not be able to stop the daemon."""
        recorder = self._recorder(sessions)
        recorder.timeline.close()  # the SQLite handle is now unusable
        recorder.on_event(new_event(EventType.MEASUREMENT, severity=EventSeverity.ERROR))

    def test_marks_and_notes_are_persisted_as_they_happen(
        self, sessions: SessionManager
    ) -> None:
        recorder = self._recorder(sessions)
        recorder.mark("power-up", source=ClientSource.HMI, note="probe on TP4")
        recorder.note("clip lead was loose")

        stored = (recorder.layout.session_json).read_text(encoding="utf-8")
        assert "power-up" in stored
        assert "clip lead was loose" in stored
        assert [mark["label"] for mark in recorder.timeline.marks()] == ["power-up"]

    def test_measurements_reach_the_timeline_with_their_units(
        self, sessions: SessionManager
    ) -> None:
        recorder = self._recorder(sessions)
        recorder.measurement(quantity="psu.current", value=0.418, device_id="psu", unit="A")
        row = recorder.timeline.measurements(quantity="psu.current")[0]
        assert row["value"] == pytest.approx(0.418)
        assert row["unit"] == "A"

    def test_registering_an_artifact_hashes_the_file_it_found(
        self, sessions: SessionManager
    ) -> None:
        recorder = self._recorder(sessions)
        path = recorder.capture_path("can", "can0", ".log")
        path.write_bytes(b"(1.0) can0 123#DEADBEEF\n")

        artifact = recorder.add_artifact(path, kind="can", device_id="can:socketcan:can0")

        assert artifact.raw is True
        assert artifact.size_bytes == path.stat().st_size
        assert artifact.sha256 == sha256_file(path)
        assert artifact.relative_path == "can/can0-0001.log"

    def test_a_derived_artifact_must_name_its_source(self, sessions: SessionManager) -> None:
        recorder = self._recorder(sessions)
        raw_path = recorder.capture_path("can", "can0", ".log")
        raw_path.write_bytes(b"(1.0) can0 123#DEADBEEF\n")
        raw = recorder.add_artifact(raw_path, kind="can")

        derived_path = recorder.capture_path("can", "can0-decoded", ".csv")
        derived_path.write_text("timestamp,signal,value\n", encoding="utf-8")
        derived = recorder.add_artifact(
            derived_path,
            kind="can",
            raw=False,
            source_artifact_ids=[raw.artifact_id],
            producer="cantools",
            producer_version="42.0.3",
        )

        assert derived.raw is False
        assert derived.source_artifact_ids == [raw.artifact_id]
        assert derived.producer_version == "42.0.3"
        # And the raw file is byte-identical afterwards.
        assert raw_path.read_bytes() == b"(1.0) can0 123#DEADBEEF\n"
        assert sha256_file(raw_path) == raw.sha256

    def test_capture_paths_never_reuse_a_name(self, sessions: SessionManager) -> None:
        recorder = self._recorder(sessions)
        first = recorder.capture_path("serial", "uart", ".bin")
        first.write_bytes(b"first capture")
        second = recorder.capture_path("serial", "uart", ".bin")

        assert second != first
        assert first.read_bytes() == b"first capture"

    def test_the_free_space_floor_is_re_checked_at_every_capture(
        self, paths: Paths
    ) -> None:
        """A session opened on a healthy card can still be asked for a capture
        on a full one."""
        paths.ensure()
        manager = SessionManager(paths.sessions_dir, min_free_mb=0)
        manager.start("long running")
        recorder = manager.recorder
        assert recorder is not None
        recorder.min_free_mb = 10**9

        with pytest.raises(CaptureError) as caught:
            recorder.capture_path("can", "can0", ".log")
        assert "intact" in (caught.value.preserved or "")
        manager.shutdown()


# ---------------------------------------------------------------------------
# Reading sessions back
# ---------------------------------------------------------------------------


class TestReading:
    def test_a_session_lists_itself_while_it_is_open(self, sessions: SessionManager) -> None:
        session = sessions.start("live")
        listed = {entry["id"]: entry for entry in sessions.list_sessions()}
        assert listed[session.id]["active"] is True
        assert listed[session.id]["state"] == str(SessionState.ACTIVE)

    def test_a_closed_session_is_still_listed(self, sessions: SessionManager) -> None:
        session = sessions.start("done")
        sessions.stop()
        listed = {entry["id"]: entry for entry in sessions.list_sessions()}
        assert listed[session.id]["active"] is False
        assert listed[session.id]["ended_utc_ns"] is not None

    def test_get_returns_metadata_timeline_and_artifacts(
        self, sessions: SessionManager
    ) -> None:
        session = sessions.start("readable")
        recorder = sessions.recorder
        assert recorder is not None
        path = recorder.capture_path("can", "can0", ".log")
        path.write_bytes(b"(1.0) can0 123#00\n")
        recorder.add_artifact(path, kind="can")
        recorder.on_event(new_event(EventType.CAPTURE_STARTED, session_id=session.id))
        recorder.flush()

        data = sessions.get(session.id)
        assert data["id"] == session.id
        assert data["timeline"]["events"] >= 1
        assert [entry["relative_path"] for entry in data["artifacts"]] == ["can/can0-0001.log"]

    def test_reading_an_unknown_session_is_refused_by_name(
        self, sessions: SessionManager
    ) -> None:
        with pytest.raises(SessionError, match="no such session"):
            sessions.get("2026-01-01_does-not-exist")

    def test_a_session_id_cannot_escape_the_session_store(
        self, sessions: SessionManager
    ) -> None:
        """The id reaches this from a client, so traversal has to be impossible."""
        with pytest.raises(SessionError):
            sessions.get("../../etc")

    def test_the_window_query_is_reachable_for_the_live_session(
        self, sessions: SessionManager
    ) -> None:
        session = sessions.start("windowed")
        recorder = sessions.recorder
        assert recorder is not None
        event = new_event(EventType.DEVICE_FAULT, session_id=session.id)
        recorder.on_event(event)

        window = sessions.window(session.id, center_monotonic_ns=event.monotonic_ns)
        assert [row["type"] for row in window["events"]] == [str(EventType.DEVICE_FAULT)]

    def test_the_summary_lists_the_quantities_that_were_measured(
        self, sessions: SessionManager
    ) -> None:
        session = sessions.start("measured")
        recorder = sessions.recorder
        assert recorder is not None
        recorder.measurement(quantity="psu.voltage", value=24.0, unit="V")
        recorder.measurement(quantity="psu.current", value=0.4, unit="A")
        recorder.mark("power-up")

        summary = sessions.summary(session.id)
        assert summary["measurement_quantities"] == ["psu.current", "psu.voltage"]
        assert [mark["label"] for mark in summary["marks"]] == ["power-up"]
