"""Sensor platform for the Nikobus integration — connection status.

Why this platform exists
------------------------
On 21.08.2026 the host was rebooted and the USB enumeration order changed: the
FTDI adapter this installation hangs on had been ``/dev/ttyUSB1`` before the
reboot and was ``/dev/ttyUSB0`` afterwards. Home Assistant went on showing the
integration as "loaded" and all 26 cover entities as available, while every
drive command died in ``nkbcommand.process_commands()`` with
``NikobusSendError: Writer is not available for sending commands.``

That lasted two hours and nobody found out, because nothing in the integration
ever published the health of the transport. It was in the log, and a log is
not a signal — nobody watches one until they already suspect something.

This sensor is that signal: a state you can put on a dashboard, alert on, or
poll from outside. Together with the automatic reconnect in
``nkbconnect.reconnect_with_backoff`` it turns a silent two-hour failure into
a visible, self-healing one.

Relationship to upstream
------------------------
Taken as literally as the fork allows from upstream fdebrus/Nikobus-HA 3.x —
same class name, same translation key, same device class, same
``nikobus_connection_status`` unique_id — so that the port described in
PORT_INVENTORY.md inherits the entity instead of creating a second one beside
it. Upstream's two discovery-progress sensors are omitted: this fork's
discovery layer has no ``SIGNAL_DISCOVERY_STATE`` dispatcher and none of the
``discovery_*`` coordinator properties they read.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NikobusDataCoordinator
from .entity import hub_device_info

PARALLEL_UPDATES = 0

_CONNECTED = "connected"
# The port is open, the installation behind it is not answering. See
# coordinator.connection_status for why this is a state of its own.
_UNREACHABLE = "unreachable"
_RECONNECTING = "reconnecting"
_DISCONNECTED = "disconnected"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Nikobus connection sensor."""
    coordinator: NikobusDataCoordinator = entry.runtime_data
    async_add_entities([NikobusConnectionSensor(coordinator)])


class NikobusConnectionSensor(CoordinatorEntity[NikobusDataCoordinator], SensorEntity):
    """Sensor that exposes the live Nikobus connection status."""

    _attr_has_entity_name = True
    _attr_translation_key = "connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [_CONNECTED, _UNREACHABLE, _RECONNECTING, _DISCONNECTED]

    def __init__(self, coordinator: NikobusDataCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        # Fixed literal, not derived from the config entry: the integration is
        # single-instance, and a stable id is the whole point. It shares no
        # prefix with any existing Nikobus unique_id (nikobus_yaml_cover_*,
        # nikobus_yaml_group_cover_*, nikobus_cover_*, nikobus_switch_*,
        # nikobus_light_*, nikobus_button_sensor_*, nikobus_push_button_*,
        # nikobus_scene_*), so it cannot collide with the 26 entities this
        # installation depends on. It is also registered in
        # ``coordinator.get_known_entity_unique_ids`` — without that, the
        # orphan cleanup in ``__init__`` would delete it on every start.
        self._attr_unique_id = f"{DOMAIN}_connection_status"
        self._attr_device_info = hub_device_info()

    @property
    def available(self) -> bool:
        """Always available.

        A sensor whose job is to report an outage must not disappear during
        one. The CoordinatorEntity default would mark it unavailable as soon
        as a refresh fails, which is exactly when its value matters.
        """
        return True

    @property
    def native_value(self) -> str:
        """Return the current connection status."""
        return self.coordinator.connection_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes.

        The connection string is deliberately NOT exposed here: state
        attributes land in the recorder DB and in any state dump, and it would
        then be in every screenshot and every downloaded history of an
        installation whose owner never chose to publish it. It remains visible
        to the owner in the config entry, which is where it belongs.

        Upstream 3.x gives the same reason and adds that its ``diagnostics.py``
        redacts ``CONF_CONNECTION_STRING`` as sensitive. That second half does
        not hold in this fork: ``diagnostics.py`` here still emits
        ``connection_string`` verbatim (see async_get_config_entry_diagnostics).
        Not touched from here — it is a separate decision on a separate file —
        but recorded so the inconsistency is not mistaken for a policy.
        """
        last = self.coordinator.last_connected
        attributes: dict[str, Any] = {
            "last_connected": last.isoformat() if last else None,
            "reconnect_attempts": self.coordinator.reconnect_attempts,
        }

        # The verdict itself is in the state - `unreachable` when the port is
        # open but the installation stopped answering. These attributes are the
        # detail behind it: which clock reading, how many failed polls, when it
        # last answered. That last one doubles as a freshness signal, because a
        # verdict nobody recomputes any more is indistinguishable from a
        # healthy one (see nkbheartbeat._register_success).
        heartbeat = getattr(self.coordinator, "nikobus_heartbeat", None)
        if heartbeat is not None:
            last_ok = heartbeat.last_ok
            attributes.update(
                {
                    "heartbeat_enabled": heartbeat.enabled,
                    "heartbeat_alive": heartbeat.is_alive,
                    "heartbeat_failures": heartbeat.consecutive_failures,
                    "heartbeat_clock": heartbeat.last_clock,
                    "heartbeat_last_ok": last_ok.isoformat() if last_ok else None,
                }
            )
        return attributes
