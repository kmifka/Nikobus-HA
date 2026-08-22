"""Nikobus system heartbeat — is the installation still alive?

What this answers that nothing else does
----------------------------------------
``nkbconnect.NikobusConnect.is_connected`` knows whether the *transport* is
open. That is a different question from whether the *installation* is still
working, and the two fail independently: the serial port can be perfectly open
while the PC-Link behind it has stopped doing anything.

This installation cannot be asked the obvious way. It was measured end to end
on 22.08.2026 (see const.HEARTBEAT_FUNCTION_CODE for the full write-up):

* It has no feedback module and runs prior_gen3.
* 0x12 and 0x17, the output-state queries, get NO answer at all. There is no
  output-state feedback here; that is confirmed and cannot be changed.
* Of all 254 function codes tried, only 0x10, 0x11, 0x14, 0x18, 0x19, 0x1B,
  0x1C, 0x1D, 0x1E, 0x1F and 0x20 answer anything. From 0x21 upwards everything
  is silent, and most of the codes that do answer reply $0E, "unknown
  response". Response time was 0.20-0.21 s throughout.
* 0x1D sent to address 9E62 answers with a frame containing a RUNNING CLOCK:

      $051D $1C FF 629E 9BC1 0000 <MM> <SS> <CRC16> <CRC8>

  MM and SS are one byte each, read as hexadecimal, and the seconds roll over
  at 60, not at 0xFF: 0x14 0x3A = 20:58 is followed by 0x15 0x01 = 21:01.
  Across 43 samples in 141 s that clock ran exactly in step with the wall
  clock - not one sample deviated by more than 1.5 s - and running the shutters
  up and down while sampling did not disturb it.

Why the clock is the right liveness signal
------------------------------------------
Any constant answer only proves that *someone* replied. It can come out of a
buffer that a hung device keeps serving forever. That is precisely how a
weather station slipped through on 21.08.2026: a valid, plausible reading from
a device that had been dead for hours. A clock cannot lie that way. If it
stands still while real time passes, the device is stuck - however politely it
keeps acknowledging.

So each poll checks three separate things:

a) did anything come back at all,
b) can MM:SS be read out of it,
c) has the clock moved on by the time that actually elapsed since the last
   poll (modulo one hour, because both the minutes and the seconds wrap).

The ping goes through ``NikobusCommandHandler.get_system_clock``, which queues
it like every other command. Nothing here ever touches the transport directly;
see that method for why that is a requirement rather than a preference.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .const import (
    HEARTBEAT_CLOCK_PERIOD,
    HEARTBEAT_CLOCK_TOLERANCE,
    HEARTBEAT_FAILURE_THRESHOLD,
    HEARTBEAT_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Layout of the clock answer, as measured on 22.08.2026. Byte offsets are
# counted from the start of the answer signal, i.e. from the "$1CFF" that
# nkbcommand builds for function code 0x1D:
#
#   $1C FF | 629E | 9BC1 0000 | MM | SS | CRC16 | CRC8
#   ^ answer signal ends here ^
#
# "9BC1 0000" was byte-identical in every one of the 43 samples, so its meaning
# is unknown and it is simply skipped. MM and SS follow immediately after it.
_CLOCK_ANSWER_PREFIX = "$1CFF"
_CLOCK_SKIP_HEX_CHARS = 8  # the four unexplained constant bytes
_CLOCK_FIELD_HEX_CHARS = 4  # MM and SS, one byte each


def clock_answer_signal(address: str) -> str:
    """Return the substring that marks the clock answer for ``address``.

    The address bytes appear byte-swapped in the answer, exactly as they do for
    every other PC-Link reply: 9E62 is answered as ``629E``.
    """
    address = address.strip().upper()
    return f"{_CLOCK_ANSWER_PREFIX}{address[2:4]}{address[0:2]}"


def parse_clock_seconds(message: str, address: str) -> Optional[int]:
    """Read MM:SS out of a clock answer and return it as seconds 0..3599.

    Returns ``None`` when the frame is not a clock answer for this address, is
    truncated, or carries values that cannot be a clock (MM or SS above 59).
    ``None`` deliberately means "no clock in here", never "the clock is fine" -
    an answer we cannot read is not evidence of a working installation.
    """
    if not message:
        return None

    signal = clock_answer_signal(address)
    index = message.find(signal)
    if index < 0:
        return None

    payload = message[index + len(signal) :]
    needed = _CLOCK_SKIP_HEX_CHARS + _CLOCK_FIELD_HEX_CHARS
    if len(payload) < needed:
        return None

    field = payload[_CLOCK_SKIP_HEX_CHARS : needed]
    try:
        minutes = int(field[0:2], 16)
        seconds = int(field[2:4], 16)
    except ValueError:
        return None

    # The seconds roll over at 60, not at 0xFF (0x3A = 58 is followed by 0x01 in
    # the next minute), so anything above 59 in either field is not a clock -
    # most likely a $0E "unknown response" that happened to contain the signal.
    if minutes > 59 or seconds > 59:
        return None

    return minutes * 60 + seconds


def clock_advance_is_plausible(
    previous: int,
    current: int,
    elapsed: float,
    tolerance: float = HEARTBEAT_CLOCK_TOLERANCE,
) -> Optional[bool]:
    """Has the clock moved on by the time that really elapsed?

    ``previous`` and ``current`` are clock readings in seconds (0..3599),
    ``elapsed`` is the monotonic time between the two readings. Returns ``True``
    if they agree within ``tolerance``, ``False`` if the clock is standing still
    or jumping, and ``None`` if the two samples cannot be compared at all.

    Both fields wrap, so the clock only spans one hour and all arithmetic on it
    is modulo HEARTBEAT_CLOCK_PERIOD. 59:58 -> 00:01 is a three-second step
    forward, not a 3597-second jump backwards. Once ``elapsed`` reaches a full
    hour the modulo becomes ambiguous - 10 s and 3610 s look identical - and
    the honest answer is ``None`` rather than a coin flip. That happens after
    an outage, not during normal operation: the poll interval is 30 s.

    The tolerance is measured, not guessed: the clock has one-second resolution,
    so any single comparison is quantised by +-1 s, and across the 43 samples of
    22.08.2026 it never drifted more than 1.5 s from the wall clock.
    """
    if elapsed < 0 or elapsed >= HEARTBEAT_CLOCK_PERIOD:
        return None

    observed = (current - previous) % HEARTBEAT_CLOCK_PERIOD
    delta = observed - elapsed
    # Fold the difference into [-1800, +1800) so a clock that slipped one second
    # backwards reads as -1 s rather than as +3599 s.
    if delta >= HEARTBEAT_CLOCK_PERIOD / 2:
        delta -= HEARTBEAT_CLOCK_PERIOD
    return abs(delta) <= tolerance


def clock_is_standing_still(
    previous: int,
    current: int,
    tolerance: float = HEARTBEAT_CLOCK_TOLERANCE,
) -> bool:
    """Has the clock not moved at all between the two readings?

    This is the actual hang detector, and it is deliberately narrower than
    ``clock_advance_is_plausible``. A reading can disagree with the elapsed time
    in two very different ways:

    * it did not move -> the device is stuck. Nothing else produces that: a
      cached answer repeats, a live clock cannot.
    * it moved, but not by the elapsed amount -> the device is demonstrably
      running, its clock simply is not where we left it. That is what a device
      looks like the moment it comes back from a hang or a power cycle, and
      treating it as another failure would keep the covers unavailable for an
      extra poll after the installation is already working again.

    Only the first is counted as a failure.
    """
    moved = (current - previous) % HEARTBEAT_CLOCK_PERIOD
    return moved <= tolerance or moved >= HEARTBEAT_CLOCK_PERIOD - tolerance


def format_clock(seconds: Optional[int]) -> Optional[str]:
    """Render a clock reading as MM:SS for the diagnostic attributes."""
    if seconds is None:
        return None
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class NikobusHeartbeat:
    """Polls the installation's clock and decides whether it is still alive."""

    def __init__(
        self,
        coordinator: Any,
        address: str | None,
        interval: float = HEARTBEAT_INTERVAL,
        tolerance: float = HEARTBEAT_CLOCK_TOLERANCE,
        failure_threshold: int = HEARTBEAT_FAILURE_THRESHOLD,
        time_source: Any = None,
    ) -> None:
        """Initialise the heartbeat.

        ``address`` may be ``None`` or empty. The heartbeat is then permanently
        disabled and reports the installation as alive: an installation whose
        clock address nobody knows must not have its covers taken away on a
        guess. See const.CONF_HEARTBEAT_ADDRESS.
        """
        self._coordinator = coordinator
        self._address = (address or "").strip().upper() or None
        self._interval = float(interval)
        self._tolerance = float(tolerance)
        self._failure_threshold = int(failure_threshold)
        self._time_source = time_source

        self._task: asyncio.Task | None = None
        self._is_alive: bool = True
        self._consecutive_failures: int = 0
        self._last_clock_seconds: int | None = None
        self._last_clock_at: float | None = None
        self._last_ok: datetime | None = None
        self._last_reason: str | None = None

    # -------------------------
    # Public API
    # -------------------------
    @property
    def enabled(self) -> bool:
        """Whether a clock address is configured at all."""
        return self._address is not None

    @property
    def address(self) -> str | None:
        """The configured clock address, or ``None`` when disabled."""
        return self._address

    @property
    def is_alive(self) -> bool:
        """Whether the installation is currently considered responsive.

        Stays ``True`` until HEARTBEAT_FAILURE_THRESHOLD consecutive polls have
        failed, so a single lost answer does not take the covers down. See the
        threshold constant for why that number is a starting value and not a
        measurement.
        """
        return self._is_alive

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive failed polls since the last good one."""
        return self._consecutive_failures

    @property
    def last_ok(self) -> datetime | None:
        """UTC timestamp of the last poll that proved the clock is running."""
        return self._last_ok

    @property
    def last_clock(self) -> str | None:
        """Last clock reading as MM:SS, for the diagnostic attributes."""
        return format_clock(self._last_clock_seconds)

    @property
    def last_reason(self) -> str | None:
        """Why the last poll was counted the way it was."""
        return self._last_reason

    async def start(self) -> None:
        """Start polling, unless no clock address is configured."""
        if not self.enabled:
            _LOGGER.info(
                "Nikobus heartbeat disabled: no clock address configured. Cover "
                "availability will follow the transport only."
            )
            return
        if self._task is not None and not self._task.done():
            return
        self._task = self._coordinator.hass.async_create_background_task(
            self._run(), name="nikobus_heartbeat"
        )
        _LOGGER.info(
            "Nikobus heartbeat started: querying the clock at %s every %.0fs",
            self._address,
            self._interval,
        )

    async def stop(self) -> None:
        """Stop polling."""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            _LOGGER.debug("Nikobus heartbeat stopped.")

    async def async_poll(self) -> bool:
        """Run one ping and fold the result into the liveness verdict.

        Returns the current value of ``is_alive``. Never raises: a heartbeat
        that dies of its own exception is worse than no heartbeat, because the
        covers would silently stay "available" forever afterwards.
        """
        if not self.enabled:
            return True

        skip = self._reason_to_skip()
        if skip is not None:
            # Not a failure. Either the transport is already known to be down -
            # in which case the reconnect owns the problem and cover
            # availability is already False for that reason - or discovery is
            # flooding the bus and no answer would prove anything.
            _LOGGER.debug("Nikobus heartbeat skipped: %s", skip)
            self._forget_baseline()
            return self._is_alive

        command = getattr(self._coordinator, "nikobus_command", None)
        if command is None:
            self._forget_baseline()
            return self._is_alive

        try:
            answer = await command.get_system_clock(self._address)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - any failure means "no answer"
            return self._register_failure(f"no answer to the clock query: {err}")

        # (a) something came back at all.
        if not answer:
            return self._register_failure("empty answer to the clock query")

        now = self._now()

        # (b) MM:SS can be read out of it.
        clock = parse_clock_seconds(answer, self._address)
        if clock is None:
            # Most codes on this installation answer $0E, "unknown response".
            # Getting one here almost always means the configured clock address
            # is not the right one for this installation.
            return self._register_failure(
                f"answer carried no readable clock (address {self._address} "
                f"may be wrong): {answer}"
            )

        previous = self._last_clock_seconds
        previous_at = self._last_clock_at
        self._last_clock_seconds = clock
        self._last_clock_at = now

        if previous is None or previous_at is None:
            # First sample after a start or an outage: there is nothing to
            # compare against yet, and (a) plus (b) already hold.
            return self._register_success("clock readable, first sample")

        # (c) the clock moved on by the time that really passed.
        plausible = clock_advance_is_plausible(
            previous, clock, now - previous_at, self._tolerance
        )
        if plausible is None:
            return self._register_success("clock readable, gap too long to compare")
        if not plausible:
            if clock_is_standing_still(previous, clock, self._tolerance):
                return self._register_failure(
                    f"clock did not advance: {format_clock(previous)} -> "
                    f"{format_clock(clock)} across {now - previous_at:.1f}s"
                )
            # It moved, just not by the elapsed amount. The installation is
            # running - this is what it looks like right after a hang or a
            # power cycle - so it is not counted as a failure. Logged because
            # on a healthy bus it should never happen: the readings are taken
            # when the answers arrive, so queue delays cannot cause it.
            _LOGGER.info(
                "Nikobus clock jumped: %s -> %s across %.1fs (expected ~%.0fs). "
                "Treating the installation as alive and re-baselining.",
                format_clock(previous),
                format_clock(clock),
                now - previous_at,
                now - previous_at,
            )
            return self._register_success("clock jumped, re-baselined")
        return self._register_success("clock running")

    # -------------------------
    # Internals
    # -------------------------
    def _now(self) -> float:
        """Monotonic seconds. Injectable so the tests do not have to wait."""
        if self._time_source is not None:
            return float(self._time_source())
        return asyncio.get_running_loop().time()

    def _reason_to_skip(self) -> str | None:
        """Return why this poll should not run, or ``None`` to go ahead."""
        connection = getattr(self._coordinator, "nikobus_connection", None)
        if connection is not None and not connection.is_connected:
            return "transport is down, the reconnect owns this"
        if getattr(self._coordinator, "discovery_running", False):
            return "discovery is running"
        return None

    def _forget_baseline(self) -> None:
        """Drop the previous clock reading.

        Called whenever polling was skipped. After a gap of unknown length -
        and worse, across a possible power cycle, which restarts the clock -
        the old reading is not a baseline any more. Comparing against it would
        report a perfectly healthy installation as stuck.
        """
        self._last_clock_seconds = None
        self._last_clock_at = None

    def _register_success(self, reason: str) -> bool:
        """Record a good poll and, if this ends an outage, say so."""
        self._last_reason = reason
        self._last_ok = datetime.now(timezone.utc)
        recovered = not self._is_alive
        had_failures = self._consecutive_failures > 0
        self._consecutive_failures = 0
        self._is_alive = True
        if recovered:
            _LOGGER.info(
                "Nikobus installation is answering again (%s at %s).",
                reason,
                self.last_clock,
            )
        elif had_failures:
            _LOGGER.debug("Nikobus heartbeat recovered before the threshold: %s", reason)
        # Repaint after EVERY good poll, not only on a transition. A verdict
        # that is only published when it changes is indistinguishable, from the
        # outside, from a verdict nobody is computing any more: if this task
        # dies quietly, `heartbeat_alive` stays True and `heartbeat_last_ok`
        # keeps whatever timestamp it had, forever. That is exactly the failure
        # that let a dead weather station read as healthy on 21.08.2026 - a
        # valid, plausible value from a device that had stopped.
        #
        # Publishing `last_ok` on every poll turns it into a freshness signal:
        # an outside watcher can require it to be no older than a few poll
        # intervals and will notice a heartbeat that stopped running, which no
        # amount of reading `heartbeat_alive` could ever tell it.
        self._notify()
        return True

    def _register_failure(self, reason: str) -> bool:
        """Record a bad poll and flip the verdict once the threshold is met."""
        self._last_reason = reason
        self._consecutive_failures += 1
        if not self._is_alive:
            # Already reported. Stay quiet - an outage over a weekend is
            # thousands of polls, and a WARNING per poll is what makes a log
            # unreadable (same reasoning as nkbconnect._log_outage).
            _LOGGER.debug(
                "Nikobus heartbeat still failing (%d): %s",
                self._consecutive_failures,
                reason,
            )
            return False

        if self._consecutive_failures < self._failure_threshold:
            _LOGGER.debug(
                "Nikobus heartbeat failed (%d/%d): %s",
                self._consecutive_failures,
                self._failure_threshold,
                reason,
            )
            return True

        self._is_alive = False
        _LOGGER.warning(
            "Nikobus installation is not responding: %d consecutive heartbeat "
            "failures (~%.0fs). Last reason: %s. Covers are now unavailable.",
            self._consecutive_failures,
            self._consecutive_failures * self._interval,
            reason,
        )
        self._notify()
        return False

    def _notify(self) -> None:
        """Push the changed verdict to the entities that read it."""
        notify = getattr(self._coordinator, "async_update_listeners", None)
        if notify is None:
            return
        try:
            notify()
        except Exception:  # pragma: no cover - a repaint must not kill the loop
            _LOGGER.debug("Heartbeat state notification failed", exc_info=True)

    async def _run(self) -> None:
        """Poll forever, one query every ``interval`` seconds."""
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self.async_poll()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - the loop must outlive bugs
                _LOGGER.exception("Nikobus heartbeat poll crashed - continuing")
