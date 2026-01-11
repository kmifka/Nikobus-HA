"""Helpers for Home Assistant entity registry interactions."""

from __future__ import annotations

import asyncio

from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.util import slugify


async def async_assign_area_if_missing(
    hass, entity_id: str | None, area_name: str | None, retries: int = 5
) -> None:
    """Assign an entity to an area if it has none yet."""
    if not entity_id or not area_name:
        return
    area_reg = ar.async_get(hass)
    ent_reg = er.async_get(hass)
    area = area_reg.async_get_area_by_name(area_name)
    if area is None:
        area = area_reg.async_get_or_create(area_name)
    for _ in range(retries):
        entry = ent_reg.async_get(entity_id)
        if entry is not None:
            if entry.area_id is None:
                ent_reg.async_update_entity(entity_id, area_id=area.id)
            break
        await asyncio.sleep(0.2)


async def async_apply_suggested_entity_id(
    hass,
    entity_id: str | None,
    domain: str | None,
    name: str | None,
    suggested_object_id: str | None,
    retries: int = 5,
) -> None:
    """Apply suggested entity id when the entity is still on its default id."""
    if not entity_id or not suggested_object_id or not name:
        return
    if domain is None:
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    if not domain:
        return
    ent_reg = er.async_get(hass)
    desired_entity_id = f"{domain}.{suggested_object_id}"
    for _ in range(retries):
        entry = ent_reg.async_get(entity_id)
        if entry is not None:
            default_entity_id = f"{domain}.{slugify(name)}"
            default_prefix = f"{default_entity_id}_"
            if (
                entry.name is None
                and (
                    entry.entity_id == default_entity_id
                    or entry.entity_id.startswith(default_prefix)
                )
                and entry.entity_id != desired_entity_id
            ):
                ent_reg.async_update_entity(
                    entry.entity_id, new_entity_id=desired_entity_id
                )
            break
        await asyncio.sleep(0.2)
