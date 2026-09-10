"""A host-side mailbox round trip, then a wait for the RF side.

Enabling fast transfer mode needs MB_MODE set in EEPROM, which needs the
security session. While it is on, every EEPROM write is refused, so this turns
it back off at the end.
"""
import board
from st25dv import ST25DV

tag = ST25DV(board.STEMMA_I2C())
print(tag)

tag.open_session()
tag.mailbox.allowed = True
tag.close_session()

tag.mailbox.enable()
print("mailbox on, %d bytes" % 256)

tag.mailbox.put(b"hello from the microcontroller")
print("put:      %d bytes waiting, from %s"
      % (tag.mailbox.available, tag.mailbox.sender))
print("get:      %r" % tag.mailbox.get())
print("now:      %d bytes waiting" % tag.mailbox.available)

print("waiting for a reader to put a message (30 s)...")
events = tag.wait_for_mailbox(timeout=30)
if events:
    print("from %s: %r" % (tag.mailbox.sender, tag.mailbox.get()))
else:
    print("nothing arrived. A phone will not do this: it needs a reader that "
          "speaks the ST25DV custom commands.")

tag.mailbox.disable()
print("mailbox off; EEPROM writes work again")
print("done")
