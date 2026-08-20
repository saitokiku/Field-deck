"""Protocol analysis actions.

Everything here is post-processing over captures that already exist, so it is
all PASSIVE and all available during an emergency stop — understanding what
happened is exactly what you want to be doing while the bench is safe.

The UDS actions report the permission each observed service *would* require
to transmit.  That number is what an operator needs before deciding whether
replaying a capture is a reasonable thing to do.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from fielddeck.common.errors import CaptureError, SessionError
from fielddeck.common.models import PermissionLevel, StrictModel
from fielddeck.drivers.base import ActionContext, ActionSpec, NoParams, action, collect_actions
from fielddeck.protocols.isotp import reassemble
from fielddeck.protocols.uds import decode_message, service_catalogue

if TYPE_CHECKING:  # pragma: no cover
    from fielddeck.daemon.service import InstrumentDaemon

__all__ = ["build_action_specs", "parse_candump"]

#: candump text format: ``(1755729000.123456) can0 7E8#0322F19000000000``
_CANDUMP = re.compile(
    r"^\((?P<ts>\d+\.\d+)\)\s+(?P<iface>\S+)\s+(?P<id>[0-9A-Fa-f]+)#(?P<data>[0-9A-Fa-f]*)"
)


def parse_candump(text: str) -> list[dict[str, Any]]:
    """Parse candump log text into frame dicts.

    Timestamps in a candump file are absolute seconds, which are converted to
    nanoseconds here so the reassembler sees the same units the live path
    produces. Lines that do not parse are skipped rather than aborting the
    whole decode: a truncated final line is normal in a capture that was
    stopped mid-write.
    """
    frames: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _CANDUMP.match(line.strip())
        if match is None:
            continue
        frames.append(
            {
                "monotonic_ns": int(float(match["ts"]) * 1e9),
                "can_id": int(match["id"], 16),
                "interface": match["iface"],
                "data": match["data"],
            }
        )
    return frames


class CaptureRefParams(StrictModel):
    artifact_path: str
    can_ids: list[int] | None = Field(default=None, description="Limit to these arbitration ids")
    session_id: str | None = None


class IsoTpParams(CaptureRefParams):
    include_flow_control: bool = False


class ProtocolActions:
    def __init__(self, daemon: InstrumentDaemon) -> None:
        self.daemon = daemon

    def _resolve(self, params: CaptureRefParams) -> Path:
        session_id = params.session_id or self.daemon.sessions.current_id
        if session_id is None:
            raise SessionError("no active session and no session_id given")
        root = (self.daemon.sessions.sessions_dir / session_id).resolve()
        candidate = (root / params.artifact_path).resolve()
        if not candidate.is_relative_to(root):
            raise CaptureError(
                "capture path escapes the session directory",
                details={"artifact_path": params.artifact_path},
                preserved="no file was read",
            )
        if not candidate.is_file():
            raise CaptureError(
                f"no capture at {params.artifact_path}",
                details={"session_id": session_id},
            )
        return candidate

    @action(
        "can.isotp",
        permission=PermissionLevel.PASSIVE,
        params=IsoTpParams,
        state_changing=False,
        description="Reassemble ISO-TP messages from a stored CAN capture.",
        allowed_during_estop=True,
        timeout_s=120.0,
    )
    async def can_isotp(self, ctx: ActionContext, params: IsoTpParams) -> dict[str, Any]:
        """Post-processing over an existing capture. Nothing reaches the bus."""
        path = self._resolve(params)

        def _work() -> dict[str, Any]:
            frames = parse_candump(path.read_text(encoding="ascii", errors="replace"))
            messages = reassemble(
                frames,
                can_ids=params.can_ids,
                include_flow_control=params.include_flow_control,
            )
            incomplete = [m for m in messages if not m.complete]
            return {
                "frames_read": len(frames),
                "messages": [message.as_dict() for message in messages],
                "count": len(messages),
                "incomplete": len(incomplete),
                # Surfaced rather than buried: a partial response usually means
                # the capture window clipped the exchange, not that the ECU
                # stayed silent.
                "problems": [problem for message in messages for problem in message.problems][:50],
            }

        return {**await asyncio.to_thread(_work), "source": params.artifact_path}

    @action(
        "can.uds_decode",
        permission=PermissionLevel.PASSIVE,
        params=CaptureRefParams,
        state_changing=False,
        description="Reassemble and decode a UDS exchange from a stored CAN capture.",
        allowed_during_estop=True,
        timeout_s=120.0,
    )
    async def can_uds_decode(self, ctx: ActionContext, params: CaptureRefParams) -> dict[str, Any]:
        path = self._resolve(params)

        def _work() -> dict[str, Any]:
            frames = parse_candump(path.read_text(encoding="ascii", errors="replace"))
            messages = reassemble(frames, can_ids=params.can_ids)
            decoded: list[dict[str, Any]] = []
            highest = PermissionLevel.PASSIVE
            for message in messages:
                if not message.data:
                    continue
                entry = decode_message(message.data)
                entry["can_id"] = f"0x{message.can_id:03X}"
                entry["monotonic_ns"] = message.start_monotonic_ns
                entry["complete"] = message.complete
                decoded.append(entry)
                permission_text = entry.get("permission_to_transmit")
                if permission_text and permission_text != "unknown":
                    level = PermissionLevel(permission_text)
                    if level.rank > highest.rank:
                        highest = level
            return {
                "messages": decoded,
                "count": len(decoded),
                "highest_permission_observed": str(highest),
            }

        result = await asyncio.to_thread(_work)
        return {
            **result,
            "source": params.artifact_path,
            "note": (
                "decoding a capture is PASSIVE; "
                f"transmitting the services seen here would require "
                f"{result['highest_permission_observed']}"
            ),
        }

    @action(
        "uds.services",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="UDS service catalogue with the permission each would require.",
        allowed_during_estop=True,
    )
    async def uds_services(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        catalogue = service_catalogue()
        return {
            "services": catalogue,
            "count": len(catalogue),
            "note": (
                "UDS spans reading a VIN and erasing an ECU over the same "
                "transport; the permission column is the difference"
            ),
        }


def build_action_specs(daemon: InstrumentDaemon) -> dict[str, ActionSpec]:
    return collect_actions(ProtocolActions(daemon))
