"""Tests for the cover platform's connection guard.

Three rules, from the 21.08.2026 outage:

1. Connection unhealthy *before* a drive command -> do not start the
   calculator (nothing moves, so the old position is still right) and let the
   failure reach the caller.
2. Connection dies *during* a travel -> the position becomes unknown, not a
   frozen intermediate value. The blind is still moving and the stop command
   cannot get through either.
3. After the connection returns -> still unknown, until a real reference
   exists. Nikobus roller relays report no position, so only a full run into
   an end stop provides one.
"""

from __future__ import annotations

import pytest

from conftest import FakeEntry

from homeassistant.exceptions import HomeAssistantError

from custom_components.nikobus.const import (
    CONF_CONNECTION_STRING,
    CONF_COVER_DOWN_CODE,
    CONF_COVER_NAME,
    CONF_COVER_STOP_CODE,
    CONF_COVER_UP_CODE,
    CONF_TRAVEL_DOWN_TIME,
    CONF_TRAVEL_UP_TIME,
)
from custom_components.nikobus.coordinator import NikobusDataCoordinator
from custom_components.nikobus.cover import NikobusYamlCoverEntity

# Identity of a cover that does NOT exist in this installation, so nothing in
# entity_identities_2026-08-22.tsv is implicated by these tests.
_TEST_UNIQUE_ID = "nikobus_yaml_cover_000000000000"


def _make_cover(hass, sent: list[str]):
    coordinator = NikobusDataCoordinator(
        hass, FakeEntry({CONF_CONNECTION_STRING: "/dev/ttyUSB0"})
    )
    coordinator.nikobus_connection._is_connected = True

    cover = NikobusYamlCoverEntity(
        coordinator,
        {
            CONF_COVER_NAME: "Esszimmer Test",
            CONF_COVER_UP_CODE: "AAAAAA",
            CONF_COVER_DOWN_CODE: "BBBBBB",
            CONF_COVER_STOP_CODE: "CCCCCC",
            CONF_TRAVEL_UP_TIME: 20.0,
            CONF_TRAVEL_DOWN_TIME: 20.0,
            "unique_id": _TEST_UNIQUE_ID,
            "suggested_object_id": "esszimmer_test",
        },
    )
    cover.hass = hass
    cover.entity_id = "cover.esszimmer_test"

    async def _fake_send(code, wait_for_completion=False, retries=0):
        sent.append(code)
        return True

    cover._send_command = _fake_send
    return coordinator, cover


def _rewind(cover, seconds: float) -> None:
    """Pretend the given number of seconds of travel have elapsed."""
    cover._tc._last_known_position_timestamp -= seconds


# ---------------------------------------------------------------------------
# 1. Unhealthy before the command
# ---------------------------------------------------------------------------
async def test_drive_on_a_dead_connection_does_not_start_the_calculator(hass):
    """No command, no timer, no state change - and the caller is told."""
    sent: list[str] = []
    coordinator, cover = _make_cover(hass, sent)
    cover._tc.set_position(100)
    coordinator.nikobus_connection._is_connected = False

    with pytest.raises(HomeAssistantError):
        await cover.async_close_cover()

    assert sent == [], "nothing may be put on the bus"
    assert cover.current_cover_position == 100, "the old position is still correct"
    assert cover._tc.is_traveling() is False
    assert cover.is_closing is False
    assert cover._tc.position_known is True


async def test_every_drive_entry_point_is_guarded(hass):
    """open, close, stop and set_position all refuse on a dead connection."""
    sent: list[str] = []
    coordinator, cover = _make_cover(hass, sent)
    cover._tc.set_position(50)
    coordinator.nikobus_connection._is_connected = False

    for call in (
        cover.async_open_cover(),
        cover.async_close_cover(),
        cover.async_stop_cover(),
        cover.async_set_cover_position(position=20),
    ):
        with pytest.raises(HomeAssistantError):
            await call

    assert sent == []
    assert cover.current_cover_position == 50


