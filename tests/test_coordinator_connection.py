"""Tests for the coordinator's connection supervision and blackout detection."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from conftest import FakeEntry

from custom_components.nikobus.const import (
    CONF_CONNECTION_STRING,
    CONF_PRIOR_GEN3,
    DOMAIN,
)
from custom_components.nikobus.coordinator import NikobusDataCoordinator


def _make_coordinator(hass) -> NikobusDataCoordinator:
    entry = FakeEntry(
        {
            CONF_CONNECTION_STRING: "/dev/ttyUSB0",
            CONF_PRIOR_GEN3: True,
        }
    )
    return NikobusDataCoordinator(hass, entry)


class _RecordingCommand:
    """Command-handler stand-in that records the lifecycle calls."""

    def __init__(self, results=None) -> None:
        self.stopped = 0
        self.started = 0
        self.was_reset = 0
        self._results = list(results or [])

    async def stop(self) -> None:
        self.stopped += 1

    async def start(self) -> None:
        self.started += 1

    def reset(self) -> None:
        self.was_reset += 1

    async def get_output_state(self, _address, _group):
        if not self._results:
            raise asyncio.TimeoutError("bus silent")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _RecordingListener:
    """Listener stand-in that records the lifecycle calls."""

    def __init__(self) -> None:
        self.stopped = 0
        self.started = 0
        self.was_reset = 0
        self.on_connection_lost = None

    async def stop(self) -> None:
        self.stopped += 1

    async def start(self) -> None:
        self.started += 1

    def reset(self) -> None:
        self.was_reset += 1


# ---------------------------------------------------------------------------
# connection_status
# ---------------------------------------------------------------------------
async def test_connection_status_follows_the_transport(hass):
    """disconnected -> connected -> reconnecting -> connected."""
    coordinator = _make_coordinator(hass)
    connection = coordinator.nikobus_connection

    assert coordinator.connection_status == "disconnected"

    connection._is_connected = True
    assert coordinator.connection_status == "connected"

    connection._is_connected = False
    assert coordinator.connection_status == "disconnected"

    async def _never() -> None:
        await asyncio.Event().wait()

    coordinator._reconnect_task = asyncio.create_task(_never())
    await asyncio.sleep(0)
    assert coordinator.connection_status == "reconnecting"

    # A live transport wins over a still-running reconnect task, so the state
    # cannot get stuck on "reconnecting" after the connection is back.
    connection._is_connected = True
    assert coordinator.connection_status == "connected"

    coordinator._reconnect_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await coordinator._reconnect_task


async def test_handle_connection_lost_is_idempotent(hass):
    """A second connection-lost notification for the same outage is a no-op.

    Blackout detection and the listener can both report the same failure. The
    second report must not stop the command handler again while the first
    reconnect is already running its handshake.
    """
    coordinator = _make_coordinator(hass)
    command = _RecordingCommand()
    listener = _RecordingListener()
    coordinator.nikobus_command = command
    coordinator.nikobus_listener = listener

    blocker = asyncio.Event()

    async def _hang(**_kwargs) -> int:
        await blocker.wait()
        return 1

    coordinator.nikobus_connection.reconnect_with_backoff = _hang

    await coordinator._handle_connection_lost()
    await asyncio.sleep(0)
    first_task = coordinator._reconnect_task

    await coordinator._handle_connection_lost()
    await coordinator._handle_connection_lost()
    await asyncio.sleep(0)

    assert command.stopped == 1, "teardown must not run a second time"
    assert listener.stopped == 1
    assert coordinator._reconnect_task is first_task

    # The loop swallows its own cancellation (see _reconnect_loop) and returns,
    # so awaiting it must simply finish rather than propagate.
    coordinator._stopping = True
    first_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first_task
    assert first_task.done()


async def test_handle_connection_lost_pushes_a_state_update(hass):
    """The sensor is told immediately, not after the first retry."""
    coordinator = _make_coordinator(hass)
    coordinator.nikobus_command = _RecordingCommand()
    coordinator.nikobus_listener = _RecordingListener()

    async def _hang(**_kwargs) -> int:
        await asyncio.Event().wait()
        return 1

    coordinator.nikobus_connection.reconnect_with_backoff = _hang

    before = coordinator.listener_updates
    await coordinator._handle_connection_lost()
    assert coordinator.listener_updates > before

    coordinator._stopping = True
    coordinator._reconnect_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await coordinator._reconnect_task


async def test_reconnect_loop_restarts_the_subsystems(hass):
    """After a successful reconnect the workers come back and state is cleared."""
    coordinator = _make_coordinator(hass)
    command = _RecordingCommand()
    listener = _RecordingListener()
    coordinator.nikobus_command = command
    coordinator.nikobus_listener = listener
    coordinator._reconnect_attempts = 7

    async def _succeed(**kwargs):
        on_attempt = kwargs.get("on_attempt")
        if on_attempt:
            on_attempt(1, 5.0)
        coordinator.nikobus_connection._is_connected = True
        return 3

    coordinator.nikobus_connection.reconnect_with_backoff = _succeed

    async def _no_data():
        return True

    coordinator._async_update_data = _no_data

    await coordinator._reconnect_loop()

    assert command.was_reset == 1
    assert listener.was_reset == 1
    assert command.started == 1
    assert listener.started == 1
    assert listener.on_connection_lost == coordinator._handle_connection_lost
    assert coordinator._reconnect_attempts == 0
    assert coordinator.last_connected is not None
    assert coordinator.connection_status == "connected"


# ---------------------------------------------------------------------------
# Blackout detection
# ---------------------------------------------------------------------------
async def test_total_blackout_triggers_the_reconnect_path(hass):
    """Every poll in a cycle failing means the bus is silent - reconnect.

    Before this, _refresh_module_type swallowed each failure and
    _async_update_data returned True either way, so a cycle in which nothing
    answered was indistinguishable from a healthy one.
    """
    coordinator = _make_coordinator(hass)
    coordinator.nikobus_command = _RecordingCommand(results=[])  # every call raises
    coordinator.dict_module_data = {
        "switch_module": {
            "0E6C": {"channels": [{}] * 6},
            "1F7D": {"channels": [{}] * 6},
        }
    }
    coordinator.nikobus_module_states = {"0E6C": bytearray(6), "1F7D": bytearray(6)}

    lost = {"n": 0}

    async def _record_lost() -> None:
        lost["n"] += 1

    coordinator._handle_connection_lost = _record_lost

    await coordinator._refresh_nikobus_data()
    for task in list(hass.background_tasks):
        await task

    assert lost["n"] == 1


async def test_partial_failure_does_not_trigger_a_reconnect(hass):
    """One deaf module is not a dead bus, and must not tear the transport down."""
    coordinator = _make_coordinator(hass)
    # Module A answers, module B times out.
    coordinator.nikobus_command = _RecordingCommand(
        results=["0000000000FF", asyncio.TimeoutError("deaf")]
    )
    coordinator.dict_module_data = {
        "switch_module": {
            "0E6C": {"channels": [{}] * 6},
            "1F7D": {"channels": [{}] * 6},
        }
    }
    coordinator.nikobus_module_states = {"0E6C": bytearray(6), "1F7D": bytearray(6)}

    lost = {"n": 0}

    async def _record_lost() -> None:
        lost["n"] += 1

    coordinator._handle_connection_lost = _record_lost

    await coordinator._refresh_nikobus_data()
    for task in list(hass.background_tasks):
        await task

    assert lost["n"] == 0


async def test_refresh_module_type_returns_polled_and_failed_counts(hass):
    """The counts the blackout check is built on."""
    coordinator = _make_coordinator(hass)
    coordinator.nikobus_command = _RecordingCommand(
        results=["0000000000FF", asyncio.TimeoutError("deaf")]
    )
    coordinator.nikobus_module_states = {"0E6C": bytearray(6), "1F7D": bytearray(6)}

    polled, failed = await coordinator._refresh_module_type(
        {"0E6C": {"channels": [{}] * 6}, "1F7D": {"channels": [{}] * 6}}
    )

    assert (polled, failed) == (2, 1)


async def test_blackout_check_is_skipped_while_stopping(hass):
    """Shutting down produces the same symptom; it must not start a reconnect."""
    coordinator = _make_coordinator(hass)
    coordinator.nikobus_command = _RecordingCommand(results=[])
    coordinator.dict_module_data = {"switch_module": {"0E6C": {"channels": [{}] * 6}}}
    coordinator.nikobus_module_states = {"0E6C": bytearray(6)}
    coordinator._stopping = True

    lost = {"n": 0}

    async def _record_lost() -> None:
        lost["n"] += 1

    coordinator._handle_connection_lost = _record_lost

    await coordinator._refresh_nikobus_data()
    for task in list(hass.background_tasks):
        await task

    assert lost["n"] == 0


# ---------------------------------------------------------------------------
# Entity identity guard
# ---------------------------------------------------------------------------
def test_connection_sensor_unique_id_is_known_and_collision_free(hass):
    """The new unique_id survives orphan cleanup and touches nothing existing.

    ``__init__._async_cleanup_orphan_entities`` deletes every entity of this
    config entry whose unique_id is not in ``get_known_entity_unique_ids()``.
    And the 26 identities recorded in entity_identities_2026-08-22.tsv are the
    hard boundary of this work - the sensor must not collide with any of them.
    """
    import csv
    from pathlib import Path

    coordinator = _make_coordinator(hass)
    hass.data[DOMAIN] = {}
    known = coordinator.get_known_entity_unique_ids()

    assert f"{DOMAIN}_connection_status" in known

    tsv = Path(__file__).resolve().parent.parent / "entity_identities_2026-08-22.tsv"
    with tsv.open(encoding="utf-8") as handle:
        recorded = {row[1] for row in csv.reader(handle, delimiter="\t") if len(row) > 1}

    assert len(recorded) == 26, "the recorded identity list changed unexpectedly"
    assert f"{DOMAIN}_connection_status" not in recorded
