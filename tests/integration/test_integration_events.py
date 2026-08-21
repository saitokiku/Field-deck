"""Event ordering, and what happens when a consumer cannot keep up.

The event bus makes two different promises to two different kinds of
consumer, and both are tested here because getting either wrong is a data
integrity bug rather than a performance one:

*Sinks never drop.*  The session recorder is a sink, so every action that ran
during a flood is still in the timeline afterwards.

*Subscriptions do drop, count it, and never block the producer.*  A slow HMI,
a paused ``fdctl events --follow``, or an assistant that stopped reading must
cost that consumer some events and must not cost the capture a single byte.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Callable

import pytest

from fielddeck.common.events import Event
from fielddeck.common.models import ClientSource
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

from .conftest import SIM_PSU, SIM_SERIAL, wait_until

#: Enough concurrent work to interleave events from several tasks, small
#: enough to stay well inside the per-connection in-flight bound.
LOAD_ACTIONS = 60


async def test_event_ordering_is_preserved_under_load(
    client: InstrumentClient, connect: Callable[..., InstrumentClient]
) -> None:
    """Sequence numbers arrive in order, whatever else the daemon is doing."""
    received: list[Event] = []
    stream = client.subscribe(maxsize=4096)

    async def pump() -> None:
        async for event in stream:
            received.append(event)

    pump_task = asyncio.create_task(pump())
    try:
        async with connect(ClientSource.FDCTL) as worker:
            # Prove the subscription is live before the load starts, rather
            # than sleeping and hoping it opened in time.
            await worker.execute("system.status")
            await wait_until(lambda: bool(received), what="the first streamed event")

            await asyncio.gather(
                *(
                    worker.execute("device.status", {"device": SIM_PSU})
                    for _ in range(LOAD_ACTIONS)
                ),
                client.execute(
                    "serial.monitor",
                    {"device": SIM_SERIAL, "duration_s": 0.5},
                    timeout_s=30.0,
                ),
            )
            await wait_until(
                lambda: len(received) > LOAD_ACTIONS * 2,
                what="the streamed events to catch up with the load",
            )
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task

    sequences = [event.seq for event in received]
    assert sequences == sorted(sequences), "events arrived out of order"
    assert len(set(sequences)) == len(sequences), "an event was delivered twice"
    stamps = [event.monotonic_ns for event in received]
    assert stamps == sorted(stamps), "the monotonic axis went backwards"


@pytest.mark.slow
async def test_a_slow_subscriber_drops_and_reports_without_stalling_capture(
    daemon: InstrumentDaemon,
    client: InstrumentClient,
    connect: Callable[..., InstrumentClient],
    session: str,
) -> None:
    """Backpressure: the slow consumer loses events, the capture loses nothing."""
    slow = daemon.bus.subscribe(maxsize=4)
    fast_seen: list[Event] = []
    fast = daemon.bus.subscribe(maxsize=4096)

    async def drain_fast() -> None:
        async for event in fast:
            fast_seen.append(event)

    drainer = asyncio.create_task(drain_fast())
    try:
        async with connect(ClientSource.FDCTL) as flooder:
            capture = asyncio.create_task(
                client.execute(
                    "serial.capture",
                    {"device": SIM_SERIAL, "duration_s": 1.5, "label": "backpressure"},
                    timeout_s=60.0,
                )
            )
            # Flood the bus while the capture is running.  The slow subscriber
            # is never read from at all, which is exactly the failure mode.
            while not capture.done():
                await asyncio.gather(
                    *(
                        flooder.execute("device.status", {"device": SIM_PSU})
                        for _ in range(LOAD_ACTIONS // 4)
                    )
                )
            result = await capture

        # Asserted while both subscriptions are still attached: the bus-wide
        # counter is a live gauge over the current subscribers, so closing
        # them first would take their drop counts with them.
        # Snapshotted, because asking the daemon anything publishes more
        # events and the slow subscriber goes on dropping them while we look.
        dropped = slow.dropped
        assert dropped > 0
        assert fast_seen, "the consumer that kept up must still have received events"
        assert daemon.bus.stats()["dropped"] >= dropped
        status = (await client.execute("system.status")).result
        assert status["events"]["dropped"] >= dropped
    finally:
        drainer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drainer
        slow.close()
        fast.close()

    # The capture is untouched: full duration, bytes on disk, hash intact.
    assert result.result["cancelled"] is False
    assert result.result["duration_s"] >= 1.4
    artifact = result.result["artifact"]
    raw = daemon.sessions.sessions_dir / session / artifact["relative_path"]
    assert raw.stat().st_size == artifact["size_bytes"] > 0
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == artifact["sha256"]


@pytest.mark.slow
async def test_the_recorder_sink_loses_nothing_during_a_flood(
    daemon: InstrumentDaemon,
    client: InstrumentClient,
    connect: Callable[..., InstrumentClient],
    session: str,
) -> None:
    """Every action run under load is still in the timeline afterwards."""
    starved = daemon.bus.subscribe(maxsize=1)
    try:
        async with connect(ClientSource.FDCTL) as flooder:
            await asyncio.gather(
                *(
                    flooder.execute("device.status", {"device": SIM_PSU})
                    for _ in range(LOAD_ACTIONS)
                )
            )
    finally:
        starved.close()

    assert starved.dropped > 0, "the test did not actually create backpressure"

    rows = (
        await client.execute(
            "session.events",
            {"session_id": session, "types": ["ACTION_COMPLETED"], "limit": 10000},
        )
    ).result["events"]
    completed = [row for row in rows if row["action"] == "device.status"]
    assert len(completed) == LOAD_ACTIONS

    sequences = [row["seq"] for row in completed]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == LOAD_ACTIONS
