"""Command helpers for repeated Nikobus actions."""

from __future__ import annotations

from ..const import CONF_COVER_SIGNAL_REPEAT, DOMAIN


def get_repeat_count(hass) -> int:
    """Return the configured repeat count for cover-related commands."""
    repeat = hass.data.get(DOMAIN, {}).get(CONF_COVER_SIGNAL_REPEAT, 1)
    try:
        return max(1, int(repeat))
    except (TypeError, ValueError):
        return 1


async def send_repeated_command(coordinator, command: str) -> None:
    """Send a command to Nikobus, repeating it if configured."""
    repeat_count = get_repeat_count(coordinator.hass)
    for _ in range(repeat_count):
        await coordinator.nikobus_command.queue_command(command)
