"""The unified timeline: batching, the window query, and provenance.

One SQLite file per session holds every event, measurement, mark and artifact
on a single monotonic axis.  Three properties are worth pinning:

* **Batched writes stay honest.**  Events are buffered for throughput, but a
  read flushes first, so a query never sees a hole that only exists in memory.
* **The window query is the flagship.**  "What happened 300 ms before the
  fault" has to return the CAN, serial, bench and operator activity together,
  already ordered, or correlation goes back to being a spreadsheet exercise.
* **Derived artifacts name their sources.**  A decoded CSV that cannot be
  traced back to the bytes on the wire is not evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fielddeck.capture.timeline import Timeline
from fielddeck.common.events import EventSeverity, EventType, new_event
from fielddeck.common.models import CaptureArtifact, ClientSource, SessionMark
from fielddeck.common.timebase import Timestamp


@pytest.fixture
def timeline(tmp_path: Path):
    with Timeline(tmp_path / "timeline.sqlite") as store:
        yield store


def event_at(monotonic_ns: int, event_type: EventType = EventType.MEASUREMENT, **kwargs):
    return new_event(event_type, timestamp=Timestamp(monotonic_ns, 1_700_000_000_000), **kwargs)


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


class TestBatching:
    """Reaches for the connection directly, on purpose.

    Every public read flushes first — which is the property being tested — so
    the only way to observe what is still buffered is to look past it.
    """

    def test_events_are_buffered_until_the_batch_fills(self, tmp_path: Path) -> None:
        with Timeline(tmp_path / "t.sqlite", batch_size=8, max_batch_age_s=3600) as store:
            for index in range(7):
                store.add_event(event_at(index))
            # Nothing is on disk yet: this is the throughput trade, made visible.
            assert store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

            store.add_event(event_at(7))
            assert store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 8

    def test_a_query_flushes_first_so_it_never_sees_a_hole(self, tmp_path: Path) -> None:
        with Timeline(tmp_path / "t.sqlite", batch_size=1000, max_batch_age_s=3600) as store:
            for index in range(5):
                store.add_event(event_at(index))
            assert len(store.events()) == 5
            assert store.counts() == {str(EventType.MEASUREMENT): 5}

    def test_an_old_batch_is_flushed_even_when_it_is_not_full(self, tmp_path: Path) -> None:
        """A quiet session must not hold its records in memory indefinitely.

        Power loss on a field device is a normal event, and the records just
        before it are the valuable ones — so the batch is bounded by age as
        well as by count.
        """
        with Timeline(tmp_path / "t.sqlite", batch_size=1000, max_batch_age_s=0.0) as store:
            store.add_event(event_at(1))
            assert store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1

    def test_closing_flushes_what_is_left(self, tmp_path: Path) -> None:
        path = tmp_path / "t.sqlite"
        store = Timeline(path, batch_size=1000, max_batch_age_s=3600)
        store.add_event(event_at(1))
        store.close()

        with Timeline(path) as reopened:
            assert len(reopened.events()) == 1

    def test_a_replayed_event_does_not_duplicate_a_row(self, tmp_path: Path) -> None:
        """Sequence numbers are the primary key, so a re-publish is idempotent."""
        with Timeline(tmp_path / "t.sqlite", batch_size=1) as store:
            event = event_at(1)
            store.add_event(event)
            store.add_event(event)
            assert len(store.events()) == 1


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestEventQueries:
    def test_rows_come_back_on_the_monotonic_axis(self, timeline: Timeline) -> None:
        for stamp in (500, 100, 300):
            timeline.add_event(event_at(stamp))
        assert [row["monotonic_ns"] for row in timeline.events()] == [100, 300, 500]

    def test_the_payload_is_returned_as_structure_not_text(self, timeline: Timeline) -> None:
        timeline.add_event(event_at(1, EventType.LIMIT_REJECTED, payload={"quantity": "psu.power"}))
        assert timeline.events()[0]["payload"] == {"quantity": "psu.power"}

    def test_filtering_by_type_device_and_severity(self, timeline: Timeline) -> None:
        timeline.add_event(event_at(1, EventType.MEASUREMENT, device_id="psu"))
        timeline.add_event(event_at(2, EventType.DEVICE_FAULT, device_id="can0"))
        timeline.add_event(
            event_at(3, EventType.ACTION_FAILED, device_id="can0", severity=EventSeverity.ERROR)
        )

        assert len(timeline.events(types=[str(EventType.DEVICE_FAULT)])) == 1
        assert len(timeline.events(device_id="can0")) == 2
        assert len(timeline.events(severity_at_least="error")) == 1
        assert len(timeline.events(severity_at_least="info")) == 3

    def test_limit_and_offset_page_without_reordering(self, timeline: Timeline) -> None:
        for index in range(10):
            timeline.add_event(event_at(index * 1000))
        first = timeline.events(limit=4)
        second = timeline.events(limit=4, offset=4)
        assert [row["monotonic_ns"] for row in first] == [0, 1000, 2000, 3000]
        assert [row["monotonic_ns"] for row in second] == [4000, 5000, 6000, 7000]

    def test_since_excludes_older_rows(self, timeline: Timeline) -> None:
        for stamp in (100, 200, 300):
            timeline.add_event(event_at(stamp))
        assert [row["monotonic_ns"] for row in timeline.events(since_monotonic_ns=200)] == [
            200,
            300,
        ]

    def test_find_event_locates_the_nth_of_a_type(self, timeline: Timeline) -> None:
        timeline.add_event(event_at(100, EventType.DEVICE_FAULT))
        timeline.add_event(event_at(200, EventType.DEVICE_FAULT))
        assert timeline.find_event(type=str(EventType.DEVICE_FAULT))["monotonic_ns"] == 100
        assert timeline.find_event(type=str(EventType.DEVICE_FAULT), nth=1)["monotonic_ns"] == 200
        assert timeline.find_event(type=str(EventType.ESTOP)) is None


# ---------------------------------------------------------------------------
# The window query
# ---------------------------------------------------------------------------


class TestWindow:
    def test_it_returns_every_kind_of_record_around_one_instant(self, timeline: Timeline) -> None:
        """The correlation query, with all four subsystems in one answer."""
        fault_at = 10_000_000_000  # 10 s on the monotonic axis

        timeline.add_event(event_at(fault_at - 400_000_000, EventType.MEASUREMENT))  # 400 ms before
        timeline.add_event(event_at(fault_at - 100_000_000, EventType.MEASUREMENT))  # 100 ms before
        timeline.add_event(event_at(fault_at, EventType.DEVICE_FAULT, device_id="can0"))
        timeline.add_event(event_at(fault_at + 50_000_000, EventType.ACTION_FAILED))
        timeline.add_event(event_at(fault_at + 500_000_000, EventType.MEASUREMENT))  # 500 ms after
        timeline.add_measurement(
            monotonic_ns=fault_at - 20_000_000,
            utc_ns=0,
            quantity="psu.current",
            value=0.91,
            device_id="psu",
            unit="A",
        )
        timeline.add_mark(
            SessionMark(
                label="probe on TP4",
                monotonic_ns=fault_at - 200_000_000,
                utc_ns=0,
                source=ClientSource.HMI,
            )
        )

        window = timeline.window(center_monotonic_ns=fault_at, before_ms=300, after_ms=100)

        assert window["start_monotonic_ns"] == fault_at - 300_000_000
        assert window["end_monotonic_ns"] == fault_at + 100_000_000
        # The 400 ms-before and 500 ms-after events are outside and excluded.
        assert [row["type"] for row in window["events"]] == [
            str(EventType.MEASUREMENT),
            str(EventType.DEVICE_FAULT),
            str(EventType.ACTION_FAILED),
        ]
        assert [row["quantity"] for row in window["measurements"]] == ["psu.current"]
        assert [row["label"] for row in window["marks"]] == ["probe on TP4"]

    def test_the_window_boundary_is_inclusive(self, timeline: Timeline) -> None:
        centre = 1_000_000_000
        timeline.add_event(event_at(centre - 300_000_000))
        timeline.add_event(event_at(centre + 100_000_000))
        window = timeline.window(center_monotonic_ns=centre, before_ms=300, after_ms=100)
        assert len(window["events"]) == 2

    def test_an_empty_window_is_empty_rather_than_an_error(self, timeline: Timeline) -> None:
        window = timeline.window(center_monotonic_ns=5, before_ms=1, after_ms=1)
        assert window["events"] == []
        assert window["measurements"] == []
        assert window["marks"] == []

    def test_the_window_flushes_pending_events_first(self, tmp_path: Path) -> None:
        with Timeline(tmp_path / "t.sqlite", batch_size=1000, max_batch_age_s=3600) as store:
            store.add_event(event_at(1_000_000_000))
            window = store.window(center_monotonic_ns=1_000_000_000, before_ms=1, after_ms=1)
            assert len(window["events"]) == 1


# ---------------------------------------------------------------------------
# Measurements, marks, artifacts
# ---------------------------------------------------------------------------


class TestMeasurements:
    def test_measurements_are_queryable_by_quantity_and_device(self, timeline: Timeline) -> None:
        timeline.add_measurement(
            monotonic_ns=1, utc_ns=1, quantity="psu.voltage", value=24.0, device_id="psu", unit="V"
        )
        timeline.add_measurement(
            monotonic_ns=2, utc_ns=2, quantity="psu.current", value=0.42, device_id="psu", unit="A"
        )
        timeline.add_measurement(
            monotonic_ns=3, utc_ns=3, quantity="psu.voltage", value=23.9, device_id="other"
        )

        assert len(timeline.measurements(quantity="psu.voltage")) == 2
        assert len(timeline.measurements(device_id="psu")) == 2
        assert timeline.measurements(quantity="psu.current")[0]["value"] == pytest.approx(0.42)

    def test_measurements_keep_their_own_timestamps(self, timeline: Timeline) -> None:
        timeline.add_measurement(monotonic_ns=99, utc_ns=1234, quantity="q", value=1.0)
        row = timeline.measurements()[0]
        assert (row["monotonic_ns"], row["utc_ns"]) == (99, 1234)


class TestArtifacts:
    def _artifact(self, **kwargs) -> CaptureArtifact:
        base = {
            "artifact_id": "art-raw",
            "session_id": "2026-08-20_bench",
            "relative_path": "can/can0-0001.log",
            "kind": "can",
            "size_bytes": 1024,
            "sha256": "a" * 64,
            "created_monotonic_ns": 1,
            "created_utc_ns": 2,
            "device_id": "can:socketcan:can0",
        }
        return CaptureArtifact(**{**base, **kwargs})

    def test_a_raw_artifact_round_trips(self, timeline: Timeline) -> None:
        timeline.add_artifact(self._artifact())
        stored = timeline.artifacts()[0]
        assert stored["artifact_id"] == "art-raw"
        assert stored["raw"] is True
        assert stored["sha256"] == "a" * 64
        assert stored["source_artifact_ids"] == []

    def test_a_derived_artifact_records_where_it_came_from(self, timeline: Timeline) -> None:
        """Provenance is what lets a decoded CSV be trusted, or doubted."""
        timeline.add_artifact(self._artifact())
        timeline.add_artifact(
            self._artifact(
                artifact_id="art-derived",
                relative_path="can/can0-0001.csv",
                raw=False,
                source_artifact_ids=["art-raw"],
                producer="cantools",
                producer_version="42.0.3",
                producer_config={"dbc_sha256": "b" * 64},
            )
        )

        derived = {entry["artifact_id"]: entry for entry in timeline.artifacts()}["art-derived"]
        assert derived["raw"] is False
        assert derived["source_artifact_ids"] == ["art-raw"]
        assert derived["producer"] == "cantools"
        assert derived["producer_version"] == "42.0.3"
        assert derived["producer_config"] == {"dbc_sha256": "b" * 64}

    def test_artifacts_come_back_in_creation_order(self, timeline: Timeline) -> None:
        timeline.add_artifact(self._artifact(artifact_id="second", created_monotonic_ns=200))
        timeline.add_artifact(self._artifact(artifact_id="first", created_monotonic_ns=100))
        assert [entry["artifact_id"] for entry in timeline.artifacts()] == ["first", "second"]


class TestMarksAndSummary:
    def test_marks_are_ordered_and_keep_their_source(self, timeline: Timeline) -> None:
        for label, stamp, source in (
            ("second", 200, ClientSource.RECIPE),
            ("first", 100, ClientSource.HMI),
        ):
            timeline.add_mark(SessionMark(label=label, monotonic_ns=stamp, utc_ns=0, source=source))
        marks = timeline.marks()
        assert [mark["label"] for mark in marks] == ["first", "second"]
        assert marks[0]["source"] == str(ClientSource.HMI)

    def test_the_summary_describes_the_whole_session(self, timeline: Timeline) -> None:
        timeline.add_event(event_at(1_000_000_000))
        timeline.add_event(event_at(3_000_000_000, EventType.ESTOP))
        timeline.add_mark(SessionMark(label="m", monotonic_ns=2_000_000_000, utc_ns=0))
        timeline.add_artifact(
            CaptureArtifact(
                artifact_id="art-1",
                session_id="s",
                relative_path="can/x.log",
                kind="can",
                created_monotonic_ns=1,
                created_utc_ns=1,
            )
        )

        summary = timeline.summary()
        assert summary["events"] == 2
        assert summary["duration_s"] == pytest.approx(2.0)
        assert summary["by_type"] == {
            str(EventType.MEASUREMENT): 1,
            str(EventType.ESTOP): 1,
        }
        assert summary["artifacts"] == 1
        assert summary["marks"] == 1

    def test_an_empty_timeline_summarises_without_dividing_by_nothing(
        self, timeline: Timeline
    ) -> None:
        summary = timeline.summary()
        assert summary["events"] == 0
        assert summary["duration_s"] == 0.0


class TestMeta:
    def test_metadata_round_trips_as_json(self, timeline: Timeline) -> None:
        timeline.set_meta("anchor", {"monotonic_ns": 1, "utc_ns": 2})
        assert timeline.get_meta("anchor") == {"monotonic_ns": 1, "utc_ns": 2}

    def test_writing_the_same_key_twice_updates_it(self, timeline: Timeline) -> None:
        timeline.set_meta("session_id", "first")
        timeline.set_meta("session_id", "second")
        assert timeline.get_meta("session_id") == "second"

    def test_a_missing_key_returns_the_default(self, timeline: Timeline) -> None:
        assert timeline.get_meta("nothing", "fallback") == "fallback"
