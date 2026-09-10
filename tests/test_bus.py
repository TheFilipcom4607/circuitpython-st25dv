"""Who owns the I2C lock, and who is allowed to give it back.

``try_lock`` answers "is it free", never "is it mine". A driver that reads the
second meaning into it will hand another bus master's lock back mid-transfer,
which on a shared STEMMA QT chain is a corrupted transaction somewhere else
entirely and nothing at all in this driver's own logs. These tests pin the
ownership rules down.
"""
import pytest

from fake_st25dv import FakeST25DV
from st25dv import BusyError, ST25DV, _Bus


@pytest.fixture
def chip():
    return FakeST25DV(2048)


@pytest.fixture
def tag(chip):
    return ST25DV(chip)


def bus(i2c, timeout=0.05):
    return _Bus(i2c, lambda: timeout)


def test_a_plain_block_locks_and_releases(chip):
    handle = bus(chip)
    with handle:
        assert chip._locked
    assert not chip._locked


def test_the_lock_a_block_never_took_is_not_handed_back(chip):
    """The regression this file exists for.

    A ``_locked`` flag that is set on the first successful lock and never
    cleared stays true for the life of the object, so the *next* block --
    entered while somebody else holds the bus -- unlocks on the way out and
    steals it. The failing acquire has to raise instead.
    """
    handle = bus(chip)
    with handle:                            # first block succeeds, as before
        pass
    assert chip.try_lock()                  # now somebody else owns the bus
    with pytest.raises(BusyError):
        with handle:
            pass
    assert chip._locked, "the other owner's lock was handed back"


def test_waiting_for_the_bus_is_bounded_and_named(chip):
    chip.try_lock()
    with pytest.raises(BusyError) as err:
        with bus(chip, 0.01):
            pass
    assert "held by something else" in str(err.value)


def test_a_bus_that_frees_up_is_waited_for_rather_than_failed(chip):
    """try_lock is polled, so a lock held for less than the timeout is fine."""
    chip.try_lock()
    releases = []

    class Freeing:
        """Frees the bus on the third try_lock, mid-spin."""

        def __init__(self, inner):
            self._inner = inner
            self.tries = 0

        def try_lock(self):
            self.tries += 1
            if self.tries == 3:
                self._inner.unlock()
                releases.append(self.tries)
            return self._inner.try_lock()

        def unlock(self):
            self._inner.unlock()

    freeing = Freeing(chip)
    with bus(freeing, 1.0):
        assert chip._locked
    assert releases == [3]
    assert not chip._locked


def test_nesting_holds_the_lock_until_the_outermost_block_exits(chip):
    """The re-entrancy the docstring promises, which a boolean cannot give."""
    handle = bus(chip)
    with handle:
        with handle:
            with handle:
                assert chip._locked
            assert chip._locked, "an inner block released the outer one's lock"
        assert chip._locked
    assert not chip._locked


def test_the_wait_follows_busy_timeout_when_it_is_changed(chip):
    """busy_timeout is a public attribute callers do change at runtime."""
    tag = ST25DV(chip)
    tag.busy_timeout = 0.01
    chip.try_lock()
    with pytest.raises(BusyError) as err:
        with tag._bus:
            pass
    assert "10 ms" in str(err.value)


def test_ordinary_traffic_leaves_the_bus_unlocked(tag, chip):
    """Every transfer path has to balance; a leak here deadlocks the next one."""
    tag.read(0, 8)
    assert not chip._locked
    tag.write(0, b"\x01\x02\x03\x04")
    assert not chip._locked
    tag.poll_events()
    assert not chip._locked


def test_a_nacked_write_still_leaves_the_bus_unlocked(chip, tag):
    """The retry loop enters and leaves the block once per attempt."""
    chip.rf_busy = True
    tag.busy_timeout = 0.01
    with pytest.raises(BusyError):
        tag.read(0, 4)
    assert not chip._locked


# -- who owns the bus object itself ------------------------------------------

def test_a_bus_built_by_from_pins_is_released_by_deinit():
    """from_pins makes the bus, so the object that comes back has to free it.

    Left undone, a script that builds a tag in a loop, or one that hands the
    pins to something else afterwards, runs out of I2C peripherals with nothing
    to point at.
    """
    tag = ST25DV.from_pins(None, None, probe=False)
    assert tag._owns_i2c
    bus_object = tag._i2c
    tag.deinit()
    assert bus_object.deinited
    tag.deinit()                            # idempotent, not a double free


def test_a_bus_handed_in_by_the_caller_is_left_alone(chip):
    """board.STEMMA_I2C() is shared; deinit-ing it would take out the others."""
    tag = ST25DV(chip)
    assert not tag._owns_i2c
    tag.deinit()
    assert not chip.deinited


def test_deinit_releases_a_gpo_pin_this_object_made(chip):
    from digitalio import DigitalInOut

    tag = ST25DV(chip, gpo=object())
    assert tag._owns_gpo
    pin = tag.gpo_pin
    tag.deinit()
    assert tag.gpo_pin is None
    assert isinstance(pin, DigitalInOut)
