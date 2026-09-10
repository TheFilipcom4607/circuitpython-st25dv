"""A simulated ST25DV that behaves like one on a bus.

The point of this file is that almost every path in the driver, including the
failure paths, runs under CPython in CI. The awkward behaviours are the ones
worth modelling, so they are all here:

* two device select codes, 0x53 and 0x57, for one chip;
* 4-byte page granularity, and a write cycle during which the chip answers
  nothing at all;
* NACK while the RF side holds the tag, which is indistinguishable on the wire
  from every other refusal;
* the security session gating writes to the system area;
* area boundaries, which a sequential write may not cross and a sequential read
  crosses only into 0xFF;
* ``IT_STS_Dyn`` clearing itself when read;
* the mailbox, including its refusal of EEPROM writes while it is enabled and
  its length register being one short.

Timing is counted in transactions rather than seconds, so the tests are
deterministic and fast: a write leaves the chip busy for ``write_busy_ticks``
per page, and each attempted transfer burns one tick.
"""

# System configuration area, factory values (Table 11 and the per-register
# tables). Anything not listed is zero.
_FACTORY_SYSTEM = {
    0x0000: 0x88,      # GPO: GPO_EN and FIELD_CHANGE_EN
    0x0001: 0x03,      # IT_TIME
    0x0002: 0x01,      # EH_MODE: energy harvesting on demand only
    0x000E: 0x07,      # MB_WDG
    0x0016: 0x03,      # BLK_SIZE, blocks are 4 bytes
}

_SYSTEM_LAST = 0x0020
_SYSTEM_WRITABLE_LAST = 0x000F     # 0x0010 upwards is read only
_PWD_ADDRESS = 0x0900

_DYN_FIRST = 0x2000
_DYN_LAST = 0x2007
_MB_FIRST = 0x2008
_MB_LAST = 0x2107


class NackError(OSError):
    """What a real bus raises when nobody acknowledges."""


