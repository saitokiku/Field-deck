"""The dual clock, and the projection between its two halves.

Every record carries both clocks: monotonic for correlation, UTC for humans.
The tests here are about the properties the rest of the system relies on —
that the monotonic axis is the one that never lies, that a :class:`TimeAnchor`
can project onto the UTC axis *without* rewriting anything, and that a wall
clock step (a Pi with no RTC finally reaching an NTP server, usually mid
session) is detected and reported rather than smoothed over.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fielddeck.common.timebase import (
    ClockWatch,
    TimeAnchor,
    Timestamp,
    format_duration_ns,
    format_utc_ns,
    monotonic_ns,
    now,
    utc_ns,
)

#: 2026-08-20T14:03:00Z, as epoch nanoseconds.
SOME_UTC_NS = 1_787_234_580_000_000_000


class TestTimestamp:
    def test_both_clocks_are_stamped_together(self) -> None:
        before_mono, before_utc = monotonic_ns(), utc_ns()
        stamp = Timestamp.now()
        after_mono, after_utc = monotonic_ns(), utc_ns()

        assert before_mono <= stamp.monotonic_ns <= after_mono
        assert before_utc <= stamp.utc_ns <= after_utc

    def test_the_monotonic_clock_never_goes_backwards(self) -> None:
        samples = [Timestamp.now().monotonic_ns for _ in range(200)]
        assert samples == sorted(samples)

    def test_now_is_the_same_thing(self) -> None:
        assert isinstance(now(), Timestamp)

    def test_it_renders_as_iso_utc(self) -> None:
        stamp = Timestamp(monotonic_ns=0, utc_ns=SOME_UTC_NS)
        assert stamp.isoformat() == "2026-08-20T14:03:00.000000Z"
        assert stamp.utc == datetime(2026, 8, 20, 14, 3, tzinfo=UTC)

    def test_it_serialises_both_halves(self) -> None:
        assert Timestamp(monotonic_ns=7, utc_ns=9).as_dict() == {"monotonic_ns": 7, "utc_ns": 9}

    def test_a_timestamp_is_immutable(self) -> None:
        """Original timestamps are never rewritten after capture."""
        stamp = Timestamp(monotonic_ns=1, utc_ns=2)
        with pytest.raises(AttributeError):
            stamp.monotonic_ns = 3  # type: ignore[misc]


class TestTimeAnchor:
    def test_projection_is_exact_at_the_anchor(self) -> None:
        anchor = TimeAnchor(monotonic_ns=5_000, utc_ns=SOME_UTC_NS)
        assert anchor.utc_for(5_000) == SOME_UTC_NS
        assert anchor.elapsed_s(5_000) == 0.0

    def test_projection_is_linear_in_both_directions(self) -> None:
        anchor = TimeAnchor(monotonic_ns=1_000_000_000, utc_ns=SOME_UTC_NS)
        one_second_later = anchor.monotonic_ns + 1_000_000_000
        one_second_earlier = anchor.monotonic_ns - 1_000_000_000

        assert anchor.utc_for(one_second_later) == SOME_UTC_NS + 1_000_000_000
        assert anchor.utc_for(one_second_earlier) == SOME_UTC_NS - 1_000_000_000
        assert anchor.elapsed_s(one_second_later) == 1.0
        assert anchor.elapsed_s(one_second_earlier) == -1.0

    def test_projection_preserves_ordering_and_spacing(self) -> None:
        """A report renders monotonic events on a wall clock without reordering."""
        anchor = TimeAnchor(monotonic_ns=0, utc_ns=SOME_UTC_NS)
        readings = [0, 1_000, 999_999_999, 1_000_000_000, 10**12]
        projected = [anchor.utc_for(value) for value in readings]
        assert projected == sorted(projected)
        assert [b - a for a, b in zip(projected, projected[1:], strict=False)] == [
            b - a for a, b in zip(readings, readings[1:], strict=False)
        ]

    def test_the_anchor_does_not_mutate_what_it_projects(self) -> None:
        anchor = TimeAnchor(monotonic_ns=10, utc_ns=20)
        reading = 1_234
        assert anchor.utc_for(reading) == 1_244
        assert reading == 1_234

    def test_capture_takes_both_clocks_from_one_instant(self) -> None:
        anchor = TimeAnchor.capture()
        assert anchor.monotonic_ns <= monotonic_ns()
        assert anchor.utc_ns <= utc_ns()
        assert anchor.as_dict() == {
            "monotonic_ns": anchor.monotonic_ns,
            "utc_ns": anchor.utc_ns,
        }

    def test_two_anchors_disagree_after_a_clock_step_and_that_is_the_point(self) -> None:
        """Each anchor is honest about the belief it was taken under.

        Records either side of an NTP step are both real; they just belong to
        different beliefs about what time it was.  Projecting with the anchor
        that was recorded at session start is what keeps a report internally
        consistent instead of silently mixing the two.
        """
        before = TimeAnchor(monotonic_ns=0, utc_ns=SOME_UTC_NS)
        after_step = TimeAnchor(monotonic_ns=0, utc_ns=SOME_UTC_NS + 3_600_000_000_000)
        assert after_step.utc_for(0) - before.utc_for(0) == 3_600_000_000_000
        # The monotonic reading itself is untouched by the step.
        assert before.elapsed_s(2_000_000_000) == after_step.elapsed_s(2_000_000_000) == 2.0


class TestClockWatch:
    def test_a_steady_clock_reports_nothing(self) -> None:
        watch = ClockWatch(threshold_s=1.0)
        assert watch.check() is None

    def test_a_forward_step_is_reported_with_its_size(self, monkeypatch) -> None:
        """A Pi that boots in 1970 and then finds NTP: the normal case."""
        fake_mono = [1_000_000_000]
        fake_utc = [SOME_UTC_NS]
        monkeypatch.setattr("fielddeck.common.timebase.monotonic_ns", lambda: fake_mono[0])
        monkeypatch.setattr("fielddeck.common.timebase.utc_ns", lambda: fake_utc[0])

        watch = ClockWatch(threshold_s=1.0)
        # 100 ms of real time passes, and the wall clock jumps an hour.
        fake_mono[0] += 100_000_000
        fake_utc[0] += 100_000_000 + 3_600_000_000_000

        step = watch.check()
        assert step is not None
        assert step == pytest.approx(3600.0)

    def test_a_backward_step_is_reported_too(self, monkeypatch) -> None:
        fake_mono = [1_000_000_000]
        fake_utc = [SOME_UTC_NS]
        monkeypatch.setattr("fielddeck.common.timebase.monotonic_ns", lambda: fake_mono[0])
        monkeypatch.setattr("fielddeck.common.timebase.utc_ns", lambda: fake_utc[0])

        watch = ClockWatch(threshold_s=1.0)
        fake_mono[0] += 1_000_000_000
        fake_utc[0] += 1_000_000_000 - 5_000_000_000

        assert watch.check() == pytest.approx(-5.0)

    def test_a_small_slew_is_not_reported(self, monkeypatch) -> None:
        """NTP disciplining is constant; warning about it would be noise."""
        fake_mono = [1_000_000_000]
        fake_utc = [SOME_UTC_NS]
        monkeypatch.setattr("fielddeck.common.timebase.monotonic_ns", lambda: fake_mono[0])
        monkeypatch.setattr("fielddeck.common.timebase.utc_ns", lambda: fake_utc[0])

        watch = ClockWatch(threshold_s=1.0)
        fake_mono[0] += 1_000_000_000
        fake_utc[0] += 1_000_000_000 + 100_000_000  # 100 ms of drift
        assert watch.check() is None

    def test_the_reference_moves_so_a_step_is_reported_once(self, monkeypatch) -> None:
        fake_mono = [0]
        fake_utc = [SOME_UTC_NS]
        monkeypatch.setattr("fielddeck.common.timebase.monotonic_ns", lambda: fake_mono[0])
        monkeypatch.setattr("fielddeck.common.timebase.utc_ns", lambda: fake_utc[0])

        watch = ClockWatch(threshold_s=1.0)
        fake_mono[0] += 1_000_000_000
        fake_utc[0] += 1_000_000_000 + 60_000_000_000
        assert watch.check() is not None

        fake_mono[0] += 1_000_000_000
        fake_utc[0] += 1_000_000_000
        assert watch.check() is None


class TestFormatting:
    def test_utc_is_rendered_with_microseconds_and_a_z(self) -> None:
        assert format_utc_ns(SOME_UTC_NS) == "2026-08-20T14:03:00.000000Z"
        assert format_utc_ns(SOME_UTC_NS + 1_500) == "2026-08-20T14:03:00.000001Z"

    def test_the_epoch_itself_formats(self) -> None:
        assert format_utc_ns(0) == "1970-01-01T00:00:00.000000Z"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (999, "999 ns"),
            (1_500, "1.5 us"),
            (1_500_000, "1.5 ms"),
            (1_500_000_000, "1.500 s"),
            (0, "0 ns"),
        ],
    )
    def test_durations_read_the_way_an_engineer_writes_them(
        self, value: int, expected: str
    ) -> None:
        assert format_duration_ns(value) == expected
