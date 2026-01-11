"""Helpers for filtering button entities."""

from __future__ import annotations

from ..const import (
    CONF_COVERS,
    CONF_COVER_UP_CODE,
    CONF_COVER_DOWN_CODE,
    CONF_COVER_STOP_CODE,
    DOMAIN,
)


def get_excluded_button_addresses(hass) -> set[str]:
    """Return addresses that should not create button/sensor entities."""
    excluded: set[str] = set()
    for cover in hass.data.get(DOMAIN, {}).get(CONF_COVERS, []):
        for key in (
            CONF_COVER_UP_CODE,
            CONF_COVER_DOWN_CODE,
            CONF_COVER_STOP_CODE,
        ):
            code = cover.get(key)
            if code:
                excluded.add(code.upper())
    return excluded
