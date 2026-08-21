"""Timing statistics, and the sample count that keeps them honest.

"100 ms period" from four frames is a guess; "100.0 ms +/- 1.8 ms over 214
frames" is a measurement.  These tests are mostly about the second half of
that sentence: the confidence a small sample is allowed to support, and the
evidence string that says why.
"""

from __future__ import annotations

import pytest

from fielddeck.analysis.timing import classify_periodicity, summarize_periods

MS = 1_000_000


def periodic(count: int, period_ms: float = 100.0, jitter_ms: float = 0.0) -> list[int]:
    """Timestamps on a grid, with a deterministic zig-zag of jitter."""
    out: list[int] = []
    for index in range(count):
        offset = (jitter_ms if index % 2 else -jitter_ms) * MS
        out.append(int(index * period_ms * MS + offset))
    return out


class TestSummary:
    def test_fewer_than_two_samples_yields_no_statistics(self) -> None:
        for timestamps in ([], [42]):
            stats = summarize_periods(timestamps)
            assert stats["samples"] == len(timestamps)
            assert stats["mean_ms"] is None
            assert stats["jitter_ms"] is None

    def test_a_perfect_stream_reports_its_period_and_no_spread(self) -> None:
        stats = summarize_periods(periodic(50, period_ms=100.0))
        assert stats["samples"] == 50
        assert stats["mean_ms"] == pytest.approx(100.0)
        assert stats["median_ms"] == pytest.approx(100.0)
        assert stats["stdev_ms"] == pytest.approx(0.0)
        assert stats["jitter_ms"] == pytest.approx(0.0)

    def test_jitter_is_peak_to_peak_the_way_a_scope_shows_it(self) -> None:
        stats = summarize_periods(periodic(21, period_ms=100.0, jitter_ms=5.0))
        assert stats["min_ms"] == pytest.approx(90.0)
        assert stats["max_ms"] == pytest.approx(110.0)
        assert stats["jitter_ms"] == pytest.approx(20.0)

    def test_out_of_order_timestamps_are_sorted_before_measuring(self) -> None:
        """Two receive threads can interleave; the intervals are still real."""
        ordered = summarize_periods([0, 100 * MS, 200 * MS])
        shuffled = summarize_periods([200 * MS, 0, 100 * MS])
        assert ordered == shuffled

    def test_two_samples_are_enough_for_one_interval(self) -> None:
        stats = summarize_periods([0, 250 * MS])
        assert stats["samples"] == 2
        assert stats["mean_ms"] == pytest.approx(250.0)
        assert stats["stdev_ms"] == pytest.approx(0.0)


class TestClassification:
    def test_a_tight_stream_is_periodic_with_high_confidence(self) -> None:
        verdict = classify_periodicity(periodic(200, period_ms=100.0, jitter_ms=0.5))
        assert verdict["periodic"] is True
        assert verdict["confidence"] >= 0.9
        assert any("spread" in item for item in verdict["evidence"])
        assert any("200 samples" in item for item in verdict["evidence"])

    def test_a_loose_stream_is_recognised_but_not_trusted(self) -> None:
        verdict = classify_periodicity(periodic(200, period_ms=100.0, jitter_ms=12.0))
        assert 0.0 < verdict["confidence"] < 0.9

    def test_an_erratic_stream_is_not_periodic(self) -> None:
        timestamps = [0, 5 * MS, 400 * MS, 410 * MS, 3_000 * MS, 3_001 * MS, 9_000 * MS]
        verdict = classify_periodicity(timestamps)
        assert verdict["periodic"] is False
        assert any("not periodic" in item for item in verdict["evidence"])

    def test_a_tiny_sample_cannot_support_a_confident_claim(self) -> None:
        """Four perfectly spaced frames are still four frames."""
        verdict = classify_periodicity(periodic(4, period_ms=100.0))
        assert verdict["confidence"] <= 0.3
        assert verdict["periodic"] is False
        assert any("only 4 samples" in item for item in verdict["evidence"])

    def test_a_medium_sample_is_capped_below_certainty(self) -> None:
        verdict = classify_periodicity(periodic(10, period_ms=100.0))
        assert verdict["confidence"] <= 0.7
        assert any("10 samples" in item for item in verdict["evidence"])

    def test_a_single_frame_yields_no_verdict_at_all(self) -> None:
        verdict = classify_periodicity([42])
        assert verdict["periodic"] is False
        assert verdict["confidence"] == 0.0
        assert verdict["evidence"] == ["fewer than two samples"]

    def test_identical_timestamps_do_not_divide_by_zero(self) -> None:
        verdict = classify_periodicity([1_000, 1_000, 1_000])
        assert verdict["periodic"] is False
        assert verdict["confidence"] == 0.0

    def test_the_statistics_come_back_alongside_the_verdict(self) -> None:
        """A verdict without its evidence is an opinion."""
        verdict = classify_periodicity(periodic(50, period_ms=20.0))
        assert verdict["mean_ms"] == pytest.approx(20.0)
        assert verdict["samples"] == 50
        assert isinstance(verdict["evidence"], list)
