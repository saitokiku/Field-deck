"""A device that vanishes must not leave an immortal lease behind.

`discover()` retires a device that is no longer present, but it did not release
the output lease held on it. The lease stayed in the safety snapshot and on the
HMI banner; no safe state could ever satisfy it, because
`apply_safe_state(device_ids=[gone])` found no driver and returned an empty
list without a word; and if the device reappeared with the same id, the stale
lease was still there.

The interesting part is not the bookkeeping. A device disappearing *while
holding an output lease* means something that was energised is no longer under
FieldDeck's control -- an unplugged supply, a USB reset, a crashed adapter. The
rail may well still be live and FieldDeck can no longer turn it off. That is
worth shouting about, not tidying away.
"""

from __future__ import annotations

from fielddeck.common.events import EventSeverity, EventType
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon

SIM_PSU = "role:psu"


async def _energise(client: InstrumentClient) -> str:
    await client.call("safety.arm", {"permission": "power", "ttl_s": 60})
    result = await client.execute(
        "psu.output", {"device": SIM_PSU, "enabled": True, "lease_ttl_s": 60}
    )
    return str(result.result["lease"]["lease_id"])


async def test_losing_a_device_surrenders_its_lease(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    lease_id = await _energise(client)
    device_id = daemon.registry.resolve(SIM_PSU).device_id
    assert [lease.lease_id for lease in daemon.safety.leases.active()] == [lease_id]

    # The supply is unplugged: the next inventory no longer finds it.
    daemon.registry.remove(device_id)
    daemon._surrender_leases_for_lost_device(device_id)

    assert daemon.safety.leases.active() == [], (
        "the lease outlived the device; nothing can ever satisfy it now"
    )


async def test_losing_a_device_holding_a_lease_is_reported_as_critical(
    client: InstrumentClient, daemon: InstrumentDaemon
) -> None:
    await _energise(client)
    device_id = daemon.registry.resolve(SIM_PSU).device_id

    daemon.registry.remove(device_id)
    daemon._surrender_leases_for_lost_device(device_id)

    recent = await client.call("events.recent", {"limit": 200})
    released = [
        event
        for event in recent["events"]
        if event["type"] == str(EventType.LEASE_RELEASED) and event.get("device_id") == device_id
    ]
    assert released, "losing a device holding a lease was not recorded"

    event = released[-1]
    assert event["severity"] == str(EventSeverity.CRITICAL)
    assert "disappeared while holding an output lease" in event["message"]
    assert "check the hardware" in event["message"]


async def test_safing_an_unregistered_device_says_so_rather_than_nothing(
    daemon: InstrumentDaemon,
) -> None:
    """An empty list read as success. It has to read as "I could not"."""
    results = await daemon.dispatcher.apply_safe_state(
        reason="test", device_ids=["definitely:not:a:device"]
    )

    assert len(results) == 1
    assert results[0]["safe"] is False
    assert results[0]["applied"] is False
    assert "not registered" in results[0]["error"]


async def test_safing_every_device_on_an_empty_registry_is_still_quiet(
    daemon: InstrumentDaemon,
) -> None:
    """The no-devices-named case must not start inventing failures."""
    for driver in list(daemon.registry.drivers):
        daemon.registry.remove(driver.device_id)

    assert await daemon.dispatcher.apply_safe_state(reason="test") == []
