"""Wait for a phone to write the tag, then read out what it wrote.

Write anything you like from an NFC app on the phone. The board notices the
write over I2C, with no GPO wire involved.
"""
import board
from st25dv import ST25DV

tag = ST25DV(board.STEMMA_I2C())

# Start from a formatted, empty tag so the phone offers to write to it.
tag.format()
print("%s formatted, %d bytes free. Write to it from your phone."
      % (tag.part, tag.capability_container.capacity))

while True:
    events = tag.wait_for_rf_write(timeout=60)
    if events is None:
        print("nothing yet")
        continue
    message = tag.ndef
    if message is None:
        print("written, but the tag is empty now")
        continue
    for record in message:
        print("%s: %r" % (record.kind, record.value))
