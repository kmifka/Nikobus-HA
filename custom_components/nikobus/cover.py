"""Cover platform for the Nikobus integration (optimized version)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta
from typing import Any, Dict, Optional

from homeassistant.components.cover import (
    CoverEntity,
    CoverEntityFeature,
    CoverDeviceClass,
    ATTR_POSITION,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval, async_track_state_change_event

from .const import (
    DOMAIN,
    BRAND,
    CONF_COVERS,
    CONF_GROUP_COVERS,
    CONF_BUTTON_UP_CODES,
    CONF_BUTTON_DOWN_CODES,
    CONF_COVER_HIDDEN,
    CONF_GROUP_STOP_TOLERANCE,
    DEFAULT_GROUP_STOP_TOLERANCE,
    CONF_END_STOP_CLEANUP_DELAY,
    DEFAULT_END_STOP_CLEANUP_DELAY,
    CONF_COVER_NAME,
    CONF_COVER_UP_CODE,
    CONF_COVER_DOWN_CODE,
    CONF_COVER_STOP_CODE,
    CONF_TRAVEL_UP_TIME,
    CONF_TRAVEL_DOWN_TIME,
    CONF_COVER_AS_SWITCH,
    CONF_COVER_AREA,
)
from .coordinator import NikobusDataCoordinator
from .entity import NikobusEntity
from .helpers.travelcalculator import TravelCalculator, TravelStatus
from .helpers.command import send_repeated_command
from .helpers.entity_registry import (
    async_assign_area_if_missing,
    async_apply_suggested_entity_id,
)

_LOGGER = logging.getLogger(__name__)

HUB_IDENTIFIER = "nikobus_hub"

STATE_STOPPED = 0x00
STATE_OPENING = 0x01
STATE_CLOSING = 0x02
STATE_ERROR = 0x03


class PositionEstimator:
    """Estimates the current position of the cover based on elapsed time and direction."""

    def __init__(self, duration_in_seconds: float, start_position: Optional[float]):
        if duration_in_seconds <= 0:
            raise ValueError("operation_time must be greater than zero")

        self._duration_in_seconds = duration_in_seconds
        self._start_time: Optional[float] = None
        self._direction_value: Optional[int] = None
        self._initial_position: Optional[float] = start_position
        self._current_position: Optional[float] = start_position
        self._is_moving = False

        _LOGGER.debug(
            "PositionEstimator initialized with duration: %.2f seconds, start position: %s",
            duration_in_seconds,
            start_position,
        )

    def start(self, direction: str, position: Optional[float] = None) -> None:
        if direction not in ("opening", "closing"):
            _LOGGER.error("Invalid direction '%s' provided to PositionEstimator", direction)
            return

        direction_value = 1 if direction == "opening" else -1
        baseline_position = self.get_position() if self._is_moving else self._current_position

        if self._is_moving and self._direction_value == direction_value:
            _LOGGER.debug(
                "Estimator already moving %s; refreshing baseline without stopping.", direction
            )
        elif self._is_moving:
            _LOGGER.debug(
                "Estimator restarting for direction change: %s -> %s.",
                "opening" if self._direction_value == 1 else "closing",
                direction,
            )

        self._direction_value = direction_value
        self._start_time = time.monotonic()
        self._is_moving = True

        # Capture the initial position once at the start.
        if position is not None:
            self._initial_position = max(0.0, min(100.0, float(position)))
        elif baseline_position is not None:
            self._initial_position = baseline_position
        else:
            self._initial_position = 100.0 if self._direction_value == 1 else 0.0
        self._current_position = self._initial_position

        _LOGGER.debug(
            "Movement started in direction: %s, initial position set to: %s",
            direction,
            self._initial_position,
        )

    def get_position(self) -> Optional[float]:
        """Calculate and return the current position estimate."""
        if (
            not self._is_moving
            or self._start_time is None
            or self._direction_value is None
            or self._initial_position is None
        ):
            _LOGGER.debug(
                "Position estimation unavailable; ensure start() is called correctly."
            )
            return None

        elapsed_time = time.monotonic() - self._start_time
        progress = (elapsed_time / self._duration_in_seconds) * 100 * self._direction_value
        # Always compute based on the fixed starting position.
        new_position = max(0.0, min(100.0, self._initial_position + progress))
        self._current_position = new_position
        return new_position

    def stop(self) -> None:
        """Stop the movement and finalize the position estimate."""
        if self._is_moving:
            final_position = self.get_position()
            if final_position is not None:
                self._current_position = final_position
            _LOGGER.debug(
                "Movement stopped. Final position estimated at: %s", self._current_position
            )
        else:
            _LOGGER.warning("Stop called without active movement; ignoring.")

        self._start_time = None
        self._direction_value = None
        self._is_moving = False

    @property
    def current_position(self) -> Optional[int]:
        if self._current_position is None:
            return None
        return int(round(self._current_position))

    @property
    def duration_in_seconds(self) -> float:
        return self._duration_in_seconds

    @property
    def is_active(self) -> bool:
        return self._is_moving


def _clamp_position(value: Optional[float]) -> Optional[int]:
    """Clamp a numeric position into the 0-100 range."""

    if value is None:
        return None
    return int(max(0, min(100, round(value))))


def _require_connection(coordinator: NikobusDataCoordinator, entity_name: str) -> None:
    """Refuse to drive a cover while the Nikobus transport is down.

    Why this is a hard failure and not a warning
    -------------------------------------------
    A cover command that never reaches the bus does not move anything, but the
    travel calculator does not know that - it starts its timer and Home
    Assistant then reports a confident 40% for a blind hanging motionless at
    the top. Real and modelled state drift apart, and nothing looks wrong until
    somebody walks past a window.

    That is precisely what happened on 21.08.2026: for two hours every command
    failed in the writer, and the only trace was a log line. So:

    * the estimator is not started at all (the old position is still correct -
      nothing moved), and
    * the failure is raised, not swallowed, so the caller sees it. A service
      call fails visibly, an automation errors, and the user is told instead of
      being shown a plausible number.

    Upstream 3.x has no equivalent check; see helpers/travelcalculator.py for
    why this fork deliberately goes further.
    """
    connection = getattr(coordinator, "nikobus_connection", None)
    if connection is None or connection.is_connected:
        return
    status = getattr(coordinator, "connection_status", "disconnected")
    _LOGGER.warning(
        "Refusing to move %s: Nikobus connection is %s.", entity_name, status
    )
    raise HomeAssistantError(
        f"Nikobus is {status}; '{entity_name}' was not moved. "
        "The command would not have reached the bus."
    )


def _system_is_available(coordinator: NikobusDataCoordinator) -> bool:
    """Return whether a command sent right now would reach the shutters.

    The verdict itself lives on the coordinator (``system_available``): the
    transport has to be open AND the installation has to still be answering the
    heartbeat clock. See coordinator.system_available and nkbheartbeat.py.

    Why the covers publish it as ``available`` at all
    -------------------------------------------------
    A shutter whose commands land in the void IS not available. Showing it as
    operable is the same lie as a travel calculator reporting a position for a
    blind that never moved - both invite the user to act on something that is
    not true. Reported honestly, Home Assistant greys the entity out and Apple
    Home says "not responding" instead of offering a slider that does nothing.

    ``getattr`` with a default, because a few tests and the reconfigure path
    build cover entities against objects that are not a full coordinator yet;
    an unknown state must not be read as "broken".
    """
    return bool(getattr(coordinator, "system_available", True))


def _safe_cancel(task: Optional[asyncio.Task]) -> None:
    """Cancel a task without raising if it is already done."""

    if task is None or task.done() or task is asyncio.current_task():
        return
    task.cancel()

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    _LOGGER.debug("Setting up Nikobus cover entities.")

    coordinator: NikobusDataCoordinator = entry.runtime_data
    roller_modules: Dict[str, Any] = coordinator.dict_module_data.get(
        "roller_module", {}
    )

    device_registry = dr.async_get(hass)
    cover_entities: list[NikobusCoverEntity] = []
    switch_entities: list[Dict[str, Any]] = []  # Store switch info for switch.py

    for address, cover_module_data in roller_modules.items():
        module_desc = cover_module_data.get("description", f"Roller Module {address}")
        module_model = cover_module_data.get("model", "Unknown Roller Model")

        _register_nikobus_roller_device(
            device_registry=device_registry,
            entry=entry,
            module_address=address,
            module_name=module_desc,
            module_model=module_model,
        )

        for channel_idx, channel_info in enumerate(
            cover_module_data.get("channels", []), start=1
        ):
            if channel_info["description"].startswith("not_in_use"):
                continue

            use_as_switch = channel_info.get("use_as_switch", False)
            _LOGGER.debug(
                f"Processing {module_desc} channel {channel_idx}: use_as_switch={use_as_switch}"
            )

            if use_as_switch:
                switch_entities.append(
                    {
                        "coordinator": coordinator,
                        "address": address,
                        "channel": channel_idx,
                        "channel_description": channel_info["description"],
                        "module_desc": module_desc,
                        "module_model": module_model,
                    }
                )
            else:
                operation_time = channel_info.get("operation_time", "30")
                cover_entities.append(
                    NikobusCoverEntity(
                        hass=hass,
                        coordinator=coordinator,
                        address=address,
                        channel=channel_idx,
                        channel_description=channel_info["description"],
                        module_desc=module_desc,
                        module_model=module_model,
                        operation_time=operation_time,
                    )
                )

    async_add_entities(cover_entities)
    _LOGGER.debug("Added %d Nikobus cover entities.", len(cover_entities))
    hass.data.setdefault(DOMAIN, {})["switch_entities"] = switch_entities

    yaml_covers = hass.data.get(DOMAIN, {}).get(CONF_COVERS, [])
    if yaml_covers:
        yaml_entities = [
            NikobusYamlCoverEntity(coordinator=coordinator, config=cover_config)
            for cover_config in yaml_covers
            if not cover_config.get(CONF_COVER_AS_SWITCH)
        ]
        async_add_entities(yaml_entities)
        _LOGGER.debug("Added %d YAML-defined Nikobus covers.", len(yaml_entities))

    group_covers = hass.data.get(DOMAIN, {}).get(CONF_GROUP_COVERS, [])
    if group_covers:
        group_entities = [
            NikobusYamlGroupCoverEntity(coordinator=coordinator, config=cover_config)
            for cover_config in group_covers
        ]
        async_add_entities(group_entities)
        _LOGGER.debug("Added %d YAML-defined Nikobus group covers.", len(group_entities))


def _register_nikobus_roller_device(
    device_registry: dr.DeviceRegistry,
    entry: ConfigEntry,
    module_address: str,
    module_name: str,
    module_model: str,
) -> None:
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, module_address)},
        manufacturer=BRAND,
        name=module_name,
        model=module_model,
        via_device=(DOMAIN, HUB_IDENTIFIER),
    )


class NikobusCoverEntity(NikobusEntity, CoverEntity, RestoreEntity):
    """Optimized representation of a Nikobus cover entity with improved task management and state consistency."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: NikobusDataCoordinator,
        address: str,
        channel: int,
        channel_description: str,
        module_desc: str,
        module_model: str,
        operation_time: str,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            address=address,
            name=module_desc,
            model=module_model,
        )
        self.hass = hass
        self._address = address
        self._channel = channel
        self._description = module_desc
        self._model = module_model
        self._state = STATE_STOPPED
        self._position = 100  # Default to fully open
        self._previous_state: Optional[int] = None
        self._movement_source = "ha"
        self._direction: Optional[str] = None  # "opening" or "closing"
        self._target_position: Optional[int] = None
        self._button_operation_time: Optional[float] = None

        self._operation_time = float(operation_time)
        self._position_estimator = PositionEstimator(
            duration_in_seconds=self._operation_time, start_position=self._position
        )

        self._in_motion = False
        self._motion_task: Optional[asyncio.Task] = None
        self._last_position_change_time = time.monotonic()
        self._unsub_button_event: Optional[Any] = None

        self._attr_name = channel_description
        self._attr_unique_id = f"{DOMAIN}_cover_{self._address}_{self._channel}"
        self._attr_device_class = CoverDeviceClass.SHUTTER

        _LOGGER.debug(
            "NikobusCoverEntity initialized for '%s' (address=%s, channel=%s, operation_time=%.2f seconds)",
            channel_description,
            address,
            channel,
            self._operation_time,
        )

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        attrs = super().extra_state_attributes or {}
        attrs.update({"position": self._position, "state": self._state})
        return attrs

    @property
    def current_cover_position(self) -> Optional[int]:
        return self._position

    @property
    def is_open(self) -> bool:
        return self._position == 100

    @property
    def is_closed(self) -> bool:
        return self._position == 0

    @property
    def is_opening(self) -> bool:
        return self._state == STATE_OPENING

    @property
    def is_closing(self) -> bool:
        return self._state == STATE_CLOSING

    @property
    def available(self) -> bool:
        return self._state != STATE_ERROR

    @property
    def supported_features(self) -> int:
        return (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )

    async def async_added_to_hass(self) -> None:
        """Register callbacks and restore state when added to Home Assistant."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state:
            last_position = last_state.attributes.get(ATTR_POSITION)
            if last_position is not None:
                self._position = float(last_position)
                _LOGGER.debug(
                    "Restored position for '%s' to %s", self._attr_name, self._position
                )
            else:
                _LOGGER.warning(
                    "No valid position found in the last state for '%s', defaulting to 100.",
                    self._attr_name,
                )
                self._position = 100
        else:
            _LOGGER.info(
                "No last state available for '%s', initializing position to default (100).",
                self._attr_name,
            )
            self._position = 100

        self._state = self.coordinator.get_cover_state(self._address, self._channel)
        self._previous_state = self._state
        _LOGGER.debug(
            "Initialized state for '%s' to %s (channel=%d, address=%s).",
            self._attr_name,
            self._state,
            self._channel,
            self._address,
        )

        self._unsub_button_event = self.hass.bus.async_listen(
            "nikobus_button_pressed", self._handle_nikobus_button_event
        )
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up listeners and running tasks when the entity is removed."""

        if self._unsub_button_event:
            self._unsub_button_event()
            self._unsub_button_event = None

        _safe_cancel(self._motion_task)
        if self._motion_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._motion_task
            self._motion_task = None

    @callback
    def _handle_coordinator_update(self) -> None:
        new_state = self.coordinator.get_cover_state(self._address, self._channel)
        if new_state != self._previous_state:
            self.hass.async_create_task(
                self._process_state_change(new_state, source="ha")
            )

    async def _handle_nikobus_button_event(self, event: Any) -> None:
        """Handle the `nikobus_button_pressed` event and update the cover state."""
        if event.data.get("impacted_module_address") != self._address:
            return

        new_state = self.coordinator.get_cover_state(self._address, self._channel)
        if new_state != self._previous_state:
            _LOGGER.debug(
                "State changed for %s: %s -> %s",
                self._attr_name,
                self._previous_state,
                new_state,
            )
            if event.data.get("button_operation_time") is not None:
                self._button_operation_time = float(
                    event.data.get("button_operation_time")
                )
                _LOGGER.debug(
                    "Received button operation time for %s: %s",
                    self._attr_name,
                    self._button_operation_time,
                )
            await self._process_state_change(new_state, source="nikobus")
        else:
            if self._in_motion and new_state in (STATE_OPENING, STATE_CLOSING):
                _LOGGER.debug(
                    "Button press for %s detected without state change; stopping motion.",
                    self._attr_name,
                )
                await self._end_motion()
            else:
                _LOGGER.debug(
                    "No state change for %s; ignoring event.", self._attr_name
                )

    async def async_open_cover(self, **kwargs: Any) -> None:
        _require_connection(self.coordinator, self._attr_name)
        await self._request_cover_motion("opening")

    async def async_close_cover(self, **kwargs: Any) -> None:
        _require_connection(self.coordinator, self._attr_name)
        await self._request_cover_motion("closing")

    async def async_stop_cover(self, **kwargs: Any) -> None:
        _require_connection(self.coordinator, self._attr_name)
        await self._end_motion(send_stop=True)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        _require_connection(self.coordinator, self._attr_name)
        target_position = kwargs.get(ATTR_POSITION)
        if target_position is None:
            return

        current_time = time.monotonic()
        if current_time - self._last_position_change_time < 1:
            _LOGGER.debug(
                "Skipping position update for %s due to rapid commands.",
                self._attr_name,
            )
            return

        self._last_position_change_time = current_time

        if self._position == target_position:
            _LOGGER.debug("Cover %s is already at target position.", self._attr_name)
            return

        direction = "opening" if target_position > self._position else "closing"
        await self._request_cover_motion(direction, target_position=target_position)

    async def _process_state_change(self, new_state: int, source: str = "ha") -> None:
        _LOGGER.debug(
            "State change detected for %s: %s -> %s",
            self._attr_name,
            self._previous_state,
            new_state,
        )

        if (new_state == STATE_OPENING and self._position == 100) or (
            new_state == STATE_CLOSING and self._position == 0
        ):
            _LOGGER.debug(
                "Cover %s already at intended position %d. No action needed.",
                self._attr_name,
                self._position,
            )
            self.coordinator.set_bytearray_state(
                self._address, self._channel, STATE_STOPPED
            )
            return

        self._previous_state = new_state
        self._movement_source = source

        if new_state in (STATE_OPENING, STATE_CLOSING):
            direction = "opening" if new_state == STATE_OPENING else "closing"
            if self._in_motion and self._direction == direction:
                _LOGGER.debug(
                    "Ignoring duplicate %s update for %s; already moving.",
                    direction,
                    self._attr_name,
                )
                self._previous_state = new_state
                self._movement_source = source
                return
            if source == "nikobus":
                self._target_position = None
            await self._begin_motion(
                direction,
                source,
                target_position=self._target_position,
                button_limit=self._button_operation_time,
            )
        elif new_state == STATE_STOPPED:
            await self._end_motion()
        elif new_state == STATE_ERROR:
            _LOGGER.warning("Error state encountered for %s.", self._attr_name)
            await self._end_motion(send_stop=True)
        else:
            _LOGGER.warning(
                "Unknown state '%s' encountered for %s.", new_state, self._attr_name
            )

    async def _request_cover_motion(
        self, direction: str, target_position: Optional[int] = None
    ) -> None:
        """Queue a cover command and start motion once executed."""

        if self._in_motion:
            await self._end_motion(send_stop=self._movement_source == "ha")

        self._movement_source = "ha"
        self._target_position = _clamp_position(target_position)

        async def completion_handler() -> None:
            await self._begin_motion(
                direction,
                source="ha",
                target_position=self._target_position,
            )

        await self._operate_cover(direction, completion_handler)

    async def _operate_cover(self, direction: str, completion_handler: Any) -> None:
        _LOGGER.debug("Operating cover %s in direction: %s", self._attr_name, direction)
        try:
            if direction == "opening":
                await self.coordinator.api.open_cover(
                    self._address, self._channel, completion_handler=completion_handler
                )
            elif direction == "closing":
                await self.coordinator.api.close_cover(
                    self._address, self._channel, completion_handler=completion_handler
                )
            else:
                _LOGGER.error(
                    "Invalid direction '%s' for cover %s", direction, self._attr_name
                )
        except Exception as exc:
            _LOGGER.error(
                "Failed to operate cover %s: %s", self._attr_name, exc, exc_info=True
            )

    async def _begin_motion(
        self,
        direction: str,
        source: str,
        target_position: Optional[int] = None,
        button_limit: Optional[float] = None,
    ) -> None:
        """Authoritative entrypoint for starting movement."""

        if self._in_motion:
            if self._direction == direction:
                _LOGGER.debug(
                    "Duplicate start for %s in direction %s; keeping current motion.",
                    self._attr_name,
                    direction,
                )
                if target_position is not None:
                    self._target_position = _clamp_position(target_position)
                if button_limit is not None:
                    self._button_operation_time = button_limit
                self._movement_source = source
                return

            _LOGGER.debug(
                "Reversing direction for %s: %s -> %s; stopping current motion first.",
                self._attr_name,
                self._direction,
                direction,
            )
            await self._end_motion(send_stop=source == "ha")

        self._direction = direction
        self._in_motion = True
        self._movement_source = source
        self._button_operation_time = button_limit
        self._target_position = _clamp_position(target_position)
        self._state = STATE_OPENING if direction == "opening" else STATE_CLOSING

        self._position_estimator.start(self._direction, self._position)

        _safe_cancel(self._motion_task)
        self._motion_task = self.hass.async_create_task(self._motion_loop())
        self.async_write_ha_state()

    async def _end_motion(
        self,
        final_position: Optional[int] = None,
        send_stop: bool = False,
    ) -> None:
        """Authoritative entrypoint for ending movement."""

        if not self._in_motion and not send_stop:
            return

        direction_for_stop = self._direction
        self._in_motion = False

        async def _finalize_state() -> None:
            self._position_estimator.stop()
            _safe_cancel(self._motion_task)
            if self._motion_task:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._motion_task
                self._motion_task = None

            estimated_position = _clamp_position(
                final_position if final_position is not None else self._position_estimator.current_position
            )
            if estimated_position is not None:
                self._position = estimated_position

            self._direction = None
            self._target_position = None
            self._button_operation_time = None
            self._state = STATE_STOPPED
            self._previous_state = STATE_STOPPED

            self.coordinator.set_bytearray_state(
                self._address, self._channel, STATE_STOPPED
            )
            self.async_write_ha_state()

        if send_stop and direction_for_stop:
            try:
                await self.coordinator.api.stop_cover(
                    self._address,
                    self._channel,
                    direction_for_stop,
                    completion_handler=_finalize_state,
                )
            except Exception as exc:
                _LOGGER.error(
                    "Failed to stop cover %s: %s", self._attr_name, exc, exc_info=True
                )
                await _finalize_state()
        else:
            await _finalize_state()

    async def _motion_loop(self) -> None:
        """Single loop responsible for motion lifecycle and estimation."""

        start_time = time.monotonic()
        try:
            while self._in_motion and self._direction:
                estimated_position = self._position_estimator.get_position()
                if estimated_position is not None:
                    clamped_position = _clamp_position(estimated_position)
                    if clamped_position is not None:
                        self._position = clamped_position

                elapsed = time.monotonic() - start_time
                if self._button_operation_time and elapsed >= self._button_operation_time:
                    _LOGGER.debug(
                        "Stopping %s due to button operation timeout.", self._attr_name
                    )
                    await self._end_motion(send_stop=self._movement_source == "ha")
                    break

                if self._target_position is not None:
                    if (
                        self._direction == "opening"
                        and self._position >= self._target_position
                        and self._target_position < 100
                    ) or (
                        self._direction == "closing"
                        and self._position <= self._target_position
                        and self._target_position > 0
                    ):
                        await self._end_motion(
                            final_position=self._target_position,
                            send_stop=self._movement_source == "ha",
                        )
                        break

                if (self._direction == "opening" and self._position >= 100) or (
                    self._direction == "closing" and self._position <= 0
                ):
                    await self._end_motion(
                        final_position=100 if self._direction == "opening" else 0,
                        send_stop=self._movement_source == "ha",
                    )
                    break

                self.async_write_ha_state()
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            _LOGGER.debug("Motion loop for %s was cancelled.", self._attr_name)
        except Exception as exc:
            _LOGGER.error(
                "Unexpected error in motion loop for %s: %s",
                self._attr_name,
                exc,
                exc_info=True,
            )
            await self._end_motion()
        finally:
            self._motion_task = None


