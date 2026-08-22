"""Tests for the Nikobus liveness heartbeat.

Everything here is built on the measurement run of 22.08.2026 (see
nkbheartbeat.py and const.HEARTBEAT_FUNCTION_CODE):

* This installation has no feedback module and runs prior_gen3. 0x12 and 0x17
  never answer, so there is no output-state feedback to lean on.
* 0x1D at address 9E62 answers with a running clock:

      $051D $1C FF 629E 9BC1 0000 <MM> <SS> <CRC16> <CRC8>

  MM and SS are one hexadecimal byte each and the seconds roll over at 60.

The four things these tests pin down:

1. A clock that keeps running means the installation is alive.
2. A clock that stands still while real time passes means it is hung - which a
   merely constant answer could never reveal, because a constant answer can
   come out of a buffer.
3. The wrap 59:58 -> 00:01 is three seconds forward, not an hour backwards.
4. The ping is queued like every other command and never written straight to
   the transport.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import FakeEntry

from custom_components.nikobus.const import (
    COMMAND_EXECUTION_DELAY,
    CONF_CONNECTION_STRING,
    CONF_HAS_FEEDBACK_MODULE,
    CONF_HEARTBEAT_ADDRESS,
    CONF_PRIOR_GEN3,
    HEARTBEAT_CLOCK_TOLERANCE,
    HEARTBEAT_FAILURE_THRESHOLD,
    HEARTBEAT_FUNCTION_CODE,
)
from custom_components.nikobus.coordinator import NikobusDataCoordinator
from custom_components.nikobus.nkbcommand import NikobusCommandHandler
from custom_components.nikobus.nkblistener import NikobusEventListener
from custom_components.nikobus.nkbheartbeat import (
    NikobusHeartbeat,
    clock_advance_is_plausible,
    clock_answer_signal,
    clock_is_standing_still,
    parse_clock_seconds,
)
from custom_components.nikobus.nkbprotocol import make_pc_link_command

_ADDRESS = "9E62"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _clock_frame(minutes: int, seconds: int, address: str = _ADDRESS) -> str:
    """Build a clock answer exactly as it was recorded on 22.08.2026.

    The trailing six digits are the CRC16 and the CRC8. They change with every
    frame - that is the "two changing bytes" from the measurement - and nothing
    on the heartbeat path validates them, so a fixed filler is enough here.
    """
    return (
        f"$051D{clock_answer_signal(address)}9BC10000"
        f"{minutes:02X}{seconds:02X}"
        "9F2C11"
    )


class _FakeClock:
    """Monotonic time the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeCommand:
    """Command-handler stand-in that serves canned clock answers."""

    def __init__(self, answers=None) -> None:
        self.answers = list(answers or [])
        self.queries: list[str] = []

    async def get_system_clock(self, address: str) -> str:
        self.queries.append(address)
        if not self.answers:
            raise asyncio.TimeoutError("bus silent")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _make_heartbeat(hass, answers=None, address: str = _ADDRESS):
    """Return (coordinator, heartbeat, command, clock) wired together."""
    coordinator = NikobusDataCoordinator(
        hass,
        FakeEntry(
            {
                CONF_CONNECTION_STRING: "/dev/ttyUSB0",
                CONF_PRIOR_GEN3: True,
                CONF_HEARTBEAT_ADDRESS: address,
            }
        ),
    )
    # The transport is fine throughout; these tests are about the installation
    # behind it, which is the separate question the heartbeat exists for.
    coordinator.nikobus_connection._is_connected = True

    command = _FakeCommand(answers)
    coordinator.nikobus_command = command

    clock = _FakeClock()
    heartbeat = NikobusHeartbeat(coordinator, address, time_source=clock)
    coordinator.nikobus_heartbeat = heartbeat
    return coordinator, heartbeat, command, clock


# ---------------------------------------------------------------------------
# Frame decoding
# ---------------------------------------------------------------------------
def test_the_measured_frame_decodes_to_the_measured_time():
    """0x14 0x3A is 20:58, read as hex, not as BCD and not as a raw byte."""
    assert parse_clock_seconds(_clock_frame(0x14, 0x3A), _ADDRESS) == 20 * 60 + 58


