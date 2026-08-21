"""Captures: the bytes on disk, the hash over them, and the timeline row.

The claim FieldDeck makes about a capture is narrow and absolute — what is in
the file is what came off the wire, it is never rewritten, and its hash is
recorded next to it so a later disagreement between a decoder and the raw
bytes can be settled.  These tests hold that claim to the letter: they hash
the file themselves, compare it byte for byte against what the simulator
generated, and check the artifact can be found again through the session
timeline rather than only in the action's reply.
"""

from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from pathlib import Path

import pytest

from fielddeck.analysis.crc import crc
from fielddeck.common.events import EventType
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

from .conftest import SIM_CAN, SIM_SERIAL

#: Long enough for the 10 ms and 100 ms simulated traffic to appear several
#: times over, short enough that the core capture tests stay under a second.
CAPTURE_S = 0.9


def session_root(daemon: InstrumentDaemon, session_id: str) -> Path:
    return daemon.sessions.sessions_dir / session_id


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def test_can_capture_artifact_is_immutable_and_hashed(
    daemon: InstrumentDaemon, client: InstrumentClient, session: str
) -> None:
    result = await client.execute(
        "can.capture",
        {"device": SIM_CAN, "duration_s": CAPTURE_S, "label": "bringup"},
        timeout_s=30.0,
    )
    artifact = result.result["artifact"]
    assert artifact is not None, "a capture inside a session must produce an artifact"

    path = session_root(daemon, session) / artifact["relative_path"]
    payload = path.read_bytes()

    # The hash is over the bytes, not over a summary of them.
    assert artifact["sha256"] == sha256_of(path)
    assert artifact["size_bytes"] == len(payload)
    assert artifact["raw"] is True
    assert artifact["device_id"] == SIM_CAN
    assert artifact["source_artifact_ids"] == []

    lines = payload.decode("ascii").splitlines()
    assert len(lines) == result.result["count"] > 0
    # candump format, which can-utils and Wireshark both read back.
    ids = {line.split()[2].split("#")[0] for line in lines}
    assert ids <= {"101", "181", "280"}

    # Immutable in practice, not just in intent: a second capture writes a new
    # file and leaves the first one exactly as it was.
    again = await client.execute(
        "can.capture",
        {"device": SIM_CAN, "duration_s": 0.4, "label": "bringup"},
        timeout_s=30.0,
    )
    second = again.result["artifact"]
    assert second["relative_path"] != artifact["relative_path"]
    assert path.read_bytes() == payload
    assert sha256_of(path) == artifact["sha256"]


async def test_can_capture_frames_are_queryable_from_the_timeline(
    daemon: InstrumentDaemon, client: InstrumentClient, session: str
) -> None:
    """The capture must be findable from the session, not only from the reply.

    An engineer coming back to a session next week has the timeline and the
    directory; they do not have the JSON that the action returned.
    """
    result = await client.execute(
        "can.capture",
        {"device": SIM_CAN, "duration_s": CAPTURE_S, "label": "timeline"},
        timeout_s=30.0,
    )
    artifact = result.result["artifact"]
    frame_count = result.result["count"]

    stored = (await client.execute("session.get", {"session_id": session})).result
    rows = {row["artifact_id"]: row for row in stored["artifacts"]}
    assert artifact["artifact_id"] in rows
    row = rows[artifact["artifact_id"]]
    assert row["sha256"] == artifact["sha256"]
    assert row["raw"] is True
    assert row["metadata"]["frames"] == frame_count

    # The recording is bracketed on the timeline, so "when was this capture
    # running?" is answerable without opening the file.
    events = (
        await client.execute(
            "session.events",
            {
                "session_id": session,
                "types": [str(EventType.CAPTURE_STARTED), str(EventType.CAPTURE_STOPPED)],
            },
        )
    ).result["events"]
    assert [event["type"] for event in events] == [
        str(EventType.CAPTURE_STARTED),
        str(EventType.CAPTURE_STOPPED),
    ]
    assert {event["device_id"] for event in events} == {SIM_CAN}
    started, stopped = events

    # And the count is on the timeline too, in the completion record.
    completed = (
        await client.execute(
            "session.events",
            {"session_id": session, "types": [str(EventType.ACTION_COMPLETED)]},
        )
    ).result["events"]
    capture_rows = [row for row in completed if row["action"] == "can.capture"]
    assert capture_rows, "can.capture must leave a completion record"
    assert capture_rows[-1]["payload"]["result"]["count"] == frame_count

    # The bracket really spans the recording, and the completion record lands
    # after it: an action that reported "done" before the capture stopped
    # would make every window query around it point at the wrong instant.
    path = session_root(daemon, session) / artifact["relative_path"]
    assert path.read_text(encoding="ascii").strip()
    recorded_ns = stopped["monotonic_ns"] - started["monotonic_ns"]
    assert recorded_ns >= CAPTURE_S * 0.8 * 1e9
    assert capture_rows[-1]["monotonic_ns"] >= stopped["monotonic_ns"]


