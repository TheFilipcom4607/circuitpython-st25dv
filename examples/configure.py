"""Open the security session, change some settings, then put them all back.

Everything written here is persistent EEPROM, so the restore at the end
matters. Take a copy of the printed "before" line before running it anywhere
you care about.
"""
import board
from st25dv import ST25DV, GPO_FIELD_CHANGE, GPO_RF_WRITE, GPO_ENABLE, hexlify

tag = ST25DV(board.STEMMA_I2C())
print(tag)

before = tag.system_dump()
print("before: %s" % hexlify(before[:0x10], " "))

tag.open_session()                     # factory password is eight zero bytes
print("session open: %s" % tag.session_open)

try:
    tag.gpo = GPO_ENABLE | GPO_FIELD_CHANGE | GPO_RF_WRITE
    tag.interrupt_pulse_us = 150
    tag.energy_harvesting_after_boot = True
    print("gpo mask       0x%02x" % tag.gpo)
    print("pulse          %.0f us" % tag.interrupt_pulse_us)
    print("harvest at boot %s" % tag.energy_harvesting_after_boot)

    # The dynamic output bit is separate, and needs no session.
    tag.gpo_enabled = True
    print("gpo driving    %s" % tag.gpo_enabled)
finally:
    tag.gpo = before[0x00]
    tag.interrupt_pulse_us = 301.0 - (before[0x01] & 0x07) * 37.65
    tag.energy_harvesting_after_boot = not before[0x02] & 0x01
    tag.close_session()

print("after:  %s" % hexlify(tag.system_dump()[:0x10], " "))
print("session open: %s" % tag.session_open)
print("done")
