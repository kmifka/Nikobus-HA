"""Tests for cover availability following the state of the installation.

The rule, and why it is the rule
--------------------------------
A shutter whose commands land in the void IS not available. Showing it as
operable is the same kind of lie as a travel calculator reporting a position it
computed for a blind that never moved: both invite somebody to act on something
that is not true. On 21.08.2026 all 26 covers stayed available and operable for
two hours while every command was dying in the writer, and nobody found out
until they physically stood in front of a blind.

Reported honestly, Home Assistant greys the entity out and Apple Home says
"not responding" instead of offering a slider that does nothing.

Hysteresis
----------
A single missed heartbeat answer must not do it - one lost answer on a bus that
also carries button traffic is normal. HEARTBEAT_FAILURE_THRESHOLD consecutive
misses (~90 s) do. See the constant for why that number is a starting value and
not a measurement.
"""

from __future__ import annotations

import asyncio

from conftest import FakeEntry

from custom_components.nikobus.const import (
    CONF_CONNECTION_STRING,
    CONF_COVER_DOWN_CODE,
    CONF_COVER_NAME,
    CONF_COVER_STOP_CODE,
    CONF_COVER_UP_CODE,
    CONF_HEARTBEAT_ADDRESS,
    CONF_PRIOR_GEN3,
    CONF_TRAVEL_DOWN_TIME,
    CONF_TRAVEL_UP_TIME,
    HEARTBEAT_FAILURE_THRESHOLD,
)
from custom_components.nikobus.coordinator import NikobusDataCoordinator
from custom_components.nikobus.cover import (
    NikobusYamlCoverEntity,
    NikobusYamlGroupCoverEntity,
)
from custom_components.nikobus.nkbheartbeat import NikobusHeartbeat

_ADDRESS = "9E62"

# Identities of covers that do NOT exist in this installation, so nothing in
# entity_identities_2026-08-22.tsv is implicated by these tests.
_TEST_UNIQUE_ID = "nikobus_yaml_cover_000000000000"
_TEST_GROUP_UNIQUE_ID = "nikobus_yaml_group_cover_000000000000"


