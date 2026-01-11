"""The Nikobus integration."""
from __future__ import annotations

import logging
import hashlib
from typing import Final

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.components import (
    switch,
    light,
    cover,
    binary_sensor,
    button,
    scene,
)
from .nkbconnect import NikobusConnect
from .exceptions import NikobusConnectionError

from homeassistant.util import slugify

from .const import (
    DOMAIN,
    CONF_CONNECTION_STRING,
    CONF_COVERS,
    CONF_COVER_NAME,
    CONF_COVER_UP_CODE,
    CONF_COVER_DOWN_CODE,
    CONF_COVER_STOP_CODE,
    CONF_TRAVEL_UP_TIME,
    CONF_TRAVEL_DOWN_TIME,
    CONF_COVER_SIGNAL_REPEAT,
    CONF_COVER_AS_SWITCH,
    CONF_COVER_AREA,
)
from .coordinator import NikobusDataCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: Final[list[str]] = [
    cover.DOMAIN,
    switch.DOMAIN,
    light.DOMAIN,
    binary_sensor.DOMAIN,
    button.DOMAIN,
    scene.DOMAIN,
]

SCAN_MODULE_SCHEMA = vol.Schema({vol.Optional("module_address", default=""): cv.string})
HUB_IDENTIFIER: Final[str] = "nikobus_hub"

_CODE_REGEX = r"^[0-9A-Fa-f]{6}$"

COVER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_COVER_NAME): cv.string,
        vol.Required(CONF_COVER_UP_CODE): vol.Match(_CODE_REGEX),
        vol.Required(CONF_COVER_DOWN_CODE): vol.Match(_CODE_REGEX),
        vol.Required(CONF_COVER_STOP_CODE): vol.Match(_CODE_REGEX),
        vol.Optional(CONF_TRAVEL_UP_TIME): vol.Coerce(float),
        vol.Optional(CONF_TRAVEL_DOWN_TIME): vol.Coerce(float),
        vol.Optional(CONF_COVER_AS_SWITCH): vol.In(["up", "down"]),
        vol.Optional(CONF_COVER_AREA): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_COVERS): vol.All(cv.ensure_list, [COVER_SCHEMA]),
                vol.Optional(CONF_COVER_SIGNAL_REPEAT, default=1): cv.positive_int,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

def _normalize_yaml_covers(raw_covers: list[dict]) -> list[dict]:
    """Normalize YAML cover definitions and ensure stable unique IDs."""
    normalized: list[dict] = []

    area_counters: dict[str, int] = {}
    default_counter = 0

    for cover in raw_covers:
        up_code = cover[CONF_COVER_UP_CODE].upper()
        down_code = cover[CONF_COVER_DOWN_CODE].upper()
        stop_code = cover[CONF_COVER_STOP_CODE].upper()
        digest = hashlib.sha1(
            ":".join(sorted([up_code, down_code, stop_code])).encode("utf-8")
        ).hexdigest()[:12]
        unique_id = f"{DOMAIN}_yaml_cover_{digest}"

        area_name = cover.get(CONF_COVER_AREA)
        raw_name = cover.get(CONF_COVER_NAME)

        if raw_name:
            name = raw_name
        else:
            default_counter += 1
            name = f"Jalousie {default_counter}"

        if area_name:
            area_counters[area_name] = area_counters.get(area_name, 0) + 1
            suggested_object_id = slugify(
                f"{area_name} {area_counters[area_name]}"
            )
        else:
            suggested_object_id = slugify(name)

        raw_up_time = cover.get(CONF_TRAVEL_UP_TIME)
        raw_down_time = cover.get(CONF_TRAVEL_DOWN_TIME)
        if raw_up_time is None or raw_down_time is None:
            travel_up_time = None
            travel_down_time = None
        else:
            travel_up_time = float(raw_up_time)
            travel_down_time = float(raw_down_time)

        normalized.append(
            {
                CONF_COVER_NAME: name,
                CONF_COVER_UP_CODE: up_code,
                CONF_COVER_DOWN_CODE: down_code,
                CONF_COVER_STOP_CODE: stop_code,
                CONF_TRAVEL_UP_TIME: travel_up_time,
                CONF_TRAVEL_DOWN_TIME: travel_down_time,
                CONF_COVER_AS_SWITCH: cover.get(CONF_COVER_AS_SWITCH),
                CONF_COVER_AREA: area_name,
                "suggested_object_id": suggested_object_id,
                "unique_id": unique_id,
            }
        )

    return normalized


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Nikobus component from YAML."""
    if DOMAIN not in config:
        return True

    hass.data.setdefault(DOMAIN, {})

    raw_covers = config[DOMAIN].get(CONF_COVERS)
    if raw_covers:
        hass.data[DOMAIN][CONF_COVERS] = _normalize_yaml_covers(raw_covers)

    hass.data[DOMAIN][CONF_COVER_SIGNAL_REPEAT] = config[DOMAIN].get(
        CONF_COVER_SIGNAL_REPEAT, 1
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Nikobus integration from a config entry (single-instance)."""
    _LOGGER.debug("Starting setup of Nikobus (single-instance)")

    try:
        connection = NikobusConnect(entry.data[CONF_CONNECTION_STRING])
        await connection.ping()
    except NikobusConnectionError as err:
        _LOGGER.warning("Nikobus interface not ready: %s", err)
        raise ConfigEntryNotReady from err   

    # Create and store the coordinator (which may start the event listener)
    coordinator = NikobusDataCoordinator(hass, entry)
    entry.runtime_data = coordinator

    # Attempt to connect the coordinator
    try:
        await coordinator.connect()
    except HomeAssistantError as err:
        _LOGGER.error("Error connecting to Nikobus: %s", err)
        raise ConfigEntryNotReady from err

    _register_hub_device(hass, entry)

    async def handle_module_discovery(call: ServiceCall) -> None:
        """Manually trigger device discovery."""
        module_address = (call.data.get("module_address", "") or "").strip().upper()
        _LOGGER.info(
            "Starting manual Nikobus discovery with module_address: %s", module_address
        )
        await coordinator.discover_devices(module_address)

    if not hass.services.has_service(DOMAIN, "query_module_inventory"):
        hass.services.async_register(
            DOMAIN,
            "query_module_inventory",
            handle_module_discovery,
            SCAN_MODULE_SCHEMA,
        )

    # Forward the setup to all configured platforms.
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _LOGGER.debug("Successfully forwarded setup to Nikobus platforms")
    except Exception as err:
        _LOGGER.error("Error forwarding setup to Nikobus platforms: %s", err)
        return False

    await _async_cleanup_orphan_entities(hass, entry, coordinator)

    _LOGGER.info("Nikobus (single-instance) setup complete.")
    return True