class FakeST25DV:
    """One simulated chip, presented as a ``busio.I2C``.

    ``capacity`` picks the part: 512 is the 4K, 2048 the 16K, 8192 the 64K.
    """

    def __init__(self, capacity=2048, revision=0x01, serial=b"\x01\x02\x03\x04",
                 user_address=0x53, system_address=0x57):
        if capacity % 32:
            raise ValueError("capacity must be a whole number of 32-byte units")
        self.user_address = user_address
        self.system_address = system_address
        self.user = bytearray(b"\xff") * capacity
        self.system = bytearray(_SYSTEM_LAST + 1)
        self.password = bytearray(8)
        self.dynamic = bytearray(8)
        self.mailbox = bytearray(256)

        for addr, value in _FACTORY_SYSTEM.items():
            self.system[addr] = value
        blocks = capacity // 4
        self.system[0x0014] = (blocks - 1) & 0xFF
        self.system[0x0015] = ((blocks - 1) >> 8) & 0xFF
        product = {512: 0x24}.get(capacity, 0x26)
        self.system[0x0017] = product
        # UID reads out LSB first: serial, then the ST product and
        # manufacturer codes, then 0xE0 (Table 85).
        self.system[0x0018:0x0020] = (bytes(serial)[:4].ljust(4, b"\x00")
                                      + bytes([0x00, product, 0x02, 0xE0]))
        self.system[0x0020] = revision
        top = capacity // 32 - 1
        self.system[0x0005] = top                # ENDA1
        self.system[0x0007] = top                # ENDA2
        self.system[0x0009] = top                # ENDA3
        self.dynamic[0x00] = self.system[0x0000]         # GPO_CTRL_Dyn
        self.dynamic[0x02] = 0x08                        # EH_CTRL_Dyn: VCC_ON
        self.dynamic[0x03] = self.system[0x0003]         # RF_MNGT_Dyn

        # -- knobs the tests turn ----------------------------------------
        #: NACK the next N transfers, whatever they are. This is what a phone
        #: mid-transaction looks like from the I2C side.
        self.nack_next = 0
        #: NACK everything until cleared.
        self.rf_busy = False
        #: Transfers the chip stays silent for, per page programmed.
        self.write_busy_ticks = 1
        #: Every transfer, as (kind, address, memory address, bytes).
        self.log = []

        self._busy = 0
        self._pointer = 0
        self._locked = False
        #: Set by deinit(), so a test can tell an owned bus from a borrowed one.
        self.deinited = False

    # ------------------------------------------------------ busio.I2C API

    def try_lock(self):
        if self._locked:
            return False
        self._locked = True
        return True

    def unlock(self):
        self._locked = False

    def deinit(self):
        self.deinited = True

    def writeto(self, address, buf, start=0, end=None):
        buf = bytes(buf)[start:end]
        if not buf:                       # ACK poll: does the chip answer yet
            self.log.append(("poll", address, None, b""))
            self._gate()
            return
        if len(buf) < 2:
            raise NackError("an ST25DV transfer always carries a 16-bit address")
        addr = (buf[0] << 8) | buf[1]
        data = buf[2:]
        self.log.append(("write", address, addr, data))
        self._gate()
        self._pointer = addr
        self._write(address, addr, data)

    def readfrom_into(self, address, buf, start=0, end=None):
        end = len(buf) if end is None else end
        self.log.append(("read", address, self._pointer, end - start))
        self._gate()
        data = self._read(address, self._pointer, end - start)
        buf[start:end] = data
        self._pointer += end - start

    def writeto_then_readfrom(self, address, out_buf, in_buf, out_start=0,
                              out_end=None, in_start=0, in_end=None):
        out = bytes(out_buf)[out_start:out_end]
        if len(out) != 2:
            raise NackError("a random address read sends exactly two address "
                            "bytes")
        addr = (out[0] << 8) | out[1]
        in_end = len(in_buf) if in_end is None else in_end
        length = in_end - in_start
        self.log.append(("read", address, addr, length))
        self._gate()
        self._pointer = addr
        in_buf[in_start:in_end] = self._read(address, addr, length)
        self._pointer = addr + length

    # ---------------------------------------------------------- internals

    def _gate(self):
        """Everything the chip refuses before it even looks at the address."""
        if self.rf_busy:
            raise NackError("RF is busy: the I2C side is NACKed (section 5.5)")
        if self.nack_next > 0:
            self.nack_next -= 1
            raise NackError("transient NACK")
        if self._busy > 0:
            self._busy -= 1
            raise NackError("internal write cycle in progress")

    # -- geometry --------------------------------------------------------

    @property
    def capacity(self):
        return len(self.user)

    @property
    def area_bounds(self):
        """Inclusive ``(start, end)`` pairs for the areas that exist."""
        top = self.capacity // 32 - 1
        limits = (self.system[0x0005], self.system[0x0007],
                  self.system[0x0009])
        out = []
        start = 0
        for limit in limits:
            end = 32 * limit + 31
            if limit >= top:
                break
            out.append((start, end))
            start = end + 1
        out.append((start, self.capacity - 1))
        return out

    def _area_of(self, addr):
        for index, (start, end) in enumerate(self.area_bounds):
            if start <= addr <= end:
                return index
        return None

    @property
    def session_open(self):
        return bool(self.dynamic[0x04] & 0x01)

    @property
    def mailbox_enabled(self):
        return bool(self.dynamic[0x06] & 0x01)

    def _program(self, addr, length):
        """Charge the write cycle: tW per page touched, partial ones included."""
        pages = ((addr + length - 1) // 4) - (addr // 4) + 1
        self._busy = pages * self.write_busy_ticks

    # -- reads -----------------------------------------------------------

    def _read(self, address, addr, length):
        if address == self.system_address:
            return self._read_system(addr, length)
        if address == self.user_address:
            if addr >= _MB_FIRST:
                return self._read_mailbox(addr, length)
            if addr >= _DYN_FIRST:
                return self._read_dynamic(addr, length)
            return self._read_user(addr, length)
        raise NackError("no device at 0x%02x" % address)

    def _read_user(self, addr, length):
        out = bytearray()
        area = self._area_of(addr)
        for offset in range(length):
            here = addr + offset
            # No rollover, and a read that leaves its area returns 0xFF from
            # there on rather than continuing into the next one.
            if here >= self.capacity or self._area_of(here) != area:
                out.append(0xFF)
            else:
                out.append(self.user[here])
        return bytes(out)

    def _read_system(self, addr, length):
        out = bytearray()
        for offset in range(length):
            here = addr + offset
            if _PWD_ADDRESS <= here < _PWD_ADDRESS + 8:
                # The password reads back only inside an open session.
                out.append(self.password[here - _PWD_ADDRESS]
                           if self.session_open else 0xFF)
            elif here <= _SYSTEM_LAST:
                out.append(self.system[here])
            else:
                out.append(0xFF)
        return bytes(out)

    def _read_dynamic(self, addr, length):
        out = bytearray()
        for offset in range(length):
            here = addr + offset
            if here <= _DYN_LAST:
                out.append(self.dynamic[here - _DYN_FIRST])
                if here == 0x2005:
                    self.dynamic[0x05] = 0x00      # read-to-clear
            elif here <= _MB_LAST:
                out.append(self._mailbox_byte(here))
            else:
                out.append(0xFF)
        return bytes(out)

    def _read_mailbox(self, addr, length):
        if not self.mailbox_enabled:
            return b"\xff" * length
        out = bytearray()
        for offset in range(length):
            out.append(self._mailbox_byte(addr + offset))
        end = addr - _MB_FIRST + length
        if end >= self.dynamic[0x07] + 1:
            # The whole message has been read, so the mailbox is free again.
            self.dynamic[0x06] &= ~0x06 & 0xFF
        return bytes(out)

    def _mailbox_byte(self, addr):
        if not self.mailbox_enabled or addr > _MB_LAST:
            return 0xFF
        return self.mailbox[addr - _MB_FIRST]

    # -- writes ----------------------------------------------------------

    def _write(self, address, addr, data):
        if address == self.system_address:
            return self._write_system(addr, data)
        if address == self.user_address:
            if addr >= _MB_FIRST:
                return self._write_mailbox(addr, data)
            if addr >= _DYN_FIRST:
                return self._write_dynamic(addr, data)
            return self._write_user(addr, data)
        raise NackError("no device at 0x%02x" % address)

    def _write_user(self, addr, data):
        if len(data) > 256:
            raise NackError("a sequential write is at most 256 bytes")
        if self.mailbox_enabled:
            raise NackError("writes transit the mailbox buffer, which is on")
        if addr + len(data) > self.capacity:
            raise NackError("write runs past the end of user memory")
        if self._area_of(addr) != self._area_of(addr + len(data) - 1):
            raise NackError("a sequential write may not cross an area border")
        protection = (self.system[0x000B] >> (2 * self._area_of(addr))) & 0x03
        if protection and not self.session_open:
            raise NackError("area is I2C write protected and the session is "
                            "closed")
        self.user[addr:addr + len(data)] = data
        self._program(addr, len(data))

    def _write_system(self, addr, data):
        if addr == _PWD_ADDRESS:
            return self._password_command(data)
        if not self.session_open:
            raise NackError("system registers need the I2C security session")
        if self.mailbox_enabled:
            raise NackError("writes transit the mailbox buffer, which is on")
        if addr + len(data) - 1 > _SYSTEM_WRITABLE_LAST:
            raise NackError("system register 0x%04x is read only" % addr)
        for offset, value in enumerate(data):
            here = addr + offset
            if here in (0x0005, 0x0007, 0x0009):
                self._write_enda(here, value)
            else:
                self.system[here] = value
            if here == 0x0000:
                self.dynamic[0x00] = value       # GPO_CTRL_Dyn tracks GPO
            if here == 0x0002 and value == 0:
                self.dynamic[0x02] |= 0x01       # EH_MODE 0 turns EH on
            if here == 0x0003:
                self.dynamic[0x03] = value
            if here == 0x000D and value == 0:
                self.dynamic[0x06] &= ~0x01 & 0xFF   # MB_MODE 0 clears MB_EN
        self._program(addr, len(data))

    def _write_enda(self, addr, value):
        """Enforce ENDAi-1 < ENDAi <= ENDAi+1 = end of memory (section 4.2)."""
        top = self.capacity // 32 - 1
        if value > top:
            raise NackError("ENDA value past the end of memory")
        enda1, enda2, enda3 = (self.system[0x0005], self.system[0x0007],
                               self.system[0x0009])
        if addr == 0x0009:
            if not enda2 < value <= top:
                raise NackError("ENDA3 needs ENDA2 < ENDA3 <= end of memory")
        elif addr == 0x0007:
            if not (enda1 < value <= enda3 and enda3 == top):
                raise NackError("ENDA2 needs ENDA1 < ENDA2 <= ENDA3 = end of "
                                "memory")
        else:
            if not (value <= enda2 and enda2 == enda3 == top):
                raise NackError("ENDA1 needs ENDA1 <= ENDA2 = ENDA3 = end of "
                                "memory")
        self.system[addr] = value

    def _password_command(self, data):
        if len(data) != 17:
            raise NackError("a password command is 8 + 1 + 8 bytes")
        first, validation, second = data[:8], data[8], data[9:]
        if first != second:
            raise NackError("the two copies of the password differ")
        if validation == 0x09:                    # present
            self.dynamic[0x04] = 0x01 if first == bytes(self.password) else 0x00
        elif validation == 0x07:                  # write
            if not self.session_open:
                raise NackError("changing the password needs an open session")
            self.password[:] = first
            self._program(_PWD_ADDRESS, 8)
        else:
            raise NackError("unknown password validation byte 0x%02x"
                            % validation)

    def _write_dynamic(self, addr, data):
        if len(data) != 1:
            raise NackError("dynamic registers cannot be written in continuity")
        value = data[0]
        if not _DYN_FIRST <= addr <= _DYN_LAST:
            raise NackError("not a dynamic register")
        if addr == 0x2000:                        # only GPO_EN is writable
            self.dynamic[0] = (self.dynamic[0] & 0x7F) | (value & 0x80)
        elif addr == 0x2002:                      # only EH_EN is writable
            self.dynamic[2] = (self.dynamic[2] & 0xFE) | (value & 0x01)
            if value & 0x01:
                self.dynamic[2] |= 0x02           # EH_ON follows EH_EN
            else:
                self.dynamic[2] &= ~0x02 & 0xFF
        elif addr == 0x2003:
            self.dynamic[3] = value
        elif addr == 0x2006:                      # only MB_EN is writable
            if value & 0x01:
                if not self.system[0x000D] & 0x01:
                    raise NackError("MB_EN cannot be set while MB_MODE is 0")
                self.dynamic[6] |= 0x01
            else:
                self.dynamic[6] = 0x00            # disabling clears the state
                self.dynamic[7] = 0x00
        else:
            raise NackError("dynamic register 0x%04x is read only" % addr)

    def _write_mailbox(self, addr, data):
        if addr != _MB_FIRST:
            raise NackError("mailbox writes must start at the first byte")
        if not self.mailbox_enabled:
            raise NackError("fast transfer mode is off")
        if not data or len(data) > 256:
            raise NackError("a mailbox message is 1 to 256 bytes")
        if self.dynamic[0x06] & 0x06:
            raise NackError("the mailbox still holds an unread message")
        self.mailbox[:len(data)] = data
        self.dynamic[0x07] = len(data) - 1        # length minus one
        self.dynamic[0x06] |= 0x02 | 0x40         # HOST_PUT_MSG, HOST_CURRENT

    # -- what the RF side would do ---------------------------------------

    def rf_event(self, bits):
        """Set bits in IT_STS_Dyn, as a reader's activity would."""
        self.dynamic[0x05] |= bits

    def rf_field(self, present):
        """Raise or drop the RF field, with the event that goes with it."""
        if present:
            self.dynamic[0x02] |= 0x04
            self.rf_event(0x10)                   # FIELD_RISING
        else:
            self.dynamic[0x02] &= ~0x04 & 0xFF
            self.rf_event(0x08)                   # FIELD_FALLING

    def rf_write(self, addr, data):
        """A reader writing user memory, with the RF_WRITE event."""
        self.user[addr:addr + len(data)] = bytes(data)
        self.rf_event(0x80)

    def rf_put_mailbox(self, data):
        """A reader leaving a mailbox message for the host."""
        if not self.mailbox_enabled:
            raise NackError("fast transfer mode is off")
        self.mailbox[:len(data)] = bytes(data)
        self.dynamic[0x07] = len(data) - 1
        self.dynamic[0x06] |= 0x04 | 0x80         # RF_PUT_MSG, RF_CURRENT_MSG
        self.rf_event(0x20)
