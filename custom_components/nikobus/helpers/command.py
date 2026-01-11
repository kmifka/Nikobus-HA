"""Command helpers for repeated Nikobus actions."""

from __future__ import annotations

import asyncio
import logging

from ..const import (
    COMMAND_ACK_WAIT_TIMEOUT,
    COMMAND_EXECUTION_DELAY,
    COMMAND_REPEAT_BURST_DELAY,
    CONF_COVER_SIGNAL_REPEAT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def get_repeat_count(hass) -> int:
    """Return the configured repeat count for cover-related commands."""
    repeat = hass.data.get(DOMAIN, {}).get(CONF_COVER_SIGNAL_REPEAT, 1)
    try:
        return max(1, int(repeat))
    except (TypeError, ValueError):
        return 1


async def send_repeated_command(
    coordinator,
    command: str,
    wait_for_completion: bool = False,
    timeout: float | None = None,
    retries: int = 0,
    use_burst_queue: bool = False,
) -> bool:
    """Send a command to Nikobus, repeating it if configured."""
    repeat_count = get_repeat_count(coordinator.hass)
    attempt = 0

    while True:
        attempt += 1
        if not wait_for_completion:
            if use_burst_queue and repeat_count > 1:
                await coordinator.nikobus_command.queue_command_batch(
                    [command] * repeat_count
                )
            else:
                for _ in range(repeat_count):
                    await coordinator.nikobus_command.queue_command(command)
            return True

        loop = coordinator.hass.loop
        done = loop.create_future()

        async def _completion_handler() -> None:
            if not done.done():
                done.set_result(True)

        if use_burst_queue and repeat_count > 1:
            await coordinator.nikobus_command.queue_command_batch(
                [command] * repeat_count, completion_handler=_completion_handler
            )
        else:
            for idx in range(repeat_count):
                handler = _completion_handler if idx == repeat_count - 1 else None
                await coordinator.nikobus_command.queue_command(
                    command, completion_handler=handler
                )

        if timeout is None:
            if use_burst_queue and repeat_count > 1:
                timeout = COMMAND_ACK_WAIT_TIMEOUT + (
                    COMMAND_REPEAT_BURST_DELAY * repeat_count
                )
            else:
                timeout = COMMAND_ACK_WAIT_TIMEOUT + (
                    COMMAND_EXECUTION_DELAY * repeat_count
                )

        try:
            await asyncio.wait_for(done, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Timeout waiting for command completion (command=%s, timeout=%.1fs, attempt=%d/%d)",
                command,
                timeout,
                attempt,
                retries + 1,
            )
            if attempt > retries + 1:
                return False
