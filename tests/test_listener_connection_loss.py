"""Tests for the listener's role as the single detector of connection loss."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from conftest import FakeEntry

from custom_components.nikobus.const import CONF_CONNECTION_STRING
from custom_components.nikobus.coordinator import NikobusDataCoordinator
from custom_components.nikobus.exceptions import NikobusReadError
from custom_components.nikobus.nkblistener import NikobusEventListener


class _FailingConnection:
    """Connection stand-in whose reader is gone."""

    def __init__(self, error=None) -> None:
        self.error = error or NikobusReadError("Reader is not available for reading data.")
        self.reads = 0
        self.is_connected = False

    async def read(self, timeout=None):
        self.reads += 1
        raise self.error


class _EofConnection:
    """Connection stand-in that reports end of file."""

    def __init__(self) -> None:
        self.reads = 0
        self.is_connected = False

    async def read(self, timeout=None):
        self.reads += 1
        return b""


def _make_listener(hass, connection):
    return NikobusEventListener(
        hass=hass,
        config_entry=FakeEntry({}),
        coordinator=None,
        nikobus_actuator=None,
        nikobus_connection=connection,
        nikobus_discovery=None,
        feedback_callback=None,
    )


async def _drain(task, turns: int = 20) -> None:
    for _ in range(turns):
        if task.done():
            return
        await asyncio.sleep(0)


async def test_read_failure_reports_the_loss_once(hass):
    """A failing read tells the coordinator exactly once and leaves the loop."""
    connection = _FailingConnection()
    listener = _make_listener(hass, connection)
    calls: list[int] = []

    async def _on_lost() -> None:
        calls.append(1)

    listener.on_connection_lost = _on_lost
    await listener.start()
    task = listener._listener_task
    await _drain(task)

    assert calls == [1]
    assert task.done()
    assert connection.reads == 1, "the loop must not spin on a dead reader"


async def test_eof_reports_the_loss(hass):
    """An empty read is end of file: the far side is gone."""
    connection = _EofConnection()
    listener = _make_listener(hass, connection)
    calls: list[int] = []

    async def _on_lost() -> None:
        calls.append(1)

    listener.on_connection_lost = _on_lost
    await listener.start()
    await _drain(listener._listener_task)

    assert calls == [1]


async def test_deliberate_stop_is_not_reported_as_a_loss(hass):
    """Unloading the integration must not schedule a reconnect."""
    connection = _FailingConnection()
    listener = _make_listener(hass, connection)
    calls: list[int] = []

    async def _on_lost() -> None:
        calls.append(1)

    listener.on_connection_lost = _on_lost
    await listener.start()
    await listener.stop()
    await asyncio.sleep(0)

    assert calls == []


async def test_reset_drops_answers_from_the_dead_connection(hass):
    """Stale answers cannot be matched to commands issued on a new transport."""
    listener = _make_listener(hass, _FailingConnection())
    listener.response_queue.put_nowait("$0515$0EFF6C0E0060")
    listener.response_queue.put_nowait("$1C6C0E000000000000")

    listener.reset()

    assert listener.response_queue.empty()


async def test_self_stop_does_not_abort_the_reconnect(hass):
    """The full detection path, end to end.

    The listener detects the loss and awaits the coordinator's handler *from
    inside its own task*; that handler stops the listener as part of the
    teardown. If ``stop()`` cancelled the current task there, the reconnect
    would never be scheduled and the outage would look exactly like
    21.08.2026 again.
    """
    coordinator = NikobusDataCoordinator(
        hass, FakeEntry({CONF_CONNECTION_STRING: "/dev/ttyUSB0"})
    )

    class _Command:
        stopped = 0

        async def stop(self) -> None:
            type(self).stopped += 1

    connection = _FailingConnection()
    listener = _make_listener(hass, connection)
    listener.on_connection_lost = coordinator._handle_connection_lost

    coordinator.nikobus_command = _Command()
    coordinator.nikobus_listener = listener

    async def _hang(**_kwargs) -> int:
        await asyncio.Event().wait()
        return 1

    coordinator.nikobus_connection.reconnect_with_backoff = _hang

    await listener.start()
    listener_task = listener._listener_task
    await _drain(listener_task)

    assert listener_task.done()
    assert listener_task.exception() is None
    assert coordinator._reconnect_task is not None
    assert not coordinator._reconnect_task.done(), "a reconnect must be running"
    assert coordinator.connection_status == "reconnecting"

    coordinator._stopping = True
    coordinator._reconnect_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await coordinator._reconnect_task


async def test_missing_handler_is_reported_loudly(hass, caplog):
    """A listener with nobody wired up must not fail silently."""
    connection = _FailingConnection()
    listener = _make_listener(hass, connection)
    listener.on_connection_lost = None

    await listener.start()
    await _drain(listener._listener_task)

    assert any(
        "will NOT be rebuilt automatically" in record.getMessage()
        for record in caplog.records
    )
