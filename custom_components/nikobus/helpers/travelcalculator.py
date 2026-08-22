"""
Module TravelCalculator provides functionality for predicting the current position of a Cover.

Position is estimated purely from elapsed time: a drive command is sent, a
timer starts, and the position is interpolated between the two end stops. That
only holds while the command actually reached the bus.

On 21.08.2026 it did not. The FTDI adapter had been re-enumerated by a reboot
(/dev/ttyUSB1 -> /dev/ttyUSB0), every command died in the writer, and for two
hours Home Assistant reported confident intermediate positions for blinds that
had never moved. Nothing looked like an error; the model and the house had
simply drifted apart.

So the calculator can now be *unknown* as well as wrong-free:

* ``mark_position_unknown()`` throws the estimate away. Used when the
  connection dies mid-travel, where freezing the last value would be the worse
  choice - the shutter is still moving (the stop command cannot get through
  either) and will most likely run into an end stop, so any frozen
  intermediate number is confidently wrong. "Unknown" is honest, and it is
  recoverable; a wrong number is not.
* While unknown, ``current_position()`` returns ``None`` and stays ``None``.
* Only a full run into an end stop restores a reference (``start_travel`` to
  ``position_open`` or ``position_closed``). Nikobus roller relays report no
  position, so there is nothing else to ask. That run is treated as a resync:
  the position stays unknown for its whole duration and becomes a fact at the
  moment the end stop is reached.

NOTE ON PROVENANCE: upstream fdebrus/Nikobus-HA 3.x does NOT do this. Its
``nkbtravelcalculator.py`` is 65 lines of pure time arithmetic with no notion
of the connection, and its ``cover.py`` never asks whether the transport is
alive. This is a deliberate improvement over upstream, not something that was
missed while porting - do not drop it when moving onto the 3.x base.
"""

from __future__ import annotations

import logging
import time
from enum import Enum

_LOGGER = logging.getLogger(__name__)


class TravelStatus(Enum):
    """Enum class for travel status."""

    DIRECTION_UP = 1
    DIRECTION_DOWN = 2
    STOPPED = 3


