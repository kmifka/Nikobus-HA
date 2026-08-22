"""Tests for the Nikobus connection sensor."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from conftest import FakeEntry

from custom_components.nikobus.const import CONF_CONNECTION_STRING, DOMAIN
from custom_components.nikobus.coordinator import NikobusDataCoordinator
from custom_components.nikobus.sensor import NikobusConnectionSensor


def _make_sensor(hass):
    coordinator = NikobusDataCoordinator(
        hass, FakeEntry({CONF_CONNECTION_STRING: "/dev/ttyUSB0"})
    )
    return coordinator, NikobusConnectionSensor(coordinator)


def test_sensor_matches_the_upstream_shape(hass):
    """Class attributes must stay identical to upstream 3.x.

    The point of copying them is that a later port inherits this entity rather
    than creating a second one next to it, so a drift here is a real defect.
    """
    _, sensor = _make_sensor(hass)

    assert sensor._attr_has_entity_name is True
    assert sensor._attr_translation_key == "connection"
    assert sensor._attr_entity_category == "diagnostic"
    assert sensor._attr_device_class == "enum"
    assert sensor._attr_options == ["connected", "reconnecting", "disconnected"]
    assert sensor._attr_unique_id == f"{DOMAIN}_connection_status"
    assert sensor._attr_unique_id == "nikobus_connection_status"


def test_sensor_attaches_to_the_bridge_device(hass):
    """The sensor belongs to the hub device the integration already registers."""
    _, sensor = _make_sensor(hass)

    assert sensor._attr_device_info["identifiers"] == {(DOMAIN, "nikobus_hub")}
    # Must match __init__._register_hub_device exactly or the device is renamed.
    assert sensor._attr_device_info["name"] == "Nikobus Bridge"
    assert sensor._attr_device_info["model"] == "PC-Link Bridge"


async def test_sensor_state_follows_the_transitions(hass):
    """connected -> disconnected -> reconnecting -> connected."""
    coordinator, sensor = _make_sensor(hass)
    connection = coordinator.nikobus_connection

    connection._is_connected = True
    assert sensor.native_value == "connected"

    connection._is_connected = False
    assert sensor.native_value == "disconnected"

    async def _never() -> None:
        await asyncio.Event().wait()

    coordinator._reconnect_task = asyncio.create_task(_never())
    await asyncio.sleep(0)
    assert sensor.native_value == "reconnecting"

    connection._is_connected = True
    assert sensor.native_value == "connected"

    coordinator._reconnect_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await coordinator._reconnect_task


def test_every_state_is_a_declared_option(hass):
    """Home Assistant rejects an ENUM state that is not in _attr_options."""
    coordinator, sensor = _make_sensor(hass)
    for value in ("connected", "disconnected"):
        coordinator.nikobus_connection._is_connected = value == "connected"
        assert sensor.native_value in sensor._attr_options


def test_sensor_stays_available_during_an_outage(hass):
    """A sensor that reports outages must not vanish during one."""
    coordinator, sensor = _make_sensor(hass)
    coordinator.nikobus_connection._is_connected = False
    assert sensor.available is True
    assert sensor.native_value == "disconnected"


def test_attributes_carry_the_diagnostics_but_not_the_connection_string(hass):
    """last_connected and reconnect_attempts; never the device path."""
    coordinator, sensor = _make_sensor(hass)

    attributes = sensor.extra_state_attributes
    assert attributes["last_connected"] is None
    assert attributes["reconnect_attempts"] == 0
    # Added 22.08.2026 alongside the heartbeat: the transport state and the
    # liveness of the installation behind it are two different questions.
    assert attributes["heartbeat_enabled"] is True
    assert attributes["heartbeat_alive"] is True
    assert attributes["heartbeat_failures"] == 0
    assert attributes["heartbeat_clock"] is None
    assert attributes["heartbeat_last_ok"] is None

    stamp = datetime(2026, 8, 21, 18, 30, tzinfo=timezone.utc)
    coordinator._last_connected = stamp
    coordinator._reconnect_attempts = 12
    attributes = sensor.extra_state_attributes

    assert attributes["last_connected"] == stamp.isoformat()
    assert attributes["reconnect_attempts"] == 12
    assert "/dev/ttyUSB0" not in str(attributes)
    assert not any("connection_string" in key for key in attributes)
