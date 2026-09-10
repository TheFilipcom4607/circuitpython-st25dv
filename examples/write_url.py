"""Put a URL on the tag, then tap a phone on it.

    cp examples/write_url.py /Volumes/CIRCUITPY/code.py
"""
import board
from st25dv import ST25DV

URL = "https://thefilip.com"

tag = ST25DV(board.STEMMA_I2C())
print(tag)

# The board arrives formatted, but its capability container declares only 512
# bytes. On a 16K part that stops a phone writing past a quarter of the tag, so
# rewrite it to match what is actually there.
container = tag.capability_container
if container.capacity + container.length < tag.memory_size:
    print("container declares %d of %d bytes; fixing it"
          % (container.capacity, tag.memory_size))
    tag.format(erase=False)

tag.ndef = URL
print("wrote %r" % tag.ndef.uri)
print("tap a phone on the tag; it should offer to open the link")

while True:
    events = tag.wait_for_field(timeout=60)
    if events:
        print("field!", events.names)
