"""ISO-TP (ISO 15765-2) reassembly.

Pure computation over frames that have already been captured.  Nothing here
opens a socket or transmits: reassembly is something you do to evidence, and
keeping it separate from the transport means a capture taken six months ago
decodes exactly the same way today.

The reassembler is deliberately tolerant of the mess real buses produce —
missing consecutive frames, a first frame that never completes, sequence
numbers that jump — and it *reports* each problem rather than quietly
dropping the message.  A silently discarded partial response is how you end
up believing an ECU never answered.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["IsoTpMessage", "reassemble"]

#: Protocol Control Information frame types, from the high nibble of byte 0.
_SINGLE_FRAME = 0x0
_FIRST_FRAME = 0x1
_CONSECUTIVE_FRAME = 0x2
_FLOW_CONTROL = 0x3

_FLOW_STATUS = {0: "ContinueToSend", 1: "Wait", 2: "Overflow"}


@dataclass(slots=True)
class IsoTpMessage:
    """One reassembled transport-layer message."""

    can_id: int
    data: bytes
    #: Monotonic timestamp of the first frame that contributed.
    start_monotonic_ns: int
    #: Monotonic timestamp of the last frame that contributed.
    end_monotonic_ns: int
    frame_count: int
    complete: bool
    #: Length the sender declared, which may differ from what arrived.
    declared_length: int | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return (self.end_monotonic_ns - self.start_monotonic_ns) / 1e6

    def as_dict(self) -> dict[str, Any]:
        return {
            "can_id": f"0x{self.can_id:03X}",
            "hex": self.data.hex().upper(),
            "length": len(self.data),
            "declared_length": self.declared_length,
            "complete": self.complete,
            "frames": self.frame_count,
            "duration_ms": round(self.duration_ms, 3),
            "problems": self.problems,
        }


@dataclass(slots=True)
class _Pending:
    can_id: int
    declared_length: int
    buffer: bytearray
    start_ns: int
    last_ns: int
    frames: int
    next_sequence: int
    problems: list[str]


def _frame_bytes(frame: dict[str, Any]) -> bytes:
    payload = frame.get("data")
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return bytes.fromhex(payload.replace(" ", ""))
    if isinstance(payload, (list, tuple)):
        return bytes(payload)
    raise ValueError(f"frame has no usable data field: {frame!r}")


def reassemble(
    frames: Iterable[dict[str, Any]],
    *,
    can_ids: Sequence[int] | None = None,
    include_flow_control: bool = False,
) -> list[IsoTpMessage]:
    """Reassemble ISO-TP messages from captured CAN frames.

    ``frames`` are dicts as produced by the CAN drivers: ``can_id``,
    ``data`` (hex string, bytes or list) and ``monotonic_ns``.

    Each arbitration id is reassembled independently, because a request and
    its response use different ids and interleave freely on a busy bus.
    """
    wanted = set(can_ids) if can_ids is not None else None
    pending: dict[int, _Pending] = {}
    messages: list[IsoTpMessage] = []

    for frame in frames:
        can_id = (
            int(frame["can_id"], 16) if isinstance(frame["can_id"], str) else int(frame["can_id"])
        )
        if wanted is not None and can_id not in wanted:
            continue
        data = _frame_bytes(frame)
        if not data:
            continue
        timestamp = int(frame.get("monotonic_ns", 0))
        pci_type = data[0] >> 4

        if pci_type == _SINGLE_FRAME:
            length = data[0] & 0x0F
            if length == 0 and len(data) > 1:
                # CAN FD escape: the real length lives in byte 1.
                length = data[1]
                payload = data[2 : 2 + length]
            else:
                payload = data[1 : 1 + length]
            problems = []
            if len(payload) < length:
                problems.append(
                    f"single frame declared {length} bytes but only {len(payload)} present"
                )
            messages.append(
                IsoTpMessage(
                    can_id=can_id,
                    data=payload,
                    start_monotonic_ns=timestamp,
                    end_monotonic_ns=timestamp,
                    frame_count=1,
                    complete=len(payload) == length,
                    declared_length=length,
                    problems=problems,
                )
            )

        elif pci_type == _FIRST_FRAME:
            declared = ((data[0] & 0x0F) << 8) | data[1]
            if declared == 0 and len(data) >= 6:
                # 32-bit escape length for large CAN FD transfers.
                declared = int.from_bytes(data[2:6], "big")
                payload = data[6:]
            else:
                payload = data[2:]
            if can_id in pending:
                stale = pending.pop(can_id)
                stale.problems.append("superseded by a new first frame before completing")
                messages.append(_finish(stale, complete=False))
            pending[can_id] = _Pending(
                can_id=can_id,
                declared_length=declared,
                buffer=bytearray(payload),
                start_ns=timestamp,
                last_ns=timestamp,
                frames=1,
                next_sequence=1,
                problems=[],
            )

        elif pci_type == _CONSECUTIVE_FRAME:
            sequence = data[0] & 0x0F
            entry = pending.get(can_id)
            if entry is None:
                # A consecutive frame with no first frame usually means the
                # capture started mid-transfer. Worth saying so.
                messages.append(
                    IsoTpMessage(
                        can_id=can_id,
                        data=data[1:],
                        start_monotonic_ns=timestamp,
                        end_monotonic_ns=timestamp,
                        frame_count=1,
                        complete=False,
                        problems=[
                            f"consecutive frame (seq {sequence}) with no preceding first "
                            "frame; the capture probably started mid-transfer"
                        ],
                    )
                )
                continue
            if sequence != entry.next_sequence:
                entry.problems.append(
                    f"sequence jumped: expected {entry.next_sequence}, got {sequence}"
                )
            entry.next_sequence = (sequence + 1) & 0x0F
            entry.buffer.extend(data[1:])
            entry.frames += 1
            entry.last_ns = timestamp
            if len(entry.buffer) >= entry.declared_length:
                messages.append(_finish(pending.pop(can_id), complete=True))

        elif pci_type == _FLOW_CONTROL and include_flow_control:
            status = data[0] & 0x0F
            block_size = data[1] if len(data) > 1 else None
            st_min = data[2] if len(data) > 2 else None
            messages.append(
                IsoTpMessage(
                    can_id=can_id,
                    data=data,
                    start_monotonic_ns=timestamp,
                    end_monotonic_ns=timestamp,
                    frame_count=1,
                    complete=True,
                    problems=[
                        f"flow control: {_FLOW_STATUS.get(status, f'reserved({status})')}"
                        f", BS={block_size}, STmin={st_min}"
                    ],
                )
            )

    # Anything still pending never completed. Report it rather than dropping it.
    for entry in pending.values():
        entry.problems.append(
            f"incomplete: {len(entry.buffer)} of {entry.declared_length} declared bytes received"
        )
        messages.append(_finish(entry, complete=False))

    messages.sort(key=lambda message: message.start_monotonic_ns)
    return messages


def _finish(entry: _Pending, *, complete: bool) -> IsoTpMessage:
    return IsoTpMessage(
        can_id=entry.can_id,
        data=bytes(entry.buffer[: entry.declared_length]) if complete else bytes(entry.buffer),
        start_monotonic_ns=entry.start_ns,
        end_monotonic_ns=entry.last_ns,
        frame_count=entry.frames,
        complete=complete,
        declared_length=entry.declared_length,
        problems=entry.problems,
    )
