"""Desktop stand-in for CircuitPython's ``digitalio`` module."""


class Pull:
    UP = "UP"
    DOWN = "DOWN"


class DriveMode:
    PUSH_PULL = "PUSH_PULL"
    OPEN_DRAIN = "OPEN_DRAIN"


class DigitalInOut:
    def __init__(self, pin):
        self.pin = pin
        self.value = False
        self.deinited = False

    def switch_to_input(self, pull=None):
        self.pull = pull

    def switch_to_output(self, value=False, drive_mode=DriveMode.PUSH_PULL):
        self.value = value

    def deinit(self):
        self.deinited = True