class _SilentCommand:
    """Command-handler stand-in that never answers the clock query."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def get_system_clock(self, address: str) -> str:
        self.queries.append(address)
        raise asyncio.TimeoutError("bus silent")


def _make_setup(hass):
    """Coordinator with a live transport and a heartbeat that gets no answers."""
    coordinator = NikobusDataCoordinator(
        hass,
        FakeEntry(
            {
                CONF_CONNECTION_STRING: "/dev/ttyUSB0",
                CONF_PRIOR_GEN3: True,
                CONF_HEARTBEAT_ADDRESS: _ADDRESS,
            }
        ),
    )
    coordinator.nikobus_connection._is_connected = True
    coordinator.nikobus_command = _SilentCommand()
    coordinator.nikobus_heartbeat = NikobusHeartbeat(coordinator, _ADDRESS)
    return coordinator


def _make_cover(hass, coordinator):
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
            # No area and no suggested_object_id: async_added_to_hass would
            # otherwise reach into the entity registry, which the stubs in
            # conftest do not provide. Neither has anything to do with
            # availability.
        },
    )
    cover.hass = hass
    cover.entity_id = "cover.esszimmer_test"
    return cover


def _make_group_cover(hass, coordinator):
    group = NikobusYamlGroupCoverEntity(
        coordinator,
        {
            CONF_COVER_NAME: "Esszimmer Gruppe Test",
            CONF_COVER_UP_CODE: "AAAAAA",
            CONF_COVER_DOWN_CODE: "BBBBBB",
            CONF_COVER_STOP_CODE: "CCCCCC",
            "unique_id": _TEST_GROUP_UNIQUE_ID,
            "members": [],
        },
    )
    group.hass = hass
    group.entity_id = "cover.esszimmer_gruppe_test"
    return group


# ---------------------------------------------------------------------------
# The hysteresis
# ---------------------------------------------------------------------------
async def test_the_first_missed_answer_takes_the_cover_away(hass):
    """HEARTBEAT_FAILURE_THRESHOLD in a row - one poll, about 30 s of silence.

    Deliberately eager. Showing a blind as operable while nothing reaches it
    invites somebody to press a button that does nothing, and the button they
    press is often the one that would have retracted an awning. Coming back is
    just as fast - one good answer restores it - so the cost of over-reacting
    is bounded at one poll interval.
    """
    coordinator = _make_setup(hass)
    cover = _make_cover(hass, coordinator)
    group = _make_group_cover(hass, coordinator)
    assert cover.available is True

    for poll in range(1, HEARTBEAT_FAILURE_THRESHOLD + 1):
        await coordinator.nikobus_heartbeat.async_poll()
        if poll < HEARTBEAT_FAILURE_THRESHOLD:
            assert cover.available is True, f"too early after {poll} miss(es)"

    assert coordinator.nikobus_heartbeat.consecutive_failures == HEARTBEAT_FAILURE_THRESHOLD
    assert coordinator.nikobus_heartbeat.is_alive is False
    assert cover.available is False
    assert group.available is False, "the group rides on the same bus"


async def test_the_cover_comes_back_when_the_installation_does(hass):
    """Availability is derived, so it cannot get stuck on the old verdict."""
    coordinator = _make_setup(hass)
    cover = _make_cover(hass, coordinator)

    for _ in range(HEARTBEAT_FAILURE_THRESHOLD):
        await coordinator.nikobus_heartbeat.async_poll()
    assert cover.available is False

    # A readable clock answer ends the outage on the next poll.
    clock_frame = "$051D$1CFF629E9BC100001E009F2C11"

    class _AnsweringCommand:
        async def get_system_clock(self, address: str) -> str:
            return clock_frame

    coordinator.nikobus_command = _AnsweringCommand()
    await coordinator.nikobus_heartbeat.async_poll()

    assert cover.available is True


# ---------------------------------------------------------------------------
# The transport half
# ---------------------------------------------------------------------------
async def test_a_dead_transport_makes_the_cover_unavailable_at_once(hass):
    """No hysteresis here: a closed writer is a certainty, not a sample.

    This is the exact state of 21.08.2026 - the FTDI adapter had moved from
    /dev/ttyUSB1 to /dev/ttyUSB0 - during which all 26 covers went on showing
    as fully operable.
    """
    coordinator = _make_setup(hass)
    cover = _make_cover(hass, coordinator)
    group = _make_group_cover(hass, coordinator)
    assert cover.available is True

    coordinator.nikobus_connection._is_connected = False

    assert cover.available is False
    assert group.available is False


async def test_a_disabled_heartbeat_never_takes_a_cover_away(hass):
    """No configured clock address means the transport alone decides.

    An installation whose clock address nobody knows must not have its covers
    removed on a guess: a wrong address answers $0E or nothing at all, which
    looks exactly like a dead installation.
    """
    coordinator = _make_setup(hass)
    coordinator.nikobus_heartbeat = NikobusHeartbeat(coordinator, "")
    cover = _make_cover(hass, coordinator)

    for _ in range(HEARTBEAT_FAILURE_THRESHOLD + 2):
        await coordinator.nikobus_heartbeat.async_poll()

    assert cover.available is True


# ---------------------------------------------------------------------------
# Getting the change onto the dashboard
# ---------------------------------------------------------------------------
async def test_the_cover_repaints_when_availability_flips(hass):
    """These covers are not CoordinatorEntity, so the repaint has to be wired.

    Without it ``available`` would only change the next time the entity happened
    to write its state for some other reason - and an installation that stopped
    answering would go on looking operable for as long as nobody touched it,
    which is exactly the failure mode this whole feature exists to remove.
    """
    coordinator = _make_setup(hass)
    cover = _make_cover(hass, coordinator)
    await cover.async_added_to_hass()
    writes_before = getattr(cover, "state_writes", 0)

    for _ in range(HEARTBEAT_FAILURE_THRESHOLD):
        await coordinator.nikobus_heartbeat.async_poll()

    assert cover.available is False
    assert getattr(cover, "state_writes", 0) > writes_before


async def test_an_unchanged_verdict_does_not_repaint(hass):
    """The coordinator notifies on every button press; 26 covers must stay quiet."""
    coordinator = _make_setup(hass)
    cover = _make_cover(hass, coordinator)
    await cover.async_added_to_hass()
    writes_before = getattr(cover, "state_writes", 0)

    for _ in range(5):
        coordinator.async_update_listeners()

    assert getattr(cover, "state_writes", 0) == writes_before


async def test_removing_the_cover_unsubscribes(hass):
    """A renamed entity is re-added; a leaked listener would repaint a ghost."""
    coordinator = _make_setup(hass)
    cover = _make_cover(hass, coordinator)
    await cover.async_added_to_hass()
    assert coordinator.listeners

    await cover.async_will_remove_from_hass()

    assert not coordinator.listeners
    assert cover._unsub_coordinator is None


# ---------------------------------------------------------------------------
# Entity identity guard
# ---------------------------------------------------------------------------
def test_availability_does_not_touch_the_recorded_identities(hass):
    """The 26 unique_ids are the hard boundary of this work.

    ``available`` is a computed property; nothing here derives, stores or
    rewrites a unique_id. This test states that explicitly, because a change to
    those ids would orphan every cover entity in the house along with its
    automations, scenes and HomeKit bindings.
    """
    import csv
    from pathlib import Path

    coordinator = _make_setup(hass)
    cover = _make_cover(hass, coordinator)
    group = _make_group_cover(hass, coordinator)

    tsv = Path(__file__).resolve().parent.parent / "entity_identities_2026-08-22.tsv"
    with tsv.open(encoding="utf-8") as handle:
        recorded = {row[1] for row in csv.reader(handle, delimiter="\t") if len(row) > 1}

    assert len(recorded) == 26, "the recorded identity list changed unexpectedly"
    assert cover._attr_unique_id == _TEST_UNIQUE_ID
    assert group._attr_unique_id == _TEST_GROUP_UNIQUE_ID
    assert _TEST_UNIQUE_ID not in recorded
    assert _TEST_GROUP_UNIQUE_ID not in recorded