async def test_serial_capture_preserves_bytes_exactly(
    daemon: InstrumentDaemon, client: InstrumentClient, session: str
) -> None:
    """The recorded bytes are the generated bytes, offset for offset.

    Verified three ways: the index sidecar tiles the file with no gap and no
    overlap, the chunks the driver reported match the file at their recorded
    offsets, and the simulator's own framing (header, rolling counter,
    CRC-16/MODBUS trailer) still parses out of the bytes on disk.  A capture
    that "cleaned up" anything would fail all three.
    """
    result = await client.execute(
        "serial.capture",
        {"device": SIM_SERIAL, "duration_s": CAPTURE_S, "label": "stream"},
        timeout_s=30.0,
    )
    artifact = result.result["artifact"]
    root = session_root(daemon, session)
    raw_path = root / artifact["relative_path"]
    raw = raw_path.read_bytes()

    assert artifact["sha256"] == sha256_of(raw_path)
    assert len(raw) == result.result["bytes"] == artifact["size_bytes"] > 0

    index_path = raw_path.with_suffix(".idx.jsonl")
    index = [json.loads(line) for line in index_path.read_text(encoding="ascii").splitlines()]
    assert index, "a capture without an arrival-time index cannot be correlated"

    offset = 0
    for entry in index:
        assert entry["offset"] == offset, "the index must tile the file exactly"
        offset += entry["len"]
    assert offset == len(raw)
    assert [entry["monotonic_ns"] for entry in index] == sorted(
        entry["monotonic_ns"] for entry in index
    )

    # The chunks the action reported are literally the bytes at those offsets.
    for entry, chunk in zip(index, result.result["chunks"], strict=False):
        assert chunk["len"] == entry["len"]
        assert chunk["monotonic_ns"] == entry["monotonic_ns"]
        assert raw[entry["offset"] : entry["offset"] + entry["len"]] == bytes.fromhex(chunk["hex"])

    # The simulator's boot banner, verbatim, including its CR/LF.
    assert raw.startswith(b"\r\n[boot] fielddeck-sim controller v1.4.2\r\n")

    packets = [
        raw[entry["offset"] : entry["offset"] + entry["len"]]
        for entry in index
        if entry["len"] == 8 and raw[entry["offset"] : entry["offset"] + 2] == b"\x55\xaa"
    ]
    assert packets, "no framed packets survived the capture"
    good = [packet for packet in packets if _crc_ok(packet)]
    # The simulator corrupts roughly one frame in forty on purpose, so a
    # perfect score would mean the corruption was silently repaired.
    assert len(good) >= len(packets) * 0.8
    counters = [packet[4] for packet in packets]
    assert all((later - earlier) % 256 == 1 for earlier, later in pairwise(counters))


def _crc_ok(packet: bytes) -> bool:
    return crc("crc16-modbus", packet[:6]).to_bytes(2, "little") == packet[6:8]


async def test_derived_artifacts_record_their_provenance(
    daemon: InstrumentDaemon, client: InstrumentClient, session: str
) -> None:
    """A derived file must name the raw bytes it came from and its producer."""
    result = await client.execute(
        "serial.capture",
        {"device": SIM_SERIAL, "duration_s": 0.5, "label": "provenance"},
        timeout_s=30.0,
    )
    raw_id = result.result["artifact"]["artifact_id"]

    stored = (await client.execute("session.get", {"session_id": session})).result
    derived = [row for row in stored["artifacts"] if not row["raw"]]
    assert derived, "the arrival-time index is a derived artifact and must be registered"
    index_row = next(row for row in derived if row["relative_path"].endswith(".idx.jsonl"))
    assert index_row["source_artifact_ids"] == [raw_id]
    assert index_row["producer"]


async def test_capture_without_a_session_says_so_instead_of_dropping_data(
    client: InstrumentClient,
) -> None:
    """No session means no artifact — and the reply has to admit it."""
    result = await client.execute(
        "serial.capture",
        {"device": SIM_SERIAL, "duration_s": 0.3, "label": "orphan"},
        timeout_s=30.0,
    )
    assert result.result["artifact"] is None
    assert "no active session" in result.result["warning"]
    assert result.result["bytes"] > 0


@pytest.mark.slow
async def test_a_longer_capture_stays_byte_consistent(
    daemon: InstrumentDaemon, client: InstrumentClient, session: str
) -> None:
    """Ten times the length, same guarantees: size, hash and index all agree."""
    result = await client.execute(
        "serial.capture",
        {"device": SIM_SERIAL, "duration_s": 3.0, "label": "long"},
        timeout_s=60.0,
    )
    artifact = result.result["artifact"]
    raw_path = session_root(daemon, session) / artifact["relative_path"]
    index_path = raw_path.with_suffix(".idx.jsonl")
    index = [json.loads(line) for line in index_path.read_text(encoding="ascii").splitlines()]

    assert sum(entry["len"] for entry in index) == raw_path.stat().st_size
    assert sha256_of(raw_path) == artifact["sha256"]
    assert len(index) > 20