def _register_hub_device(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the Nikobus bridge (hub) as a device in Home Assistant."""
    device_registry = dr.async_get(hass)
    if device_registry.async_get_device(identifiers={(DOMAIN, HUB_IDENTIFIER)}):
        _LOGGER.debug("Nikobus hub device already exists in registry.")
        return

    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, HUB_IDENTIFIER)},
        manufacturer="Niko",
        name="Nikobus Bridge",
        model="PC-Link Bridge",
    )
    _LOGGER.debug("Nikobus hub registered in Home Assistant device registry.")

async def _async_cleanup_orphan_entities(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: NikobusDataCoordinator
) -> None:
    """Remove entities & devices that no longer exist in current Nikobus config."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    valid_entity_ids = coordinator.get_known_entity_unique_ids()
    _LOGGER.debug("Valid Nikobus entity IDs: %s", valid_entity_ids)

    entities = [
        entity
        for entity in ent_reg.entities.values()
        if entity.config_entry_id == entry.entry_id and entity.platform == DOMAIN
    ]

    for entity in entities:
        if entity.unique_id not in valid_entity_ids:
            _LOGGER.warning(
                "Removing orphan Nikobus entity: %s (unique_id=%s)",
                entity.entity_id,
                entity.unique_id,
            )
            ent_reg.async_remove(entity.entity_id)

    # Rebuild after entity removals
    ent_reg = er.async_get(hass)
    hub_identifier = (DOMAIN, HUB_IDENTIFIER)

    devices_with_entities = {
        entity.device_id
        for entity in ent_reg.entities.values()
        if entity.config_entry_id == entry.entry_id
        and entity.platform == DOMAIN
        and entity.device_id
    }

    for device in list(dev_reg.devices.values()):
        if entry.entry_id not in device.config_entries:
            continue
        if hub_identifier in device.identifiers:
            continue
        if device.id not in devices_with_entities:
            _LOGGER.warning(
                "Removing orphan Nikobus device: %s (id=%s, identifiers=%s)",
                device.name,
                device.id,
                device.identifiers,
            )
            dev_reg.async_remove_device(device.id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the single Nikobus integration entry."""
    _LOGGER.debug("Unloading Nikobus (single-instance)")
    coordinator = entry.runtime_data

    # Cancel the scheduled discovery if it exists.
    if hasattr(coordinator, "remove_listener"):
        coordinator.remove_listener()

    if coordinator and hasattr(coordinator, "stop"):
        try:
            await coordinator.stop()
        except Exception as err:
            _LOGGER.error("Error stopping Nikobus coordinator: %s", err)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        _LOGGER.error("Failed to unload Nikobus platforms.")
        return False

    if hass.services.has_service(DOMAIN, "query_module_inventory"):
        hass.services.async_remove(DOMAIN, "query_module_inventory")

    _LOGGER.info("Nikobus integration fully unloaded.")
    return True
