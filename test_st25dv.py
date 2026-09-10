# On-device unit tests: run the pure-logic half of the driver under
# CircuitPython, where CPython-only behaviour actually shows up.
#
#     python tools/run_on_board.py test_st25dv.py
#
# Everything here is self-contained, because the board only gets one file. The
# bus below models just enough of a chip to exercise the driver's plumbing:
# identity, chunking, registers, NDEF. The awkward chip behaviours -- NACK
# arbitration, page timing, area rules, the session, the mailbox state machine
# -- are covered against the fuller simulation in tests/fake_st25dv.py, which
# runs on the desktop.
import st25dv
from st25dv import (Area, CapabilityContainer, Events, NDEFError, NDEFMessage,
                    NDEFRecord, ST25DV, hexlify)

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s\n       got  %r\n       want %r" % (name, got, want))


def check_raises(name, fn, exc):
    global passed, failed
    try:
        fn()
    except exc:
        passed += 1
        print("  ok   %s" % name)
        return
    except Exception as e:
        failed += 1
        print("  FAIL %s raised %s not %s" % (name, type(e).__name__,
                                              exc.__name__))
        return
    failed += 1
    print("  FAIL %s did not raise" % name)


class TinyBus:
    """A 2048-byte ST25DV with no timing, no arbitration and no protection."""

    def __init__(self, capacity=2048):
        self.user = bytearray(b"\xff") * capacity
        self.system = bytearray(0x21)
        self.dynamic = bytearray(8)
        self.mailbox = bytearray(256)
        blocks = capacity // 4
        self.system[0x14] = (blocks - 1) & 0xFF
        self.system[0x15] = ((blocks - 1) >> 8) & 0xFF
        self.system[0x16] = 0x03
        self.system[0x17] = 0x26
        self.system[0x18:0x20] = b"\x01\x02\x03\x04\x00\x26\x02\xe0"
        self.system[0x20] = 0x01
        top = capacity // 32 - 1
        self.system[0x05] = top
        self.system[0x07] = top
        self.system[0x09] = top
        self.dynamic[0x02] = 0x08
        self.dynamic[0x04] = 0x01          # session already open, for brevity
        self.writes = []
        self._locked = False

    def try_lock(self):
        if self._locked:
            return False
        self._locked = True
        return True

    def unlock(self):
        self._locked = False

    def _store(self, address, addr):
        if address == 0x57:
            return self.system, addr
        if addr >= 0x2008:
            return self.mailbox, addr - 0x2008
        if addr >= 0x2000:
            return self.dynamic, addr - 0x2000
        return self.user, addr

    def writeto(self, address, buf, start=0, end=None):
        buf = bytes(buf)[start:end]
        if not buf:
            return
        addr = (buf[0] << 8) | buf[1]
        data = buf[2:]
        self.writes.append((address, addr, len(data)))
        store, offset = self._store(address, addr)
        store[offset:offset + len(data)] = data

    def readfrom_into(self, address, buf, start=0, end=None):
        raise OSError("this driver always reads with a repeated start")

    def writeto_then_readfrom(self, address, out_buf, in_buf, out_start=0,
                              out_end=None, in_start=0, in_end=None):
        out = bytes(out_buf)[out_start:out_end]
        addr = (out[0] << 8) | out[1]
        in_end = len(in_buf) if in_end is None else in_end
        store, offset = self._store(address, addr)
        length = in_end - in_start
        chunk = bytes(store[offset:offset + length])
        chunk += b"\xff" * (length - len(chunk))
        in_buf[in_start:in_end] = chunk
        if addr == 0x2005:
            self.dynamic[0x05] = 0x00      # read-to-clear

    def deinit(self):
        pass


print("--- NDEF records ---")
check("uri prefix picked", NDEFRecord.uri("https://example.com").payload[0], 4)
check("uri www prefix", NDEFRecord.uri("https://www.a.co").payload[0], 2)
check("no prefix", NDEFRecord.uri("gopher://a.co").payload[0], 0)
check("uri value", NDEFRecord.uri("https://example.com").value,
      "https://example.com")
