"""Deterministic session reports.

The report is the artefact that leaves the bench: it is what gets attached to
a ticket, emailed to a supplier, or read back in six months when the same
fault reappears.  Two properties matter more than presentation.

**It is deterministic.**  The same session produces the same report, byte for
byte.  Nothing is summarised by a model, nothing is rephrased, nothing is
inferred.

**Facts and interpretation are separated.**  Measurements, artifact hashes,
authorizations and faults go in the factual body.  Anything an assistant
concluded is collected under its own clearly-labelled heading, because a
narrative that reads as measured data is how a hypothesis quietly becomes a
"finding" in someone else's head.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fielddeck import __version__
from fielddeck.capture.storage import SessionLayout
from fielddeck.capture.timeline import Timeline
from fielddeck.common.errors import SessionError
from fielddeck.common.events import EventType
from fielddeck.common.timebase import format_utc_ns

__all__ = ["build_report", "render_markdown"]

#: Events that belong in the "what was authorized" section of a report.
_AUTHORIZATION = {
    str(EventType.ARM_GRANTED),
    str(EventType.ARM_REVOKED),
    str(EventType.ARM_EXPIRED),
    str(EventType.ACTION_DENIED),
    str(EventType.LIMIT_REJECTED),
}
_FAULTS = {
    str(EventType.DEVICE_FAULT),
    str(EventType.ACTION_FAILED),
    str(EventType.ESTOP),
    str(EventType.CAPTURE_OVERFLOW),
    str(EventType.LEASE_EXPIRED),
}
_POWER = {
    str(EventType.OUTPUT_ENABLED),
    str(EventType.OUTPUT_DISABLED),
    str(EventType.SAFE_STATE_APPLIED),
}


def build_report(session_dir: Path) -> dict[str, Any]:
    """Assemble the factual report structure for one session."""
    layout = SessionLayout(session_dir)
    if not layout.session_json.exists():
        raise SessionError(
            f"{session_dir} does not look like a session directory",
            details={"path": str(session_dir)},
        )
    session = json.loads(layout.session_json.read_text(encoding="utf-8"))

    with Timeline(layout.timeline_db) as timeline:
        summary = timeline.summary()
        artifacts = timeline.artifacts()
        marks = timeline.marks()
        measurements = timeline.measurements(limit=100_000)
        authorizations = timeline.events(types=sorted(_AUTHORIZATION), limit=2000)
        faults = timeline.events(types=sorted(_FAULTS), limit=2000)
        power = timeline.events(types=sorted(_POWER), limit=2000)
        assistant = timeline.events(types=[str(EventType.ASSISTANT_OBSERVATION)], limit=500)
        recipes = timeline.events(
            types=[
                str(EventType.RECIPE_FINISHED),
                str(EventType.RECIPE_ASSERTION),
            ],
            limit=1000,
        )

    anchor = session.get("started_monotonic_ns", 0)
    by_quantity: dict[str, dict[str, Any]] = {}
    for row in measurements:
        entry = by_quantity.setdefault(
            row["quantity"],
            {"unit": row.get("unit"), "count": 0, "min": None, "max": None, "sum": 0.0},
        )
        value = float(row["value"])
        entry["count"] += 1
        entry["sum"] += value
        entry["min"] = value if entry["min"] is None else min(entry["min"], value)
        entry["max"] = value if entry["max"] is None else max(entry["max"], value)
    for entry in by_quantity.values():
        entry["mean"] = round(entry["sum"] / entry["count"], 6) if entry["count"] else None
        entry.pop("sum")

    return {
        "report_version": 1,
        "generated_by": f"fielddeck {__version__}",
        "session": session,
        "timeline": summary,
        "devices": session.get("devices", []),
        "marks": marks,
        "measurements": by_quantity,
        "artifacts": artifacts,
        "authorizations": authorizations,
        "power_events": power,
        "faults": faults,
        "recipes": recipes,
        # Kept separate from every factual section above, on purpose.
        "assistant_observations": assistant,
        "anchor_monotonic_ns": anchor,
    }


def _offset(monotonic_ns: int, anchor: int) -> str:
    return f"+{(monotonic_ns - anchor) / 1e9:9.6f}s"


def render_markdown(report: dict[str, Any]) -> str:
    """Render the report as Markdown.

    Deterministic: no timestamps of its own, no random ordering, no wording
    that depends on anything but the session's contents.
    """
    session = report["session"]
    anchor = report["anchor_monotonic_ns"]
    out: list[str] = []
    add = out.append

    add(f"# Session report: {session.get('name', session['id'])}")
    add("")
    if session.get("simulated"):
        add("> **Simulated session.** No physical hardware was involved.")
        add("")
    add(f"- **Session id**: `{session['id']}`")
    add(f"- **Started (UTC)**: {format_utc_ns(session['started_utc_ns'])}")
    if session.get("ended_utc_ns"):
        add(f"- **Ended (UTC)**: {format_utc_ns(session['ended_utc_ns'])}")
        duration = (session["ended_utc_ns"] - session["started_utc_ns"]) / 1e9
        add(f"- **Duration**: {duration:.1f} s")
    if session.get("operator"):
        add(f"- **Operator**: {session['operator']}")
    add(f"- **Software**: {json.dumps(session.get('software', {}), sort_keys=True)}")
    add(f"- **Events recorded**: {report['timeline']['events']}")
    add("")

    add("## Hardware attached")
    add("")
    devices = report["devices"]
    if devices:
        add("| Device | Kind | Identity | Simulated |")
        add("|---|---|---|---|")
        for device in devices:
            identity = device.get("serial_number") or device.get("path") or "-"
            add(
                f"| {device['display_name']} | {device['kind']} | "
                f"`{identity}` | {'yes' if device.get('simulated') else 'no'} |"
            )
    else:
        add("No devices were recorded for this session.")
    add("")

    add("## Authorizations granted")
    add("")
    add(
        "Every permission the operator granted, and every request that was "
        "refused, in the order they happened."
    )
    add("")
    if report["authorizations"]:
        add("| Time | Event | Permission | Detail |")
        add("|---|---|---|---|")
        for event in report["authorizations"]:
            add(
                f"| {_offset(event['monotonic_ns'], anchor)} | {event['type']} | "
                f"{event.get('permission') or '-'} | "
                f"{(event.get('message') or '').replace('|', '/')} |"
            )
    else:
        add("Nothing was armed during this session; it remained SAFE throughout.")
    add("")

    if report["power_events"]:
        add("## Power and safe-state events")
        add("")
        add("| Time | Event | Device | Detail |")
        add("|---|---|---|---|")
        for event in report["power_events"]:
            add(
                f"| {_offset(event['monotonic_ns'], anchor)} | {event['type']} | "
                f"`{event.get('device_id') or '-'}` | "
                f"{(event.get('message') or '').replace('|', '/')} |"
            )
        add("")

    add("## Measurements")
    add("")
    if report["measurements"]:
        add("| Quantity | Samples | Min | Mean | Max | Unit |")
        add("|---|---|---|---|---|---|")
        for quantity, stats in sorted(report["measurements"].items()):
            add(
                f"| {quantity} | {stats['count']} | {stats['min']} | "
                f"{stats['mean']} | {stats['max']} | {stats['unit'] or ''} |"
            )
    else:
        add("No measurements were recorded.")
    add("")

    add("## Operator marks")
    add("")
    if report["marks"]:
        add("| Time | Label | Source | Note |")
        add("|---|---|---|---|")
        for mark in report["marks"]:
            add(
                f"| {_offset(mark['monotonic_ns'], anchor)} | {mark['label']} | "
                f"{mark['source']} | {mark.get('note') or ''} |"
            )
    else:
        add("No marks were placed.")
    add("")

    add("## Faults and failures")
    add("")
    if report["faults"]:
        add("| Time | Event | Device | Detail |")
        add("|---|---|---|---|")
        for event in report["faults"]:
            add(
                f"| {_offset(event['monotonic_ns'], anchor)} | {event['type']} | "
                f"`{event.get('device_id') or '-'}` | "
                f"{(event.get('message') or '').replace('|', '/')} |"
            )
    else:
        add("No faults, failures or emergency stops were recorded.")
    add("")

    add("## Captured artifacts")
    add("")
    add(
        "Raw captures are immutable. Derived artifacts name the source they "
        "were produced from and the tool that produced them."
    )
    add("")
    if report["artifacts"]:
        add("| Path | Kind | Size | Raw | Producer | SHA-256 |")
        add("|---|---|---|---|---|---|")
        for artifact in report["artifacts"]:
            digest = (artifact.get("sha256") or "")[:16]
            add(
                f"| `{artifact['relative_path']}` | {artifact['kind']} | "
                f"{artifact['size_bytes']} B | {'yes' if artifact['raw'] else 'no'} | "
                f"{artifact.get('producer') or '-'} | `{digest}...` |"
            )
        derived = [a for a in report["artifacts"] if not a["raw"] and a.get("source_artifact_ids")]
        if derived:
            add("")
            add("Provenance:")
            add("")
            by_id = {a["artifact_id"]: a["relative_path"] for a in report["artifacts"]}
            for artifact in derived:
                sources = ", ".join(
                    f"`{by_id.get(source, source)}`" for source in artifact["source_artifact_ids"]
                )
                add(
                    f"- `{artifact['relative_path']}` <- {sources} "
                    f"(via {artifact.get('producer') or 'unknown'} "
                    f"{artifact.get('producer_version') or ''})".rstrip()
                )
    else:
        add("No artifacts were captured.")
    add("")

    if report["recipes"]:
        add("## Recipe results")
        add("")
        add("| Time | Event | Detail |")
        add("|---|---|---|")
        for event in report["recipes"]:
            add(
                f"| {_offset(event['monotonic_ns'], anchor)} | {event['type']} | "
                f"{(event.get('message') or '').replace('|', '/')} |"
            )
        add("")

    if session.get("notes"):
        add("## Operator notes")
        add("")
        for note in session["notes"]:
            add(f"- {note}")
        add("")

    add("## Assistant observations")
    add("")
    add(
        "*The following are AI-generated interpretations, recorded separately "
        "from the measured data above. They are hypotheses, not measurements.*"
    )
    add("")
    if report["assistant_observations"]:
        for event in report["assistant_observations"]:
            add(f"- {_offset(event['monotonic_ns'], anchor)} {event.get('message') or ''}")
    else:
        add("None recorded.")
    add("")

    return "\n".join(out) + "\n"
