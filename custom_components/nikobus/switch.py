"""Switch platform for the Nikobus integration with module-level devices."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import area_registry as ar, entity_registry as er

from .const import (
    DOMAIN,
    BRAND,
    CONF_COVERS,
    CONF_COVER_AS_SWITCH,
    CONF_COVER_NAME,
    CONF_COVER_UP_CODE,
    CONF_COVER_DOWN_CODE,
    CONF_COVER_STOP_CODE,
    CONF_COVER_SIGNAL_REPEAT,
    CONF_COVER_AREA,
)
from .coordinator import NikobusDataCoordinator
from .entity import NikobusEntity
from .exceptions import NikobusError

_LOGGER = logging.getLogger(__name__)

HUB_IDENTIFIER = "nikobus_hub"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Nikobus switch entities from a config entry."""
    _LOGGER.debug("Setting up Nikobus switch entities (modules).")

    coordinator: NikobusDataCoordinator = entry.runtime_data
    device_registry = dr.async_get(hass)
    entities: list[SwitchEntity] = []

    # Process standard switch_module entities
    const_switch_modules: dict[str, Any] = coordinator.dict_module_data.get(
        "switch_module", {}
    )
    for address, switch_module_data in const_switch_modules.items():
        module_desc = switch_module_data.get("description", f"Module {address}")
        model = switch_module_data.get("model", "Unknown Module Model")

        _register_nikobus_module_device(
            device_registry=device_registry,
            entry=entry,
            module_address=address,
            module_name=module_desc,
            module_model=model,
        )

        for channel_index, channel_info in enumerate(
            switch_module_data.get("channels", []), start=1
        ):
            if channel_info["description"].startswith("not_in_use"):
                continue

            entities.append(
                NikobusSwitchEntity(
                    coordinator=coordinator,
                    address=address,
                    channel=channel_index,
                    channel_description=channel_info["description"],
                    module_name=module_desc,
                    module_model=model,
                )
            )

    # Process roller_module channels marked with use_as_switch
    roller_switch_data = hass.data.setdefault(DOMAIN, {}).get("switch_entities", [])
    for switch_data in roller_switch_data:
        entities.append(
            NikobusSwitchCoverEntity(
                coordinator=switch_data["coordinator"],
                address=switch_data["address"],
                channel=switch_data["channel"],
                channel_description=switch_data["channel_description"],
                module_desc=switch_data["module_desc"],
                module_model=switch_data["module_model"],
            )
        )

    # Process YAML-defined covers configured as switches
    yaml_covers = hass.data.get(DOMAIN, {}).get(CONF_COVERS, [])
    for cover_config in yaml_covers:
        direction = cover_config.get(CONF_COVER_AS_SWITCH)
        if direction in ("up", "down"):
            entities.append(
                NikobusYamlCoverSwitchEntity(
                    coordinator=coordinator,
                    config=cover_config,
                )
            )

    async_add_entities(entities)
    _LOGGER.debug("Added %d Nikobus switch entities.", len(entities))


def _register_nikobus_module_device(
    device_registry: dr.DeviceRegistry,
    entry: ConfigEntry,
    module_address: str,
    module_name: str,
    module_model: str,
) -> None:
    """Register a single Nikobus module as a child device in the device registry."""
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, module_address)},
        manufacturer=BRAND,
        name=module_name,
        model=module_model,
        via_device=(DOMAIN, HUB_IDENTIFIER),
    )


class NikobusSwitchCoverEntity(NikobusEntity, SwitchEntity):
    """A switch entity for roller modules using `use_as_switch`."""

    def __init__(
        self,
        coordinator: NikobusDataCoordinator,
        address: str,
        channel: int,
        channel_description: str,
        module_desc: str,
        module_model: str,
    ) -> None:
        """Initialize the switch entity for a roller module."""
        super().__init__(coordinator, address, module_desc, module_model)
        self.coordinator = coordinator
        self.address = address
        self.channel = channel
        self.channel_description = channel_description

        self._attr_name = f"{module_desc} - {channel_description}"
        self._attr_unique_id = f"{DOMAIN}_switch_{self.address}_{self.channel}"

    @property
    def is_on(self) -> bool:
        """Return True if the simulated switch (cover open) is on."""
        return self.coordinator.get_cover_state(self.address, self.channel) == 0x01

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Simulate turning on the switch (opening cover)."""
        _LOGGER.debug("Turning ON (simulating open) for %s", self.channel_description)
        try:
            await self.coordinator.api.open_cover(self.address, self.channel)
        except NikobusError as err:
            _LOGGER.error(
                "Failed to open cover for %s: %s",
                self.channel_description,
                err,
                exc_info=True,
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Simulate turning off the switch (stopping cover)."""
        _LOGGER.debug("Turning OFF (simulating stop) for %s", self.channel_description)
        try:
            await self.coordinator.api.stop_cover(
                self.address, self.channel, direction="closing"
            )
        except NikobusError as err:
            _LOGGER.error(
                "Failed to stop cover for %s: %s",
                self.channel_description,
                err,
                exc_info=True,
            )


