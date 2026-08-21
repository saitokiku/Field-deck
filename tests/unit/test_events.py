"""The event model and the bus's two kinds of consumer.

The asymmetry is the design.  **Sinks** are synchronous and never drop, because
the session recorder and the audit log are sinks and losing an ESTOP record
because a queue was full is not an acceptable failure mode.  **Subscriptions**
are bounded queues that *do* drop — oldest first — and count what they dropped,
because a slow HMI or a slow assistant must never be able to stall a CAN
reader.

The drop *count* matters as much as the dropping: a live view that quietly
skipped four hundred frames is a live view that lies about the bus.
"""

from __future__ import annotations

import asyncio

import pytest

from fielddeck.common.events import (
    AUDIT_EVENTS,
    Event,
    EventSeverity,
    EventType,
    new_event,
)
from fielddeck.common.models import ClientSource, PermissionLevel
from fielddeck.common.timebase import Timestamp
from fielddeck.daemon.events import EventBus


def an_event(
    event_type: EventType = EventType.MEASUREMENT,
    *,
    session_id: str | None = None,
    severity: EventSeverity = EventSeverity.INFO,
) -> Event:
    return new_event(event_type, session_id=session_id, severity=severity)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


class TestEventModel:
    def test_an_event_carries_both_clocks(self) -> None:
        event = new_event(EventType.ACTION_STARTED)
        assert event.monotonic_ns > 0
        assert event.utc_ns > 0
        assert event.utc_iso().endswith("Z")

    def test_a_supplied_timestamp_is_used_verbatim(self) -> None:
        """Correlated records share one instant rather than three near ones."""
        stamp = Timestamp(monotonic_ns=123, utc_ns=456)
        event = new_event(EventType.MEASUREMENT, timestamp=stamp)
        assert (event.monotonic_ns, event.utc_ns) == (123, 456)

    def test_sequence_numbers_increase_across_the_process(self) -> None:
        first = new_event(EventType.MEASUREMENT)
        second = new_event(EventType.MEASUREMENT)
        assert second.seq > first.seq
        assert first.event_id != second.event_id

    def test_defaults_are_the_safe_ones(self) -> None:
        event = new_event(EventType.DEVICE_DISCOVERED)
        assert event.source is ClientSource.SYSTEM
        assert event.severity is EventSeverity.INFO
        assert event.payload == {}

    def test_unknown_fields_are_refused(self) -> None:
        """Events cross the RPC boundary, so the model is strict there too."""
        with pytest.raises(ValueError):
            Event.model_validate(
                {
                    "event_id": "ev-1",
                    "seq": 1,
                    "type": str(EventType.MEASUREMENT),
                    "monotonic_ns": 1,
                    "utc_ns": 1,
                    "unexpected": True,
                }
            )

    def test_it_round_trips_through_json(self) -> None:
        event = new_event(
            EventType.ACTION_DENIED,
            source=ClientSource.CLAUDE,
            permission=PermissionLevel.POWER,
            device_id="sim:visa:sim-psu-0",
            action="psu.set",
            message="no active POWER grant",
            payload={"reason": "no active POWER grant"},
        )
        restored = Event.model_validate(event.model_dump(mode="json"))
        assert restored == event
        assert restored.permission is PermissionLevel.POWER


class TestAuditClassification:
    @pytest.mark.parametrize("event_type", sorted(AUDIT_EVENTS))
    def test_every_audit_type_is_flagged_for_the_audit_log(self, event_type: EventType) -> None:
        assert new_event(event_type).is_audit

    def test_the_authorization_story_is_all_audit(self) -> None:
        """Denials and revocations are as important as the successes."""
        for event_type in (
            EventType.ESTOP,
            EventType.ARM_GRANTED,
            EventType.ARM_REVOKED,
            EventType.ACTION_DENIED,
            EventType.LIMIT_REJECTED,
            EventType.SAFE_STATE_APPLIED,
        ):
            assert event_type in AUDIT_EVENTS

    def test_severity_promotes_an_ordinary_event_into_the_audit_log(self) -> None:
        assert new_event(EventType.MEASUREMENT, severity=EventSeverity.ERROR).is_audit
        assert new_event(EventType.MEASUREMENT, severity=EventSeverity.CRITICAL).is_audit
        assert not new_event(EventType.MEASUREMENT).is_audit


# ---------------------------------------------------------------------------
# Sinks: lossless
# ---------------------------------------------------------------------------


class TestSinks:
    def test_a_sink_sees_every_event_in_order(self, bus: EventBus) -> None:
        seen: list[Event] = []
        bus.add_sink(seen.append)

        published = [bus.publish(an_event()) for _ in range(2000)]

        assert seen == published, "a sink that drops is a session record with holes"

    def test_a_sink_is_called_synchronously(self, bus: EventBus) -> None:
        """No queue, no task: by the time publish returns it is recorded."""
        seen: list[Event] = []
        bus.add_sink(seen.append)
        event = bus.publish(an_event())
        assert seen == [event]

    def test_removing_a_sink_stops_delivery(self, bus: EventBus) -> None:
        seen: list[Event] = []
        remove = bus.add_sink(seen.append)
        bus.publish(an_event())
        remove()
        bus.publish(an_event())
        assert len(seen) == 1

    def test_removing_a_sink_twice_is_harmless(self, bus: EventBus) -> None:
        remove = bus.add_sink(lambda event: None)
        remove()
        remove()

    def test_one_broken_sink_does_not_stop_the_others(self, bus: EventBus) -> None:
        """A recorder fault must not take the event bus down with it."""
        seen: list[Event] = []

        def explode(event: Event) -> None:
            raise RuntimeError("the SD card went away")

        bus.add_sink(explode)
        bus.add_sink(seen.append)

        event = bus.publish(an_event())
        assert seen == [event]

    def test_publish_returns_the_event_it_published(self, bus: EventBus) -> None:
        event = an_event()
        assert bus.publish(event) is event