check("text value", NDEFRecord.text("hello").value, "hello")
check("text language", NDEFRecord.text("hello", "fr").language, "fr")
check("mime kind", NDEFRecord.mime("image/png", b"\x89P").kind, "mime")
check("external kind", NDEFRecord.external("a.com:b", b"x").kind, "external")

message = NDEFMessage([NDEFRecord.uri("https://a.co"), NDEFRecord.text("hi")])
check("round trip", NDEFMessage.from_bytes(message.to_bytes()), message)
check("first record flagged MB", message.to_bytes()[0] & 0x80, 0x80)
check("last record flagged ME", NDEFMessage.from_bytes(
    message.to_bytes()).records[1].value, "hi")

long_message = NDEFMessage.from_text("x" * 400)
check("long payload uses the 4-byte length", long_message.to_bytes()[0] & 0x10,
      0x00)
check("long round trip", NDEFMessage.from_bytes(
    long_message.to_bytes()).text, "x" * 400)
check_raises("empty parse raises", lambda: NDEFMessage.from_bytes(b""),
             NDEFError)

print("--- TLV ---")
tlv = st25dv._ndef_to_tlv(NDEFMessage.from_uri("https://a.co"))
check("tlv type", tlv[0], 0x03)
check("tlv terminator", tlv[-1], 0xFE)
check("tlv parses back", st25dv._ndef_from_tlv(tlv).uri, "https://a.co")
long_tlv = st25dv._ndef_to_tlv(long_message)
check("three-byte length marker", long_tlv[1], 0xFF)
check("three-byte length value", (long_tlv[2] << 8) | long_tlv[3],
      len(long_message.to_bytes()))
check("long tlv parses back", st25dv._ndef_from_tlv(long_tlv).text, "x" * 400)
check("terminator alone is no message", st25dv._ndef_from_tlv(b"\xfe"), None)
check("empty ndef tlv is no message", st25dv._ndef_from_tlv(b"\x03\x00\xfe"),
      None)

print("--- capability container ---")
cc = CapabilityContainer.from_bytes(b"\xe1\x40\x40\x05")
check("short form length", cc.length, 4)
check("short form capacity", cc.capacity, 512)
check("short form version", (cc.major_version, cc.minor_version), (1, 0))
check("short form writable", cc.writable, True)
cc = CapabilityContainer.from_bytes(b"\xe2\x40\x00\x01\x00\x00\x03\xff")
check("extended length", cc.length, 8)
check("extended capacity", cc.capacity, 0x3FF * 8)
check("extended re-encodes", cc.to_bytes(),
      b"\xe2\x40\x00\x01\x00\x00\x03\xff")
check("4K takes the short form", CapabilityContainer.for_capacity(512).length,
      4)
check("16K takes the extended form",
      CapabilityContainer.for_capacity(2048).to_bytes(),
      b"\xe2\x40\x00\x05\x00\x00\x00\xff")
check("64K capacity", CapabilityContainer.for_capacity(8192).capacity, 8184)
check_raises("unformatted is named",
             lambda: CapabilityContainer.from_bytes(b"\xff\xff\xff\xff"),
             NDEFError)

print("--- events ---")
events = Events(st25dv.IT_RF_WRITE | st25dv.IT_FIELD_RISING)
check("rf_write", events.rf_write, True)
check("field_rising", events.field_rising, True)
check("field_change", events.field_change, True)
check("rf_put_msg", events.rf_put_msg, False)
check("names", events.names, ["field_rising", "rf_write"])
check("empty is falsy", bool(Events(0)), False)

print("--- helpers ---")
check("hexlify", hexlify(b"\x01\xab"), "01:ab")
check("hexlify with spaces", hexlify(b"\x01\xab", " "), "01 ab")
check("area contains", 100 in Area(1, 0, 511), True)
check("area size", Area(1, 512, 1023).size, 512)