class NikobusYamlCoverEntity(CoverEntity, RestoreEntity):
    """Cover entity controlled by raw Nikobus command codes from YAML."""

    def __init__(
        self,
        coordinator: NikobusDataCoordinator,
        config: dict[str, Any],
    ) -> None:
        self._coordinator = coordinator
        self._name = config[CONF_COVER_NAME]
        self._up_code = config[CONF_COVER_UP_CODE]
        self._down_code = config[CONF_COVER_DOWN_CODE]
        self._stop_code = config[CONF_COVER_STOP_CODE]
        self._unique_id = config["unique_id"]
        self._area_name = config.get(CONF_COVER_AREA)
        self._button_up_codes = config.get(CONF_BUTTON_UP_CODES) or []
        self._button_down_codes = config.get(CONF_BUTTON_DOWN_CODES) or []
        self._unsub_button_event = None
        self._last_button_press: Dict[str, float] = {}

        travel_up = config.get(CONF_TRAVEL_UP_TIME)
        travel_down = config.get(CONF_TRAVEL_DOWN_TIME)
        if travel_up is not None and travel_down is not None:
            self._tc = TravelCalculator(
                travel_time_down=float(travel_down),
                travel_time_up=float(travel_up),
            )
        else:
            self._tc = None
        self._unsubscribe_auto_updater = None
        self._unsub_group_event = None
        self._is_manual_position = False
        self._stop_in_progress = False
        # Travel that only mirrors a physical bus button must never put a
        # command back on the bus: the actuator is already doing the work.
        self._suppress_stop_command = False
        # Position the cover should stop at. The travel calculator itself always
        # runs to the end stop, so the modelled position keeps advancing while a
        # stop command is still queued - that is what the real shutter does.
        self._pending_target: Optional[int] = None
        # Counts every action on this cover. A pending cleanup only fires while
        # the counter is unchanged, so anything that happens in between wins.
        self._action_seq = 0
        self._cleanup_task: Optional[asyncio.Task] = None
        self._state: Optional[str] = None
        # Last availability this entity published. Kept so the coordinator
        # listener can write a state only when the answer actually changed -
        # the coordinator notifies on every button press and every refresh, and
        # repainting 26 covers each time would be pure noise.
        self._unsub_coordinator = None
        self._last_available: Optional[bool] = None

        self._attr_name = self._name
        self._attr_unique_id = self._unique_id
        self._attr_suggested_object_id = config.get("suggested_object_id")
        self._attr_device_class = CoverDeviceClass.SHUTTER
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )
        if self._tc:
            self._attr_supported_features |= CoverEntityFeature.SET_POSITION

    async def async_added_to_hass(self) -> None:
        """Restore last known position."""
        self._subscribe_to_availability()
        self._unsub_group_event = self.hass.bus.async_listen(
            "nikobus_group_cover_command", self._handle_group_cover_command
        )
        if (
            self._button_up_codes or self._button_down_codes
        ) and self._unsub_button_event is None:
            # Adding an entity can run twice (a rename re-adds it), so registering
            # unconditionally would leave a leaked second listener behind and make
            # every press act twice.
            self._unsub_button_event = self.hass.bus.async_listen(
                "nikobus_button_pressed", self._handle_bus_button_press
            )
        last_state = await self.async_get_last_state()
        if not self._tc:
            await self._assign_area()
            await self._ensure_suggested_entity_id()
            return

        position = None
        if last_state:
            position = last_state.attributes.get(ATTR_POSITION)
            if position is None:
                position = last_state.attributes.get("current_position")
            if position is None:
                if last_state.state == "open":
                    position = 100
                elif last_state.state == "closed":
                    position = 0

        if position is None:
            position = self._tc.position_closed
        self._tc.set_position(int(position))
        await self._assign_area()
        await self._ensure_suggested_entity_id()

    async def _assign_area(self) -> None:
        """Assign entity to a Home Assistant area if configured."""
        await async_assign_area_if_missing(
            self.hass, self.entity_id, self._area_name
        )

    async def _ensure_suggested_entity_id(self) -> None:
        """Apply suggested entity id for new entities without user overrides."""
        await async_apply_suggested_entity_id(
            self.hass,
            self.entity_id,
            "cover",
            self._attr_name,
            self._attr_suggested_object_id,
        )

    @property
    def available(self) -> bool:
        """Whether this shutter can actually be driven right now.

        Until 22.08.2026 this class had no ``available`` at all, so Home
        Assistant assumed "yes, always" - which is how all 26 covers stayed
        fully operable through the two-hour outage on 21.08.2026 while every
        command was dying in the writer.

        The verdict is the coordinator's; see ``_system_is_available`` for why
        a cover that cannot be reached must say so rather than keep up
        appearances.
        """
        return _system_is_available(self._coordinator)

    @property
    def current_cover_position(self) -> Optional[int]:
        if not self._tc:
            return None
        return self._tc.current_position()

    @property
    def is_opening(self) -> bool:
        if self._tc:
            return self._tc.is_traveling() and self._tc.travel_direction == TravelStatus.DIRECTION_UP
        return self._state == "opening"

    @property
    def is_closing(self) -> bool:
        if self._tc:
            return self._tc.is_traveling() and self._tc.travel_direction == TravelStatus.DIRECTION_DOWN
        return self._state == "closing"

    @property
    def is_closed(self) -> Optional[bool]:
        if self._tc:
            if not self._tc.position_known:
                # None makes Home Assistant render the cover as "unknown"
                # rather than picking open or closed. That is the whole point
                # after a mid-travel connection loss: an honest gap beats a
                # confident guess, and it is what tells the user to run the
                # blind fully once.
                return None
            return self._tc.is_closed()
        return False

    async def async_open_cover(self, **kwargs: Any) -> None:
        _require_connection(self._coordinator, self._attr_name)
        self._cancel_end_stop_cleanup()
        sent = await self._send_command(
            self._up_code, wait_for_completion=True, retries=1
        )
        if not sent:
            _LOGGER.warning("Open command failed for %s", self._attr_name)
            return
        if self._tc:
            self._is_manual_position = False
            self._tc.start_travel_up()
            self._start_auto_updater()
        self._state = "opening"
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        _require_connection(self._coordinator, self._attr_name)
        self._cancel_end_stop_cleanup()
        sent = await self._send_command(
            self._down_code, wait_for_completion=True, retries=1
        )
        if not sent:
            _LOGGER.warning("Close command failed for %s", self._attr_name)
            return
        if self._tc:
            self._is_manual_position = False
            self._tc.start_travel_down()
            self._start_auto_updater()
        self._state = "closing"
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        _require_connection(self._coordinator, self._attr_name)
        self._cancel_end_stop_cleanup()
        sent = await self._send_command(self._stop_code, wait_for_completion=True)
        if not sent:
            _LOGGER.warning("Stop command failed for %s", self._attr_name)
            return
        if self._tc and self._tc.is_traveling():
            self._tc.stop()
        self._stop_auto_updater()
        self._state = None
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        if not self._tc:
            return
        _require_connection(self._coordinator, self._attr_name)
        self._cancel_end_stop_cleanup()
        position = kwargs.get(ATTR_POSITION)
        if position is None:
            return

        if not self._tc.position_known and position not in (0, 100):
            # The position was thrown away after a connection loss and there is
            # nothing to interpolate from - a Nikobus roller relay reports no
            # position, so an intermediate target cannot be computed, only
            # guessed. Say so instead of inventing a starting point.
            raise HomeAssistantError(
                f"Position of '{self._attr_name}' is unknown after a connection "
                "loss. Open or close it fully once to re-establish it."
            )

        current_position = self._tc.current_position()
        if current_position is None:
            current_position = self._tc.position_closed

        sent = True
        if position < current_position and self._tc.travel_direction != TravelStatus.DIRECTION_DOWN:
            sent = await self._send_command(
                self._down_code, wait_for_completion=True, retries=1
            )
            if not sent:
                _LOGGER.warning("Position command failed for %s", self._attr_name)
                return
        elif position > current_position and self._tc.travel_direction != TravelStatus.DIRECTION_UP:
            sent = await self._send_command(
                self._up_code, wait_for_completion=True, retries=1
            )
            if not sent:
                _LOGGER.warning("Position command failed for %s", self._attr_name)
                return

        self._is_manual_position = position not in (0, 100)
        # Fully open/close must run into the end stop so the shutter
        # re-synchronises there; only an intermediate position needs a stop.
        self._pending_target = int(position) if position not in (0, 100) else None
        if position > current_position:
            self._tc.start_travel_up()
            self._state = "opening"
        else:
            self._tc.start_travel_down()
            self._state = "closing"
        self._start_auto_updater()

    async def _send_command(
        self, code: str, wait_for_completion: bool = False, retries: int = 0
    ) -> bool:
        command = f"#N{code}\r#E1"
        return await send_repeated_command(
            self._coordinator,
            command,
            wait_for_completion=wait_for_completion,
            retries=retries,
            use_burst_queue=True,
        )

    async def async_will_remove_from_hass(self) -> None:
        self._stop_auto_updater()
        if self._unsub_coordinator:
            self._unsub_coordinator()
            self._unsub_coordinator = None
        if self._unsub_group_event:
            self._unsub_group_event()
            self._unsub_group_event = None

    def _subscribe_to_availability(self) -> None:
        """Listen for coordinator updates so availability can be repainted.

        These YAML covers are not CoordinatorEntity - they carry their own
        travel calculator and are driven by raw bus codes, not by module state -
        so nothing was pushing them a state update. Without this subscription
        ``available`` would only change the next time the entity happened to
        write its state for some other reason, and an installation that stopped
        answering would go on looking operable for as long as nobody touched it.
        """
        add_listener = getattr(self._coordinator, "async_add_listener", None)
        if add_listener is None or self._unsub_coordinator is not None:
            return
        self._last_available = self.available
        self._unsub_coordinator = add_listener(self._handle_availability_update)

    @callback
    def _handle_availability_update(self) -> None:
        """Write a state only when availability actually flipped."""
        available = self.available
        if available == self._last_available:
            return
        self._last_available = available
        self.async_write_ha_state()

    def _start_auto_updater(self) -> None:
        if not self._unsubscribe_auto_updater:
            self._unsubscribe_auto_updater = async_track_time_interval(
                self.hass, self._auto_updater_hook, timedelta(seconds=0.5)
            )

    def _stop_auto_updater(self) -> None:
        if self._unsubscribe_auto_updater:
            self._unsubscribe_auto_updater()
            self._unsubscribe_auto_updater = None

    def _connection_is_healthy(self) -> bool:
        """Return whether the Nikobus transport is currently usable."""
        connection = getattr(self._coordinator, "nikobus_connection", None)
        return connection is None or connection.is_connected

    def _abandon_travel_estimate(self) -> None:
        """Give up on the position estimate after losing the bus mid-travel.

        Freezing the last estimate would be the worse option here: the drive
        command was already on the bus, so the shutter keeps moving - and the
        stop command that should have ended the run cannot get through either,
        so it very likely runs all the way into an end stop. A frozen 40% would
        then be confidently wrong for as long as nobody looks. Unknown is
        honest, and unlike a wrong number it can be repaired (see
        helpers/travelcalculator.TravelCalculator.start_travel).
        """
        _LOGGER.warning(
            "Nikobus connection lost while %s was travelling - position is now "
            "unknown. Open or close it fully once to re-establish it.",
            self.entity_id or self._attr_name,
        )
        self._tc.mark_position_unknown()
        self._stop_auto_updater()
        self._cancel_end_stop_cleanup()
        self._pending_target = None
        self._suppress_stop_command = False
        self._stop_in_progress = False
        self._state = None
        self.async_write_ha_state()

    @callback
    async def _auto_updater_hook(self, now) -> None:
        if not self._tc:
            return

        if not self._connection_is_healthy():
            # The auto updater only runs while this cover is travelling, so
            # reaching here means the transport died mid-run.
            self._abandon_travel_estimate()
            return

        # Always publish the position, even while a stop command is still waiting
        # in the queue - the shutter is physically still moving during that time.
        self.async_write_ha_state()

        if self._stop_in_progress:
            return

        position = self._tc.current_position()
        if not self._target_reached(position):
            return

        self._stop_in_progress = True
        if position not in (0, 100) and not self._suppress_stop_command:
            sent = await self._send_command(
                self._stop_code, wait_for_completion=True, retries=1
            )
            if not sent:
                self._stop_in_progress = False
                return
        # Stop where the cover actually is now, not where it was when the command
        # was queued.
        self._tc.stop()
        self._stop_auto_updater()
        self._pending_target = None
        self._suppress_stop_command = False
        self._state = None
        self._stop_in_progress = False
        self.async_write_ha_state()
        if position in (0, 100):
            self._schedule_end_stop_cleanup()

    def _cancel_end_stop_cleanup(self) -> None:
        """Drop a pending cleanup because something else happened."""
        self._action_seq += 1
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        self._cleanup_task = None

    def _schedule_end_stop_cleanup(self) -> None:
        """Release the relay a while after the cover settled at an end position."""
        delay = self.hass.data.get(DOMAIN, {}).get(
            CONF_END_STOP_CLEANUP_DELAY, DEFAULT_END_STOP_CLEANUP_DELAY
        )
        if not delay:
            return
        self._action_seq += 1
        seq = self._action_seq
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        self._cleanup_task = self.hass.async_create_task(
            self._run_end_stop_cleanup(seq, delay)
        )

    async def _run_end_stop_cleanup(self, seq: int, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
            if seq != self._action_seq:
                return  # something moved this cover in the meantime
            if self._tc and self._tc.is_traveling():
                return
            position = self._tc.current_position() if self._tc else None
            if position not in (0, 100):
                return
            _LOGGER.info(
                "Releasing relay for %s after %ss at %s%%",
                self.entity_id,
                delay,
                position,
            )
            await self._send_command(self._stop_code, wait_for_completion=True)
        except asyncio.CancelledError:
            raise

    def _target_reached(self, position: Optional[int]) -> bool:
        """Report whether the cover has arrived where it should stop."""
        if position is None:
            # Unknown position (mid-resync, or just discarded). Nothing can be
            # compared against a target, and claiming "reached" would stop a
            # resync run before it ever hit the end stop.
            return False
        target = self._pending_target
        if target is None:
            return self._tc.position_reached()
        if self._state == "opening":
            return position >= target
        if self._state == "closing":
            return position <= target
        return True

    def _direction_for_button(self, address: Optional[str]) -> Optional[str]:
        """Map a bus button address to a travel direction, if it is ours."""
        if not address:
            return None
        address = str(address).upper()
        if address in self._button_up_codes:
            return "opening"
        if address in self._button_down_codes:
            return "closing"
        return None

    async def _handle_bus_button_press(self, event: Any) -> None:
        """Mirror a physical bus button press onto this cover's state.

        The Nikobus module reacts to the button on its own, so nothing is sent
        here - only the internal travel state is updated. A press while the
        cover is moving stops it, regardless of the direction pressed.
        """
        address = event.data.get("address")
        direction = self._direction_for_button(address)
        if direction is None:
            return
        if self._button_press_is_repeat(str(address).upper()):
            return

        if self._is_travelling():
            direction = "stopped"

        _LOGGER.info(
            "Bus button mirrored onto %s: %s", self.entity_id, direction
        )
        self._apply_travel_state(direction, mirror_only=True)

    def _button_press_is_repeat(self, address: str) -> bool:
        """Swallow a repeated delivery of the same press within a short window."""
        now = time.monotonic()
        last = self._last_button_press.get(address)
        self._last_button_press[address] = now
        return last is not None and (now - last) < 0.5

    def _is_travelling(self) -> bool:
        """Report whether this cover is currently moving in either direction."""
        if self._tc:
            return self._tc.is_traveling()
        return self._state in ("opening", "closing")

    def _apply_travel_state(
        self,
        direction: str,
        target_position: Optional[int] = None,
        mirror_only: bool = False,
        group_stop: bool = False,
    ) -> None:
        """Start or stop the travel calculator without sending any command.

        With mirror_only the cover is following a physical bus button, so the
        auto updater is barred from sending a stop code when it reaches an
        intermediate target.
        """
        self._cancel_end_stop_cleanup()

        if direction in ("opening", "closing"):
            self._suppress_stop_command = mirror_only or group_stop
            # With group_stop the group issues one stop for everyone, so this
            # cover must not stop itself at the target.
            self._pending_target = None if group_stop else target_position
        elif direction == "stopped":
            self._suppress_stop_command = False
            self._pending_target = None

        if direction == "opening":
            if self._tc:
                self._is_manual_position = False
                self._tc.start_travel_up()
                self._start_auto_updater()
            self._state = "opening"
        elif direction == "closing":
            if self._tc:
                self._is_manual_position = False
                self._tc.start_travel_down()
                self._start_auto_updater()
            self._state = "closing"
        elif direction == "stopped":
            if self._tc and self._tc.is_traveling():
                self._tc.stop()
            self._stop_auto_updater()
            self._state = None
            # Being stopped by a group at an end position still leaves this
            # cover's relay engaged, so it needs the same cleanup job.
            position = self._tc.current_position() if self._tc else None
            if position in (0, 100):
                self._schedule_end_stop_cleanup()
        else:
            return

        self.async_write_ha_state()

    async def _handle_group_cover_command(self, event: Any) -> None:
        """Handle group cover commands and update local motion state."""
        members = event.data.get("members") or []
        direction = event.data.get("direction")
        target_position = event.data.get("target_position")

        if members:
            members_lower = [str(m).lower() for m in members]
            entity_match = self.entity_id and self.entity_id.lower() in members_lower
            if not entity_match:
                return
        _LOGGER.info(
            "Member cover handling group event: %s (direction=%s, target=%s)",
            self.entity_id,
            direction,
            target_position,
        )

        if direction == "stopped" and event.data.get("send_stop"):
            # No group code covers this cover, so it stops itself.
            position = self._tc.current_position() if self._tc else None
            if position not in (0, 100):
                await self._send_command(
                    self._stop_code, wait_for_completion=True, retries=1
                )

        self._apply_travel_state(
            direction,
            target_position,
            mirror_only=bool(event.data.get("mirror_only")),
            group_stop=bool(event.data.get("group_stop")),
        )


class NikobusYamlGroupCoverEntity(CoverEntity):
    """Cover entity for raw group commands with aggregated member state."""

    def __init__(self, coordinator: NikobusDataCoordinator, config: dict[str, Any]) -> None:
        self._coordinator = coordinator
        self._name = config[CONF_COVER_NAME]
        self._up_code = config[CONF_COVER_UP_CODE]
        self._down_code = config[CONF_COVER_DOWN_CODE]
        self._stop_code = config[CONF_COVER_STOP_CODE]
        self._unique_id = config["unique_id"]
        self._area_name = config.get(CONF_COVER_AREA)
        self._members = config.get("members", [])
        self._button_up_codes = config.get(CONF_BUTTON_UP_CODES) or []
        self._button_down_codes = config.get(CONF_BUTTON_DOWN_CODES) or []
        self._unsub_state = None
        self._unsub_button_event = None
        self._last_button_press: Dict[str, float] = {}
        self._group_stop_task: Optional[asyncio.Task] = None
        # See NikobusYamlCoverEntity for why availability is tracked this way.
        self._unsub_coordinator = None
        self._last_available: Optional[bool] = None
        # A button-only group still needs the entity - it carries the listener and
        # the aggregated state - but it does not belong on any dashboard.
        if config.get(CONF_COVER_HIDDEN):
            self._attr_entity_registry_visible_default = False

        self._attr_name = self._name
        self._attr_unique_id = self._unique_id
        self._attr_suggested_object_id = config.get("suggested_object_id")
        self._attr_device_class = CoverDeviceClass.SHUTTER
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )
        self._attr_is_closed = None
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_current_cover_position = None

    @property
    def available(self) -> bool:
        """Whether this group can actually be driven right now.

        Same reasoning as the single cover: a group whose commands do not reach
        the bus is not operable, and saying otherwise only hides the outage.
        The group is unavailable as a whole because the group command is one
        telegram on one bus - if that bus is gone, no member moves.
        """
        return _system_is_available(self._coordinator)

    async def async_added_to_hass(self) -> None:
        self._subscribe_to_availability()
        await async_assign_area_if_missing(
            self.hass, self.entity_id, self._area_name
        )
        await async_apply_suggested_entity_id(
            self.hass,
            self.entity_id,
            "cover",
            self._attr_name,
            self._attr_suggested_object_id,
        )
        if self._members and self._unsub_state is None:
            self._unsub_state = async_track_state_change_event(
                self.hass, self._members, self._handle_member_state_change
            )
        if (
            self._button_up_codes or self._button_down_codes
        ) and self._unsub_button_event is None:
            # Adding an entity can run twice (a rename re-adds it), so registering
            # unconditionally would leave a leaked second listener behind and make
            # every press act twice.
            self._unsub_button_event = self.hass.bus.async_listen(
                "nikobus_button_pressed", self._handle_bus_button_press
            )
        self._refresh_group_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_coordinator:
            self._unsub_coordinator()
            self._unsub_coordinator = None
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_button_event:
            self._unsub_button_event()
            self._unsub_button_event = None

    def _subscribe_to_availability(self) -> None:
        """Repaint on coordinator updates - see NikobusYamlCoverEntity."""
        add_listener = getattr(self._coordinator, "async_add_listener", None)
        if add_listener is None or self._unsub_coordinator is not None:
            return
        self._last_available = self.available
        self._unsub_coordinator = add_listener(self._handle_availability_update)

    @callback
    def _handle_availability_update(self) -> None:
        """Write a state only when availability actually flipped."""
        available = self.available
        if available == self._last_available:
            return
        self._last_available = available
        self.async_write_ha_state()

    async def _handle_bus_button_press(self, event: Any) -> None:
        """Mirror a physical bus button press onto every member of this group.

        The modules react to the button themselves, so no command is sent here.
        A press while any member is moving stops the group, whatever direction
        was pressed; the next press starts travel in the pressed direction.
        """
        address = event.data.get("address")
        if not address:
            return
        address = str(address).upper()
        if address in self._button_up_codes:
            direction = "opening"
        elif address in self._button_down_codes:
            direction = "closing"
        else:
            return
        if self._button_press_is_repeat(address):
            return

        if self._any_member_state("opening") or self._any_member_state("closing"):
            direction = "stopped"

        _LOGGER.info(
            "Bus button mirrored onto group %s: %s (members=%s)",
            self._attr_name,
            direction,
            self._members,
        )
        self._fire_group_event(direction, mirror_only=True)

    async def async_open_cover(self, **kwargs: Any) -> None:
        _require_connection(self._coordinator, self._attr_name)
        _LOGGER.info(
            "Group cover open requested: %s (members=%s)", self._attr_name, self._members
        )
        self._fire_group_event("opening")
        sent = await self._send_command(
            self._up_code, wait_for_completion=True, retries=1
        )
        if not sent:
            _LOGGER.warning("Open command failed for %s", self._attr_name)
            return

    async def async_close_cover(self, **kwargs: Any) -> None:
        _require_connection(self._coordinator, self._attr_name)
        _LOGGER.info(
            "Group cover close requested: %s (members=%s)", self._attr_name, self._members
        )
        self._fire_group_event("closing")
        sent = await self._send_command(
            self._down_code, wait_for_completion=True, retries=1
        )
        if not sent:
            _LOGGER.warning("Close command failed for %s", self._attr_name)
            return

    async def async_stop_cover(self, **kwargs: Any) -> None:
        _require_connection(self._coordinator, self._attr_name)
        _LOGGER.info(
            "Group cover stop requested: %s (members=%s)", self._attr_name, self._members
        )
        self._fire_group_event("stopped")
        sent = await self._send_command(self._stop_code, wait_for_completion=True)
        if not sent:
            _LOGGER.warning("Stop command failed for %s", self._attr_name)
            return

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        _require_connection(self._coordinator, self._attr_name)
        position = kwargs.get(ATTR_POSITION)
        if position is None:
            return
        if not self._members:
            return

        target = int(position)
        # Determine direction based on average current position of members.
        positions = self._member_positions()
        if not positions:
            return

        avg_position = sum(positions) / len(positions)
        if target == int(round(avg_position)):
            return

        direction = "opening" if target > avg_position else "closing"

        code = self._up_code if direction == "opening" else self._down_code

        if target in (0, 100):
            # Full open/close: let every member run into the end stop. No stop is
            # sent at all, which is what re-synchronises the shutters.
            _LOGGER.info(
                "Group %s to %s%%: end stop, no stop command", self._attr_name, target
            )
            self._fire_group_event(direction)
            if not await self._send_command(code, wait_for_completion=True, retries=1):
                _LOGGER.warning("Position command failed for %s", self._attr_name)
            return

        # Every member is stopped by this group: either together with others via a
        # configured group code, or individually. None of them stops itself.
        self._fire_group_event(direction, target_position=target, group_stop=True)

        sent = await self._send_command(code, wait_for_completion=True, retries=1)
        if not sent:
            _LOGGER.warning("Position command failed for %s", self._attr_name)
            return

        if self._group_stop_task and not self._group_stop_task.done():
            self._group_stop_task.cancel()
        self._group_stop_task = self.hass.async_create_task(
            self._stop_group_at(target, direction)
        )

    async def _stop_group_at(self, target: int, direction: str) -> None:
        """Stop the movement, always using the largest configured group that fits.

        Members do not all arrive together - a short shutter reaches the target
        long before a tall one. Whenever a configured group is completely ready,
        that group is stopped with its own single code; anything not covered by
        a group stops itself.
        """
        try:
            tolerance = self.hass.data.get(DOMAIN, {}).get(
                CONF_GROUP_STOP_TOLERANCE, DEFAULT_GROUP_STOP_TOLERANCE
            )
            pending = set(self._members)
            for _ in range(1200):  # hard stop after ~6 minutes
                if not pending:
                    return
                await asyncio.sleep(0.3)
                soft = {m for m in pending if self._member_reached(m, target, direction, tolerance)}
                hard = {m for m in pending if self._member_reached(m, target, direction, 0)}

                # Group stops first: the biggest configured group fully inside the
                # set that is (nearly) there.
                while True:
                    best = self._best_group_for(soft)
                    if best is None:
                        break
                    name, members, stop_code = best
                    if target not in (0, 100):
                        await self._send_command(
                            stop_code, wait_for_completion=True, retries=1
                        )
                    self._fire_group_event("stopped", members=sorted(members))
                    _LOGGER.info(
                        "Group stop via %s for %d cover(s): %s",
                        name,
                        len(members),
                        sorted(members),
                    )
                    pending -= members
                    soft -= members
                    hard -= members

                # Whatever is really there and not covered by a group stops itself.
                for entity_id in sorted(hard):
                    self._fire_group_event(
                        "stopped", members=[entity_id], send_stop=(target not in (0, 100))
                    )
                    _LOGGER.info("Individual stop for %s", entity_id)
                    pending.discard(entity_id)
            _LOGGER.warning(
                "Group %s: %d cover(s) never reached %s%%",
                self._attr_name,
                len(pending),
                target,
            )
        except asyncio.CancelledError:
            raise

    def _member_reached(
        self, entity_id: str, target: int, direction: str, tolerance: int
    ) -> bool:
        """Report whether a member is at the target, within the given slack."""
        state = self.hass.states.get(entity_id)
        if not state:
            return False
        pos = state.attributes.get(ATTR_POSITION)
        if pos is None:
            pos = state.attributes.get("current_position")
        if pos is None:
            return False
        try:
            pos = float(pos)
        except (TypeError, ValueError):
            return False
        if direction == "opening":
            return pos >= target - tolerance
        return pos <= target + tolerance

    def _best_group_for(self, ready: set) -> Optional[tuple]:
        """Largest configured group whose members are all ready to be stopped."""
        best = None
        for cfg in self.hass.data.get(DOMAIN, {}).get(CONF_GROUP_COVERS, []) or []:
            members = {str(m) for m in (cfg.get("members") or [])}
            if not members or not members <= ready:
                continue
            if best is None or len(members) > len(best[1]):
                best = (cfg.get(CONF_COVER_NAME), members, cfg.get(CONF_COVER_STOP_CODE))
        return best

    def _member_positions(self) -> list[float]:
        """Current positions of all members that report one."""
        out: list[float] = []
        for entity_id in self._members:
            state = self.hass.states.get(entity_id)
            if not state:
                continue
            pos = state.attributes.get(ATTR_POSITION)
            if pos is None:
                pos = state.attributes.get("current_position")
            if pos is not None:
                try:
                    out.append(float(pos))
                except (TypeError, ValueError):
                    pass
        return out

    async def _send_command(
        self, code: str, wait_for_completion: bool = False, retries: int = 0
    ) -> bool:
        command = f"#N{code}\r#E1"
        return await send_repeated_command(
            self._coordinator,
            command,
            wait_for_completion=wait_for_completion,
            retries=retries,
            use_burst_queue=True,
        )

    def _button_press_is_repeat(self, address: str) -> bool:
        """Swallow a repeated delivery of the same press within a short window."""
        now = time.monotonic()
        last = self._last_button_press.get(address)
        self._last_button_press[address] = now
        return last is not None and (now - last) < 0.5

    def _any_member_state(self, target_state: str) -> bool:
        if not self._members:
            return False
        for entity_id in self._members:
            state = self.hass.states.get(entity_id)
            if state and state.state == target_state:
                return True
        return False

    def _fire_group_event(
        self,
        direction: str,
        target_position: Optional[int] = None,
        mirror_only: bool = False,
        group_stop: bool = False,
        members: Optional[list[str]] = None,
        send_stop: bool = False,
    ) -> None:
        data: dict[str, Any] = {
            "members": list(members) if members is not None else self._members,
            "direction": direction,
        }
        if target_position is not None:
            data["target_position"] = target_position
        if mirror_only:
            data["mirror_only"] = True
        if group_stop:
            data["group_stop"] = True
        if send_stop:
            data["send_stop"] = True
        _LOGGER.info(
            "Group cover event fired: %s (members=%s, target=%s)",
            direction,
            self._members,
            target_position,
        )
        self.hass.bus.async_fire("nikobus_group_cover_command", data)

    @callback
    def _handle_member_state_change(self, event: Any) -> None:
        self._refresh_group_state()

    def _refresh_group_state(self) -> None:
        if not self._members:
            self._attr_current_cover_position = None
            self._attr_is_opening = False
            self._attr_is_closing = False
            self._attr_is_closed = None
            self.async_write_ha_state()
            return

        positions: list[float] = []
        opening = False
        closing = False
        closed_count = 0
        open_count = 0
        total = 0

        for entity_id in self._members:
            state = self.hass.states.get(entity_id)
            if not state:
                continue
            total += 1
            if state.state == "opening":
                opening = True
            elif state.state == "closing":
                closing = True
            elif state.state == "closed":
                closed_count += 1
            elif state.state == "open":
                open_count += 1

            pos = state.attributes.get(ATTR_POSITION)
            if pos is None:
                pos = state.attributes.get("current_position")
            if pos is not None:
                try:
                    positions.append(float(pos))
                except (TypeError, ValueError):
                    pass

        if positions:
            self._attr_current_cover_position = int(round(sum(positions) / len(positions)))
        else:
            self._attr_current_cover_position = None

        if opening:
            self._attr_is_opening = True
            self._attr_is_closing = False
            self._attr_is_closed = False
        elif closing:
            self._attr_is_opening = False
            self._attr_is_closing = True
            self._attr_is_closed = False
        else:
            self._attr_is_opening = False
            self._attr_is_closing = False
            if total > 0:
                # Home Assistant convention: a cover counts as closed only when
                # everything is closed, anything else is open. Reporting None for
                # a mixed group would surface as "unknown".
                self._attr_is_closed = closed_count == total
            else:
                self._attr_is_closed = None

        self.async_write_ha_state()