# ---------------------------------------------------------------------------
# Subscriptions: bounded and lossy, with a count
# ---------------------------------------------------------------------------


class TestSubscriptions:
    async def test_a_subscription_receives_what_it_asked_for(self, bus: EventBus) -> None:
        subscription = bus.subscribe(types=[EventType.ESTOP])
        bus.publish(an_event(EventType.MEASUREMENT))
        estop = bus.publish(an_event(EventType.ESTOP))

        received = await asyncio.wait_for(subscription.get(), timeout=1.0)
        assert received is estop
        subscription.close()

    async def test_a_session_filter_excludes_other_sessions(self, bus: EventBus) -> None:
        subscription = bus.subscribe(session_id="2026-08-20_bench")
        bus.publish(an_event(session_id="2026-08-19_other"))
        wanted = bus.publish(an_event(session_id="2026-08-20_bench"))

        assert await asyncio.wait_for(subscription.get(), timeout=1.0) is wanted
        subscription.close()

    async def test_a_full_subscription_drops_the_oldest_and_counts_it(self, bus: EventBus) -> None:
        """The newest state is what a live display needs; the count is the honesty."""
        subscription = bus.subscribe(maxsize=4)
        published = [bus.publish(an_event()) for _ in range(10)]

        assert subscription.dropped == 6
        drained = [await asyncio.wait_for(subscription.get(), timeout=1.0) for _ in range(4)]
        assert drained == published[-4:], "the queue kept the newest four, in order"
        subscription.close()

    def test_dropping_never_blocks_the_publisher(self, bus: EventBus) -> None:
        """A slow consumer must not be able to stall a CAN reader."""
        bus.subscribe(maxsize=1)
        for _ in range(10_000):
            bus.publish(an_event())
        assert bus.stats()["dropped"] == 9_999

    async def test_a_closed_subscription_stops_receiving_and_unregisters(
        self, bus: EventBus
    ) -> None:
        subscription = bus.subscribe(maxsize=4)
        subscription.close()
        bus.publish(an_event())

        assert bus.stats()["subscribers"] == 0
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(subscription.get(), timeout=0.05)

    def test_the_context_manager_closes_it(self, bus: EventBus) -> None:
        with bus.subscribe() as subscription:
            assert bus.stats()["subscribers"] == 1
            assert subscription.wants(an_event())
        assert bus.stats()["subscribers"] == 0

    async def test_iteration_yields_events_as_they_arrive(self, bus: EventBus) -> None:
        subscription = bus.subscribe()
        received: list[Event] = []

        async def consume() -> None:
            async for event in subscription:
                received.append(event)
                if len(received) == 3:
                    return

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        for _ in range(3):
            bus.publish(an_event())
        await asyncio.wait_for(task, timeout=1.0)

        assert len(received) == 3
        subscription.close()

    def test_a_filtered_subscription_drops_nothing_it_did_not_want(self, bus: EventBus) -> None:
        """Filtering happens before the queue, so noise cannot cause drops."""
        subscription = bus.subscribe(maxsize=2, types=[EventType.ESTOP])
        for _ in range(100):
            bus.publish(an_event(EventType.MEASUREMENT))
        assert subscription.dropped == 0

    def test_sinks_and_subscriptions_coexist(self, bus: EventBus) -> None:
        """The lossless consumer stays complete while the lossy one drops."""
        recorded: list[Event] = []
        bus.add_sink(recorded.append)
        subscription = bus.subscribe(maxsize=2)

        for _ in range(50):
            bus.publish(an_event())

        assert len(recorded) == 50
        assert subscription.dropped == 48


# ---------------------------------------------------------------------------
# History and statistics
# ---------------------------------------------------------------------------


class TestHistory:
    def test_recent_returns_the_newest_events_in_order(self, bus: EventBus) -> None:
        published = [bus.publish(an_event()) for _ in range(10)]
        recent = bus.recent(limit=3)
        assert recent == published[-3:]

    def test_recent_can_be_filtered_by_type(self, bus: EventBus) -> None:
        bus.publish(an_event(EventType.MEASUREMENT))
        estop = bus.publish(an_event(EventType.ESTOP))
        bus.publish(an_event(EventType.MEASUREMENT))
        assert bus.recent(types=[EventType.ESTOP]) == [estop]

    def test_history_is_bounded(self, bus: EventBus) -> None:
        """A daemon that runs for a week must not grow a list for a week."""
        small = EventBus(history=16)
        for _ in range(100):
            small.publish(an_event())
        assert small.stats()["history"] == 16

    def test_statistics_report_what_was_published_and_lost(self, bus: EventBus) -> None:
        bus.add_sink(lambda event: None)
        subscription = bus.subscribe(maxsize=1)
        for _ in range(5):
            bus.publish(an_event())

        stats = bus.stats()
        assert stats["published"] == 5
        assert stats["sinks"] == 1
        assert stats["subscribers"] == 1
        assert stats["dropped"] == subscription.dropped == 4
