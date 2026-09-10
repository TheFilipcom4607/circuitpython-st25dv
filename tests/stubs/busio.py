"""Desktop stand-in for CircuitPython's ``busio`` module.

Enough of an I2C to construct an ST25DV; it answers no traffic, so only the
pure-logic paths are exercised against it. ``tests/fake_st25dv.py`` supplies a
bus that does answer.
"""


class I2C:
    def __init__(self, scl=None, sda=None, frequency=100000):
        self.scl, self.sda, self.frequency = scl, sda, frequency
        self._locked = False
        #: Set by deinit(), so a test can tell an owned bus from a borrowed one.
        self.deinited = False

    def try_lock(self):
        if self._locked:
            return False
        self._locked = True
        return True

    def unlock(self):
        self._locked = False

    def readfrom_into(self, address, buf, start=0, end=None):
        raise OSError("no device on the stub bus")

    def writeto(self, address, buf):
        raise OSError("no device on the stub bus")

    def writeto_then_readfrom(self, address, out_buf, in_buf, out_start=0,
                              out_end=None, in_start=0, in_end=None):
        raise OSError("no device on the stub bus")

    def deinit(self):
        self.deinited = True