def test_the_two_recorded_raw_samples_decode():
    """The first and last of the 43 samples taken on 22.08.2026.

    Recorded raw as the four bytes that follow the constant prefix: MM, SS and
    the two-byte CRC16 that changes with them. Reading those four bytes as one
    32-bit integer is the wrong turn that produced a phantom "activity counter"
    during the measurement run, so both readings are pinned here.
    """
    for raw, minutes, seconds in (("133A6D8D", 19, 58), ("16132733", 22, 19)):
        frame = f"$051D{clock_answer_signal(_ADDRESS)}9BC10000{raw}11"
        assert parse_clock_seconds(frame, _ADDRESS) == minutes * 60 + seconds


def test_an_answer_without_a_clock_is_not_a_clock():
    """$0E "unknown response" is what most function codes reply here."""
    assert parse_clock_seconds("$0EFF629E0060", _ADDRESS) is None
    assert parse_clock_seconds("", _ADDRESS) is None
    # Truncated frame: the bytes for MM and SS never arrived.
    assert parse_clock_seconds(f"$051D{clock_answer_signal(_ADDRESS)}9BC1", _ADDRESS) is None


def test_an_answer_from_another_address_is_ignored():
    """The address is byte-swapped in the answer; a different one must not match."""
    assert parse_clock_seconds(_clock_frame(10, 10, address="1234"), _ADDRESS) is None


def test_values_that_cannot_be_a_clock_are_rejected():
    """The seconds roll over at 60, so 0x77 is not a reading, it is noise."""
    assert parse_clock_seconds(_clock_frame(0x14, 0x77), _ADDRESS) is None
    assert parse_clock_seconds(_clock_frame(0x77, 0x14), _ADDRESS) is None


# ---------------------------------------------------------------------------
# The advance check
# ---------------------------------------------------------------------------
def test_a_running_clock_matches_the_elapsed_time():
    assert clock_advance_is_plausible(20 * 60 + 58, 21 * 60 + 28, 30.0) is True


def test_a_standing_clock_does_not():
    assert clock_advance_is_plausible(20 * 60 + 58, 20 * 60 + 58, 30.0) is False


def test_the_measured_tolerance_is_honoured():
    """+-2 s, from the one-second resolution plus the 22.08.2026 spread."""
    base = 10 * 60
    assert clock_advance_is_plausible(base, base + 30, 30 + HEARTBEAT_CLOCK_TOLERANCE)
    assert clock_advance_is_plausible(base, base + 30, 30 - HEARTBEAT_CLOCK_TOLERANCE)
    assert not clock_advance_is_plausible(base, base + 30, 30 + HEARTBEAT_CLOCK_TOLERANCE + 0.5)


def test_the_hour_wrap_reads_as_three_seconds_forward():
    """59:58 -> 00:01 is +3 s, not a jump backwards.

    Both fields wrap, so the clock only spans an hour. Read naively this looks
    like the installation lost 3597 seconds, which would be reported as a hang
    on a perfectly healthy system once an hour.
    """
    previous = 59 * 60 + 58
    current = 0 * 60 + 1
    assert clock_advance_is_plausible(previous, current, 3.0) is True
    # And it is still a stall if no time has actually been made up.
    assert clock_advance_is_plausible(previous, current, 30.0) is False


def test_a_gap_of_more_than_an_hour_cannot_be_compared():
    """Modulo 3600 makes 10 s and 3610 s identical - say so instead of guessing."""
    assert clock_advance_is_plausible(0, 10, 3610.0) is None


def test_standing_still_and_out_of_step_are_not_the_same_thing():
    """Only a clock that did not move at all is evidence of a hang."""
    assert clock_is_standing_still(20 * 60 + 58, 20 * 60 + 58) is True
    # Within the tolerance still counts as standing still.
    assert clock_is_standing_still(20 * 60 + 58, 21 * 60 + 0) is True
    # A jump is a running device whose clock is simply not where we left it.
    assert clock_is_standing_still(20 * 60 + 58, 22 * 60 + 28) is False


