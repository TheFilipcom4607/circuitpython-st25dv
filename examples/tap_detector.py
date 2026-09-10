"""Report taps and RF writes using nothing but the STEMMA QT cable.

The GPO pin is the documented way to get interrupts out of an ST25DV, but it
needs a wire. Everything it signals is also in IT_STS_Dyn, which is one I2C
read away, so a board with only the four-wire cable can still react.
"""
import board
from st25dv import ST25DV

tag = ST25DV(board.STEMMA_I2C())
print("watching %s. Tap a phone on the tag." % tag.part)

while True:
    events = tag.poll_events()
    if events:
        print("events: %s" % ", ".join(events.names))
    if events.field_rising:
        print("  a reader arrived")
    if events.field_falling:
        print("  it left")
    if events.rf_write:
        print("  it wrote to memory; ndef is now %r"
              % (tag.ndef.value if tag.ndef else None))
