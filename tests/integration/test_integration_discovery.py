"""Discovery, device identity and the connect/disconnect lifecycle.

Discovery is Stage 1 of the auto-detect engine and the one stage that runs
without anybody asking, so these tests care about two properties above all:
that it *enumerates* (nothing here may transmit to a DUT), and that a device
which goes away is retired loudly rather than left in the inventory as a
ghost an operator will later try to drive.
"""

from __future__ import annotations

import pytest

from fielddeck.common.errors import DeviceNotFound
from fielddeck.common.events import EventSeverity, EventType
from fielddeck.common.models import ConnectionState, DeviceRole, TransportKind
from fielddeck.daemon.client import InstrumentClient
from fielddeck.daemon.service import InstrumentDaemon
from fielddeck.sim import build_simulated_devices

from .conftest import SIM_CAN, SIM_DMM, SIM_PSU, SIM_SERIAL, EventLog


async def test_discovery_finds_the_simulated_bench(client: InstrumentClient) -> None:
    result = await client.execute("device.list")
    devices = {device["id"]: device for device in result.result["devices"]}

    assert {SIM_CAN, SIM_SERIAL, SIM_PSU, SIM_DMM} <= set(devices)
    for device in devices.values():
        # A client must never have to guess whether it is looking at hardware.
        assert device["simulated"] is True
        assert device["state"] == str(ConnectionState.READY)

    assert devices[SIM_CAN]["kind"] == str(TransportKind.CAN)
    assert devices[SIM_SERIAL]["kind"] == str(TransportKind.SERIAL)
    assert str(DeviceRole.PSU) in devices[SIM_PSU]["roles"]
    assert str(DeviceRole.DMM) in devices[SIM_DMM]["roles"]


async def test_boot_emitted_discovery_events_and_a_safe_state(
    daemon: InstrumentDaemon, client: InstrumentClient
) -> None:
    """Boot state is SAFE, and the inventory is on the timeline, not implied."""
    recent = daemon.bus.recent(limit=200)
    discovered = {event.device_id for event in recent if event.type is EventType.DEVICE_DISCOVERED}
    safed = {event.device_id for event in recent if event.type is EventType.SAFE_STATE_APPLIED}
    assert {SIM_CAN, SIM_SERIAL, SIM_PSU, SIM_DMM} <= discovered
    assert {SIM_CAN, SIM_SERIAL, SIM_PSU} <= safed

    status = (await client.execute("system.status")).result
    assert status["safety"]["state"] == "SAFE"
    assert status["safety"]["armed"] == []
    assert status["simulated"] is True


async def test_rediscovery_is_idempotent(client: InstrumentClient) -> None:
    """Re-running the inventory must not churn identity.

    A device id that changes between two scans of the same unchanged bench
    would break every alias, every scoped grant and every session record that
    referred to it.
    """
    first = (await client.execute("system.discover")).result
    assert first["added"] == []
    assert first["removed"] == []

    second = (await client.execute("system.discover")).result
    assert second["added"] == []
    assert {device["id"] for device in second["devices"]} >= {SIM_CAN, SIM_PSU}


async def test_vanished_device_is_retired_and_announced(
    daemon: InstrumentDaemon,
    client: InstrumentClient,
    events: EventLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unplugging the CAN adapter retires it, disconnects it and says so."""
    doomed = daemon.registry.get(SIM_CAN)
    assert doomed is not None

    async def scan_without_can(_config: object) -> list[object]:
        return [driver for driver in build_simulated_devices() if driver.device_id != SIM_CAN]  # type: ignore[return-value]

    monkeypatch.setattr("fielddeck.discovery.scan", scan_without_can)

    result = (await client.execute("system.discover")).result
    assert result["removed"] == [SIM_CAN]
    assert SIM_CAN not in {device["id"] for device in result["devices"]}

    lost = await events.wait_for(
        EventType.DEVICE_LOST, match=lambda event: event.device_id == SIM_CAN
    )
    # A device that vanished mid-bench is not routine news.
    assert lost.severity is EventSeverity.WARNING
    assert SIM_CAN in (lost.message or "")

    # Retired, disconnected, and no longer addressable.
    assert daemon.registry.get(SIM_CAN) is None
    assert doomed.descriptor.state is ConnectionState.DISCOVERED
    with pytest.raises(DeviceNotFound):
        await client.execute("can.status", {"device": SIM_CAN})


async def test_device_connect_and_disconnect_lifecycle(
    daemon: InstrumentDaemon, client: InstrumentClient
) -> None:
    """A driver's connection state is reported, not inferred by the client."""
    driver = daemon.registry.get(SIM_SERIAL)
    assert driver is not None
    assert await driver.probe() is True

    await driver.disconnect()
    payload = (await client.execute("device.status", {"device": SIM_SERIAL})).result
    assert payload["descriptor"]["state"] == str(ConnectionState.DISCOVERED)
    assert payload["busy_with"] is None

    await driver.connect()
    payload = (await client.execute("device.status", {"device": SIM_SERIAL})).result
    assert payload["descriptor"]["state"] == str(ConnectionState.READY)
    # device.status is PASSIVE and must stay readable without any grant.
    assert payload["status"]["baudrate"] == 115200


async def test_role_and_alias_resolution_refuses_to_guess(
    daemon: InstrumentDaemon, client: InstrumentClient
) -> None:
    """``role:psu`` resolves; an ambiguous role is an error, not a coin flip."""
    payload = (await client.execute("device.status", {"device": "role:psu"})).result
    assert payload["descriptor"]["id"] == SIM_PSU

    with pytest.raises(DeviceNotFound) as ambiguous:
        # Both simulated bus devices carry DeviceRole.BUS.
        await client.execute("device.status", {"device": "role:bus"})
    assert "name one explicitly" in ambiguous.value.message
    assert SIM_CAN in ambiguous.value.details["candidates"]

    with pytest.raises(DeviceNotFound):
        await client.execute("device.status", {"device": "no-such-device"})
