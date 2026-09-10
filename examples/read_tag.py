"""Everything the tag will tell you, read only. Nothing here writes.

Run this first on a new board: it settles which part is fitted, records the
factory register values, and shows what the tag currently offers a phone.
"""
import board
from st25dv import ST25DV, hexlify

tag = ST25DV(board.STEMMA_I2C())

print("part          %s" % tag.part)
print("memory        %d bytes (%d blocks of %d)"
      % (tag.memory_size, tag.block_count, tag.block_size))
print("uid           %s" % tag.uid_hex)
print("ic_ref        0x%02x  (0x26 on both the 16K and the 64K)" % tag.ic_ref)
print("ic_revision   0x%02x" % tag.ic_revision)
print("areas         %s" % (tag.areas,))
print("field / vcc   %s / %s" % (tag.field_present, tag.vcc_present))
print("session open  %s" % tag.session_open)

print()
print("system area, 0x0000 to 0x0020. Keep this: there is no reset command.")
blob = tag.system_dump()
for offset in range(0, len(blob), 8):
    print("  %04x  %s" % (offset, hexlify(blob[offset:offset + 8], " ")))

print()
try:
    container = tag.capability_container
    print("capability container  %s" % container)
    print("  raw                 %s" % hexlify(container.to_bytes(), " "))
    if container.length + container.capacity < tag.memory_size:
        print("  NOTE: declares %d bytes of the %d the part has. A phone will"
              % (container.capacity, tag.memory_size))
        print("        refuse to write past that. tag.format() fixes it.")
except Exception as err:                       # noqa: BLE001
    print("capability container  unreadable: %s" % err)

try:
    message = tag.ndef
    if message is None:
        print("ndef                  none (formatted but empty)")
    else:
        for record in message:
            print("ndef                  %s: %r" % (record.kind, record.value))
except Exception as err:                       # noqa: BLE001
    print("ndef                  unreadable: %s" % err)

print()
print("done")
