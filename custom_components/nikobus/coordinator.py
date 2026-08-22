"""Coordinator for Nikobus integration."""

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError

from .nkbAPI import NikobusAPI
from .nkbconnect import NikobusConnect
from .nkbconfig import NikobusConfig
from .nkblistener import NikobusEventListener
from .nkbcommand import NikobusCommandHandler
from .nkbactuator import NikobusActuator
from .nkbheartbeat import NikobusHeartbeat
from .discovery import NikobusDiscovery

from .const import (
    CONF_CONNECTION_STRING,
    CONF_REFRESH_INTERVAL,
    CONF_HAS_FEEDBACK_MODULE,
    CONF_HEARTBEAT_ADDRESS,
    CONF_PRIOR_GEN3,
    CONF_COVERS,
    CONF_GROUP_COVERS,
    DEFAULT_HEARTBEAT_ADDRESS,
    DOMAIN,
    RECONNECT_DELAY_INITIAL,
    RECONNECT_DELAY_MAX,
)
from .exceptions import NikobusConnectionError, NikobusDataError

_LOGGER = logging.getLogger(__name__)
_MODULE_TYPES = ("switch_module", "dimmer_module", "roller_module")


class NikobusDataCoordinator(DataUpdateCoordinator):
    """Coordinator for managing asynchronous updates and connections to the Nikobus system."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator with Home Assistant and configuration entry."""
        self.hass = hass
        self.api = None

        self.config_entry = config_entry
        self.connection_string = config_entry.data.get(CONF_CONNECTION_STRING)
        self._refresh_interval = config_entry.data.get(CONF_REFRESH_INTERVAL, 120)
        self._has_feedback_module = config_entry.data.get(
            CONF_HAS_FEEDBACK_MODULE, False
        )
        self._prior_gen3 = config_entry.data.get(CONF_PRIOR_GEN3, False)
        self._update_interval = self._get_update_interval()

        super().__init__(
            self.hass,
            _LOGGER,
            name="Nikobus",
            update_method=self._async_update_data,
            update_interval=self._update_interval,
        )

        self.nikobus_connection = NikobusConnect(self.connection_string)
        self.nikobus_config = NikobusConfig(self.hass)

        self.dict_module_data = {}
        self.dict_button_data = {}
        self.dict_scene_data = {}
        self.nikobus_module_states = {}

        self.nikobus_actuator = None
        self.nikobus_listener = None
        self.nikobus_command = None
        self.nikobus_discovery = None
        self._discovery_running = False
        self._discovery_module = None
        self.discovery_module_address = None
        self._reload_task = None

        # --- connection supervision -------------------------------------
        # See const.RECONNECT_DELAY_* for the incident these exist for.
        # Field names and semantics are upstream 3.x's, so the port in
        # PORT_INVENTORY.md finds what it expects.
        self._stopping: bool = False
        self._reconnect_task: asyncio.Task | None = None
        self._last_connected: datetime | None = None
        self._reconnect_attempts: int = 0

        # --- liveness supervision ---------------------------------------
        # An open serial port is not a working installation. See
        # nkbheartbeat.py for the 22.08.2026 measurements this is built on.
        #
        # The address is configuration, not a literal: 9E62 is what answers
        # here, other installations have a different one. An explicitly empty
        # value switches the heartbeat off rather than letting it guess, which
        # would take all 26 covers down on a wrong address.
        self._heartbeat_address = str(
            config_entry.data.get(CONF_HEARTBEAT_ADDRESS, DEFAULT_HEARTBEAT_ADDRESS)
            or ""
        ).strip()
        self.nikobus_heartbeat = NikobusHeartbeat(self, self._heartbeat_address)

    @property
    def connection_status(self) -> str:
        """Return 'connected', 'unreachable', 'reconnecting', or 'disconnected'.

        Read by NikobusConnectionSensor. Derived rather than stored, so it can
        never disagree with what it describes - which is precisely what went
        wrong on 21.08.2026, when Home Assistant's own view of the integration
        ("loaded", every entity available) had nothing to do with the state of
        the serial port.

        The four values answer one question - "would a command get through?" -
        and name the reason it would not:

            connected     the port is open and the installation answers.
            unreachable   the port is open, the installation does not answer.
            reconnecting  the port is shut, a reconnect is running.
            disconnected  the port is shut.

        `unreachable` was added on 22.08.2026 after pulling the power from the
        installation while the USB adapter stayed in the host. Everything
        behaved correctly - 25 covers went unavailable within one poll and the
        alarm reached Home Assistant - and this sensor still read `connected`,
        because the serial port really was open. True, and useless: the one
        entity whose name promises to say whether Nikobus is reachable was the
        only place still claiming it was.

        The transport on its own remains available via `nikobus_connection.
        is_connected` for the places that genuinely mean the port, and the
        heartbeat detail stays in the sensor's attributes.
        """
        if self.nikobus_connection.is_connected:
            heartbeat = self.nikobus_heartbeat
            if heartbeat is not None and not heartbeat.is_alive:
                return "unreachable"
            return "connected"
        if self._reconnect_task and not self._reconnect_task.done():
            return "reconnecting"
        return "disconnected"

    @property
    def system_available(self) -> bool:
        """Whether commands sent right now would actually reach the shutters.

        This is what the YAML cover entities publish as ``available``. Two
        independent things have to hold, and each of them failed in a real
        incident:

        1. The transport is open. On 21.08.2026 it was not - the FTDI adapter
           had moved from /dev/ttyUSB1 to /dev/ttyUSB0 - and every drive command
           died in the writer for two hours while all 26 covers went on showing
           as available and operable.
        2. The installation behind it is still answering. That is the heartbeat,
           and it is a separate question: the serial port can be perfectly open
           while the PC-Link behind it has stopped doing anything.

        The two are treated differently on purpose. A closed transport is a
        certainty, so it counts immediately. A missing heartbeat answer is a
        single sample on a shared bus, so it only counts after
        HEARTBEAT_FAILURE_THRESHOLD of them in a row.

        Showing a cover as available when neither holds is the same kind of lie
        as a travel calculator reporting a position it computed for a shutter
        that never moved (see cover._require_connection): it invites the user to
        press a button that does nothing. Reporting unavailable makes Apple Home
        say "not responding" instead of pretending the blind is operable.
        """
        if not self.nikobus_connection.is_connected:
            return False
        heartbeat = self.nikobus_heartbeat
        if heartbeat is None:
            return True
        return heartbeat.is_alive

    @property
    def last_connected(self) -> datetime | None:
        """Timestamp of the last successful connect (UTC), or ``None``.

        Surfaced by the connection sensor's attributes.
        """
        return self._last_connected

    @property
    def reconnect_attempts(self) -> int:
        """Consecutive reconnect attempts since the last successful connect.

        Surfaced by the connection sensor's attributes. A value that keeps
        climbing is the signal that the cause is not going to fix itself.
        """
        return self._reconnect_attempts

    def _get_update_interval(self) -> timedelta | None:
        """Compute the update interval based on configuration."""
        if self._has_feedback_module or self._prior_gen3:
            return None
        return timedelta(seconds=self._refresh_interval)

    @property
    def discovery_running(self) -> bool:
        return self._discovery_running

    @property
    def discovery_module(self):
        return self._discovery_module

    @discovery_running.setter
    def discovery_running(self, value: bool) -> None:
        self._discovery_running = value

    @discovery_module.setter
    def discovery_module(self, value) -> None:
        self._discovery_module = value

    async def connect(self) -> None:
        """Connect to the Nikobus system."""
        try:
            await self.nikobus_connection.connect()
        except NikobusConnectionError as e:
            _LOGGER.error("Failed to connect to Nikobus: %s", e)
            raise
        else:
            try:
                # Load JSON configuration for modules, buttons, and scenes
                self.dict_module_data = await self.nikobus_config.load_json_data(
                    "nikobus_module_config.json", "module"
                )
                self.dict_button_data = await self.nikobus_config.load_json_data(
                    "nikobus_button_config.json", "button"
                ) or {"nikobus_button": {}}
                self.dict_scene_data = await self.nikobus_config.load_json_data(
                    "nikobus_scene_config.json", "scene"
                )

                # Initialize module state tracking dynamically based on channels
                for modules in self.dict_module_data.values():
                    for address, module_info in modules.items():
                        channels = module_info.get("channels", [])
                        self.nikobus_module_states[address] = bytearray(len(channels))

                # Instantiate main Nikobus components
                self.nikobus_actuator = NikobusActuator(
                    self.hass, self, self.dict_button_data, self.dict_module_data
                )
                self.nikobus_discovery = NikobusDiscovery(self.hass, self)
                self.nikobus_discovery.on_discovery_finished = (
                    self._handle_discovery_finished
                )
                self.nikobus_listener = NikobusEventListener(
                    self.hass,
                    self.config_entry,
                    self,
                    self.nikobus_actuator,
                    self.nikobus_connection,
                    self.nikobus_discovery,
                    self.process_feedback_data,
                )
                self.nikobus_command = NikobusCommandHandler(
                    self.hass,
                    self,
                    self.nikobus_connection,
                    self.nikobus_listener,
                    self.nikobus_module_states,
                )

                # Expose API to Home Assistant
                self.api = NikobusAPI(self.hass, self)

                # The listener is the detector of connection loss: a send()
                # failure closes the streams, so the next read() fails. Wiring
                # the hook here (and again in _reconnect_loop) is upstream 3.x's
                # arrangement.
                self.nikobus_listener.on_connection_lost = self._handle_connection_lost

                # Start event listener and command handler
                await self.nikobus_command.start()
                await self.nikobus_listener.start()
                # Started after the command handler on purpose: the heartbeat
                # queues its ping through it and has nothing to talk to before.
                await self.nikobus_heartbeat.start()
                self._last_connected = datetime.now(timezone.utc)

                # Perform an initial data refresh
                await self.async_refresh()
            except HomeAssistantError as e:
                _LOGGER.exception("Failed to initialize Nikobus components: %s", e)
                raise

    async def discover_devices(self, module_address) -> None:
        """Discover available module / button."""
        module_address = module_address.upper() if isinstance(module_address, str) else ""
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "message": "Nikobus discovery is in progress. Please wait...",
                "title": "Nikobus Discovery",
                "notification_id": "nikobus_discovery",
            },
            blocking=True,
        )

        self._discovery_running = True
        _LOGGER.debug("Starting device discovery from Nikobus")
        try:
            if module_address == "ALL":
                self._discovery_module = False
                await self.nikobus_discovery.query_module_inventory(module_address)
            elif module_address:
                self._discovery_module = True
                self.discovery_module_address = module_address
                await self.nikobus_discovery.query_module_inventory(module_address)
            else:
                self._discovery_module = False
                await self.nikobus_command.queue_command("#A")
        except Exception as e:
            _LOGGER.exception("Error during discovery: %s", e)
            raise
        finally:
            await self.hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": "nikobus_discovery"},
                blocking=True,
            )

    async def _async_update_data(self):
        """Fetch the latest data from the Nikobus system.

        Total-blackout auto-recovery (upstream issue #337): if every poll in a
        single cycle fails - every output module runs into its timeout - the
        bus is silent, and the same reconnect path the listener uses is
        triggered.

        This is a *different* failure from a dead transport, and the two can
        fail independently: the serial port can be perfectly open while the
        PC-Link behind it has gone to sleep (it does, after the 120 s gap
        between polls), and the bus can be fine while the FTDI node vanished.
        On 21.08.2026 it was the transport; this branch covers the other half.

        Until now the fork could not see it at all: ``_refresh_module_type``
        swallowed every per-module failure, logged an ERROR, kept looping, and
        ``_async_update_data`` returned True either way. A cycle in which
        nothing answered was indistinguishable from a healthy one.
        """
        try:
            if not self._discovery_running:
                _LOGGER.debug("Refreshing Nikobus data")
                return await self._refresh_nikobus_data()
        except NikobusDataError as e:
            _LOGGER.error("Error fetching Nikobus data: %s", e)
            raise UpdateFailed(f"Error fetching Nikobus data: {e}")

    async def _refresh_nikobus_data(self) -> bool:
        """Refresh data from all Nikobus modules, watching for a silent bus."""
        polled = 0
        failures = 0
        try:
            for module_type in _MODULE_TYPES:
                if module_type in self.dict_module_data:
                    polled_n, failed_n = await self._refresh_module_type(
                        self.dict_module_data[module_type]
                    )
                    polled += polled_n
                    failures += failed_n
            return True
        finally:
            if polled > 0 and failures == polled and not self._stopping:
                _LOGGER.warning(
                    "Nikobus poll cycle: %d/%d commands timed out - bus silent. "
                    "Triggering reconnect.",
                    failures,
                    polled,
                )
                # Background task - this must not block the coordinator's
                # refresh-cycle slot. ``_handle_connection_lost`` is idempotent
                # (a no-op while a reconnect is already running), so a blackout
                # lasting several cycles cannot start a retry storm.
                self.hass.async_create_background_task(
                    self._handle_connection_lost(),
                    name="nikobus_blackout_recovery",
                )

    async def _refresh_module_type(self, modules_dict) -> tuple[int, int]:
        """Refresh data for a specific type of module.

        Returns ``(polled, failed)``. Per-module failures are still swallowed
        so one bad module does not stop the others from refreshing in the same
        cycle; only the aggregate counts leave this method, and they are what
        ``_refresh_nikobus_data`` uses to tell "one module is deaf" from "the
        whole bus is deaf".
        """
        polled = 0
        failed = 0
        for address, module_data in modules_dict.items():
            _LOGGER.debug("Refreshing data for module address: %s", address)
            channels = module_data.get("channels", [])
            expected_channels = len(channels)
            groups_to_query = (1,) if expected_channels <= 6 else (1, 2)
            group_states = []
            for group in groups_to_query:
                polled += 1
                try:
                    group_state = (
                        await self.nikobus_command.get_output_state(address, group)
                        or ""
                    )
                    _LOGGER.debug(
                        "State for group %s: %s (Address: %s)",
                        group,
                        group_state,
                        address,
                    )
                    if not group_state:
                        failed += 1
                    group_states.append(group_state)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    failed += 1
                    # DEBUG, not ERROR (upstream made the same change): during
                    # a bus-silent window this fires once per module per cycle
                    # and floods the System Log, while the aggregate WARNING in
                    # _refresh_nikobus_data already carries the actionable
                    # signal. Individual failures stay available in a debug log.
                    _LOGGER.debug(
                        "Error retrieving state for address %s, group %s: %s",
                        address,
                        group,
                        e,
                    )
            state_hex = "".join(group_states)
            expected_hex_length = expected_channels * 2
            if len(state_hex) < expected_hex_length:
                state_hex = state_hex.ljust(expected_hex_length, "0")
                _LOGGER.debug(
                    "Padded state_hex for module %s to: %s", address, state_hex
                )
            try:
                self.nikobus_module_states[address] = bytearray.fromhex(state_hex)
                _LOGGER.debug(
                    "Updated module state for %s: %s",
                    address,
                    self.nikobus_module_states[address].hex(),
                )
            except ValueError:
                _LOGGER.error(
                    "Invalid hex state received for %s, setting default state.",
                    address,
                )
                self.nikobus_module_states[address] = bytearray(expected_channels)
        return polled, failed

    async def process_feedback_data(self, module_group, data) -> None:
        """Process feedback data from Nikobus."""
        try:
            module_address_raw = data[3:7]
            module_address = module_address_raw[2:] + module_address_raw[:2]
            module_type = self.get_module_type(module_address)
            module_state_raw = data[9:21]

            _LOGGER.debug(
                "Processing feedback data: module_type=%s, module_address=%s, "
                "group=%s, module_state=%s",
                module_type,
                module_address,
                module_group,
                module_state_raw,
            )

            if module_address not in self.nikobus_module_states:
                self.nikobus_module_states[module_address] = bytearray(12)

            if module_group == 1:
                self.nikobus_module_states[module_address][:6] = bytearray.fromhex(
                    module_state_raw
                )
            elif module_group == 2:
                self.nikobus_module_states[module_address][6:] = bytearray.fromhex(
                    module_state_raw
                )
            else:
                raise ValueError("Invalid module group: %s" % module_group)

            await self.async_event_handler(
                "nikobus_refreshed",
                {
                    "impacted_module_address": module_address,
                    "impacted_module_group": module_group,
                },
            )

        except Exception as e:
            _LOGGER.exception("Error processing feedback data: %s", e)

    def get_bytearray_state(self, address: str, channel: int) -> int:
        """Get the state of a specific channel, ensuring defaults if missing."""
        num_channels = self.get_module_channel_count(address)
        state = self.nikobus_module_states.get(address, bytearray(num_channels))
        if channel - 1 >= len(state) or channel - 1 < 0:
            _LOGGER.error(
                "Channel index %d out of range for module %s (max channels: %d)",
                channel,
                address,
                len(state),
            )
            return 0
        return state[channel - 1]

    def get_bytearray_group_state(self, address: str, group: int) -> bytearray:
        """Get the state of a specific group."""
        if address in self.nikobus_module_states:
            return (
                self.nikobus_module_states[address][:6]
                if int(group) == 1
                else self.nikobus_module_states[address][6:12]
            )
        _LOGGER.error(
            "Module address %s not found, returning empty bytearray.", address
        )
        return bytearray(6)

    def set_bytearray_state(self, address: str, channel: int, value: int) -> None:
        """Set the state of a specific channel safely."""
        if address in self.nikobus_module_states:
            self.nikobus_module_states[address][channel - 1] = value
        else:
            _LOGGER.warning("Module %s not found, creating new state array.", address)
            self.nikobus_module_states[address] = bytearray(12)
            self.nikobus_module_states[address][channel - 1] = value

    def set_bytearray_group_state(self, address: str, group: int, value: str) -> None:
        """Update the state of a specific group safely using the actual channel count."""
        group = int(group)
        num_channels = self.get_module_channel_count(address)
        if address not in self.nikobus_module_states:
            _LOGGER.warning("Module %s not found, creating new state array.", address)
            self.nikobus_module_states[address] = bytearray(num_channels)

        state = self.nikobus_module_states[address]
        byte_value = bytearray.fromhex(value)

        if group == 1:
            end_index = min(6, num_channels)
            state[0:end_index] = byte_value[:end_index]
        elif group == 2:
            if num_channels > 6:
                state[6:num_channels] = byte_value[: num_channels - 6]
            else:
                _LOGGER.error(
                    "Module %s has only %d channels; skipping group 2 update.",
                    address,
                    num_channels,
                )
                return
        _LOGGER.debug("Updated state for %s: %s", address, state.hex())

    async def async_event_handler(self, event, data) -> None:
        """Handle events with improved logging."""
        _LOGGER.debug("Handling event: %s with data: %s", event, data)
        if event == "ha_button_pressed":
            await self._handle_ha_button_pressed(data)
        elif event == "nikobus_refreshed":
            await self._handle_nikobus_refreshed(data)
        self.async_update_listeners()

    async def async_reconfigure(self, entry: ConfigEntry) -> None:
        """Handle configuration changes via reconfigure."""
        _LOGGER.info("Reconfiguring Nikobus integration.")

        self.connection_string = entry.data.get(CONF_CONNECTION_STRING)
        self._refresh_interval = entry.data.get(CONF_REFRESH_INTERVAL, 120)
        self._has_feedback_module = entry.data.get(CONF_HAS_FEEDBACK_MODULE, False)
        self._prior_gen3 = entry.data.get(CONF_PRIOR_GEN3, False)
        self._update_interval = self._get_update_interval()
        self.update_interval = self._update_interval

        # A changed clock address means a different question is being asked, so
        # the old verdict and the old baseline reading must not survive it.
        heartbeat_address = str(
            entry.data.get(CONF_HEARTBEAT_ADDRESS, DEFAULT_HEARTBEAT_ADDRESS) or ""
        ).strip()
        if heartbeat_address != self._heartbeat_address:
            await self.nikobus_heartbeat.stop()
            self._heartbeat_address = heartbeat_address
            self.nikobus_heartbeat = NikobusHeartbeat(self, heartbeat_address)

        await self.connect()
        await self.async_refresh()

    async def _handle_ha_button_pressed(self, data) -> None:
        """Handle HA button press events."""
        address = data.get("address")
        operation_time = data.get("operation_time")
        _LOGGER.debug(
            "HA Button %s pressed with operation_time: %s",
            address,
            operation_time,
        )
        await self.nikobus_command.queue_command(f"#N{address}\r#E1")


    async def _handle_nikobus_refreshed(self, data) -> None:
        """Handle Nikobus refreshed events."""
        impacted_module_address = data.get("impacted_module_address")
        impacted_module_group = data.get("impacted_module_group")
        _LOGGER.debug(
            "Nikobus refreshed for module %s group %s",
            impacted_module_address,
            impacted_module_group,
        )

    def get_module_type(self, module_id: str) -> str:
        """Determine the module type based on the module ID."""
        for module_type, modules in self.dict_module_data.items():
            if module_id in modules:
                return module_type
        _LOGGER.error("Module ID %s not found in known module types", module_id)
        return "unknown"

    def get_module_channel_count(self, module_id: str) -> int:
        for modules in self.dict_module_data.values():
            if module_id in modules:
                module_data = modules[module_id]
                return len(module_data.get("channels", []))
        _LOGGER.error("Module ID %s not found in module configuration", module_id)
        return 0

    def get_light_state(self, address: str, channel: int) -> bool:
        """Get the state of a light based on its address and channel."""
        return self.get_bytearray_state(address, channel) != 0x00

    def get_switch_state(self, address: str, channel: int) -> bool:
        """Get the state of a switch based on its address and channel."""
        return self.get_bytearray_state(address, channel) == 0xFF

    def get_light_brightness(self, address: str, channel: int) -> int:
        """Get the brightness of a light based on its address and channel."""
        return self.get_bytearray_state(address, channel)

    def get_cover_state(self, address: str, channel: int) -> int:
        """Get the state of a cover based on its address and channel."""
        return self.get_bytearray_state(address, channel)

    # ------------------------------------------------------------------
    # Connection lost / reconnect
    # ------------------------------------------------------------------

    async def _handle_connection_lost(self) -> None:
        """Tear the protocol stack down and schedule a reconnect.

        Called by the listener when its reader fails, and by the
        blackout-recovery path in ``_refresh_nikobus_data``.

        Idempotent on purpose: while a reconnect task is alive this is a
        no-op. Both callers can fire for the same outage - blackout detection
        notices the silent bus, the reconnect opens a new FD, the old
        listener's pending read then fails and reports the loss as well - and
        without the guard the second call would stop the command handler again
        in the middle of the in-flight handshake. Upstream hit exactly that as
        the "Reconnect 1 failed: Cannot send: Not connected." follow-up to
        issue #337; coalescing at function entry collapses call #2 into
        nothing.
        """
        if self._stopping:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            _LOGGER.debug(
                "Reconnect already in progress - coalescing duplicate "
                "connection-lost notification"
            )
            return

        _LOGGER.warning("Nikobus connection lost - scheduling reconnect")
        # Push the state change out immediately so the connection sensor flips
        # to "reconnecting" now, not after the first retry. The whole point of
        # 21.08.2026 is that the outage must be visible while it is happening.
        self.async_update_listeners()

        if self.nikobus_command:
            await self.nikobus_command.stop()
        if self.nikobus_listener:
            # Stop the listener BEFORE the reconnect runs. Left running, its
            # pending read() on the old reader fires the moment connect()
            # opens a new FD, read() closes the shared connection object
            # mid-handshake, and the handshake's next send() fails - the
            # reconnect then loses to its own predecessor. When the listener
            # itself reported the loss, it is already leaving its loop and
            # stop() is a no-op (see NikobusEventListener.stop, which refuses
            # to cancel itself). Safe in both paths.
            await self.nikobus_listener.stop()

        self._reconnect_task = self.hass.async_create_background_task(
            self._reconnect_loop(), name="nikobus_reconnect"
        )

    async def _reconnect_loop(self) -> None:
        """Rebuild the transport, then bring the protocol stack back up.

        ``nikobus_connection.reconnect_with_backoff`` owns the transport half
        (close, reopen, re-run the handshake, exponential capped backoff,
        retry forever). This loop only orchestrates the Home Assistant side:
        clear state that belonged to the dead connection, restart the workers,
        refresh, and tell the entities. That split is upstream 3.x's, where
        the transport half lives in the external library instead.
        """

        def _on_attempt(attempt: int, _delay: float) -> None:
            self._reconnect_attempts += 1
            # Repaints the connection sensor's reconnect_attempts attribute.
            self.async_update_listeners()

        while not self._stopping:
            try:
                attempts = await self.nikobus_connection.reconnect_with_backoff(
                    initial_delay=RECONNECT_DELAY_INITIAL,
                    max_delay=RECONNECT_DELAY_MAX,
                    on_attempt=_on_attempt,
                )
            except asyncio.CancelledError:
                return  # stop() cancelled us

            try:
                # Drop state queued against the dead connection, then bring the
                # pipeline back up on the new one.
                self.nikobus_command.reset()
                self.nikobus_listener.reset()
                await self.nikobus_command.start()
                self.nikobus_listener.on_connection_lost = self._handle_connection_lost
                await self.nikobus_listener.start()
                # Idempotent: the heartbeat task is not torn down for an outage,
                # it just skips its polls while the transport is down (see
                # NikobusHeartbeat._reason_to_skip). This only matters when the
                # very first connect never got far enough to start it.
                await self.nikobus_heartbeat.start()
                self._last_connected = datetime.now(timezone.utc)
                self._reconnect_attempts = 0
                await self._async_update_data()
                self.async_update_listeners()
                _LOGGER.info("Nikobus reconnected after %d attempt(s)", attempts)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "Nikobus subsystem restart failed after reconnect - retrying"
                )
                await self.nikobus_connection.disconnect()

    async def stop(self) -> None:
        """Stop the coordinator and its running tasks."""
        _LOGGER.debug("Stopping NikobusDataCoordinator")
        # Set first: it makes _handle_connection_lost a no-op, so the teardown
        # below cannot trip the reconnect it is trying to shut down.
        self._stopping = True

        task = self._reconnect_task
        self._reconnect_task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if self.nikobus_heartbeat:
            try:
                await self.nikobus_heartbeat.stop()
                _LOGGER.debug("Nikobus heartbeat stopped.")
            except Exception as e:
                _LOGGER.error("Error stopping Nikobus heartbeat: %s", e)

        if self.nikobus_listener:
            try:
                await self.nikobus_listener.stop()
                _LOGGER.debug("Nikobus listener stopped.")
            except Exception as e:
                _LOGGER.error("Error stopping Nikobus listener: %s", e)
        if self.nikobus_command:
            try:
                await self.nikobus_command.stop()
                _LOGGER.debug("Nikobus command handler stopped.")
            except Exception as e:
                _LOGGER.error("Error stopping Nikobus command handler: %s", e)
        if self.nikobus_connection:
            try:
                await self.nikobus_connection.disconnect()
                _LOGGER.debug("Nikobus connection disconnected.")
            except Exception as e:
                _LOGGER.error("Error disconnecting Nikobus connection: %s", e)

    # DISCOVERY SPECIFICS

    def get_all_module_addresses(self):
        """Return a list of all module addresses from the module configuration."""
        return [
            address
            for modules in self.dict_module_data.values()
            for address in modules.keys()
        ]

    async def _handle_discovery_finished(self):
        """Reload the config entry after discovery so new devices are applied."""
        self._discovery_running = False
        if self._reload_task and not self._reload_task.done():
            _LOGGER.debug("Discovery reload already in progress; skipping new reload.")
            return

        async def _reload_entry():
            try:
                _LOGGER.info("Discovery finished; reloading Nikobus config entry.")
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            except Exception as err:  # pragma: no cover - safety net
                _LOGGER.error(
                    "Failed to reload Nikobus config entry after discovery: %s", err
                )

        self._reload_task = self.hass.async_create_task(_reload_entry())

    def get_button_channels(self, main_address: str):
        """Return the discovery channels for a given button discovered_info address."""
        buttons = self.dict_button_data.get("nikobus_button", {})
        return next(
            (
                info.get("channels")
                for button in buttons.values()
                for info in (button.get("discovered_info") or [])
                if isinstance(info, dict) and info.get("address") == main_address
            ),
            None,
        )

    def get_known_entity_unique_ids(self) -> set[str]:
        """Return the set of valid unique_ids for all Nikobus entities
        based on current JSON configuration."""

        known: set[str] = set()

        # -----------------------
        # 1) MODULE-BASED ENTITIES
        #    - dimmer_module  -> nikobus_light_{address}_{channel}
        #    - switch_module  -> nikobus_switch_{address}_{channel}
        #    - roller_module  -> nikobus_cover_{address}_{channel} unless use_as_switch
        # -----------------------
        for module_type, modules in self.dict_module_data.items():
            for address, module_data in modules.items():
                for index, ch_info in enumerate(module_data.get("channels", []), start=1):
                    desc = ch_info.get("description", "")
                    if desc.startswith("not_in_use"):
                        continue

                    if module_type == "dimmer_module":
                        known.add(f"{DOMAIN}_light_{address}_{index}")
                    elif module_type == "switch_module":
                        known.add(f"{DOMAIN}_switch_{address}_{index}")
                    elif module_type == "roller_module":
                        if ch_info.get("use_as_switch", False):
                            known.add(f"{DOMAIN}_switch_{address}_{index}")
                        else:
                            known.add(f"{DOMAIN}_cover_{address}_{index}")
                    else:
                        known.add(f"{DOMAIN}_{address}_{index}")

        # -----------------------
        # 2) Button sensors
        # using: nikobus_button_sensor_{address}
        # -----------------------
        for button in self.dict_button_data.get("nikobus_button", {}).values():
            addr = button.get("address")
            if addr:
                known.add(f"{DOMAIN}_button_sensor_{addr}")
                # -----------------------
                # 3) Push buttons
                # using: nikobus_push_button_{address}
                # -----------------------
                known.add(f"{DOMAIN}_push_button_{addr}")

        # -----------------------
        # 4) Scenes
        # using: nikobus_scene_{scene_id}
        # -----------------------
        scene_list = self.dict_scene_data.get("scene", [])
        for scene in scene_list:
            sid = scene.get("id")
            if sid:
                known.add(f"{DOMAIN}_scene_{sid}")

        # -----------------------
        # 5) YAML-defined covers
        # using: nikobus_yaml_cover_{name}
        # -----------------------
        yaml_covers = self.hass.data.get(DOMAIN, {}).get(CONF_COVERS, [])
        for cover in yaml_covers:
            unique_id = cover.get("unique_id")
            if unique_id:
                known.add(unique_id)
                if cover.get("as_switch") in ("up", "down"):
                    known.add(f"{unique_id}_switch")

        # -----------------------
        # 6) YAML-defined group covers
        # using: nikobus_yaml_group_cover_{name}
        # -----------------------
        yaml_group_covers = self.hass.data.get(DOMAIN, {}).get(CONF_GROUP_COVERS, [])
        for cover in yaml_group_covers:
            unique_id = cover.get("unique_id")
            if unique_id:
                known.add(unique_id)

        # -----------------------
        # 7) Bridge-level diagnostic entities
        # -----------------------
        # Without this the orphan cleanup in __init__ would delete the
        # connection sensor on every single start: it removes any entity of
        # this config entry whose unique_id is not in this set. Same literal
        # as upstream 3.x, so the port keeps the entity instead of recreating
        # it under a "_2" suffix.
        known.add(f"{DOMAIN}_connection_status")

        return known
