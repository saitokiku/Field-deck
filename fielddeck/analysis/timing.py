"""Timing analysis.

Period and jitter statistics, computed with the standard library so this
works on a Pi with no numpy installed.  Every figure reports the evidence it
came from — sample count and spread — because "100 ms period" from four
frames is a guess and "100.0 ms +/- 1.8 ms over 214 frames" is a measurement.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from itertools import pairwise
from typing import Any

__all__ = ["classify_periodicity", "summarize_periods"]


def summarize_periods(timestamps_ns: Sequence[int]) -> dict[str, Any]:
    """Inter-arrival statistics for one stream of timestamps."""
    if len(timestamps_ns) < 2:
        return {
            "samples": len(timestamps_ns),
            "mean_ms": None,
            "median_ms": None,
            "min_ms": None,
            "max_ms": None,
            "stdev_ms": None,
            "jitter_ms": None,
        }
    ordered = sorted(timestamps_ns)
    deltas_ms = [(later - earlier) / 1e6 for earlier, later in pairwise(ordered)]
    mean = statistics.fmean(deltas_ms)
    stdev = statistics.pstdev(deltas_ms) if len(deltas_ms) > 1 else 0.0
    return {
        "samples": len(ordered),
        "mean_ms": round(mean, 4),
        "median_ms": round(statistics.median(deltas_ms), 4),
        "min_ms": round(min(deltas_ms), 4),
        "max_ms": round(max(deltas_ms), 4),
        "stdev_ms": round(stdev, 4),
        # Peak-to-peak jitter is what an engineer reads off a scope.
        "jitter_ms": round(max(deltas_ms) - min(deltas_ms), 4),
    }


def classify_periodicity(timestamps_ns: Sequence[int]) -> dict[str, Any]:
    """Decide whether a stream is periodic, and say why.

    Returns a confidence with its supporting evidence rather than a bare
    verdict.  Sparse data yields low confidence, not a confident guess.
    """
    stats = summarize_periods(timestamps_ns)
    if stats["mean_ms"] is None or stats["mean_ms"] <= 0:
        return {
            **stats,
            "periodic": False,
            "confidence": 0.0,
            "evidence": ["fewer than two samples"],
        }

    relative_spread = stats["stdev_ms"] / stats["mean_ms"]
    evidence: list[str] = []
    confidence = 0.0

    if relative_spread < 0.02:
        confidence = 0.95
        evidence.append(f"interval spread {relative_spread * 100:.1f}% of the mean")
    elif relative_spread < 0.10:
        confidence = 0.75
        evidence.append(f"interval spread {relative_spread * 100:.1f}% of the mean")
    elif relative_spread < 0.30:
        confidence = 0.4
        evidence.append(f"loose but recognisable interval, spread {relative_spread * 100:.1f}%")
    else:
        evidence.append(f"intervals vary by {relative_spread * 100:.0f}%; not periodic")

    # Few samples cannot support a strong claim, however tight they look.
    samples = stats["samples"]
    if samples < 5:
        confidence = min(confidence, 0.3)
        evidence.append(f"only {samples} samples")
    elif samples < 20:
        confidence = min(confidence, 0.7)
        evidence.append(f"{samples} samples")
    else:
        evidence.append(f"{samples} samples")

    return {
        **stats,
        "periodic": confidence >= 0.6,
        "confidence": round(confidence, 2),
        "evidence": evidence,
    }
