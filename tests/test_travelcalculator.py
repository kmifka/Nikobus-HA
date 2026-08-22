"""Tests for the unknown-position handling in the travel calculator.

The calculator estimates position purely from elapsed time. That is only valid
while the drive commands actually reach the bus - which on 21.08.2026 they did
not, for two hours, while Home Assistant reported confident percentages for
blinds that never moved.

Note this behaviour has no upstream counterpart: upstream 3.x's
nkbtravelcalculator.py is pure time arithmetic with no notion of the
connection.
"""

from __future__ import annotations

from custom_components.nikobus.helpers.travelcalculator import (
    TravelCalculator,
    TravelStatus,
)


def _calculator() -> TravelCalculator:
    return TravelCalculator(travel_time_down=20.0, travel_time_up=20.0)


def _rewind(calculator: TravelCalculator, seconds: float) -> None:
    """Pretend the given number of seconds of travel have elapsed."""
    calculator._last_known_position_timestamp -= seconds


def test_a_fresh_calculator_has_no_reference():
    calculator = _calculator()
    assert calculator.position_known is False
    assert calculator.current_position() is None


def test_set_position_establishes_the_reference():
    calculator = _calculator()
    calculator.set_position(40)
    assert calculator.position_known is True
    assert calculator.current_position() == 40


def test_mark_position_unknown_discards_the_estimate():
    """The last value is dropped, not frozen."""
    calculator = _calculator()
    calculator.set_position(100)
    calculator.start_travel_down()
    _rewind(calculator, 8)
    mid = calculator.current_position()
    assert mid is not None and 0 < mid < 100

    calculator.mark_position_unknown()

    assert calculator.position_known is False
    assert calculator.current_position() is None
    assert calculator.current_position() != mid
    assert calculator.is_traveling() is False
    assert calculator.travel_direction is TravelStatus.STOPPED


def test_mark_position_unknown_is_idempotent():
    calculator = _calculator()
    calculator.set_position(50)
    calculator.mark_position_unknown()
    calculator.mark_position_unknown()
    assert calculator.current_position() is None


def test_an_intermediate_target_cannot_restore_the_reference():
    """Only an end stop is a real reference; 50% is a guess."""
    calculator = _calculator()
    calculator.set_position(70)
    calculator.mark_position_unknown()

    calculator.start_travel(50)

    assert calculator.position_known is False
    assert calculator.current_position() is None


def test_a_full_run_into_the_end_stop_restores_the_reference():
    """Unknown for the whole run, a fact the moment the end stop is reached."""
    calculator = _calculator()
    calculator.set_position(70)
    calculator.mark_position_unknown()

    calculator.start_travel_up()

    # Part-way through: still moving, still unknown - deliberately not an
    # interpolated number, because the starting point is only an assumption.
    _rewind(calculator, 5)
    assert calculator.current_position() is None
    assert calculator.position_known is False
    assert calculator.is_opening() is True

    # The full travel time from the opposite end stop has now elapsed.
    _rewind(calculator, 20)
    assert calculator.current_position() == 100
    assert calculator.position_known is True
    assert calculator.is_open() is True
    assert calculator.is_traveling() is False


def test_resync_assumes_the_worst_case_starting_point():
    """A resync must not finish early.

    The shutter could have been anywhere, so the run is measured as if it
    started at the far end stop. Anything shorter would declare a position
    known while the blind is still moving.
    """
    calculator = _calculator()
    calculator.set_position(90)  # very close to open...
    calculator.mark_position_unknown()
    calculator.start_travel_up()

    # ...but 15 s (of the 20 s full travel) is still not enough.
    _rewind(calculator, 15)
    assert calculator.current_position() is None

    _rewind(calculator, 6)
    assert calculator.current_position() == 100


def test_stopping_mid_resync_leaves_the_position_unknown():
    """An aborted resync learned nothing."""
    calculator = _calculator()
    calculator.set_position(30)
    calculator.mark_position_unknown()
    calculator.start_travel_down()

    _rewind(calculator, 4)
    calculator.stop()

    assert calculator.position_known is False
    assert calculator.current_position() is None
    assert calculator.travel_direction is TravelStatus.STOPPED

    # And the aborted run must not silently complete later.
    _rewind(calculator, 60)
    assert calculator.current_position() is None


def test_normal_travel_is_unaffected():
    """The ordinary path keeps behaving exactly as before."""
    calculator = _calculator()
    calculator.set_position(0)
    calculator.start_travel_up()

    _rewind(calculator, 10)
    position = calculator.current_position()
    assert position is not None and 40 <= position <= 60
    assert calculator.is_opening() is True

    _rewind(calculator, 15)
    assert calculator.current_position() == 100
    assert calculator.is_open() is True