async def test_a_jumped_clock_is_not_counted_as_a_hang(hass):
    """What a device looks like the moment it comes back from a hang.

    Its clock is running again but no longer lines up with the baseline we
    recorded before the hang. Counting that as another failure would keep the
    covers unavailable for one more poll after the house is already working.
    """
    _, heartbeat, command, clock = _make_heartbeat(
        hass, answers=[_clock_frame(20, 58) for _ in range(4)]
    )
    await heartbeat.async_poll()
    for _ in range(HEARTBEAT_FAILURE_THRESHOLD):
        clock.advance(30)
        await heartbeat.async_poll()
    assert heartbeat.is_alive is False

    # 90 seconds of clock in 30 seconds of wall time: out of step, but moving.
    command.answers = [_clock_frame(22, 28)]
    clock.advance(30)
    assert await heartbeat.async_poll() is True
    assert heartbeat.is_alive is True
    assert heartbeat.consecutive_failures == 0


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
async def test_a_running_clock_keeps_the_installation_alive(hass):
    """The good case: three polls, three advancing readings, no complaints."""
    _, heartbeat, command, clock = _make_heartbeat(
        hass,
        answers=[
            _clock_frame(20, 58),
            _clock_frame(21, 28),
            _clock_frame(21, 58),
        ],
    )

    for _ in range(3):
        assert await heartbeat.async_poll() is True
        clock.advance(30)

    assert heartbeat.is_alive is True
    assert heartbeat.consecutive_failures == 0
    assert heartbeat.last_clock == "21:58"
    assert command.queries == [_ADDRESS] * 3


async def test_every_good_poll_is_published_not_only_the_transitions(hass):
    """The verdict has to be pushed out after each poll, not just on a change.

    A value that is only published when it changes cannot be told apart, from
    outside this process, from a value nobody computes any more. If this task
    dies quietly, ``heartbeat_alive`` stays True and ``heartbeat_last_ok``
    keeps its old timestamp for as long as Home Assistant runs - which is
    precisely how a weather station that had been dead for hours went on
    reading as healthy on 21.08.2026.

    Publishing after every poll is what turns ``last_ok`` into a freshness
    signal, and freshness is the only thing that can catch a watchdog that
    stopped watching. Watchtower's `nikobus_bridge` check requires it to be no
    older than 150 s (three missed 30 s polls plus reserve); that requirement
    is meaningless unless a healthy poll refreshes it.
    """
    coordinator, heartbeat, _, clock = _make_heartbeat(
        hass,
        answers=[_clock_frame(20, 58), _clock_frame(21, 28), _clock_frame(21, 58)],
    )
    coordinator.listener_updates = 0

    for _ in range(3):
        assert await heartbeat.async_poll() is True
        clock.advance(30)

    assert heartbeat.is_alive is True
    assert coordinator.listener_updates == 3, (
        "three healthy polls must produce three repaints - one per poll, so "
        "that last_ok keeps moving while nothing else changes"
    )
    assert heartbeat.last_ok is not None


async def test_a_standing_clock_is_detected_as_hung(hass):
    """The whole point: it still answers, and it is still dead.

    A constant answer proves only that something replies - it can come out of a
    buffer, which is how a dead weather station passed for healthy on
    21.08.2026. A clock that does not move while 30 s of real time pass cannot
    be explained that way.
    """
    _, heartbeat, _, clock = _make_heartbeat(
        hass, answers=[_clock_frame(20, 58) for _ in range(4)]
    )

    # First sample only establishes the baseline.
    assert await heartbeat.async_poll() is True

    for expected_failures in range(1, HEARTBEAT_FAILURE_THRESHOLD + 1):
        clock.advance(30)
        await heartbeat.async_poll()
        assert heartbeat.consecutive_failures == expected_failures

    assert heartbeat.is_alive is False
    assert "did not advance" in heartbeat.last_reason


async def test_the_clock_starts_running_again_and_so_does_the_verdict(hass):
    """Recovery is not gated behind a second threshold."""
    _, heartbeat, command, clock = _make_heartbeat(hass, answers=[])

    for _ in range(HEARTBEAT_FAILURE_THRESHOLD):
        await heartbeat.async_poll()
        clock.advance(30)
    assert heartbeat.is_alive is False

    command.answers = [_clock_frame(22, 28), _clock_frame(22, 58)]
    assert await heartbeat.async_poll() is True
    assert heartbeat.is_alive is True
    assert heartbeat.consecutive_failures == 0

    clock.advance(30)
    assert await heartbeat.async_poll() is True
    assert heartbeat.last_clock == "22:58"


