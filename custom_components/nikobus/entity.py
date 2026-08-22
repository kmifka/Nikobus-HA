"""Shared entity helpers for the Nikobus integration."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BRAND, DOMAIN, HUB_IDENTIFIER
from .coordinator import NikobusDataCoordinator


def hub_device_info() -> DeviceInfo:
    """DeviceInfo for the Nikobus bridge (hub).

    Single source of truth for the bridge device: the ``via_device`` parent of
    the module devices and the device the bridge-level entities (currently the
    connection sensor) attach to.

    Taken from upstream 3.x, where it is also shared with the hub registration
    in ``__init__``. Here it is NOT - ``__init__._register_hub_device`` still
    builds its own dict with the same three values, because that function sits
    in the file that also derives the YAML cover unique_ids and that file is
    off limits. The values below must therefore stay byte-identical to it, or
    Home Assistant will rename the bridge device on the next start.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, HUB_IDENTIFIER)},
        name="Nikobus Bridge",
        manufacturer=BRAND,
        model="PC-Link Bridge",
    )


class NikobusEntity(CoordinatorEntity):
    """Base entity for Nikobus devices with common device info."""

    def __init__(
        self,
        coordinator: NikobusDataCoordinator,
        address: str,
        name: str,
        model: str,
    ) -> None:
        """Initialize the entity with shared device information."""
        super().__init__(coordinator)
        self._address = address
        self._device_name = name
        self._device_model = model

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for Home Assistant."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            name=self._device_name,
            manufacturer=BRAND,
            model=self._device_model,
        )


def device_entry_diagnostics(device: DeviceEntry) -> dict[str, Any]:
    """Return diagnostics data for a Nikobus device entry."""
    return {
        "id": device.id,
        "name": device.name,
        "model": device.model,
        "manufacturer": device.manufacturer,
        "sw_version": device.sw_version,
        "identifiers": sorted(device.identifiers),
    }