class NikobusSwitchEntity(NikobusEntity, SwitchEntity):
    """A switch entity representing one channel on a Nikobus module."""

    def __init__(
        self,
        coordinator: NikobusDataCoordinator,
        address: str,
        channel: int,
        channel_description: str,
        module_name: str,
        module_model: str,
    ) -> None:
        """Initialize the switch entity."""
        super().__init__(coordinator, address, module_name, module_model)
        self._address = address
        self._channel = channel
        self._channel_description = channel_description

        self._attr_unique_id = f"{DOMAIN}_switch_{self._address}_{self._channel}"
        self._attr_name = channel_description
        self._is_on: bool | None = None

    @property
    def is_on(self) -> bool:
        """Return True if the switch is on."""
        return self._is_on if self._is_on is not None else self._read_current_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle new data from the coordinator."""
        self._is_on = None
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn the switch on."""
        self._is_on = True
        self.async_write_ha_state()
        try:
            await self.coordinator.api.turn_on_switch(self._address, self._channel)
        except NikobusError as err:
            _LOGGER.error(
                "Failed to turn on switch (module=%s, channel=%d): %s",
                self._address,
                self._channel,
                err,
            )
            self._is_on = None
            self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the switch off."""
        self._is_on = False
        self.async_write_ha_state()
        try:
            await self.coordinator.api.turn_off_switch(self._address, self._channel)
        except NikobusError as err:
            _LOGGER.error(
                "Failed to turn off switch (module=%s, channel=%d): %s",
                self._address,
                self._channel,
                err,
            )
            self._is_on = None
            self.async_write_ha_state()

    def _read_current_state(self) -> bool:
        """Fetch real-time state from the coordinator."""
        try:
            return self.coordinator.get_switch_state(self._address, self._channel)
        except NikobusError as err:
            _LOGGER.error(
                "Failed to get state for switch (module=%s, channel=%d): %s",
                self._address,
                self._channel,
                err,
            )
            return False


class NikobusYamlCoverSwitchEntity(SwitchEntity):
    """Switch entity that triggers a YAML-defined cover direction."""

    def __init__(self, coordinator: NikobusDataCoordinator, config: dict[str, Any]) -> None:
        self.coordinator = coordinator
        self._name = config[CONF_COVER_NAME]
        self._direction = config[CONF_COVER_AS_SWITCH]
        self._up_code = config[CONF_COVER_UP_CODE]
        self._down_code = config[CONF_COVER_DOWN_CODE]
        self._stop_code = config[CONF_COVER_STOP_CODE]
        self._unique_id = f"{config['unique_id']}_switch"
        self._is_on = False
        self._area_name = config.get(CONF_COVER_AREA)

        self._attr_name = self._name
        self._attr_unique_id = self._unique_id
        self._attr_suggested_object_id = config.get("suggested_object_id")

    async def async_added_to_hass(self) -> None:
        if not self._area_name or not self.entity_id:
            return
        area_reg = ar.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        area = area_reg.async_get_area_by_name(self._area_name)
        if area is None:
            area = area_reg.async_get_or_create(self._area_name)
        for _ in range(5):
            entry = ent_reg.async_get(self.entity_id)
            if entry is not None:
                if entry.area_id is None:
                    ent_reg.async_update_entity(self.entity_id, area_id=area.id)
                break
            await asyncio.sleep(0.2)

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        code = self._up_code if self._direction == "up" else self._down_code
        await self._send_command(code)
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send_command(self._stop_code)
        self._is_on = False
        self.async_write_ha_state()

    async def _send_command(self, code: str) -> None:
        command = f"#N{code}\r#E1"
        repeat = (
            self.coordinator.hass.data.get(DOMAIN, {})
            .get(CONF_COVER_SIGNAL_REPEAT, 1)
        )
        try:
            repeat_count = max(1, int(repeat))
        except (TypeError, ValueError):
            repeat_count = 1

        for _ in range(repeat_count):
            await self.coordinator.nikobus_command.queue_command(command)
