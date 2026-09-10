"""Desktop stand-in for CircuitPython's ``micropython`` module."""


def const(value):
    """On CircuitPython this is a compiler hint; here it is the identity."""
    return value
