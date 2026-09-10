"""Hex dump user memory and the system area."""
import board
from st25dv import ST25DV, hexlify

tag = ST25DV(board.STEMMA_I2C())
print("%s, %d bytes" % (tag.part, tag.memory_size))

print()
print("user memory")
for line in tag.dump(0, 256):
    print("  " + line)
print("  ... %d more bytes" % (tag.memory_size - 256))

print()
print("system area")
blob = tag.system_dump()
for offset in range(0, len(blob), 16):
    print("  %04x  %s" % (offset, hexlify(blob[offset:offset + 16], " ")))

print()
print("dynamic registers")
for name in ("gpo_enabled", "field_present", "vcc_present",
             "energy_harvesting", "rf_disabled", "rf_sleep", "session_open"):
    print("  %-18s %s" % (name, getattr(tag, name)))
print("  %-18s %s" % ("mailbox", tag.mailbox))

print()
print("done")