print("--- driver against a simulated bus ---")
bus = TinyBus()
tag = ST25DV(bus)
check("part", tag.part, "ST25DV16K-IE")
check("memory size", tag.memory_size, 2048)
check("block count", tag.block_count, 512)
check("block size", tag.block_size, 4)
check("uid is msb first", tag.uid, b"\xe0\x02\x26\x00\x04\x03\x02\x01")
check("uid hex", tag.uid_hex, "e0:02:26:00:04:03:02:01")
check("ic ref", tag.ic_ref, 0x26)
check("len", len(tag), 2048)
check("one area", tag.areas, (Area(1, 0, 2047),))

tag.write(0, b"ABCD")
check("write then read", tag.read(0, 4), b"ABCD")
tag[10] = 0x5A
check("index write", tag[10], 0x5A)
tag[20:24] = b"wxyz"
check("slice write", tag[20:24], b"wxyz")
tag.write(0, b"abcd")
check("open-ended slice start", tag[:4], b"abcd")
check("negative slice", tag[-4:], bytes(bus.user[-4:]))
check("whole slice length", len(tag[:]), 2048)
check("backwards slice is empty", tag[10:5], b"")
check_raises("stepped slice refused",
             lambda: tag.__getitem__(slice(0, 10, 2)), ValueError)
check_raises("slice length must match",
             lambda: tag.__setitem__(slice(20, 24), b"toolong"), ValueError)
check_raises("read past the end", lambda: tag.read(2040, 16), ValueError)

bus.writes = []
tag.write(0, b"\x00" * 600)
check("write split at 256", [n for _, _, n in bus.writes], [256, 256, 88])

written = tag.format()
check("format wrote the extended form", written.length, 8)
check("container reads back", tag.capability_container.capacity, 2040)
check("empty tag reads as none", tag.ndef, None)

tag.ndef = "https://thefilip.com"
check("ndef uri round trip", tag.ndef.uri, "https://thefilip.com")
tag.ndef = "a plain note"
check("ndef text round trip", tag.ndef.text, "a plain note")
tag.ndef = "z" * 400
check("long ndef round trip", tag.ndef.text, "z" * 400)
check("long ndef used the 3-byte tlv", bus.user[9], 0xFF)

tag.format(capacity=64)
check_raises("oversized message refused", lambda: tag.write_ndef("z" * 200),
             NDEFError)
tag.format()

check("no field", tag.field_present, False)
check("vcc seen", tag.vcc_present, True)
bus.dynamic[0x05] = st25dv.IT_RF_WRITE
check("events drained once", tag.poll_events().rf_write, True)
check("events cleared by the read", tag.poll_events().rf_write, False)
bus.dynamic[0x05] = st25dv.IT_RF_WRITE
repr(tag)
check("repr left the events alone", tag.poll_events().rf_write, True)

bus.dynamic[0x00] = 0x00
tag.gpo_enabled = True
check("gpo output bit set", bus.dynamic[0x00] & 0x80, 0x80)
tag.gpo_enabled = False
check("gpo output bit cleared", tag.gpo_enabled, False)
tag.energy_harvesting = True
check("energy harvesting on", bus.dynamic[0x02] & 0x01, 0x01)
tag.rf_sleep = True
check("rf sleep set", tag.rf_sleep, True)
tag.rf_sleep = False
check("rf sleep cleared", tag.rf_sleep, False)
check("interrupt pulse", round(tag.interrupt_pulse_us), 301)

print("--- area arithmetic ---")
check("max area limit", tag.max_area_limit, 63)
check_raises("areas must fill memory", lambda: tag.set_areas([512, 512]),
             st25dv.AreaError)
check_raises("areas are 32-byte multiples",
             lambda: tag.set_areas([500, 1548]), st25dv.AreaError)
check_raises("limits may not decrease",
             lambda: tag.set_area_limits(0x20, 0x10, 0x3F), st25dv.AreaError)
tag.set_areas([1024, 1024])
check("split into two", [a.size for a in tag.read_areas()], [1024, 1024])
check("second area starts after the first", tag.read_areas()[1].start, 1024)
tag.set_areas([2048])
check("merged back", len(tag.read_areas()), 1)