async def test_a_single_missed_answer_is_not_an_outage(hass):
    """One lost answer on a bus that also carries button traffic is normal."""
    _, heartbeat, command, clock = _make_heartbeat(
        hass, answers=[_clock_frame(20, 58)]
    )
    await heartbeat.async_poll()

    clock.advance(30)
    assert await heartbeat.async_poll() is True  # answers list is now empty
    assert heartbeat.consecutive_failures == 1
    assert heartbeat.is_alive is True


async def test_three_missed_answers_are(hass):
    """HEARTBEAT_FAILURE_THRESHOLD consecutive misses, i.e. ~90 s of silence."""
    _, heartbeat, _, clock = _make_heartbeat(hass, answers=[])

    for expected in range(1, HEARTBEAT_FAILURE_THRESHOLD + 1):
        await heartbeat.async_poll()
        clock.advance(30)
        assert heartbeat.consecutive_failures == expected

    assert heartbeat.is_alive is False


async def test_the_failure_counter_is_consecutive_not_cumulative(hass):
    """Two misses spread around a good answer must not add up to three."""
    _, heartbeat, command, clock = _make_heartbeat(hass, answers=[])

    await heartbeat.async_poll()
    await heartbeat.async_poll()
    assert heartbeat.consecutive_failures == 2

    command.answers = [_clock_frame(30, 0)]
    await heartbeat.async_poll()
    assert heartbeat.consecutive_failures == 0

    await heartbeat.async_poll()
    assert heartbeat.is_alive is True


async def test_an_unreadable_answer_counts_as_a_failure(hass):
    """A reply we cannot read is not evidence that anything works.

    Most function codes on this installation answer $0E, "unknown response".
    Getting one here almost always means the configured address is wrong.
    """
    _, heartbeat, _, _ = _make_heartbeat(hass, answers=["$0EFF629E0060"] * 3)

    for _ in range(HEARTBEAT_FAILURE_THRESHOLD):
        await heartbeat.async_poll()

    assert heartbeat.is_alive is False
    assert "no readable clock" in heartbeat.last_reason


async def test_polling_is_skipped_while_the_transport_is_down(hass):
    """The reconnect owns that failure; counting it twice helps nobody."""
    coordinator, heartbeat, command, _ = _make_heartbeat(
        hass, answers=[_clock_frame(20, 58)]
    )
    coordinator.nikobus_connection._is_connected = False

    await heartbeat.async_poll()

    assert command.queries == [], "no bus traffic while the transport is gone"
    assert heartbeat.consecutive_failures == 0
    assert heartbeat.is_alive is True


async def test_the_baseline_is_dropped_across_an_outage(hass):
    """A power-cycled installation restarts its clock - do not read that as a hang."""
    coordinator, heartbeat, command, clock = _make_heartbeat(
        hass, answers=[_clock_frame(40, 0)]
    )
    await heartbeat.async_poll()
    assert heartbeat.last_clock == "40:00"

    coordinator.nikobus_connection._is_connected = False
    await heartbeat.async_poll()
    coordinator.nikobus_connection._is_connected = True

    # Back, with a clock that restarted from zero.
    command.answers = [_clock_frame(0, 5)]
    clock.advance(300)
    assert await heartbeat.async_poll() is True
    assert heartbeat.is_alive is True


async def test_without_an_address_the_heartbeat_stays_out_of_the_way(hass):
    """No configured clock address means no pinging and no verdict.

    A guessed address answers $0E or nothing, which looks exactly like a dead
    installation - and would take all 26 covers down for no reason.
    """
    _, heartbeat, command, _ = _make_heartbeat(hass, answers=[], address="")

    assert heartbeat.enabled is False
    await heartbeat.start()
    for _ in range(HEARTBEAT_FAILURE_THRESHOLD + 2):
        assert await heartbeat.async_poll() is True

    assert command.queries == []
    assert heartbeat.is_alive is True