async def test_a_healthy_connection_still_drives_normally(hass):
    """The guard must not get in the way when everything is fine."""
    sent: list[str] = []
    _, cover = _make_cover(hass, sent)
    cover._tc.set_position(0)

    await cover.async_open_cover()

    assert sent == ["AAAAAA"]
    assert cover.is_opening is True
    assert cover._tc.position_known is True


# ---------------------------------------------------------------------------
# 2. Connection dies during the travel
# ---------------------------------------------------------------------------
async def test_connection_loss_mid_travel_makes_the_position_unknown(hass):
    """Unknown, not a frozen intermediate value."""
    sent: list[str] = []
    coordinator, cover = _make_cover(hass, sent)
    cover._tc.set_position(0)

    await cover.async_open_cover()
    _rewind(cover, 8)
    frozen = cover.current_cover_position
    assert frozen is not None and 0 < frozen < 100

    # The bus dies while the shutter is on its way.
    coordinator.nikobus_connection._is_connected = False
    await cover._auto_updater_hook(None)

    assert cover.current_cover_position is None, "position must not be frozen"
    assert cover.current_cover_position != frozen
    # `None` renders the entity as unknown in Home Assistant rather than
    # guessing open or closed.
    assert cover.is_closed is None
    assert cover.is_opening is False
    assert cover.is_closing is False
    assert cover._tc.position_known is False


async def test_mid_travel_loss_clears_the_pending_stop_bookkeeping(hass):
    """Nothing is left armed that could later act on the discarded estimate."""
    sent: list[str] = []
    coordinator, cover = _make_cover(hass, sent)
    cover._tc.set_position(100)

    await cover.async_set_cover_position(position=40)
    assert cover._pending_target == 40

    _rewind(cover, 5)
    coordinator.nikobus_connection._is_connected = False
    await cover._auto_updater_hook(None)

    assert cover._pending_target is None
    assert cover._stop_in_progress is False
    assert cover._state is None


# ---------------------------------------------------------------------------
# 3. After the connection returns
# ---------------------------------------------------------------------------
async def test_position_stays_unknown_after_the_connection_returns(hass):
    """A working cable is not a position. Only an end stop is."""
    sent: list[str] = []
    coordinator, cover = _make_cover(hass, sent)
    cover._tc.set_position(0)
    await cover.async_open_cover()
    _rewind(cover, 8)

    coordinator.nikobus_connection._is_connected = False
    await cover._auto_updater_hook(None)
    assert cover.current_cover_position is None

    # Connection is back...
    coordinator.nikobus_connection._is_connected = True
    assert cover.current_cover_position is None, (
        "reconnecting must not invent a position"
    )
    assert cover.is_closed is None


async def test_an_intermediate_target_is_refused_while_unknown(hass):
    """There is nothing to interpolate from, so say so instead of guessing."""
    sent: list[str] = []
    coordinator, cover = _make_cover(hass, sent)
    cover._tc.set_position(0)
    await cover.async_open_cover()
    _rewind(cover, 8)
    coordinator.nikobus_connection._is_connected = False
    await cover._auto_updater_hook(None)
    coordinator.nikobus_connection._is_connected = True
    sent.clear()

    with pytest.raises(HomeAssistantError):
        await cover.async_set_cover_position(position=55)

    assert sent == []
    assert cover.current_cover_position is None


async def test_a_full_run_into_the_end_stop_recovers_the_position(hass):
    """The documented way back: open or close it fully once."""
    sent: list[str] = []
    coordinator, cover = _make_cover(hass, sent)
    cover._tc.set_position(0)
    await cover.async_open_cover()
    _rewind(cover, 8)
    coordinator.nikobus_connection._is_connected = False
    await cover._auto_updater_hook(None)
    coordinator.nikobus_connection._is_connected = True
    sent.clear()

    await cover.async_close_cover()

    assert sent == ["BBBBBB"]
    # Still unknown for the whole run - the starting point was only assumed.
    _rewind(cover, 10)
    assert cover.current_cover_position is None
    assert cover.is_closing is True

    # Full travel time elapsed: the shutter is against the end stop.
    _rewind(cover, 11)
    assert cover.current_cover_position == 0
    assert cover._tc.position_known is True
    assert cover.is_closed is True