print("--- credential records ---")
# These lean on dicts, string methods and byte building that CPython is more
# forgiving about than the firmware, which is the whole reason they run here.
rec = NDEFRecord.tel("+441632960961")
check("tel prefix code", rec.payload[0], 5)
check("tel value", rec.value, "tel:+441632960961")
check("tel scheme not doubled", NDEFRecord.tel("tel:+1").value, "tel:+1")
check("sms body encoded", NDEFRecord.sms("+1", "on my way!").value,
      "sms:+1?body=on%20my%20way%21")

mail = NDEFRecord.email("grace@example.com", "Hello there")
check("mailto prefix code", mail.payload[0], 6)
check("mailto value", mail.value,
      "mailto:grace@example.com?subject=Hello%20there")
check("mailto scheme not doubled", NDEFRecord.email("mailto:a@b.c").value,
      "mailto:a@b.c")

wifi = NDEFRecord.wifi("Guest Wi-Fi", "correct horse")
check("wifi kind", wifi.kind, "wifi")
check("wifi mime", bytes(wifi.type), b"application/vnd.wfa.wsc")
decoded = wifi.value
check("wifi ssid", decoded["ssid"], "Guest Wi-Fi")
check("wifi password", decoded["password"], "correct horse")
check("wifi security", decoded["security"], "wpa2")
check("wifi utf8 ssid", NDEFRecord.wifi("caf\u00e9", "hunter22").value["ssid"],
      "caf\u00e9")
check("open network", NDEFRecord.wifi("Cafe").value["security"], "open")
check_raises("open network takes no password",
             lambda: NDEFRecord.wifi("Cafe", "x" * 8,
                                     authentication=st25dv.WIFI_OPEN),
             NDEFError)
check_raises("ssid must fit", lambda: NDEFRecord.wifi("x" * 33, "hunter22"),
             NDEFError)

card = NDEFRecord.contact("Grace Hopper", phone="+441632960961",
                          email="grace@example.com", organization="US Navy")
check("contact kind", card.kind, "contact")
check("contact name", card.value["name"], "Grace Hopper")
check("contact phone", card.value["phone"], ["+441632960961"])
check("contact org", card.value["organization"], "US Navy")
check("surname split", "N:Hopper;Grace;;;" in card.value["text"], True)

bt = NDEFRecord.bluetooth("a4:c1:38:01:02:03", name="Speaker")
check("bd_addr little endian", bytes(bt.payload[2:8]),
      b"\x03\x02\x01\x38\xc1\xa4")
check("bt length counts itself", bt.payload[0] | (bt.payload[1] << 8),
      len(bt.payload))
check("bt address read back", bt.value["address"], "a4:c1:38:01:02:03")
check("bt name", bt.value["name"], "Speaker")
ble = NDEFRecord.bluetooth_le("a4:c1:38:01:02:03", address_type=1,
                              role=st25dv.BLE_CENTRAL_ONLY)
check("ble kind", ble.kind, "bluetooth_le")
check("ble address type", ble.value["address_type"], "random")
check("ble role", ble.value["role"], "central")
check("address without separators", NDEFRecord.bluetooth("a4c138010203")
      .value["address"], "a4:c1:38:01:02:03")
check_raises("short address refused",
             lambda: NDEFRecord.bluetooth("a4:c1:38"), NDEFError)

check("homekit scheme added", NDEFRecord.homekit("0024K0M6P00HB").value,
      "X-HM://0024K0M6P00HB")

combined = NDEFMessage([NDEFRecord.uri("https://example.com"),
                        NDEFRecord.wifi("Guest", "hunter22")])
round_tripped = NDEFMessage.from_bytes(combined.to_bytes())
check("two records survive encoding", len(round_tripped), 2)
check("first() finds the uri", round_tripped.first("uri"),
      "https://example.com")
check("wifi accessor", round_tripped.wifi["ssid"], "Guest")
check("missing kind is none", round_tripped.first("contact"), None)

tag.ndef = "tel:+441632960961"
check("tel string stored as uri", tag.ndef.uri, "tel:+441632960961")
tag.ndef = "Note: buy milk"
check("prose with a colon stays text", tag.ndef.text, "Note: buy milk")

print()
print("%d passed, %d failed" % (passed, failed))
