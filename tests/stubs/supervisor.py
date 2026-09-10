"""Desktop stand-in for CircuitPython's ``supervisor`` module."""
import time

_TICKS_PERIOD = 1 << 29


def ticks_ms():
    """Milliseconds, wrapping at 2**29 exactly as CircuitPython's does."""
    return int(time.monotonic() * 1000) % _TICKS_PERIOD
