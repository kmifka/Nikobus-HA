"""Tests for the supervised reconnect in nkbconnect.py.

Every case here is a piece of what went wrong on 21.08.2026, when a reboot
re-enumerated the FTDI adapter from /dev/ttyUSB1 to /dev/ttyUSB0 and the
integration kept reporting itself healthy for two hours while every drive
command died in the writer.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from conftest import FakeReader, FakeWriter

from custom_components.nikobus.const import (
    COMMANDS_HANDSHAKE,
    RECONNECT_DELAY_INITIAL,
    RECONNECT_DELAY_MAX,
)
from custom_components.nikobus.exceptions import (
    NikobusConnectionError,
    NikobusSendError,
)
from custom_components.nikobus.nkbconnect import NikobusConnect

_LOGGER_NAME = "custom_components.nikobus.nkbconnect"


@pytest.mark.asyncio
async def test_backoff_sequence_grows_and_is_capped(monkeypatch, instant_sleep):
    """Delays double from the initial value and never exceed the cap."""
    conn = NikobusConnect("/dev/ttyUSB0")
    calls = {"n": 0}

    async def fake_connect() -> None:
        calls["n"] += 1
        if calls["n"] <= 8:
            raise NikobusConnectionError("device is not there")
        conn._is_connected = True

    monkeypatch.setattr(conn, "connect", fake_connect)

    attempts = await conn.reconnect_with_backoff()

    assert attempts == 9
    # 5, 10, 20, 40 then pinned at the 60 s cap for every further attempt.
    assert instant_sleep == [5, 10, 20, 40, 60, 60, 60, 60]
    assert max(instant_sleep) == RECONNECT_DELAY_MAX
    assert instant_sleep[0] == RECONNECT_DELAY_INITIAL


@pytest.mark.asyncio
async def test_backoff_honours_custom_bounds(monkeypatch, instant_sleep):
    """The caller's initial/max values are respected, not the module defaults."""
    conn = NikobusConnect("/dev/ttyUSB0")
    calls = {"n": 0}

    async def fake_connect() -> None:
        calls["n"] += 1
        if calls["n"] <= 5:
            raise NikobusConnectionError("nope")

    monkeypatch.setattr(conn, "connect", fake_connect)

    await conn.reconnect_with_backoff(initial_delay=1, max_delay=4)

    assert instant_sleep == [1, 2, 4, 4, 4]


@pytest.mark.asyncio
async def test_handshake_is_run_again_after_reconnect(monkeypatch, instant_sleep):
    """A rebuilt transport is handshaken again before it is used.

    A PC-Link that lost power in the meantime is in its power-on state, so
    skipping COMMANDS_HANDSHAKE on reconnect would produce an open port that
    answers nothing.
    """
    conn = NikobusConnect("/dev/ttyUSB0")
    written: list[bytes] = []
    opens = {"n": 0}

    async def fake_open(url=None, baudrate=None):
        opens["n"] += 1
        if opens["n"] == 1:
            # The device node is gone - the 21.08.2026 failure.
            raise FileNotFoundError(2, "No such file or directory", url)
        return FakeReader(), FakeWriter(written)

    monkeypatch.setattr(
        "custom_components.nikobus.nkbconnect.serial_asyncio.open_serial_connection",
        fake_open,
    )

    attempts = await conn.reconnect_with_backoff(initial_delay=1, max_delay=1)

    assert attempts == 2
    assert conn.is_connected is True

    sent = [frame.rstrip(b"\r").decode() for frame in written]
    assert sent == list(COMMANDS_HANDSHAKE), (
        "the full handshake must be replayed on the new transport"
    )


@pytest.mark.asyncio
async def test_repeated_failures_do_not_repeat_the_warning(
    monkeypatch, instant_sleep, caplog
):
    """One WARNING per outage, DEBUG for everything after it.

    A retry every 60 s over a long weekend is thousands of attempts. Logging
    each at WARNING is how an integration teaches its owner to ignore it - part
    of why the real outage went unnoticed even though it was in the log.
    """
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    conn = NikobusConnect("/dev/ttyUSB0")
    calls = {"n": 0}

    async def fake_connect() -> None:
        calls["n"] += 1
        if calls["n"] <= 12:
            raise NikobusConnectionError("still gone")

    monkeypatch.setattr(conn, "connect", fake_connect)
    await conn.reconnect_with_backoff(initial_delay=1, max_delay=1)

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    warnings = [r for r in records if r.levelno >= logging.WARNING]
    debugs = [r for r in records if r.levelno == logging.DEBUG]

    assert len(warnings) == 1, f"expected exactly one WARNING, got {len(warnings)}"
    assert len(debugs) >= 11, "later attempts must still be traceable at DEBUG"


