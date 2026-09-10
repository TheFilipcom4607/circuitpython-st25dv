# circuitpython-st25dv

[![tests](https://github.com/TheFilipcom4607/circuitpython-st25dv/actions/workflows/ci.yml/badge.svg)](https://github.com/TheFilipcom4607/circuitpython-st25dv/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A single-file CircuitPython driver for the ST **ST25DV** dual-interface NFC
tag, with NDEF, the Type 5 capability container, the I2C security session,
memory areas, GPO configuration and the fast transfer mode mailbox. Written
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
[Why not Neradoc's](#why-not-neradoccircuitpython-st25dv) · [API](#api) ·
[NDEF and the capability container](#ndef-and-the-capability-container) ·
[Hardware notes](#hardware-notes) · [Troubleshooting](#troubleshooting) ·
[Known limitations](#known-limitations) ·
[Verified on hardware](#verified-on-hardware) ·
[Tests and tooling](#tests-and-tooling) · [License](#license-and-credits)

## Install

Copy `st25dv.py` to `CIRCUITPY/lib/`. There are no dependencies beyond
CircuitPython core modules: `micropython`, `supervisor`, `time`, and
`digitalio` only if you pass a GPO pin.

On a memory-tight board, compile it first:

```bash
mpy-cross st25dv.py && cp st25dv.mpy /Volumes/CIRCUITPY/lib/
```

## Examples

| File | What it does |
|---|---|
| [`examples/read_tag.py`](examples/read_tag.py) | dumps identity, system area, capability container and NDEF. Read only. Run this first |
| [`examples/write_url.py`](examples/write_url.py) | writes a URL, fixes the capability container, then waits for a tap |
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

## Why not Neradoc/circuitpython-st25dv

[The only other CircuitPython attempt](https://github.com/Neradoc/circuitpython-st25dv)
is a 2 kB experiment that subclasses `adafruit_24lc32`'s EEPROM class. It
hardcodes the size at 2048 bytes, only ever talks to 0x53, and has no access to
the system area, the capability container, the security session, the dynamic
registers, GPO or the mailbox. It treats an ST25DV as a plain I2C EEPROM.

The differences that matter are not features so much as correctness:

* **Both device addresses.** 0x53 is user memory, the dynamic registers and the
  mailbox; 0x57 is the system area and the password. Half the chip is invisible
  from 0x53 alone.
* **NACK is not an error.** When the RF side is busy the I2C side answers NoAck
  to everything (section 5.5). Every transfer here retries with backoff before
  it is allowed to raise, so a phone touching the tag does not turn into an
  exception in your program.
* **Size is read, not assumed.** `IC_REF` is 0x26 on both the 16K and the 64K,
  so capacity comes from `MEM_SIZE` and `BLK_SIZE`.
* **Area boundaries.** A sequential write may not cross one, and a sequential
  read that does returns 0xFF from there on. The chunker splits on them.
* **The mailbox blocks writes.** EEPROM writes transit the fast transfer mode
  buffer, so with it enabled they are silently refused. Checked, and named.

## API

### `ST25DV(i2c, address=0x53, system_address=0x57, gpo=None, debug=False, busy_timeout=0.5, probe=True)`

`i2c` is a `busio.I2C` or anything with the same four methods. The two
addresses are separate arguments because they are two device select codes for
one chip, not two chips. `debug=True` prints every transfer in both directions.
`busy_timeout` is how long a NACKed transfer keeps retrying before raising
`BusyError`. `probe=False` skips the identity read at construction.

Also `ST25DV.from_board(board)`, which uses `STEMMA_I2C` or `I2C`, and
`ST25DV.from_pins(scl, sda, frequency=400000)`.

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
| `BusyError` | NACKed for longer than `busy_timeout`. Usually a reader |
| `SessionRequired` | a system register needs the session open |
| `ProtectedError` | the chip refused a write its protection settings forbid |
| `NDEFError` | malformed NDEF or capability container, or a message too big |
| `MailboxError` | the mailbox used in a state that forbids it |
| `AreaError` | an area rule broken |

When a write exhausts its retries the driver asks the chip why before giving
up, so a protected write, a closed session, an enabled mailbox and a busy
reader come back as four different exceptions rather than one.

## NDEF and the capability container

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
write past 512 bytes. `tag.format()` writes a correct one; `tag.format(erase=False)`
fixes it and carries the existing message across, which matters because the
corrected container is 8 bytes rather than 4 and so moves the NDEF area. Adafruit's own product text
describes the board as carrying an ST25DV04 while the product name says 16K, so
run `examples/read_tag.py` and let `MEM_SIZE` settle it.

Byte 3 of the container is the Type 5 feature flags. The driver treats it as
opaque and carries forward whatever the tag already had, defaulting to the 0x05
the board ships with.

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
  all during the cycle (section 6.4.2). Standard ACK polling applies.
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
* **`IT_STS_Dyn` is read-to-clear** (section 5.2.3).
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
  is, over I2C; the pin was never jumpered. Pulse width, polarity and open-drain
  behaviour are implemented from the datasheet and unverified.
* **RF-side mailbox delivery is not exercised.** It needs a reader that speaks
  the ST25DV custom commands. A phone will not do it. The host side of the
  mailbox is fully reachable over I2C.
* **RF passwords and RF-side area protection are not implemented.** `RF_PWD_0`
  through `RF_PWD_3` have no I2C access at all (Table 59), so they can be
  neither set nor verified from this side. `RFAiSS` is readable and reported,
  but its effect cannot be tested without an RF reader that presents passwords.
* **`LOCK_CCFILE` and `LOCK_CFG` are one-way from the RF side.** The driver
  will happily set them. Setting `lock_ccfile` blocks RF writes to blocks 0 and
  1 permanently.

## Verified on hardware

**Nothing yet.** The board was not reachable from the machine this was written
on: no CIRCUITPY drive and no USB serial port were present. Everything below
runs green against the simulated chip in `tests/fake_st25dv.py` and under the
on-device suite executed on the host, which is not the same as running on
silicon.

The checklist, in the order to work through it:

1. **Probe, read only.** `python tools/run_on_board.py examples/read_tag.py --until done`.
   Settles 4K versus 16K from `MEM_SIZE`, and prints the factory system-area
   bytes. Save that output: there is no reset command.
2. **On-device suite.** `python tools/run_on_board.py test_st25dv.py`. Catches
   the constructs CPython accepts and CircuitPython does not.
3. **Phone reads it.** `examples/write_url.py --until wrote`, then tap an Android phone.
   Then a message over 255 bytes, to exercise the three-byte TLV length, and
   one over 512 bytes, to check the container fix.
4. **Phone writes it.** `examples/phone_writes_back.py`, write from the phone,
   confirm `wait_for_rf_write` fires and the board reads it back.
5. **Mailbox.** `examples/mailbox_echo.py`, host-side put and get.
6. **Restore.** Put the factory register values back and rewrite the
   adafruit.com URL, so the board ends where it started.

Steps 3 and 4 need a person with a phone. The rest are automatable.

## Tests and tooling

```bash
python -m pytest -q                          # 120 tests against a simulated chip
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

## License and credits

MIT. See [LICENSE](LICENSE).

Register facts from ST's datasheet **DS10925 Rev 11** (ST25DV04K / ST25DV16K /
ST25DV64K) and cross-checked against ST's reference driver,
[stm32duino/ST25DV](https://github.com/stm32duino/ST25DV). The NDEF encoder and
decoder come from
[circuitpython-pn7150](https://github.com/TheFilipcom4607/circuitpython-pn7150).