class TravelCalculator:
    """Class for calculating the current position of a cover."""

    __slots__ = (
        "_last_known_position",
        "_last_known_position_timestamp",
        "_position_confirmed",
        "_position_known",
        "_resyncing",
        "_travel_to_position",
        "position_closed",
        "position_open",
        "travel_direction",
        "travel_time_down",
        "travel_time_up",
    )

    def __init__(self, travel_time_down: float, travel_time_up: float) -> None:
        """Initialize TravelCalculator class."""
        self.travel_direction = TravelStatus.STOPPED
        self.travel_time_down = travel_time_down
        self.travel_time_up = travel_time_up

        self._last_known_position: int | None = None
        self._last_known_position_timestamp: float = 0.0
        self._position_confirmed: bool = False
        self._travel_to_position: int | None = None

        #: False until something establishes a reference: a restored state, an
        #: explicit set_position, or a completed run into an end stop.
        self._position_known: bool = False
        #: True while a full run into an end stop is re-establishing that
        #: reference after the position was thrown away.
        self._resyncing: bool = False

        # 100 is open, 0 is closed
        self.position_closed: int = 0
        self.position_open: int = 100

    @property
    def position_known(self) -> bool:
        """Return whether the calculator currently has a usable reference."""
        return self._position_known

    def set_position(self, position: int) -> None:
        """Set position and target of cover."""
        self._travel_to_position = position
        self.update_position(position)

    def update_position(self, position: int) -> None:
        """Update known position of cover."""
        self._last_known_position = position
        self._last_known_position_timestamp = time.time()
        # Anything that hands us a real position also ends an unknown phase.
        self._position_known = True
        self._resyncing = False
        if position == self._travel_to_position:
            self._position_confirmed = True

    def mark_position_unknown(self) -> None:
        """Discard the estimate; the model can no longer be trusted.

        Called when the connection dies while the cover is travelling. See the
        module docstring for why the last estimate is dropped rather than
        frozen. Idempotent.
        """
        if not self._position_known and not self._resyncing:
            return
        _LOGGER.warning(
            "Cover position discarded: the connection dropped mid-travel, so the "
            "shutter kept moving without anything left to measure it against."
        )
        self._position_known = False
        self._resyncing = False
        self._position_confirmed = False
        self._last_known_position = None
        self._travel_to_position = None
        self.travel_direction = TravelStatus.STOPPED

    def stop(self) -> None:
        """Stop traveling."""
        if not self._position_known:
            # Stopped part-way through a resync run: the shutter never reached
            # the end stop, so nothing was learned and the position stays
            # unknown. Without this branch the old code would fall through to
            # ``current_position() is None -> return`` and silently leave
            # ``_resyncing`` armed, so the next read would "complete" a resync
            # that never happened.
            self._resyncing = False
            self._travel_to_position = None
            self.travel_direction = TravelStatus.STOPPED
            return
        stop_position = self.current_position()
        if stop_position is None:
            return
        self._last_known_position = stop_position
        self._travel_to_position = stop_position
        self._position_confirmed = False
        self.travel_direction = TravelStatus.STOPPED

    def start_travel(self, _travel_to_position: int) -> None:
        """Start traveling to position."""
        if not self._position_known:
            # Recovery after an outage. Only a full run into an end stop gives
            # a trustworthy reference back - a Nikobus roller relay reports no
            # position, so there is nothing to query and no other way to learn
            # where the shutter is. An intermediate target cannot resync
            # anything, so the calculator stays out of it entirely and the
            # position remains unknown.
            if _travel_to_position in (self.position_open, self.position_closed):
                self._begin_resync(_travel_to_position)
            else:
                _LOGGER.debug(
                    "Ignoring travel to %s%%: position is unknown and only a "
                    "full open/close can re-establish it.",
                    _travel_to_position,
                )
            return
        if self._last_known_position is None:
            self.set_position(_travel_to_position)
            return
        self.stop()
        self._last_known_position_timestamp = time.time()
        self._travel_to_position = _travel_to_position
        self._position_confirmed = False

        self.travel_direction = (
            TravelStatus.DIRECTION_DOWN
            if _travel_to_position < self._last_known_position
            else TravelStatus.DIRECTION_UP
        )

    def start_travel_up(self) -> None:
        """Start traveling up."""
        self.start_travel(self.position_open)

    def start_travel_down(self) -> None:
        """Start traveling down."""
        self.start_travel(self.position_closed)

    def _begin_resync(self, target: int) -> None:
        """Start the full run into an end stop that restores the reference.

        The starting point is assumed to be the *opposite* end stop: that is
        the worst case, so the full travel time has to elapse before the target
        is declared reached. Assuming anything shorter would let the position
        become "known" while the shutter is still moving - the exact class of
        confident-but-wrong value this whole mechanism exists to avoid.
        """
        self._resyncing = True
        self._position_confirmed = False
        self._last_known_position = (
            self.position_closed if target == self.position_open else self.position_open
        )
        self._last_known_position_timestamp = time.time()
        self._travel_to_position = target
        self.travel_direction = (
            TravelStatus.DIRECTION_UP
            if target == self.position_open
            else TravelStatus.DIRECTION_DOWN
        )
        _LOGGER.info(
            "Cover position unknown; running to %s%% to re-establish it.", target
        )

    def current_position(self) -> int | None:
        """Return current (calculated or known) position, or None if unknown."""
        if not self._position_known:
            if not self._resyncing:
                return None
            position = self._calculate_position()
            if position != self._travel_to_position:
                # Still on the way to the end stop: honestly unknown.
                return None
            # The end stop has been reached, so the shutter is physically
            # against it. That is a fact, not an estimate, and it is the only
            # thing that ends an unknown phase on its own. Completing the
            # resync here (rather than in a separate tick) is what makes the
            # transition happen the first time anybody looks.
            self._last_known_position = position
            self._position_confirmed = True
            self._position_known = True
            self._resyncing = False
            _LOGGER.info("Cover position re-established at %s%%.", position)
            return position
        if not self._position_confirmed:
            return self._calculate_position()
        return self._last_known_position

    def is_traveling(self) -> bool:
        """Return if cover is traveling."""
        return self.current_position() != self._travel_to_position

    def is_opening(self) -> bool:
        """Return if the cover is opening."""
        return (
            self.is_traveling()
            and self.travel_direction == TravelStatus.DIRECTION_UP
        )

    def is_closing(self) -> bool:
        """Return if the cover is closing."""
        return (
            self.is_traveling()
            and self.travel_direction == TravelStatus.DIRECTION_DOWN
        )

    def position_reached(self) -> bool:
        """Return if cover has reached designated position."""
        return self.current_position() == self._travel_to_position

    def is_open(self) -> bool:
        """Return if cover is (fully) open."""
        return self.current_position() == self.position_open

    def is_closed(self) -> bool:
        """Return if cover is (fully) closed."""
        return self.current_position() == self.position_closed

    def _calculate_position(self) -> int | None:
        """Return calculated position."""
        if self._travel_to_position is None or self._last_known_position is None:
            return self._last_known_position
        relative_position = self._travel_to_position - self._last_known_position

        def position_reached_or_exceeded(relative_position: int) -> bool:
            """Return if designated position was reached."""
            return (
                relative_position >= 0
                and self.travel_direction == TravelStatus.DIRECTION_DOWN
            ) or (
                relative_position <= 0
                and self.travel_direction == TravelStatus.DIRECTION_UP
            )

        if position_reached_or_exceeded(relative_position):
            return self._travel_to_position

        remaining_travel_time = self.calculate_travel_time(
            from_position=self._last_known_position,
            to_position=self._travel_to_position,
        )
        if time.time() > self._last_known_position_timestamp + remaining_travel_time:
            return self._travel_to_position

        progress = (
            time.time() - self._last_known_position_timestamp
        ) / remaining_travel_time
        return int(self._last_known_position + relative_position * progress)

    def calculate_travel_time(self, from_position: int, to_position: int) -> float:
        """Calculate time to travel from one position to another."""
        travel_range = to_position - from_position
        travel_time_full = (
            self.travel_time_up if travel_range > 0 else self.travel_time_down
        )
        return travel_time_full * abs(travel_range) / self.position_open
