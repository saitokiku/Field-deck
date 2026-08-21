"""Deadlines, cancellation, and the exclusivity of a device.

Three properties an instrument has to get right when something is
interrupted:

* a long capture actually *stops* when it is cancelled or its deadline
  passes — not eventually, and not only after the duration it was asked for;
* whatever it had already written is still on disk and still findable through
  the session, because a capture that vanishes when the operator presses stop
  teaches people never to press stop;
* two state-changing actions never share one device, and the loser is told
  who is holding it rather than being queued behind them.

The streaming driver below exists because the simulated devices materialise
their data at the end.  The real transports (``fielddeck.transports``) write
as bytes arrive, which is what makes partial data survive, so the driver here
mirrors that shape: write, flush, register the artifact in a ``finally``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from pydantic import Field

from fielddeck.common.errors import ActionCancelled, ActionTimeout, CaptureError, DeviceBusy
from fielddeck.common.events import EventType
from fielddeck.common.models import (
    ConnectionState,
    DeviceCapability,
    DeviceDescriptor,
    DeviceRole,
    PermissionLevel,
    TransportKind,
)
from fielddeck.common.timebase import monotonic_ns
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon
from fielddeck.drivers.base import ActionContext, DeviceParams, Driver, action

from .conftest import SIM_SERIAL, arm

STREAM_DEVICE = "test:stream:stream-0"

#: One chunk of "wire" data.  Small and frequent, so a cancellation lands in
#: the middle of a stream rather than between two large writes.
CHUNK = bytes(range(64))


class StreamCaptureParams(DeviceParams):
    duration_s: float = Field(default=30.0, gt=0, le=600)
    chunk_ms: float = Field(default=10.0, gt=0, le=1000)
    label: str = "partial"


class HoldParams(DeviceParams):
    seconds: float = Field(default=1.0, gt=0, le=30)


class StreamingDriver(Driver):
    """A driver that writes as it goes, the way the real transports do."""

    kind = TransportKind.SERIAL

    def __init__(self, name: str = "stream-0") -> None:
        super().__init__(
            DeviceDescriptor(
                id=f"test:stream:{name}",
                kind=TransportKind.SERIAL,
                display_name="streaming test device",
                roles=[DeviceRole.BUS],
                capabilities=[DeviceCapability.RX, DeviceCapability.STREAM],
                state=ConnectionState.READY,
                simulated=True,
            )
        )
        self.written = 0

    async def status(self) -> dict[str, Any]:
        return {"written_bytes": self.written}

    @action(
        "stream.capture",
        permission=PermissionLevel.PASSIVE,
        params=StreamCaptureParams,
        state_changing=False,
        description="Record a synthetic stream, flushing every chunk.",
        cancelable=True,
        timeout_s=600.0,
    )
    async def stream_capture(self, ctx: ActionContext, params: StreamCaptureParams) -> Any:
        if ctx.recorder is None:
            raise CaptureError("this capture writes into a session; start one first")
        path = ctx.recorder.capture_path("serial", params.label, ".bin")
        deadline = monotonic_ns() + int(params.duration_s * 1e9)
        written = 0
        handle = path.open("wb")
        try:
            while monotonic_ns() < deadline and not ctx.cancelled:
                handle.write(CHUNK)
                handle.flush()
                written += len(CHUNK)
                self.written += len(CHUNK)
                await asyncio.sleep(params.chunk_ms / 1000.0)
        finally:
            # Registered on every path, including cancellation and the
            # dispatcher's deadline: the bytes are already on the disk, so the
            # only question is whether the session knows about them.
            handle.close()
            ctx.recorder.add_artifact(
                path,
                kind="serial",
                device_id=self.device_id,
                raw=True,
                metadata={"bytes": written, "complete": monotonic_ns() >= deadline},
            )
        return {"bytes": written, "cancelled": ctx.cancelled}

    @action(
        "stream.hold",
        permission=PermissionLevel.CONTROL,
        params=HoldParams,
        state_changing=True,
        description="Hold exclusive control of the device for a while.",
        cancelable=True,
        timeout_s=60.0,
    )
    async def stream_hold(self, ctx: ActionContext, params: HoldParams) -> Any:
        await asyncio.sleep(params.seconds)
        return {"held_s": params.seconds}


@pytest.fixture
def streaming(daemon: InstrumentDaemon) -> StreamingDriver:
    """A streaming driver registered with the live daemon."""
    driver = StreamingDriver()
    daemon.registry.add(driver)
    return driver


async def test_cancelling_a_capture_stops_it_and_keeps_the_bytes(
    daemon: InstrumentDaemon, client: InstrumentClient, session: str, streaming: StreamingDriver
) -> None:
    request_id = "cancel-me"
    started = time.monotonic()
    running = asyncio.create_task(
        client.execute(
            "stream.capture",
            {"device": STREAM_DEVICE, "duration_s": 30.0, "label": "cancelled"},
            timeout_s=120.0,
            request_id=request_id,
        )
    )
    # Let it get some bytes onto the disk before pulling the plug.
    await asyncio.sleep(0.3)
    assert (await client.call("action.cancel", {"request_id": request_id}))["cancelled"] is True

    with pytest.raises(ActionCancelled) as cancelled:
        await running
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, "a cancelled capture must stop, not run to its duration"
    assert "on disk" in (cancelled.value.preserved or "")

    # The partial capture is registered, hashed, and matches what is on disk.
    stored = (await client.execute("session.get", {"session_id": session})).result
    partial = [row for row in stored["artifacts"] if "cancelled" in row["relative_path"]]
    assert len(partial) == 1
    row = partial[0]
    path = daemon.sessions.sessions_dir / session / row["relative_path"]
    data = path.read_bytes()
    assert len(data) == row["size_bytes"] > 0
    assert row["metadata"]["complete"] is False
    assert data == CHUNK * (len(data) // len(CHUNK)), "the partial file is not a whole prefix"

    # The timeline shows where the recording ended, not just that it started.
    events = (
        await client.execute(
            "session.events",
            {
                "session_id": session,
                "types": [str(EventType.CAPTURE_STARTED), str(EventType.CAPTURE_STOPPED)],
            },
        )
    ).result["events"]
    assert [row["type"] for row in events] == [
        str(EventType.CAPTURE_STARTED),
        str(EventType.CAPTURE_STOPPED),
    ]


async def test_a_deadline_stops_a_capture_and_keeps_the_bytes(
    daemon: InstrumentDaemon, client: InstrumentClient, session: str, streaming: StreamingDriver
) -> None:
    started = time.monotonic()
    with pytest.raises(ActionTimeout) as timed_out:
        await client.execute(
            "stream.capture",
            {"device": STREAM_DEVICE, "duration_s": 30.0, "label": "timedout"},
            timeout_s=0.6,
        )
    elapsed = time.monotonic() - started
    assert 0.3 < elapsed < 6.0
    assert timed_out.value.details["timeout_s"] == 0.6
    assert "on disk" in (timed_out.value.preserved or "")

    stored = (await client.execute("session.get", {"session_id": session})).result
    partial = [row for row in stored["artifacts"] if "timedout" in row["relative_path"]]
    assert len(partial) == 1
    path = daemon.sessions.sessions_dir / session / partial[0]["relative_path"]
    assert path.stat().st_size > 0


async def test_cancelling_a_simulated_capture_stops_it_promptly(
    client: InstrumentClient, session: str
) -> None:
    """The same contract against a driver nobody wrote for this test."""
    request_id = "sim-cancel"
    running = asyncio.create_task(
        client.execute(
            "serial.capture",
            {"device": SIM_SERIAL, "duration_s": 60.0, "label": "interrupted"},
            timeout_s=120.0,
            request_id=request_id,
        )
    )
    await asyncio.sleep(0.4)
    started = time.monotonic()
    await client.call("action.cancel", {"request_id": request_id})

    with pytest.raises(ActionCancelled):
        await running
    assert time.monotonic() - started < 5.0

    # The session survived being interrupted and still accepts work.
    assert (await client.execute("session.mark", {"label": "after-cancel"})).ok


async def test_cancelling_an_unknown_request_is_reported_not_guessed(
    client: InstrumentClient,
) -> None:
    reply = await client.call("action.cancel", {"request_id": "nothing-like-this"})
    assert reply["cancelled"] is False


async def test_second_state_changing_action_gets_device_busy_naming_the_owner(
    client: InstrumentClient, streaming: StreamingDriver
) -> None:
    await arm(client, PermissionLevel.CONTROL, ttl_s=60.0)

    holder = asyncio.create_task(
        client.execute("stream.hold", {"device": STREAM_DEVICE, "seconds": 0.8}, timeout_s=30.0)
    )
    await asyncio.sleep(0.2)

    with pytest.raises(DeviceBusy) as busy:
        await client.execute("stream.hold", {"device": STREAM_DEVICE, "seconds": 0.1})

    assert busy.value.details["device_id"] == STREAM_DEVICE
    assert busy.value.details["busy_with"] == "stream.hold"
    assert "stream.hold" in busy.value.message
    assert "not disturbed" in (busy.value.preserved or "")

    # The one that had the device keeps it, and finishes normally.
    first = await holder
    assert first.ok
    assert first.result["held_s"] == 0.8

    # Once it is free the device takes work again.
    assert (await client.execute("stream.hold", {"device": STREAM_DEVICE, "seconds": 0.05})).ok


async def test_passive_readers_are_not_serialised_behind_each_other(
    client: InstrumentClient, streaming: StreamingDriver
) -> None:
    """Only state-changing work is exclusive; two readers must coexist."""
    results = await asyncio.gather(
        client.execute("device.status", {"device": STREAM_DEVICE}),
        client.execute("device.status", {"device": STREAM_DEVICE}),
    )
    assert all(result.ok for result in results)
