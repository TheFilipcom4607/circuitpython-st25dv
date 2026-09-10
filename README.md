# circuitpython-st25dv

[![tests](https://github.com/TheFilipcom4607/circuitpython-st25dv/actions/workflows/ci.yml/badge.svg)](https://github.com/TheFilipcom4607/circuitpython-st25dv/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A single-file CircuitPython driver for the ST **ST25DV** dual-interface NFC
tag, with NDEF, the Type 5 capability container, the I2C security session,
memory areas, GPO configuration and the fast transfer mode mailbox. Writes
links, phone numbers, Wi-Fi credentials, contact cards and Bluetooth pairing
data, and decodes all of them back. Written
against the [Adafruit ST25DV16K breakout (4701)](https://www.adafruit.com/product/4701),
but nothing in it is board specific. Covers the 4K, 16K and 64K parts, because
it reads the memory size from the chip rather than assuming one.

```python
import board
from st25dv import ST25DV

tag = ST25DV(board.STEMMA_I2C())
print(tag.part, tag.memory_size, tag.uid_hex)

tag.ndef = "https://thefilip.com"      # a phone now reads this
print(tag.ndef.uri)
```

```
ST25DV16K-IE 2048 e0:02:26:00:12:34:56:78
https://thefilip.com
```

The ST25DV is a tag two masters share: an I2C host on one side, an ISO/IEC
15693 reader on the other. The interesting work is on that boundary, which is
why this driver also does things a memory driver has no reason to:

```python
if tag.wait_for_field(timeout=30):     # a phone arrived
    events = tag.wait_for_rf_write(timeout=10)
    if events:
        print("it wrote:", tag.ndef.value)
```

No wire to the GPO pin is needed for any of that.

**Contents:** [Install](#install) · [Examples](#examples) · [Wiring](#wiring) ·
[NDEF and the capability container](#ndef-and-the-capability-container) ·
[API](#api) · [Hardware notes](#hardware-notes) ·
[Troubleshooting](#troubleshooting) · [Known limitations](#known-limitations) ·
[Verified on hardware](#verified-on-hardware) ·
[Tests and tooling](#tests-and-tooling) · [Releasing](#releasing) ·
[License](#license-and-credits)

## Install

With [circup](https://github.com/adafruit/circup), which fetches the compiled
build from this repo's latest release:

```bash
circup bundle-add TheFilipcom4607/circuitpython-st25dv   # one time
circup install st25dv
```

Or by hand — download `circuitpython-st25dv-<major>.x-mpy-<version>.zip` from
[the latest release](https://github.com/TheFilipcom4607/circuitpython-st25dv/releases/latest),
matching the zip's major version to the CircuitPython on your board, and copy
`lib/st25dv.mpy` out of it:

```bash
cp st25dv.mpy /Volumes/CIRCUITPY/lib/
```

**Prefer the `.mpy`.** The source is 96 kB that CircuitPython must compile into
RAM at every import, where the compiled build is 27 kB and loads with no
compile step at all. The driver is mostly prose, and every docstring in it
becomes a string object living in RAM for as long as the module does. On an
RP2040 that difference decides whether the driver and a large `code.py` fit
together.

Copy `st25dv.py` instead only when you want to read or edit it in place. If you
want both, `tools/minify.py` strips the docstrings and comments and changes
nothing else:

```bash
python tools/minify.py st25dv.py st25dv_small.py   # 96 kB -> 58 kB
```

Either way there are no dependencies. It imports only core modules —
`micropython`, `supervisor`, `time`, and `digitalio` only if you pass a GPO
pin — and notably not `adafruit_bus_device`, so there is nothing to install
alongside it. Compiled builds are published for CircuitPython 9.x and 10.x;
the source runs on either.

## Examples

| File | What it does |
|---|---|
| [`examples/read_tag.py`](examples/read_tag.py) | dumps identity, system area, capability container and NDEF. Read only. Run this first |
| [`examples/write_url.py`](examples/write_url.py) | writes a URL, fixes the capability container, then waits for a tap |
| [`examples/write_credentials.py`](examples/write_credentials.py) | phone number, Wi-Fi, contact, Bluetooth and HomeKit records, written and decoded |
| [`examples/tap_detector.py`](examples/tap_detector.py) | polls the interrupt register and reports field changes and RF writes |
| [`examples/phone_writes_back.py`](examples/phone_writes_back.py) | phone writes a message, the board notices and reads it out |
| [`examples/eeprom_dump.py`](examples/eeprom_dump.py) | hex dump of user memory, the system area and the dynamic registers |
| [`examples/configure.py`](examples/configure.py) | opens the session, sets GPO and energy harvesting, then restores |
| [`examples/mailbox_echo.py`](examples/mailbox_echo.py) | host-side mailbox round trip |

## Wiring

STEMMA QT to STEMMA QT is the whole story: SDA, SCL, 3V and ground. The
breakout has the pull-ups on it.

| Breakout | Board |
|---|---|
| SDA | SDA |
| SCL | SCL |
| VIN | 3.3 V |
| GND | GND |
| GPO | optional, see below |

The bus can run at up to 1 MHz (Table 249), so unlike some NFC controllers
there is no reason to drop to 100 kHz. `board.STEMMA_I2C()` gives you whatever
the board set up, usually 100 kHz; for a dedicated 400 kHz bus:

```python
tag = ST25DV.from_pins(board.SCL, board.SDA, frequency=400000)
```

GPO is an open-drain interrupt output on the -IE part and push-pull on the -JF.
It is optional and this driver never needs it: field presence, RF writes and
mailbox traffic are all readable over I2C. Pass it if you want one:

```python
tag = ST25DV(board.STEMMA_I2C(), gpo=board.D5)
tag.gpo_enabled = True                 # the bit that actually drives the pin
print(tag.gpo_value)
```

## NDEF and the capability container

This comes before the API reference because it is what actually bites. A tag
that reads as empty, or that a phone refuses to write more than a few hundred
bytes to, is almost always a capability container problem rather than a driver
one.

A phone reads the capability container at user memory offset 0 first. It is 4
bytes when `MLEN` fits in one byte and 8 bytes otherwise, which the third byte
being zero announces. `MLEN` counts 8-byte units.

| Part | Memory | Container | Encoded |
|---|---|---|---|
| ST25DV04K | 512 B | 4 bytes, MLEN 63 | `e1 40 3f 05` |
| ST25DV16K | 2048 B | 8 bytes, MLEN 255 | `e2 40 00 05 00 00 00 ff` |
| ST25DV64K | 8192 B | 8 bytes, MLEN 1023 | `e2 40 00 05 00 00 03 ff` |

The short form is used whenever its one-byte `MLEN` can still describe the
whole tag. At 2048 bytes it cannot, which is why the 16K gets the extended
form.

**The Adafruit 4701 ships with `e1 40 40 05`**, declaring 512 bytes, followed by
an NDEF URI record for its own product page. If the fitted part is a 16K, that
container under-declares the tag by a factor of four and a phone will refuse to
write past 512 bytes. `tag.format()` writes a correct one;
`tag.format(erase=False)` fixes it and carries the existing message across,
which matters because the corrected container is 8 bytes rather than 4 and so
moves the NDEF area. Adafruit's own product text describes the board as
carrying an ST25DV04 while the product name says 16K, so run
`examples/read_tag.py` and let `MEM_SIZE` settle it.

Byte 3 of the container is the Type 5 feature flags. The driver treats it as
opaque and carries forward whatever the tag already had, defaulting to the 0x05
the board ships with.

## API

### `ST25DV(i2c, address=0x53, system_address=0x57, gpo=None, debug=False, busy_timeout=0.5, probe=True)`

`i2c` is a `busio.I2C` or anything with the same four methods. The two
addresses are separate arguments because they are two device select codes for
one chip, not two chips. `debug=True` prints every transfer in both directions.
`busy_timeout` is how long a NACKed transfer keeps retrying before raising
`BusyError`, and also how long a transfer waits for the bus lock when something
else on a shared bus is holding it. `probe=False` skips the identity read at
construction.

Also `ST25DV.from_board(board)`, which uses `STEMMA_I2C` or `I2C`, and
`ST25DV.from_pins(scl, sda, frequency=400000)`. The bus `from_pins` builds
belongs to the object it returns, so `deinit()` releases it; a bus handed to
the constructor is the caller's and is left alone.

### Identity

| | |
|---|---|
| `part` | `"ST25DV16K-IE"`. Capacity from `MEM_SIZE`, package from UID byte 5 |
| `memory_size` | user memory in bytes: `(MEM_SIZE + 1) * (BLK_SIZE + 1)` |
| `block_count`, `block_size` | the two halves of that |
| `uid`, `uid_hex` | 8 bytes, most significant first, starting `e0:02` |
| `ic_ref`, `ic_revision` | 0x24 on the 4K, 0x26 on the 16K *and* the 64K |

### User memory

```python
tag.read(0, 16)
tag.write(0, b"hello")
tag[0]                                 # one byte
tag[0:16]                              # bytes
tag[0:5] = b"hello"                    # slice length must match
len(tag)
tag.dump(0, 64)                        # list of hex-dump lines
```

Both `read` and `write` go through a chunker that respects the 256-byte
transaction cap, the area boundaries and the per-page program time. Reading or
writing past the end raises `ValueError` before anything reaches the bus, since
the chip has no rollover and would just return 0xFF forever.

### NDEF

```python
tag.ndef = "https://thefilip.com"      # URI record
tag.ndef = "just some words"           # text record
tag.ndef = NDEFMessage([NDEFRecord.uri("https://a.co"),
                        NDEFRecord.text("hi", "fr")])
message = tag.ndef                     # NDEFMessage or None
message.uri, message.text, message.value
tag.format()                           # write a correct capability container
tag.capability_container
```

`NDEFMessage` and `NDEFRecord` are the encoder and decoder from
[circuitpython-pn7150](https://github.com/TheFilipcom4607/circuitpython-pn7150),
including all 36 URI prefix codes. `read_ndef()` and `write_ndef(msg)` are the
same thing spelled out.

A plain string becomes a URI record when it starts with a scheme the prefix
table knows, and a text record otherwise, so `"tel:+441632960961"` is a URI
while `"Note: buy milk"` stays prose.

### More than a URL

A tag is not limited to links. These build the records phones already know what
to do with, and reading one back decodes it in the same terms.

```python
tag.ndef = NDEFRecord.tel("+441632960961")            # opens the dialler
tag.ndef = NDEFRecord.sms("+441632960961", "on my way")
tag.ndef = NDEFRecord.email("grace@example.com", "Hello", "see you at 6")
tag.ndef = NDEFRecord.wifi("Guest Wi-Fi", "correct horse")
tag.ndef = NDEFRecord.contact("Grace Hopper", phone="+441632960961",
                              email="grace@example.com")
tag.ndef = NDEFRecord.bluetooth("a4:c1:38:01:02:03", name="Speaker")
tag.ndef = NDEFRecord.bluetooth_le("a4:c1:38:01:02:03", name="Sensor")
tag.ndef = NDEFRecord.homekit("X-HM://0024K0M6P00HB")
```

| Builder | Record written | What a phone does with it |
|---|---|---|
| `tel(number)` | URI, prefix code 5 | offers to call |
| `sms(number, body)` | URI, body percent-encoded | opens the composer, prefilled |
| `email(address, subject, body)` | URI, prefix code 6 | opens the mail composer, prefilled |
| `wifi(ssid, password, ...)` | MIME `application/vnd.wfa.wsc` | Android offers to join, iOS ignores it |
| `contact(name, phone=, email=, ...)` | MIME `text/vcard`, vCard 3.0 | Android offers to save it, iOS ignores vCard entirely |
| `bluetooth(address, name=)` | MIME `application/vnd.bluetooth.ep.oob` | offers to pair, BR/EDR |
| `bluetooth_le(address, ...)` | MIME `application/vnd.bluetooth.le.oob` | neither phone acted on it |
| `homekit(payload)` | URI, `X-HM://` | see the caveat below |

Reading is symmetric. `record.kind` grows the values `"wifi"`, `"contact"`,
`"bluetooth"` and `"bluetooth_le"`, and `record.value` decodes each into a
dict instead of raw bytes:

```python
credentials = tag.ndef.wifi
# {"ssid": "Guest Wi-Fi", "password": "correct horse", "security": "wpa2",
#  "authentication": 32, "encryption": 8, "mac": None}

tag.ndef.contact["name"]               # "Grace Hopper"
tag.ndef.first("bluetooth")["address"] # "a4:c1:38:01:02:03"
```

`message.first(kind)` is the general form; `message.uri`, `.text`, `.wifi` and
`.contact` are shortcuts for the common ones. A message can hold several
records, so a URL and Wi-Fi credentials can share one tag:

```python
tag.ndef = NDEFMessage([NDEFRecord.uri("https://thefilip.com"),
                        NDEFRecord.wifi("Guest Wi-Fi", "correct horse")])
```

Wi-Fi defaults to WPA2 Personal with AES when a password is given, which is
what nearly every home network is. Pass `authentication=WIFI_OPEN` for an open
one; naming a secured type with no password is refused rather than quietly
written as open. Bluetooth addresses are given the way people write them, most
significant byte first, and are reversed on the wire where the format wants
them little-endian.

**HomeKit is the one to be careful with.** `homekit()` writes the `X-HM://`
setup payload, the same string printed under the QR code on an accessory, as a
URI record. That is all it does. It does not make the tag a HomeKit accessory:
iOS pairs with a device that answers back, and a tag holding a setup payload
has nothing behind it. Useful for carrying a setup code that a real accessory
will honour, not as a substitute for one. This is the only builder here whose
end-to-end behaviour is unverified.

### Security session

```python
tag.open_session()                     # factory password is eight zero bytes
tag.session_open
tag.change_password(b"secret!!")
tag.close_session()
```

Every static register is write-protected while the session is closed (Table 11).
Writing one anyway raises `SessionRequired` rather than failing as an opaque
NACK.

### Configuration

| | |
|---|---|
| `gpo` | the static `GPO` event mask. Or the `GPO_*` constants together |
| `gpo_enabled` | bit 7 of `GPO_CTRL_Dyn`, the bit that actually drives the pin |
| `gpo_value` | the pin's level, if you passed one |
| `interrupt_pulse_us` | `301 - IT_TIME * 37.65`, so 37 to 301 us |
| `energy_harvesting` | `EH_CTRL_Dyn` bit 0, live and volatile |
| `energy_harvesting_after_boot` | `EH_MODE`, read the non-inverted way round |
| `rf_disabled`, `rf_sleep` | `RF_MNGT_Dyn`: live, no session needed |
| `rf_management` | the static `RF_MNGT` byte behind them |
| `areas` | the ENDAi split, read as `Area` objects and set as byte sizes |
| `i2c_protection`, `rf_protection` | `I2CSS` and the four `RFAiSS` |
| `lock_cfg`, `lock_ccfile` | RF write protection of the config and of blocks 0-1 |
| `system_dump()` | the whole readable system area, 0x0000 to 0x0020 |

Static and dynamic variants are distinguished rather than conflated: `rf_sleep`
is the live bit, `rf_management` is the one that survives a power cycle.

Splitting memory has an ordering rule the chip enforces, and the driver hides:

```python
tag.open_session()
tag.areas = [512, 512, 512, 512]       # four areas on a 16K
tag.areas
# (<Area 1 0x0000..0x01ff (512 bytes)>, <Area 2 0x0200..0x03ff (512 bytes)>, ...)
```

### Live RF status

`IT_STS_Dyn` is read-to-clear, so a wait reports whatever latched since the
register was last read, which can be older than the call. That is right for
"did a reader visit" and wrong for "wait for the next visitor". Pass
`drain=True` for the second:

```python
if tag.wait_for_field(timeout=30, drain=True):
    print("a phone arrived just now")
```

A field already resting on the tag still counts either way, because that is
read from `EH_CTRL_Dyn` rather than from the latch. Note also that the wait
itself consumes the flags: they live in the object it returns, so keep it
rather than polling again afterwards.


This is what makes the library useful with only the STEMMA QT cable attached.

| | |
|---|---|
| `field_present`, `vcc_present` | `EH_CTRL_Dyn` bits 2 and 3 |
| `poll_events()` | drains `IT_STS_Dyn` once and returns an `Events` |
| `wait_for_field(timeout)` | a reader arrived, or is already there |
| `wait_for_rf_write(timeout)` | a reader changed EEPROM |
| `wait_for_mailbox(timeout)` | a reader left a mailbox message |

`IT_STS_Dyn` clears itself when read (section 5.2.3), so it is exposed as one
explicit call and nothing else in the driver touches it. `__repr__` and every
status property deliberately stay away from it: a driver that peeked would
destroy exactly the events its caller was waiting for. The `wait_for_*` calls
accumulate what they drain, so an event that arrives while they are waiting for
a different one is still in the object that comes back.

### Mailbox

```python
tag.open_session()
tag.mailbox.allowed = True             # MB_MODE, persistent
tag.close_session()
tag.mailbox.enable()                   # MB_EN, volatile
tag.mailbox.put(b"hello")
tag.mailbox.available, tag.mailbox.sender, tag.mailbox.missed
tag.mailbox.get()
tag.mailbox.disable()                  # EEPROM writes work again
```

### Errors

Everything derives from `ST25DVError`.

| | |
|---|---|
| `NotFoundError` | nothing answered, or what answered is not an ST25DV |
| `BusyError` | NACKed, or the bus lock was held, past `busy_timeout`. Usually a reader |
| `SessionRequired` | a system register needs the session open |
| `ProtectedError` | the chip refused a write its protection settings forbid |
| `NDEFError` | malformed NDEF or capability container, or a message too big |
| `MailboxError` | the mailbox used in a state that forbids it |
| `AreaError` | an area rule broken |

When a write exhausts its retries the driver asks the chip why before giving
up, so a protected write, a closed session, an enabled mailbox and a busy
reader come back as four different exceptions rather than one.

## Hardware notes

Everything here is from datasheet **DS10925 Rev 11**, cited so it can be
rechecked.

* **Two I2C addresses, one chip.** The device select code is `1010 E2 1 1`
  (Table 88). E2=0 is 0x53: user memory, dynamic registers, mailbox. E2=1 is
  0x57: system area and password. Both appear on an I2C scan.
* **16-bit addresses, big-endian, sent before the data** (section 6.4).
* **The RF side NACKs the I2C side.** Arbitration is first-talk-first-served,
  and while RF is busy the I2C interface NoAcks everything (section 5.5). A
  NACK does not mean the device is absent or the write was refused.
* **Writes take time and the chip goes silent.** Pages are 4 bytes, tW is 5 ms
  per page touched including partial ones, and the device does not respond at
  all during the cycle (section 6.4.2). Standard ACK polling applies. The
  datasheet's own worked example is 256 bytes starting at 0x0002, which spans
  65 pages and costs tW x 65; the driver computes the same 65, and a test
  pins it.
* **A sequential write does not roll over.** The byte address counter simply
  increments (section 6.4.2), so an unaligned 256-byte write is fine, unlike
  the aligned-buffer wraparound many EEPROMs have. The only two limits are the
  256-byte length and the area border, and those are what the chunker splits
  on.
* **Writes transit the mailbox buffer.** Fast transfer mode must be off or the
  write is NACKed and does nothing (section 6.4, Caution).
* **Sequential writes cannot cross an area boundary** (section 6.4.2), and
  sequential reads that do return 0xFF forever after (section 6.5.3). There is
  no rollover at the end of memory.
* **System registers need the session.** Opening it is 17 bytes to 0x0900 on
  0x57: the password, the validation byte 0x09, the password again
  (section 6.6.1). Changing it uses 0x07 (section 6.6.2). Factory password is
  eight zero bytes. Success shows up in `I2C_SSO_Dyn` at 0x2004.
* **Memory size must be read.** `IC_REF` is 0x26 for both the 16K and the 64K
  (Table 83). Capacity is `(MEM_SIZE + 1) * (BLK_SIZE + 1)`.
* **`IT_STS_Dyn` is read-to-clear** (section 5.2.3), and only reports events
  enabled in the static `GPO` mask. The factory mask is field changes only, so
  `wait_for_rf_write()` cannot fire until `GPO_RF_WRITE` is set.
* **GPO output needs the dynamic bit.** The pin is driven only when bit 7 of
  `GPO_CTRL_Dyn` is set (Table 33). Over I2C bits 0 to 6 of that register are
  read only; the event mask lives in the static `GPO` register.
* **Register 0x0001 is `IT_TIME` on this part.** ST's shared header calls that
  address `GPO2` because it also covers the newer ST25DVxxKC family. Table 27
  confirms `IT_TIME` for the plain ST25DV, which is what this board has.
* **Areas have an ordering rule.** An `ENDAi` can only be programmed once its
  successor sits at end of memory, in the order ENDA3, ENDA2, then ascending
  (section 4.2). Break it and the chip NACKs and writes nothing. Limits are in
  32-byte units: the last byte of area *i* is `32 * ENDAi + 31`.
* **`MB_LEN_Dyn` is length minus one** (Table 21), and `MB_EN` cannot be set
  unless `MB_MODE` is 1 in EEPROM.

## Troubleshooting

**`NotFoundError` at construction.** Scan the bus. You should see both 0x53 and
0x57. Seeing neither is wiring; seeing only one is the wrong `address` or
`system_address`.

**`BusyError` on everything.** A reader is parked on the tag, or something else
on the bus is holding it. Raise `tag.busy_timeout`.

**`SessionRequired` writing a config register.** Call `open_session()`. The
factory password is eight zero bytes; if someone changed it, there is no way
back from the I2C side.

**`MailboxError` on an ordinary write.** Fast transfer mode is on. Call
`tag.mailbox.disable()`.

**A phone reads the tag but will not write past a few hundred bytes.** The
capability container under-declares the memory. `tag.format()`.

**A phone sees nothing at all.** Check `tag.rf_disabled` and `tag.rf_sleep`,
which silence the RF side entirely, and that the container's magic byte is 0xE1
or 0xE2.

## Known limitations

* **GPO pin electrical behaviour is not exercised.** The register configuration
  is, over I2C, including that the event mask gates `IT_STS_Dyn`; but the pin
  itself was never jumpered. Pulse width, polarity and open-drain behaviour are
  implemented from the datasheet and unverified.
* **RF-side mailbox delivery is not exercised.** It needs a reader that speaks
  the ST25DV custom commands. A phone will not do it. The host side of the
  mailbox is fully reachable over I2C.
* **RF passwords and RF-side area protection are not implemented.** `RF_PWD_0`
  through `RF_PWD_3` have no I2C access at all (Table 59), so they can be
  neither set nor verified from this side. `RFAiSS` is readable and reported,
  but its effect cannot be tested without an RF reader that presents passwords.
* **What a phone does with a credential record is the phone's business.**
  The driver's job ends at correct bytes. Android raises a join dialog for
  Wi-Fi records; iOS does not act on them from a tag, so treat Wi-Fi as an
  Android feature. Do not confuse this with Wi-Fi **QR codes**, which iOS has
  read from the Camera app since iOS 11 and which work everywhere. Core NFC
  reads NDEF and writes it, but Apple never wired a Wi-Fi join into the
  background tag handler, and encoding the credentials as a `WIFI:S:...;;`
  URI string instead of the WSC record does not change that. Contacts and
  Bluetooth are handled by both. The `X-HM://`
  HomeKit payload is written correctly but has nothing behind it to pair with,
  which is a property of NFC HomeKit pairing rather than of this driver.
* **Bluetooth LE pairing records are not acted on by either phone tested.**
  The bytes are well formed and decode correctly, but neither a Pixel 10 nor an
  iPhone raised anything, on a phone that had just handled a classic Bluetooth
  record correctly. Classic BR/EDR works; treat LE as write-only for now.
* **`LOCK_CCFILE` and `LOCK_CFG` are one-way from the RF side.** The driver
  will happily set them. Setting `lock_ccfile` blocks RF writes to blocks 0 and
  1 permanently.

## Verified on hardware

**All six checklist steps pass.** Tested on an Adafruit Feather RP2350 running
CircuitPython 10.3.0, against a Pixel 10, an iPhone 16 Pro and a Flipper Zero.

The part is an **ST25DV16K-IE**, 2048 bytes, settled from `MEM_SIZE` rather
than `IC_REF`, which reads 0x26 on the 16K and the 64K alike. A Flipper Zero
independently reads it as ISO 15693 with UID `E0 02 26 01 D9 71 91 69`, which
matches what the driver reports after reversing the least-significant-byte-first
order it comes off the chip in.

| Step | Result |
|---|---|
| 1. Probe, read only | identity, system area, areas and the extended container all decode |
| 2. On-device suite | 126 assertions, 0 failures, on the firmware |
| 3. Phone reads it | notification and link open, for a 17-byte and a 607-byte message |
| 4. Phone writes it | `wait_for_rf_write` fires, and an 83-byte foreign text record decodes |
| 5. Mailbox | full host-side round trip, and everything guarding it |
| 6. Restore | system area, container, message and area map byte-identical afterwards |

**Step 3** covers more than a short URL. A 607-byte message exercises the
three-byte TLV length form and the four-byte record payload length at once, and
at that size it only fits because the container was corrected: the factory one
declared 512 bytes. RF field detection was reliable across dozens of taps,
catching both the two-edge case and the field-still-present case.

**Step 4** needed `GPO_RF_WRITE` enabled first. See the note below, which is
the main thing this round of testing turned up. Once enabled, a message written
by NFC Tools on a phone decoded correctly, including the text record's language
field, which is an independent check on the parser against a foreign encoder.

**Step 5** exercised far more than the mailbox: the security session opened
with the factory password, a system register written through it, the mailbox
state machine, `MB_LEN_Dyn` reporting 64 rather than 63 for a 64-byte message,
EEPROM writes refused while fast transfer mode is on and working again after,
and the session closing cleanly.

### The event mask gates the status register, not just the pin

`IT_STS_Dyn` "cumulates all events which generate interruptions", and RF events
reach it only "when enabled" in the static `GPO` register (Table 32's notes).
`GPO_ENABLE`, bit 7, controls the physical output alone. An event whose bit is
clear in the mask is reported **nowhere**, so `poll_events()` never sees it.

The factory mask is `0x88`, `GPO_ENABLE | GPO_FIELD_CHANGE`. Field changes work
out of the box; nothing else does. So on an untouched tag `wait_for_rf_write()`
and `wait_for_mailbox()` wait forever and look broken:

```python
tag.open_session()
tag.gpo |= GPO_RF_WRITE        # now wait_for_rf_write() can fire
```

This was confirmed by changing that one bit and nothing else: the same phone
doing the same write went from never firing the event to firing it at once.

### What a phone does with a credential record

Every record is verified byte for byte over I2C. What a handset then does with
it is the handset's business, and it varies:

| Record | Pixel 10 | iPhone 16 Pro |
|---|---|---|
| URL | opens it | opens it |
| `tel:` | dialler, prefilled | inconclusive |
| `sms:` | composer, body and punctuation intact | inconclusive |
| `mailto:` | composer, subject and body intact | inconclusive |
| Contact vCard | saves it, every field | ignored, Apple does not support vCard from a tag |
| Wi-Fi | offers to join, names the network | ignored, Wi-Fi over NFC is Android only |
| Bluetooth | offers to pair, names the device | inconclusive |
| Bluetooth LE | nothing, though the bytes are well formed | inconclusive |
| HomeKit `X-HM://` | not applicable | untested, and see the caveat above |

**Both phones deduplicate.** Re-presenting content a phone has already read is
the fastest way to get a false negative, and it cost a long stretch of this
session before we noticed. On the iPhone it eventually suppressed even a plain
URL, which is why several rows read inconclusive rather than failed. If you test
this yourself, **use unique content for every single tap**.

Bluetooth LE is a real negative rather than an artefact: it was tested with
fresh content on a phone that had just handled classic Bluetooth correctly.

## Tests and tooling

```bash
python -m pytest -q                          # 193 tests against a simulated chip
python tools/run_on_board.py test_st25dv.py  # the same logic, on CircuitPython
python tools/minify.py st25dv.py small.py     # strip docstrings for tight boards
```

`tests/fake_st25dv.py` is what makes the desktop suite worth having. It models
the register map, both device addresses, 4-byte page timing, the
NACK-while-writing and NACK-while-RF-busy behaviours, session gating on system
writes, area boundaries and their write-crossing refusal, the read-to-clear
interrupt register and the mailbox state machine. Timing is counted in
transactions rather than seconds, so the failure paths that are awkward to
provoke on real silicon are the easy ones to test here.

`tests/stubs/` supplies the CircuitPython-only modules so the driver imports
under CPython. `tests/test_device_suite.py` runs the on-device suite on the
host, so CI fails if the board suite would.

## Releasing

`.github/workflows/release_gh.yml` builds the bundle zips and attaches them to
a published GitHub release, using Adafruit's `circuitpython-build-tools`. The
tag is the only source of truth for the version: `__version__` in `st25dv.py`
and `version` in `pyproject.toml` both read `0.0.0+auto.0` in a checkout, and
the build rewrites that literal to the tag.

To cut a release, push a plain semver tag — no `v` prefix, because `circup`
parses the tag as a version — then publish a GitHub release for it. The tag
alone does nothing; the workflow fires on the release being *published*:

```bash
git tag 1.0.0 && git push origin 1.0.0
```

The workflow then attaches six assets:

```
circuitpython-st25dv-py-1.0.0.zip           source, lib/st25dv.py
circuitpython-st25dv-9.x-mpy-1.0.0.zip      compiled for CircuitPython 9.x
circuitpython-st25dv-10.x-mpy-1.0.0.zip     compiled for CircuitPython 10.x
circuitpython-st25dv-examples-1.0.0.zip     examples/
circuitpython-st25dv-1.0.0.json             bundle metadata for circup
z-build_tools_version-*.ignore              which build-tools cut the release
```

Those names are what `circup bundle-add TheFilipcom4607/circuitpython-st25dv`
expects, and they are derived from the repository name — so the repository has
to be called `circuitpython-st25dv`, matching `pyproject.toml` and the badges
above, and renaming it later breaks `circup` until the next release.

`requirements.txt` must exist at the repo root even though the driver has no
dependencies: Adafruit's `actions-ci/install.sh` runs `pip install -r
requirements.txt` with no existence check, and the build dies at that step
without it.

Two more things to know. GitHub runs release-triggered workflows from the copy
of the file on the default branch, so the workflow has to be on `main` before a
release will build anything. And a release build checks out the *tag*, not
`main`, so a fix to the build itself only takes effect in a new tag — re-running
a failed job against an old tag just rebuilds the old tree.

## License and credits

MIT. See [LICENSE](LICENSE).

Register facts from ST's datasheet **DS10925 Rev 11** (ST25DV04K / ST25DV16K /
ST25DV64K) and cross-checked against ST's reference driver,
[stm32duino/ST25DV](https://github.com/stm32duino/ST25DV). The NDEF encoder and
decoder come from
[circuitpython-pn7150](https://github.com/TheFilipcom4607/circuitpython-pn7150).