@pytest.mark.asyncio
async def test_outage_warning_is_loud_again_for_the_next_outage(
    monkeypatch, instant_sleep, caplog
):
    """The quiet flag is cleared on success, so a new outage is reported."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    conn = NikobusConnect("/dev/ttyUSB0")
    calls = {"n": 0}

    async def fake_connect() -> None:
        calls["n"] += 1
        if calls["n"] in (1, 2, 4, 5):
            raise NikobusConnectionError("gone")
        conn._is_connected = True
        conn._outage_logged = False

    monkeypatch.setattr(conn, "connect", fake_connect)

    await conn.reconnect_with_backoff(initial_delay=1, max_delay=1)
    await conn.reconnect_with_backoff(initial_delay=1, max_delay=1)

    warnings = [
        r
        for r in caplog.records
        if r.name == _LOGGER_NAME and r.levelno >= logging.WARNING
    ]
    assert len(warnings) == 2, "each separate outage gets its own first warning"


@pytest.mark.asyncio
async def test_missing_serial_device_keeps_retrying(monkeypatch, instant_sleep):
    """A device node that does not exist must not end the reconnect.

    This is the 21.08.2026 case exactly: /dev/ttyUSB1 was gone. Giving up on
    FileNotFoundError would have left the integration dead until a restart;
    retrying means it heals by itself once the node is back.
    """
    conn = NikobusConnect("/dev/ttyUSB1")
    opens = {"n": 0}

    async def always_missing(url=None, baudrate=None):
        opens["n"] += 1
        raise FileNotFoundError(2, "No such file or directory", url)

    monkeypatch.setattr(
        "custom_components.nikobus.nkbconnect.serial_asyncio.open_serial_connection",
        always_missing,
    )

    task = asyncio.create_task(conn.reconnect_with_backoff(initial_delay=1, max_delay=1))
    # instant_sleep makes each attempt a single event-loop turn.
    for _ in range(40):
        await asyncio.sleep(0)
        if opens["n"] > 5:
            break

    assert not task.done(), "reconnect gave up on a missing device"
    assert opens["n"] > 5

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_reconnect_reports_every_attempt_to_the_caller(
    monkeypatch, instant_sleep
):
    """on_attempt fires once per attempt, before the attempt is made."""
    conn = NikobusConnect("/dev/ttyUSB0")
    seen: list[tuple[int, float]] = []
    calls = {"n": 0}

    async def fake_connect() -> None:
        calls["n"] += 1
        if calls["n"] <= 3:
            raise NikobusConnectionError("nope")

    monkeypatch.setattr(conn, "connect", fake_connect)

    await conn.reconnect_with_backoff(
        initial_delay=1, max_delay=8, on_attempt=lambda a, d: seen.append((a, d))
    )

    assert [a for a, _ in seen] == [1, 2, 3, 4]
    assert [d for _, d in seen] == [1, 2, 4, 8]


@pytest.mark.asyncio
async def test_send_without_writer_reports_loss_quietly_after_the_first(caplog):
    """The line that flooded the log for two hours is loud exactly once.

    ``NikobusSendError`` still reaches the caller every time - only the log
    volume changes.
    """
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    conn = NikobusConnect("/dev/ttyUSB0")

    for _ in range(25):
        with pytest.raises(NikobusSendError):
            await conn.send("#N001122\r#E1")

    warnings = [
        r
        for r in caplog.records
        if r.name == _LOGGER_NAME and r.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_is_connected_tracks_the_transport(monkeypatch):
    """is_connected is only true between a completed handshake and the close."""
    conn = NikobusConnect("/dev/ttyUSB0")
    assert conn.is_connected is False

    written: list[bytes] = []

    async def fake_open(url=None, baudrate=None):
        return FakeReader(), FakeWriter(written)

    monkeypatch.setattr(
        "custom_components.nikobus.nkbconnect.serial_asyncio.open_serial_connection",
        fake_open,
    )

    await conn.connect()
    assert conn.is_connected is True

    await conn.disconnect()
    assert conn.is_connected is False
