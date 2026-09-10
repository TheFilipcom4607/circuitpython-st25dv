"""Write something other than a URL, and read it back.

Cycles through the record types a phone knows what to do with, writing each to
the tag and decoding it again so you can see both sides. Tap a phone at any
point; whatever is on the tag at that moment is what it will offer.

Nothing here is destructive beyond the NDEF area, but it does rewrite it
several times, so the message the tag arrived with is gone at the end. The
capability container and every system register are left alone.
"""
import board
from st25dv import ST25DV, NDEFMessage, NDEFRecord

tag = ST25DV(board.STEMMA_I2C())
print(tag)

# Fill these in before running; the defaults are documentation examples.
PHONE = "+441632960961"
WIFI_SSID = "Guest Wi-Fi"
WIFI_PASSWORD = "correct horse"
SPEAKER = "a4:c1:38:01:02:03"

records = [
    ("phone number", NDEFRecord.tel(PHONE)),
    ("text message", NDEFRecord.sms(PHONE, "on my way")),
    ("email", NDEFRecord.email("grace@example.com", "Hello", "see you at 6")),
    ("wi-fi", NDEFRecord.wifi(WIFI_SSID, WIFI_PASSWORD)),
    ("contact", NDEFRecord.contact("Grace Hopper", phone=PHONE,
                                   email="grace@example.com",
                                   organization="US Navy")),
    ("bluetooth", NDEFRecord.bluetooth(SPEAKER, name="Speaker")),
    ("bluetooth le", NDEFRecord.bluetooth_le(SPEAKER, name="Sensor")),
    ("homekit", NDEFRecord.homekit("X-HM://0024K0M6P00HB")),
]

for label, record in records:
    tag.ndef = record
    back = tag.ndef.records[0]
    print()
    print("%-13s %d bytes on the tag" % (label, len(record.to_bytes())))
    print("  kind        %s" % back.kind)
    print("  decoded     %r" % (back.value,))

# One tag can carry several records at once. A phone acts on the first one it
# understands, so put the URL first and the credentials behind it.
print()
tag.ndef = NDEFMessage([
    NDEFRecord.uri("https://thefilip.com"),
    NDEFRecord.wifi(WIFI_SSID, WIFI_PASSWORD),
    NDEFRecord.tel(PHONE),
])
message = tag.ndef
print("combined      %d records, %d bytes"
      % (len(message), len(message.to_bytes())))
print("  uri         %s" % message.uri)
print("  wi-fi ssid  %s" % message.wifi["ssid"])
print("  contact     %s" % message.contact)     # None: there isn't one

print()
print("tap a phone now; it should offer the link")
print("done")