async def test_the_loop_actually_polls_and_stops_cleanly(hass):
    """start() puts a real background task behind async_poll, stop() ends it."""
    _, heartbeat, command, _ = _make_heartbeat(
        hass, answers=[_clock_frame(20, 58) for _ in range(5)]
    )
    heartbeat._interval = 0.001  # 30 s in production; see HEARTBEAT_INTERVAL

    await heartbeat.start()
    for _ in range(200):
        if command.queries:
            break
        await asyncio.sleep(0.002)
    await heartbeat.stop()

    assert command.queries, "the loop must actually query the clock"
    assert heartbeat._task is None
    # Stopping twice must not raise - the coordinator's teardown is not the
    # place to discover a bookkeeping bug.
    await heartbeat.stop()


async def test_a_disabled_heartbeat_starts_no_task(hass):
    """No address, no background task, no bus traffic."""
    _, heartbeat, command, _ = _make_heartbeat(hass, answers=[], address="")
    await heartbeat.start()
    assert heartbeat._task is None
    assert command.queries == []


# ---------------------------------------------------------------------------
# The ping goes through the queue
# ---------------------------------------------------------------------------
class _RecordingConnection:
    """Transport stand-in that records every write."""

    def __init__(self, response_queue=None, reply: str | None = None) -> None:
        self.sent: list[str] = []
        self._response_queue = response_queue
        self._reply = reply
        self.is_connected = True

    async def send(self, command: str) -> None:
        self.sent.append(command)
        if self._response_queue is not None and self._reply is not None:
            # The installation answered in 0.20-0.21 s throughout on
            # 22.08.2026; here the loopback is immediate.
            await self._response_queue.put(self._reply)


class _FakeListener:
    """Listener stand-in that only owns the response queue."""

    def __init__(self) -> None:
        self.response_queue: asyncio.Queue[str] = asyncio.Queue()


async def test_the_ping_is_queued_and_never_written_straight_to_the_bus(hass):
    """The user's requirement, pinned down.

    Everything in this integration is paced by the single worker in
    process_commands, which sleeps COMMAND_EXECUTION_DELAY (0.7 s) between
    items. A ping that wrote to the transport itself would slip past that and
    put two telegrams on the bus at once. On an installation with no output
    feedback at all, nothing would ever report the resulting collision - a
    shutter command would simply be lost.
    """
    coordinator = NikobusDataCoordinator(
        hass, FakeEntry({CONF_CONNECTION_STRING: "/dev/ttyUSB0"})
    )
    connection = _RecordingConnection()
    handler = NikobusCommandHandler(hass, coordinator, connection, _FakeListener(), {})

    # The worker is deliberately NOT started: anything that reaches the bus now
    # can only have got there by bypassing the queue.
    task = asyncio.create_task(handler.get_system_clock(_ADDRESS))
    await asyncio.sleep(0)

    assert connection.sent == [], "the ping must not talk to the transport itself"
    assert handler._command_queue.qsize() == 1

    item = handler._command_queue.get_nowait()
    assert item["command"] == make_pc_link_command(HEARTBEAT_FUNCTION_CODE, _ADDRESS)
    assert item["address"] == _ADDRESS
    assert item["future"] is not None
    assert item["raw_answer"] is True
    # One attempt, not MAX_ATTEMPTS: an unanswered ping would otherwise hold the
    # single queue worker for three COMMAND_ANSWER_WAIT_TIMEOUT windows and
    # block every cover command in the house while it does.
    assert item["max_attempts"] == 1

    task.cancel()
    with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
        await task


async def test_the_queued_ping_round_trips_and_keeps_the_pacing(hass, instant_sleep):
    """End to end through the real worker: raw frame back, 0.7 s pacing kept."""
    coordinator = NikobusDataCoordinator(
        hass, FakeEntry({CONF_CONNECTION_STRING: "/dev/ttyUSB0"})
    )
    listener = _FakeListener()
    frame = _clock_frame(20, 58)
    connection = _RecordingConnection(listener.response_queue, frame)
    handler = NikobusCommandHandler(hass, coordinator, connection, listener, {})
    coordinator.nikobus_command = handler
    await handler.start()

    try:
        answer = await handler.get_system_clock(_ADDRESS)
    finally:
        await handler.stop()

    # The raw frame, not the 12-hex-digit module state _parse_state_from_message
    # would have carved out of it.
    assert answer == frame
    assert parse_clock_seconds(answer, _ADDRESS) == 20 * 60 + 58
    assert connection.sent == [
        make_pc_link_command(HEARTBEAT_FUNCTION_CODE, _ADDRESS)
    ]
    assert COMMAND_EXECUTION_DELAY in instant_sleep


async def test_an_unanswered_ping_is_tried_once_not_three_times(hass):
    """A silent installation must not block the queue for 15 s out of every 30."""
    coordinator = NikobusDataCoordinator(
        hass, FakeEntry({CONF_CONNECTION_STRING: "/dev/ttyUSB0"})
    )
    listener = _FakeListener()
    connection = _RecordingConnection()  # sends fine, nothing ever answers
    handler = NikobusCommandHandler(hass, coordinator, connection, listener, {})

    # Only the retry count is under test here, so the wait itself is short-circuited.
    async def _never_answers(*_args, **_kwargs):
        return None

    handler._wait_for_ack_and_answer_state = _never_answers

    with pytest.raises(Exception):
        await handler.send_command_get_answer(
            make_pc_link_command(HEARTBEAT_FUNCTION_CODE, _ADDRESS),
            _ADDRESS,
            raw_answer=True,
            max_attempts=1,
        )

    assert len(connection.sent) == 1

    # And every other caller keeps its three attempts.
    connection.sent.clear()
    with pytest.raises(Exception):
        await handler.send_command_get_answer(
            make_pc_link_command(0x12, "0E6C"), "0E6C"
        )
    assert len(connection.sent) == 3


def test_the_ping_asks_for_the_signals_the_installation_actually_answers_with():
    """$051D and $1CFF629E - the extra FF is what 22.08.2026 measured.

    Without the FF the answer signal would be "$1C629E", which never occurs in
    a clock frame: the ping would time out on a healthy installation and take
    the covers down with it.
    """
    coordinator = NikobusDataCoordinator.__new__(NikobusDataCoordinator)
    handler = NikobusCommandHandler.__new__(NikobusCommandHandler)
    handler._coordinator = coordinator

    command = make_pc_link_command(HEARTBEAT_FUNCTION_CODE, _ADDRESS)
    ack, answer = handler._prepare_ack_and_answer_signals(command, _ADDRESS)

    assert ack == "$051D"
    assert answer == "$1CFF629E"
    assert answer in _clock_frame(20, 58)
    assert ack in _clock_frame(20, 58)


async def test_the_clock_answer_survives_the_listener(hass):
    """This installation has no feedback module, and $1C frames are dropped there.

    ``dispatch_message`` throws away anything that *starts* with $1C when
    has_feedbackmodule is false. The clock answer starts with the $051D ack and
    only carries the $1C frame inside it, so it falls through to the response
    queue - which is the only reason the ping can be answered at all here. If
    that ever changes, the heartbeat goes deaf and every cover in the house
    goes unavailable, so it is pinned down.
    """
    listener = NikobusEventListener(
        hass=hass,
        config_entry=FakeEntry({CONF_HAS_FEEDBACK_MODULE: False}),
        coordinator=_DiscoveryIdleCoordinator(),
        nikobus_actuator=None,
        nikobus_connection=None,
        nikobus_discovery=None,
        feedback_callback=None,
    )

    frame = _clock_frame(20, 58)
    await listener.dispatch_message(frame)

    assert listener.response_queue.get_nowait() == frame


class _DiscoveryIdleCoordinator:
    """Just enough coordinator for dispatch_message."""

    discovery_running = False
    discovery_module_address = None


def test_the_output_state_signals_are_left_alone():
    """0x12 must keep its old answer signal - it is the module-state path."""
    handler = NikobusCommandHandler.__new__(NikobusCommandHandler)
    ack, answer = handler._prepare_ack_and_answer_signals(
        make_pc_link_command(0x12, "0E6C"), "0E6C"
    )
    assert (ack, answer) == ("$0512", "$1C6C0E")
