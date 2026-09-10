# SPDX-License-Identifier: MIT
"""
`st25dv` - a CircuitPython driver for the ST ST25DV dual-interface NFC tag.

The ST25DV is not an EEPROM with an antenna glued on. It is a tag that two
masters share: an I2C host on one side, an ISO/IEC 15693 (NFC Forum Type 5)
reader on the other. Treating it as a plain EEPROM works right up until a phone
touches it, at which point every I2C transfer starts returning NACK.

Quick start::

    import board
    from st25dv import ST25DV

    tag = ST25DV(board.STEMMA_I2C())
    print(tag.part, tag.memory_size, tag.uid_hex)

    tag.ndef = "https://thefilip.com"      # a phone now reads this
    print(tag.ndef.uri)

React to a tap without wiring the GPO pin::

    if tag.wait_for_field(timeout=30):
        print("phone!")

Design notes
------------
* Two I2C addresses, one chip. 0x53 reaches user memory, the dynamic registers
  and the mailbox; 0x57 reaches the system area and the password register. The
  driver picks the right one per register, so callers never see the split.
* Every transfer is retried on NACK, because a NACK usually means "the RF side
  is mid-transaction", not "the device is gone" (DS10925 section 5.5).
* Memory size is read from the part, never assumed, so the same code covers the
  4K, 16K and 64K variants.
* NDEF is decoded *and* encoded, including the capability container, so putting
  a URL on the tag is one assignment.
* Errors are a hierarchy under :class:`ST25DVError`, and every wait takes a
  ``timeout=``.

Every register address, bit position and timing constant below is cited to
datasheet DS10925 Rev 11 so it can be rechecked.
"""

from time import sleep
from micropython import const
from supervisor import ticks_ms

__version__ = "0.0.0+auto.0"

_TICKS_PERIOD = const(1 << 29)

# ---------------------------------------------------------------- exceptions


class ST25DVError(Exception):
    """Base class for every error this driver raises."""


class NotFoundError(ST25DVError):
    """No ST25DV answered on the bus, or it answered with nonsense."""


class BusyError(ST25DVError):
    """The chip NACKed for longer than the timeout allowed.

    Section 5.5: arbitration between the two interfaces is first-talk-first-
    served, and "when RF is busy, I2C interface answers by NoAck on any I2C
    command". So this usually means a reader is holding the tag, not that the
    tag is broken. Retrying later normally works.
    """


class SessionRequired(ST25DVError):
    """A system register was written with the I2C security session closed.

    Table 11 note 2: every static register is writable only while the session
    is open. Call :meth:`ST25DV.open_session` first.
    """


class ProtectedError(ST25DVError):
    """The chip refused a write that its protection settings forbid."""


class NDEFError(ST25DVError):
    """The tag's NDEF data or capability container is malformed, or the
    message being written does not fit the declared capacity."""


class MailboxError(ST25DVError):
    """The fast transfer mode mailbox was used in a state that forbids it."""


class AreaError(ST25DVError):
    """An area (ENDAi) operation broke one of the chip's ordering rules, or a
    transfer would have crossed an area boundary in a way the chip forbids."""


# ----------------------------------------------------------------- constants

#: Device select code with E2=0: user memory, dynamic registers, mailbox
#: (Table 88).
ADDRESS_USER = const(0x53)
#: Device select code with E2=1: system configuration area and I2C_PWD.
ADDRESS_SYSTEM = const(0x57)

# -- system configuration area, device select E2=1 (Table 11) ---------------
REG_GPO = const(0x0000)          # GPO event mask + GPO_EN
REG_IT_TIME = const(0x0001)      # interrupt pulse duration
REG_EH_MODE = const(0x0002)      # energy harvesting strategy after power on
REG_RF_MNGT = const(0x0003)      # RF interface state after power on
REG_RFA1SS = const(0x0004)       # area 1 RF access protection
REG_ENDA1 = const(0x0005)        # area 1 ending point
REG_RFA2SS = const(0x0006)
REG_ENDA2 = const(0x0007)
REG_RFA3SS = const(0x0008)
REG_ENDA3 = const(0x0009)
REG_RFA4SS = const(0x000A)
REG_I2CSS = const(0x000B)        # area 1..4 I2C access protection
REG_LOCK_CCFILE = const(0x000C)  # RF write protection of blocks 0 and 1
REG_MB_MODE = const(0x000D)      # may fast transfer mode be enabled at all
REG_MB_WDG = const(0x000E)       # mailbox watchdog
REG_LOCK_CFG = const(0x000F)     # protect RF writes to system registers
REG_LOCK_DSFID = const(0x0010)
REG_LOCK_AFI = const(0x0011)
REG_DSFID = const(0x0012)
REG_AFI = const(0x0013)
REG_MEM_SIZE = const(0x0014)     # 2 bytes, little endian, blocks minus one
REG_BLK_SIZE = const(0x0016)     # block size in bytes minus one
REG_IC_REF = const(0x0017)
REG_UID = const(0x0018)          # 8 bytes, LSB first
REG_IC_REV = const(0x0020)
REG_I2C_PWD = const(0x0900)      # 8 bytes; also the present/write password port

# -- dynamic registers, device select E2=0 (Table 12) -----------------------
REG_GPO_CTRL_DYN = const(0x2000)
REG_EH_CTRL_DYN = const(0x2002)
REG_RF_MNGT_DYN = const(0x2003)
REG_I2C_SSO_DYN = const(0x2004)
REG_IT_STS_DYN = const(0x2005)   # read-to-clear, see poll_events()
REG_MB_CTRL_DYN = const(0x2006)
REG_MB_LEN_DYN = const(0x2007)   # message length minus one (Table 21)
REG_MAILBOX = const(0x2008)      # 256 bytes, 0x2008..0x2107 (Table 13)

#: GPO / GPO_CTRL_Dyn event bits (Tables 26 and 30).
GPO_RF_USER = const(0x01)
GPO_RF_ACTIVITY = const(0x02)
GPO_RF_INTERRUPT = const(0x04)
GPO_FIELD_CHANGE = const(0x08)
GPO_RF_PUT_MSG = const(0x10)
GPO_RF_GET_MSG = const(0x20)
GPO_RF_WRITE = const(0x40)
GPO_ENABLE = const(0x80)

#: IT_STS_Dyn event bits (Table 32).
IT_RF_USER = const(0x01)
IT_RF_ACTIVITY = const(0x02)
IT_RF_INTERRUPT = const(0x04)
IT_FIELD_FALLING = const(0x08)
IT_FIELD_RISING = const(0x10)
IT_RF_PUT_MSG = const(0x20)
IT_RF_GET_MSG = const(0x40)
IT_RF_WRITE = const(0x80)

#: EH_CTRL_Dyn bits (Table 37).
EH_EN = const(0x01)
EH_ON = const(0x02)
EH_FIELD_ON = const(0x04)
EH_VCC_ON = const(0x08)

#: RF_MNGT / RF_MNGT_Dyn bits (Tables 40 and 42).
RF_DISABLE = const(0x01)
RF_SLEEP = const(0x02)

#: MB_CTRL_Dyn bits (Table 19).
MB_EN = const(0x01)
MB_HOST_PUT_MSG = const(0x02)
MB_RF_PUT_MSG = const(0x04)
MB_HOST_MISS_MSG = const(0x10)
MB_RF_MISS_MSG = const(0x20)
MB_HOST_CURRENT_MSG = const(0x40)
MB_RF_CURRENT_MSG = const(0x80)

#: Validation bytes for the two password commands (sections 6.6.1 and 6.6.2).
_PWD_PRESENT = const(0x09)
_PWD_WRITE = const(0x07)

#: EEPROM page size in bytes (section 6.4.2). Write time is charged per page
#: touched, including partial ones.
PAGE_SIZE = const(4)
#: Maximum data bytes in one sequential write (section 6.4.2).
MAX_WRITE = const(256)
#: Bytes read in one transaction. Not a chip limit; it bounds buffer size.
MAX_READ = const(256)
#: Worst-case write time for one page, seconds. tW = 5 ms at 85 degrees C,
#: 5.5 ms at 125 (Tables 248 and 249). The larger figure is used.
PAGE_WRITE_TIME = 0.0055
#: Mailbox size in bytes (Table 13).
MAILBOX_SIZE = const(256)
#: Area limits are programmed in units of 32 bytes (Table 4).
AREA_GRANULARITY = const(32)

#: How long a transfer keeps retrying through NACKs before raising BusyError.
#: Long enough to sit out a phone's transaction, short enough that a missing
#: chip is reported promptly.
DEFAULT_BUSY_TIMEOUT = 0.5


def hexlify(data, sep=":"):
    """``b'\\x01\\x02'`` -> ``'01:02'``. Handy for printing UIDs."""
    return sep.join("%02x" % b for b in data)


def _deadline(seconds):
    """A wrap-safe deadline, `seconds` from now."""
    return (ticks_ms() + int(seconds * 1000)) % _TICKS_PERIOD


def _expired(deadline):
    """True once `deadline` has passed. ticks_ms() wraps at 2**29 ms, so
    compare via a signed difference rather than subtracting directly."""
    diff = (ticks_ms() - deadline + _TICKS_PERIOD // 2) % _TICKS_PERIOD \
        - _TICKS_PERIOD // 2
    return diff >= 0


class _Bus:
    """Hold the I2C lock for the duration of a block, re-entrantly.

    ``try_lock`` reports only that someone holds the lock, never who, so
    nesting is counted here rather than asked of the bus: the outermost block
    acquires, inner ones only count, and the release happens once when the
    outermost exits. Anything else sharing the bus - another driver on the same
    STEMMA QT chain, another asyncio task - is waited out rather than unlocked
    out from under, which is what a driver that trusts ``try_lock`` to mean
    "mine" ends up doing.
    """

    def __init__(self, i2c, timeout):
        self._i2c = i2c
        # A callable, not a number, so that changing tag.busy_timeout at
        # runtime is honoured here too.
        self._timeout = timeout
        self._depth = 0

    def __enter__(self):
        if self._depth == 0:
            deadline = _deadline(self._timeout())
            while not self._i2c.try_lock():
                if _expired(deadline):
                    raise BusyError(
                        "the I2C bus was still held by something else after "
                        "%d ms. This driver never nests its own locks, so the "
                        "holder is another driver or task on the same bus."
                        % int(self._timeout() * 1000))
                sleep(0.0002)
        self._depth += 1
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            self._i2c.unlock()
        return False


# ---------------------------------------------------------------------- NDEF

TNF_EMPTY = const(0x00)
TNF_WELL_KNOWN = const(0x01)
TNF_MIME = const(0x02)
TNF_ABSOLUTE_URI = const(0x03)
TNF_EXTERNAL = const(0x04)
TNF_UNKNOWN = const(0x05)
TNF_UNCHANGED = const(0x06)

# NFC Forum URI Record Type Definition, table 3.
_URI_PREFIXES = (
    "", "http://www.", "https://www.", "http://", "https://", "tel:",
    "mailto:", "ftp://anonymous:anonymous@", "ftp://ftp.", "ftps://",
    "sftp://", "smb://", "nfs://", "ftp://", "dav://", "news:",
    "telnet://", "imap:", "rtsp://", "urn:", "pop:", "sip:", "sips:",
    "tftp:", "btspp://", "btl2cap://", "btgoep://", "tcpobex://",
    "irdaobex://", "file://", "urn:epc:id:", "urn:epc:tag:",
    "urn:epc:pat:", "urn:epc:raw:", "urn:epc:", "urn:nfc:",
)


# ------------------------------------------------------- credential payloads

#: MIME types a phone dispatches on. They have to be exact: Android matches
#: them literally when it decides which system dialog to raise.
MIME_WIFI = "application/vnd.wfa.wsc"
MIME_BLUETOOTH = "application/vnd.bluetooth.ep.oob"
MIME_BLUETOOTH_LE = "application/vnd.bluetooth.le.oob"
MIME_VCARD = "text/vcard"

#: A HomeKit setup payload is an Apple URI scheme, the same string printed
#: under the QR code on an accessory. See :meth:`NDEFRecord.homekit`.
HOMEKIT_SCHEME = "X-HM://"

_UNRESERVED = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
               "abcdefghijklmnopqrstuvwxyz0123456789-_.~")


def _percent_encode(text):
    out = ""
    for byte in text.encode("utf-8"):
        char = chr(byte)
        out += char if char in _UNRESERVED else "%%%02X" % byte
    return out


def _be16(value):
    return bytes([(value >> 8) & 0xFF, value & 0xFF])


def _as_list(value):
    """One string, an iterable of them, or nothing, as a tuple."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _mac_bytes(mac, reverse=False):
    """``"a4:c1:38:01:02:03"``, ``"a4c1380102 03"`` or six raw bytes.

    ``reverse`` gives the little-endian order Bluetooth puts a BD_ADDR in,
    built with an explicit loop because CircuitPython does not take ``[::-1]``
    on every buffer type.
    """
    if isinstance(mac, str):
        text = ""
        for char in mac:
            if char not in ":-. ":
                text += char
        if len(text) != 12:
            raise NDEFError("a MAC address is six bytes, got %r" % mac)
        raw = bytearray(6)
        for i in range(6):
            try:
                raw[i] = int(text[2 * i:2 * i + 2], 16)
            except ValueError:
                raise NDEFError("a MAC address is hexadecimal, got %r" % mac)
    else:
        raw = bytearray(mac)
        if len(raw) != 6:
            raise NDEFError("a MAC address is six bytes, got %d" % len(raw))
    if not reverse:
        return bytes(raw)
    out = bytearray(6)
    for i in range(6):
        out[5 - i] = raw[i]
    return bytes(out)


def _mac_hex(raw, reverse=False):
    """Six bytes back to ``"a4:c1:38:01:02:03"``."""
    if not reverse:
        return hexlify(raw)
    out = bytearray(6)
    for i in range(6):
        out[5 - i] = raw[i]
    return hexlify(out)


# -- Wi-Fi Simple Configuration ---------------------------------------------
# The payload is a flat run of big-endian TLVs, two bytes of type and two of
# length, with a single Credential TLV wrapping the network's own fields.

_WSC_CREDENTIAL = const(0x100E)
_WSC_AUTH_TYPE = const(0x1003)
_WSC_ENCRYPT_TYPE = const(0x100F)
_WSC_MAC_ADDRESS = const(0x1020)
_WSC_NETWORK_INDEX = const(0x1026)
_WSC_NETWORK_KEY = const(0x1027)
_WSC_SSID = const(0x1045)

#: Authentication types.
WIFI_OPEN = const(0x0001)
WIFI_WPA_PSK = const(0x0002)
WIFI_SHARED = const(0x0004)
WIFI_WPA_EAP = const(0x0008)
WIFI_WPA2_EAP = const(0x0010)
WIFI_WPA2_PSK = const(0x0020)
WIFI_WPA_WPA2_PSK = const(0x0022)

#: Encryption types.
WIFI_ENC_NONE = const(0x0001)
WIFI_ENC_WEP = const(0x0002)
WIFI_ENC_TKIP = const(0x0004)
WIFI_ENC_AES = const(0x0008)
WIFI_ENC_AES_TKIP = const(0x000C)

_WIFI_SECURITY_NAMES = {
    WIFI_OPEN: "open",
    WIFI_WPA_PSK: "wpa",
    WIFI_SHARED: "wep-shared",
    WIFI_WPA_EAP: "wpa-enterprise",
    WIFI_WPA2_EAP: "wpa2-enterprise",
    WIFI_WPA2_PSK: "wpa2",
    WIFI_WPA_WPA2_PSK: "wpa/wpa2",
}


def _wsc_tlv(kind, value):
    value = bytes(value)
    return _be16(kind) + _be16(len(value)) + value


def _wsc_walk(data):
    """``(type, value)`` per TLV, stopping at the first one that runs off."""
    out = []
    data = bytes(data)
    i = 0
    while i + 4 <= len(data):
        kind = (data[i] << 8) | data[i + 1]
        length = (data[i + 2] << 8) | data[i + 3]
        i += 4
        if i + length > len(data):
            break
        out.append((kind, data[i:i + length]))
        i += length
    return out


def _wifi_payload(ssid, password="", authentication=None, encryption=None,
                  mac=None, network_index=1):
    ssid = ssid.encode("utf-8") if isinstance(ssid, str) else bytes(ssid)
    key = password.encode("utf-8") if isinstance(password, str) \
        else bytes(password)
    if not 1 <= len(ssid) <= 32:
        raise NDEFError("an SSID is 1 to 32 bytes, got %d" % len(ssid))
    if authentication is None:
        authentication = WIFI_WPA2_PSK if key else WIFI_OPEN
    if encryption is None:
        encryption = WIFI_ENC_AES if key else WIFI_ENC_NONE
    if authentication == WIFI_OPEN and key:
        raise NDEFError("an open network takes no password")
    if authentication != WIFI_OPEN and not key:
        raise NDEFError("a secured network needs a password; pass "
                        "authentication=WIFI_OPEN for an open one")
    # 8 to 63 for a passphrase, exactly 64 for a hex PSK.
    if key and not 8 <= len(key) <= 64:
        raise NDEFError("a WPA key is 8 to 63 characters, or 64 hex digits; "
                        "got %d" % len(key))
    body = _wsc_tlv(_WSC_NETWORK_INDEX, bytes([network_index & 0xFF]))
    body += _wsc_tlv(_WSC_SSID, ssid)
    body += _wsc_tlv(_WSC_AUTH_TYPE, _be16(authentication))
    body += _wsc_tlv(_WSC_ENCRYPT_TYPE, _be16(encryption))
    body += _wsc_tlv(_WSC_NETWORK_KEY, key)
    if mac is not None:
        body += _wsc_tlv(_WSC_MAC_ADDRESS, _mac_bytes(mac))
    return _wsc_tlv(_WSC_CREDENTIAL, body)


def _wifi_decode(payload):
    out = {"ssid": None, "password": "", "authentication": None,
           "encryption": None, "mac": None, "security": "unknown"}
    for kind, value in _wsc_walk(payload):
        if kind != _WSC_CREDENTIAL:
            continue
        for inner, data in _wsc_walk(value):
            if inner == _WSC_SSID:
                out["ssid"] = _to_str(data)
            elif inner == _WSC_NETWORK_KEY:
                out["password"] = _to_str(data)
            elif inner == _WSC_AUTH_TYPE and len(data) >= 2:
                out["authentication"] = (data[0] << 8) | data[1]
            elif inner == _WSC_ENCRYPT_TYPE and len(data) >= 2:
                out["encryption"] = (data[0] << 8) | data[1]
            elif inner == _WSC_MAC_ADDRESS and len(data) == 6:
                out["mac"] = _mac_hex(data)
        break
    if out["ssid"] is None:
        raise NDEFError("no Wi-Fi credential in this record: no SSID field "
                        "inside a Credential TLV")
    out["security"] = _WIFI_SECURITY_NAMES.get(out["authentication"],
                                               "unknown")
    return out


# -- Bluetooth out-of-band pairing ------------------------------------------
# BR/EDR carries a length and a BD_ADDR then EIR structures; Low Energy is
# EIR structures alone, with the address as one of them.

_EIR_SHORT_NAME = const(0x08)
_EIR_COMPLETE_NAME = const(0x09)
_EIR_CLASS_OF_DEVICE = const(0x0D)
_EIR_LE_APPEARANCE = const(0x19)
_EIR_LE_DEVICE_ADDRESS = const(0x1B)
_EIR_LE_ROLE = const(0x1C)

#: LE roles.
BLE_PERIPHERAL_ONLY = const(0x00)
BLE_CENTRAL_ONLY = const(0x01)
BLE_PERIPHERAL_PREFERRED = const(0x02)
BLE_CENTRAL_PREFERRED = const(0x03)

_BLE_ROLE_NAMES = ("peripheral", "central", "peripheral-preferred",
                   "central-preferred")


def _eir(kind, value):
    """One EIR/AD structure: a length covering type and value, then both."""
    value = bytes(value)
    return bytes([len(value) + 1, kind & 0xFF]) + value


def _eir_walk(data):
    out = []
    data = bytes(data)
    i = 0
    while i < len(data):
        length = data[i]
        if length == 0 or i + 1 + length > len(data):
            break
        out.append((data[i + 1], data[i + 2:i + 1 + length]))
        i += 1 + length
    return out


def _bluetooth_payload(address, name=None, class_of_device=None):
    body = b""
    if name is not None:
        body += _eir(_EIR_COMPLETE_NAME, name.encode("utf-8"))
    if class_of_device is not None:
        body += _eir(_EIR_CLASS_OF_DEVICE,
                     bytes([class_of_device & 0xFF,
                            (class_of_device >> 8) & 0xFF,
                            (class_of_device >> 16) & 0xFF]))
    addr = _mac_bytes(address, reverse=True)
    total = len(addr) + len(body) + 2          # the two length bytes count
    return bytes([total & 0xFF, (total >> 8) & 0xFF]) + addr + body


def _bluetooth_le_payload(address, address_type=0, role=BLE_PERIPHERAL_ONLY,
                          name=None, appearance=None):
    addr = _mac_bytes(address, reverse=True)
    body = _eir(_EIR_LE_DEVICE_ADDRESS, addr + bytes([address_type & 0x01]))
    body += _eir(_EIR_LE_ROLE, bytes([role & 0xFF]))
    if name is not None:
        body += _eir(_EIR_COMPLETE_NAME, name.encode("utf-8"))
    if appearance is not None:
        body += _eir(_EIR_LE_APPEARANCE,
                     bytes([appearance & 0xFF, (appearance >> 8) & 0xFF]))
    return body


def _bluetooth_decode(payload, low_energy):
    payload = bytes(payload)
    out = {"address": None, "name": None, "low_energy": bool(low_energy),
           "address_type": None, "role": None, "class_of_device": None}
    if low_energy:
        structures = _eir_walk(payload)
    else:
        if len(payload) < 8:
            raise NDEFError("a Bluetooth OOB record is at least 8 bytes, "
                            "got %d" % len(payload))
        out["address"] = _mac_hex(payload[2:8], reverse=True)
        structures = _eir_walk(payload[8:])
    for kind, value in structures:
        if kind in (_EIR_COMPLETE_NAME, _EIR_SHORT_NAME):
            out["name"] = _to_str(value)
        elif kind == _EIR_CLASS_OF_DEVICE and len(value) == 3:
            out["class_of_device"] = (value[0] | (value[1] << 8)
                                      | (value[2] << 16))
        elif kind == _EIR_LE_DEVICE_ADDRESS and len(value) == 7:
            out["address"] = _mac_hex(value[:6], reverse=True)
            out["address_type"] = "random" if value[6] & 0x01 else "public"
        elif kind == _EIR_LE_ROLE and value:
            out["role"] = _BLE_ROLE_NAMES[value[0]] \
                if value[0] < len(_BLE_ROLE_NAMES) else value[0]
    if out["address"] is None:
        raise NDEFError("no Bluetooth device address in this record")
    return out


# -- vCard ------------------------------------------------------------------


def _vcard_escape(value):
    out = ""
    for char in value:
        if char in "\\,;":
            out += "\\" + char
        elif char == "\n":
            out += "\\n"
        elif char != "\r":
            out += char
    return out


def _vcard_payload(name=None, phone=None, email=None, first=None, last=None,
                   organization=None, title=None, url=None, address=None,
                   note=None):
    if first is None and last is None and name:
        cut = name.rfind(" ")
        first, last = (name[:cut], name[cut + 1:]) if cut > 0 else (name, "")
    first = first or ""
    last = last or ""
    if not name:
        name = (first + " " + last).strip()
    if not name:
        raise NDEFError("a contact needs a name, or a first or last name")
    lines = ["BEGIN:VCARD", "VERSION:3.0",
             "N:%s;%s;;;" % (_vcard_escape(last), _vcard_escape(first)),
             "FN:%s" % _vcard_escape(name)]
    for number in _as_list(phone):
        lines.append("TEL;TYPE=CELL:%s" % _vcard_escape(number))
    for one in _as_list(email):
        lines.append("EMAIL;TYPE=INTERNET:%s" % _vcard_escape(one))
    if organization:
        lines.append("ORG:%s" % _vcard_escape(organization))
    if title:
        lines.append("TITLE:%s" % _vcard_escape(title))
    if url:
        lines.append("URL:%s" % _vcard_escape(url))
    if address:
        lines.append("ADR;TYPE=HOME:;;%s" % _vcard_escape(address))
    if note:
        lines.append("NOTE:%s" % _vcard_escape(note))
    lines.append("END:VCARD")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _vcard_decode(payload):
    text = _to_str(payload)
    out = {"text": text, "name": None, "phone": [], "email": [],
           "organization": None}
    for line in text.replace("\r\n", "\n").split("\n"):
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        key = parts[0].split(";")[0].upper()
        value = parts[1]
        if key == "FN":
            out["name"] = value
        elif key == "TEL":
            out["phone"].append(value)
        elif key == "EMAIL":
            out["email"].append(value)
        elif key == "ORG":
            out["organization"] = value
    return out



class NDEFRecord:
    """A single NDEF record.

    The useful bits are :attr:`kind` (``"uri"``, ``"text"``, ``"mime"``,
    ``"external"`` or ``"unknown"``) and :attr:`value`, which is already
    decoded: a ``str`` for URI and text records, ``bytes`` otherwise.
    """

    def __init__(self, tnf, rtype, payload, rid=b""):
        self.tnf = tnf
        self.type = rtype
        self.payload = payload
        self.id = rid

    @property
    def kind(self):
        if self.tnf == TNF_WELL_KNOWN and self.type == b"U":
            return "uri"
        if self.tnf == TNF_WELL_KNOWN and self.type == b"T":
            return "text"
        if self.tnf == TNF_MIME:
            mime = _to_str(self.type).lower()
            if mime == MIME_WIFI:
                return "wifi"
            if mime == MIME_BLUETOOTH:
                return "bluetooth"
            if mime == MIME_BLUETOOTH_LE:
                return "bluetooth_le"
            if mime in (MIME_VCARD, "text/x-vcard"):
                return "contact"
            return "mime"
        if self.tnf == TNF_ABSOLUTE_URI:
            return "uri"
        if self.tnf == TNF_EXTERNAL:
            return "external"
        return "unknown"

    @property
    def value(self):
        """The decoded contents: ``str`` for uri/text, else raw ``bytes``."""
        kind = self.kind
        if kind == "uri":
            if self.tnf == TNF_ABSOLUTE_URI:
                return _to_str(self.payload)
            if not self.payload:
                return ""
            code = self.payload[0]
            prefix = _URI_PREFIXES[code] if code < len(_URI_PREFIXES) else ""
            return prefix + _to_str(self.payload[1:])
        if kind == "text":
            if not self.payload:
                return ""
            lang_len = self.payload[0] & 0x3F
            return _to_str(self.payload[1 + lang_len:])
        if kind == "wifi":
            return _wifi_decode(self.payload)
        if kind == "bluetooth":
            return _bluetooth_decode(self.payload, False)
        if kind == "bluetooth_le":
            return _bluetooth_decode(self.payload, True)
        if kind == "contact":
            return _vcard_decode(self.payload)
        return self.payload

    @property
    def language(self):
        """Language code of a text record, else ``None``."""
        if self.kind != "text" or not self.payload:
            return None
        lang_len = self.payload[0] & 0x3F
        return _to_str(self.payload[1:1 + lang_len])

    def __repr__(self):
        val = self.value
        if isinstance(val, bytes):
            val = hexlify(val[:12]) + ("..." if len(val) > 12 else "")
        return "<NDEFRecord %s %r>" % (self.kind, val)

    def __eq__(self, other):
        # CircuitPython does not derive __ne__ from __eq__, so both are here.
        return (isinstance(other, NDEFRecord)
                and self.tnf == other.tnf
                and bytes(self.type) == bytes(other.type)
                and bytes(self.payload) == bytes(other.payload)
                and bytes(self.id) == bytes(other.id))

    def __ne__(self, other):
        return not self.__eq__(other)

    def to_bytes(self, first=True, last=True):
        """Encode this record. ``first``/``last`` set the MB/ME flags."""
        flags = self.tnf & 0x07
        if first:
            flags |= 0x80
        if last:
            flags |= 0x40
        if self.id:
            flags |= 0x08
        short = len(self.payload) < 256
        if short:
            flags |= 0x10
        out = bytearray([flags, len(self.type)])
        if short:
            out.append(len(self.payload))
        else:
            n = len(self.payload)
            out += bytes([(n >> 24) & 0xFF, (n >> 16) & 0xFF,
                          (n >> 8) & 0xFF, n & 0xFF])
        if self.id:
            out.append(len(self.id))
        out += self.type
        out += self.id
        out += self.payload
        return bytes(out)

    # -- constructors ----------------------------------------------------

    @classmethod
    def uri(cls, uri):
        """Build a URI record, choosing the shortest prefix code."""
        best_i, best_p = 0, ""
        for i, pre in enumerate(_URI_PREFIXES):
            if pre and uri.startswith(pre) and len(pre) > len(best_p):
                best_i, best_p = i, pre
        return cls(TNF_WELL_KNOWN, b"U",
                   bytes([best_i]) + uri[len(best_p):].encode("utf-8"))

    @classmethod
    def text(cls, text, language="en"):
        """Build a UTF-8 text record."""
        lang = language.encode("utf-8")
        return cls(TNF_WELL_KNOWN, b"T",
                   bytes([len(lang) & 0x3F]) + lang + text.encode("utf-8"))

    @classmethod
    def mime(cls, mime_type, data):
        """Build a MIME record, e.g. ``mime("image/png", data)``."""
        return cls(TNF_MIME, mime_type.encode("utf-8"), bytes(data))

    @classmethod
    def external(cls, type_name, data):
        """Build an external-type record.

        ``type_name`` is the domain-qualified name without the ``urn:nfc:ext:``
        prefix, e.g. ``external("example.com:widget", b"...")``.
        """
        return cls(TNF_EXTERNAL, type_name.encode("utf-8"), bytes(data))

    # -- credentials -----------------------------------------------------

    @classmethod
    def tel(cls, number):
        """A phone number, as the ``tel:`` URI a dialler acts on.

        The scheme is added if it is missing, and ``tel:`` is prefix code 5 in
        the URI table, so the record costs one byte more than the digits.
        """
        number = str(number).strip()
        if not number.startswith("tel:"):
            number = "tel:" + number
        return cls.uri(number)

    @classmethod
    def sms(cls, number, message=""):
        """A prefilled text message: opens the composer, addressed and typed.

        The body is percent-encoded, so spaces and punctuation survive.
        """
        number = str(number).strip()
        if number.startswith("sms:"):
            number = number[4:]
        uri = "sms:" + number
        if message:
            uri += "?body=" + _percent_encode(message)
        return cls.uri(uri)

    @classmethod
    def email(cls, address, subject="", body=""):
        """An email, as a ``mailto:`` URI, optionally prefilled.

        Opens the mail composer addressed and typed. Subject and body are
        percent-encoded; the address is left alone, since ``@`` and ``.`` are
        legal there and encoding them helps nobody. ``mailto:`` is prefix
        code 6, so the scheme costs a single byte.
        """
        address = str(address).strip()
        if address.startswith("mailto:"):
            address = address[7:]
        uri = "mailto:" + address
        query = []
        if subject:
            query.append("subject=" + _percent_encode(subject))
        if body:
            query.append("body=" + _percent_encode(body))
        if query:
            uri += "?" + "&".join(query)
        return cls.uri(uri)

    @classmethod
    def wifi(cls, ssid, password="", authentication=None, encryption=None,
             mac=None, network_index=1):
        """Wi-Fi credentials, in the Wi-Fi Simple Configuration format.

        Android offers to join the network straight from the tap. With a
        password and nothing else said, the network is taken to be WPA2
        Personal with AES, which is what almost every home network is; pass
        ``authentication=WIFI_OPEN`` for an open one. iOS does not act on these
        from a tag, so treat it as an Android feature.
        """
        return cls(TNF_MIME, MIME_WIFI.encode("utf-8"),
                   _wifi_payload(ssid, password, authentication, encryption,
                                 mac, network_index))

    @classmethod
    def contact(cls, name=None, phone=None, email=None, first=None, last=None,
                organization=None, title=None, url=None, address=None,
                note=None):
        """A contact card, as vCard 3.0, which both phones offer to save.

        ``name`` is split on the last space when ``first`` and ``last`` are not
        given. ``phone`` and ``email`` each take one string or several.
        """
        return cls(TNF_MIME, MIME_VCARD.encode("utf-8"),
                   _vcard_payload(name, phone, email, first, last,
                                  organization, title, url, address, note))

    @classmethod
    def bluetooth(cls, address, name=None, class_of_device=None):
        """Bluetooth BR/EDR pairing data (Secure Simple Pairing out of band).

        ``address`` is the device address the way it is normally written,
        most significant byte first; it goes on the wire reversed.
        """
        return cls(TNF_MIME, MIME_BLUETOOTH.encode("utf-8"),
                   _bluetooth_payload(address, name, class_of_device))

    @classmethod
    def bluetooth_le(cls, address, address_type=0, role=BLE_PERIPHERAL_ONLY,
                     name=None, appearance=None):
        """Bluetooth Low Energy pairing data.

        ``address_type`` is 0 for a public address and 1 for a random one.
        """
        return cls(TNF_MIME, MIME_BLUETOOTH_LE.encode("utf-8"),
                   _bluetooth_le_payload(address, address_type, role, name,
                                         appearance))

    @classmethod
    def homekit(cls, setup_payload):
        """A HomeKit setup payload, as its ``X-HM://`` URI.

        This is the string printed under the QR code on an accessory, and this
        writes it verbatim. It does **not** turn the tag into a HomeKit
        accessory: iOS pairs with a device that answers, and a tag holding a
        setup payload has nothing behind it to pair with. Useful for carrying a
        setup code that a real accessory will honour, not as a substitute for
        one. See the README.
        """
        text = str(setup_payload)
        if not text.startswith(HOMEKIT_SCHEME):
            text = HOMEKIT_SCHEME + text.lstrip("/")
        return cls.uri(text)


class NDEFMessage:
    """A list of :class:`NDEFRecord`, with shortcuts for the common case."""

    def __init__(self, records=None):
        self.records = list(records) if records else []

    def __len__(self):
        return len(self.records)

    def __iter__(self):
        return iter(self.records)

    def __getitem__(self, i):
        return self.records[i]

    @property
    def value(self):
        """Decoded value of the first record, or ``None`` if empty."""
        return self.records[0].value if self.records else None

    def first(self, kind):
        """The decoded value of the first record of ``kind``, or ``None``.

        ``kind`` is whatever :attr:`NDEFRecord.kind` reports: ``"uri"``,
        ``"text"``, ``"wifi"``, ``"contact"``, ``"bluetooth"``,
        ``"bluetooth_le"``, ``"mime"``, ``"external"`` or ``"unknown"``.
        """
        for rec in self.records:
            if rec.kind == kind:
                return rec.value
        return None

    @property
    def uri(self):
        """First URI record's value, or ``None``."""
        return self.first("uri")

    @property
    def text(self):
        """First text record's value, or ``None``."""
        return self.first("text")

    @property
    def wifi(self):
        """First Wi-Fi credential as a dict, or ``None``."""
        return self.first("wifi")

    @property
    def contact(self):
        """First contact card as a dict, or ``None``."""
        return self.first("contact")

    def __repr__(self):
        return "<NDEFMessage %r>" % (self.records,)

    def __eq__(self, other):
        return (isinstance(other, NDEFMessage)
                and len(self.records) == len(other.records)
                and all(a == b for a, b in zip(self.records, other.records)))

    def __ne__(self, other):
        return not self.__eq__(other)

    def to_bytes(self):
        """Encode the whole message."""
        out = bytearray()
        n = len(self.records)
        for i, rec in enumerate(self.records):
            out += rec.to_bytes(first=(i == 0), last=(i == n - 1))
        return bytes(out)

    @classmethod
    def from_uri(cls, uri):
        return cls([NDEFRecord.uri(uri)])

    @classmethod
    def from_text(cls, text, language="en"):
        return cls([NDEFRecord.text(text, language)])

    @classmethod
    def from_bytes(cls, data):
        """Parse a raw NDEF message (no TLV wrapper)."""
        records = []
        i = 0
        data = bytes(data)
        while i < len(data):
            flags = data[i]
            tnf = flags & 0x07
            short = bool(flags & 0x10)
            has_id = bool(flags & 0x08)
            i += 1
            if i >= len(data):
                break
            type_len = data[i]
            i += 1
            if short:
                if i >= len(data):
                    break
                payload_len = data[i]
                i += 1
            else:
                if i + 4 > len(data):
                    break
                payload_len = (data[i] << 24) | (data[i + 1] << 16) | \
                              (data[i + 2] << 8) | data[i + 3]
                i += 4
            id_len = 0
            if has_id:
                if i >= len(data):
                    break
                id_len = data[i]
                i += 1
            rtype = data[i:i + type_len]
            i += type_len
            rid = data[i:i + id_len]
            i += id_len
            payload = data[i:i + payload_len]
            i += payload_len
            if tnf != TNF_EMPTY:
                records.append(NDEFRecord(tnf, rtype, payload, rid))
            if flags & 0x40:      # ME - message end
                break
        if not records:
            raise NDEFError("no NDEF records found")
        return cls(records)


def _to_str(data):
    try:
        return str(data, "utf-8")
    except (UnicodeError, ValueError):
        return "".join(chr(c) if 32 <= c < 127 else "." for c in data)


#: Schemes worth treating as URIs that the prefix table has no code for.
_BARE_URI_SCHEMES = ("sms:", "geo:")


def _looks_like_uri(text):
    """Does this string want to be a URI record rather than a text one?

    ``://`` settles most of it. The rest is the schemes the prefix table
    already knows, so ``tel:+1234`` and ``mailto:a@b.c`` stop being written as
    prose while ``Note: buy milk`` still is.
    """
    if "://" in text:
        return True
    for prefix in _URI_PREFIXES + _BARE_URI_SCHEMES:
        if prefix.endswith(":") and len(text) > len(prefix) \
                and text.startswith(prefix):
            return True
    return False


def _as_ndef_message(message):
    """Accept an :class:`NDEFMessage`, a record, or a plain string.

    A string with a scheme becomes a URI record, anything else a text record -
    the same coercion :meth:`Type4Tag.write_ndef` has always done, factored out
    so the card emulator behaves identically.
    """
    if message is None or isinstance(message, NDEFMessage):
        return message
    if isinstance(message, str):
        return NDEFMessage.from_uri(message) if _looks_like_uri(message) \
            else NDEFMessage.from_text(message)
    if isinstance(message, NDEFRecord):
        return NDEFMessage([message])
    return NDEFMessage(list(message))


def _ndef_from_tlv(data):
    """Walk a Type 2/Type 5 TLV area and return the NDEF message inside."""
    i = 0
    data = bytes(data)
    while i < len(data):
        tag = data[i]
        if tag == 0x00:                      # NULL TLV, skip
            i += 1
            continue
        if tag == 0xFE:                      # terminator
            return None
        if i + 1 >= len(data):
            return None
        length = data[i + 1]
        i += 2
        if length == 0xFF:                   # 3-byte length form
            if i + 2 > len(data):
                return None
            length = (data[i] << 8) | data[i + 1]
            i += 2
        value = data[i:i + length]
        if tag == 0x03:                      # NDEF message TLV
            if length == 0:
                # A formatted but empty tag holds `03 00 FE`. That is a valid
                # empty message, not a malformed one.
                return None
            if len(value) < length:
                raise NDEFError(
                    "NDEF message truncated: TLV says %d bytes, read %d "
                    "(read more pages)" % (length, len(value)))
            return NDEFMessage.from_bytes(value)
        i += length
    return None


def _ndef_to_tlv(message):
    """Wrap an NDEF message in a Type 2 TLV block, with terminator."""
    payload = message.to_bytes()
    if len(payload) < 0xFF:
        head = bytes([0x03, len(payload)])
    else:
        head = bytes([0x03, 0xFF, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF])
    return head + payload + b"\xfe"




# -------------------------------------------------- Type 5 capability container

#: Capability container magic numbers. 0xE1 goes with the 4-byte form, 0xE2
#: with the 8-byte form that a tag larger than 2044 bytes needs.
CC_MAGIC_SHORT = const(0xE1)
CC_MAGIC_EXTENDED = const(0xE2)

#: Version 1.0, read and write both allowed without security. This is byte 1 of
#: the container the Adafruit 4701 ships with.
CC_VERSION_ACCESS = const(0x40)

#: Byte 3, the Type 5 feature flags. The driver treats it as opaque and carries
#: whatever the tag already had; 0x05 is what the Adafruit board ships with and
#: what is written when there is nothing to carry forward.
CC_DEFAULT_FLAGS = const(0x05)

#: Largest NDEF area a 4-byte container can describe: MLEN counts 8-byte units
#: and tops out at 0xFF.
_CC_SHORT_MAX = const(0xFF * 8)


class CapabilityContainer:
    """The Type 5 capability container at the start of user memory.

    A phone reads this first. It says where the NDEF area starts, how big it
    is, and whether the tag may be written. Getting it wrong is the usual
    reason a tag reads as "empty" or refuses a large message.
    """

    def __init__(self, magic, version_access, mlen, flags, length):
        self.magic = magic
        self.version_access = version_access
        #: NDEF area size in bytes (MLEN already multiplied by 8).
        self.capacity = mlen * 8
        self.flags = flags
        #: 4 or 8: the container's own size, and so the NDEF area's offset.
        self.length = length

    @property
    def major_version(self):
        return (self.version_access >> 6) & 0x03

    @property
    def minor_version(self):
        return (self.version_access >> 4) & 0x03

    @property
    def read_access(self):
        """0 when reading needs no security. Non-zero means restricted."""
        return (self.version_access >> 2) & 0x03

    @property
    def write_access(self):
        """0 when writing needs no security. Non-zero means restricted."""
        return self.version_access & 0x03

    @property
    def writable(self):
        return self.write_access == 0

    @property
    def extended(self):
        return self.length == 8

    def __repr__(self):
        return ("<CapabilityContainer v%d.%d %d bytes at offset %d%s>"
                % (self.major_version, self.minor_version, self.capacity,
                   self.length, "" if self.writable else " read-only"))

    def __eq__(self, other):
        return (isinstance(other, CapabilityContainer)
                and self.to_bytes() == other.to_bytes())

    def __ne__(self, other):
        return not self.__eq__(other)

    def to_bytes(self):
        """Encode the container exactly as it sits in memory."""
        mlen = self.capacity // 8
        if self.length == 8:
            return bytes([self.magic, self.version_access, 0x00, self.flags,
                          0x00, 0x00, (mlen >> 8) & 0xFF, mlen & 0xFF])
        return bytes([self.magic, self.version_access, mlen & 0xFF, self.flags])

    @classmethod
    def from_bytes(cls, data):
        """Parse a container. ``data`` must hold at least 8 bytes when the
        third one is zero, which is how the extended form announces itself."""
        data = bytes(data)
        if len(data) < 4:
            raise NDEFError("capability container truncated: %d bytes"
                            % len(data))
        magic = data[0]
        if magic not in (CC_MAGIC_SHORT, CC_MAGIC_EXTENDED):
            raise NDEFError(
                "not a formatted Type 5 tag: capability container starts "
                "%02x, expected e1 or e2. Call format() to write one." % magic)
        if data[2] == 0x00:
            if len(data) < 8:
                raise NDEFError(
                    "extended capability container needs 8 bytes, got %d"
                    % len(data))
            mlen = (data[6] << 8) | data[7]
            length = 8
        else:
            mlen = data[2]
            length = 4
        if mlen == 0:
            raise NDEFError("capability container declares a zero-byte "
                            "NDEF area")
        return cls(magic, data[1], mlen, data[3], length)

    @classmethod
    def for_capacity(cls, capacity, flags=CC_DEFAULT_FLAGS,
                     version_access=CC_VERSION_ACCESS):
        """Build the container that describes a tag of ``capacity`` bytes.

        The 4-byte form is used whenever its one-byte MLEN can still describe
        the whole tag. It cannot once the memory past the container exceeds
        0xFF * 8 = 2040 bytes, which is exactly where the 16K part lands, so
        the 16K and 64K get the 8-byte form and the 4K keeps the short one.
        """
        if capacity < 8:
            raise NDEFError("a tag of %d bytes is too small to format"
                            % capacity)
        if capacity - 4 > _CC_SHORT_MAX:
            length, magic = 8, CC_MAGIC_EXTENDED
        else:
            length, magic = 4, CC_MAGIC_SHORT
        mlen = (capacity - length) // 8
        return cls(magic, version_access, mlen, flags, length)


# ------------------------------------------------------------------- events


class Events:
    """One reading of ``IT_STS_Dyn``, decoded.

    The register is read-to-clear (section 5.2.3), so this object is the only
    record of what happened. Nothing else in the driver touches 0x2005.
    """

    def __init__(self, raw):
        self.raw = raw

    @property
    def rf_user(self):
        """A reader used Manage GPO to set the pin."""
        return bool(self.raw & IT_RF_USER)

    @property
    def rf_activity(self):
        """A reader addressed the tag."""
        return bool(self.raw & IT_RF_ACTIVITY)

    @property
    def rf_interrupt(self):
        """A reader asked for a pulse with Manage GPO."""
        return bool(self.raw & IT_RF_INTERRUPT)

    @property
    def field_falling(self):
        """The RF field went away."""
        return bool(self.raw & IT_FIELD_FALLING)

    @property
    def field_rising(self):
        """An RF field appeared: something was tapped on the tag."""
        return bool(self.raw & IT_FIELD_RISING)

    @property
    def field_change(self):
        return bool(self.raw & (IT_FIELD_RISING | IT_FIELD_FALLING))

    @property
    def rf_put_msg(self):
        """A reader wrote a mailbox message."""
        return bool(self.raw & IT_RF_PUT_MSG)

    @property
    def rf_get_msg(self):
        """A reader read a mailbox message to its end."""
        return bool(self.raw & IT_RF_GET_MSG)

    @property
    def rf_write(self):
        """A reader wrote to EEPROM: the tag's contents just changed."""
        return bool(self.raw & IT_RF_WRITE)

    def __bool__(self):
        return self.raw != 0

    # CircuitPython does not derive __ne__ from __eq__, so both are spelled out.
    def __eq__(self, other):
        return isinstance(other, Events) and self.raw == other.raw

    def __ne__(self, other):
        return not self.__eq__(other)

    @property
    def names(self):
        """The set bits as readable names, in bit order."""
        out = []
        for bit, name in ((IT_RF_USER, "rf_user"),
                          (IT_RF_ACTIVITY, "rf_activity"),
                          (IT_RF_INTERRUPT, "rf_interrupt"),
                          (IT_FIELD_FALLING, "field_falling"),
                          (IT_FIELD_RISING, "field_rising"),
                          (IT_RF_PUT_MSG, "rf_put_msg"),
                          (IT_RF_GET_MSG, "rf_get_msg"),
                          (IT_RF_WRITE, "rf_write")):
            if self.raw & bit:
                out.append(name)
        return out

    def __repr__(self):
        return "<Events %s>" % (", ".join(self.names) or "none")


class Area:
    """One of the up to four user-memory areas, in I2C byte addresses.

    ``end`` is inclusive, matching the datasheet's "last byte of area i is
    32 * ENDAi + 31".
    """

    def __init__(self, index, start, end):
        self.index = index
        self.start = start
        self.end = end

    @property
    def size(self):
        return self.end - self.start + 1

    def __contains__(self, addr):
        return self.start <= addr <= self.end

    def __repr__(self):
        return "<Area %d 0x%04x..0x%04x (%d bytes)>" % (
            self.index, self.start, self.end, self.size)

    def __eq__(self, other):
        return (isinstance(other, Area) and self.index == other.index
                and self.start == other.start and self.end == other.end)

    def __ne__(self, other):
        return not self.__eq__(other)


# ------------------------------------------------------------------ mailbox


class Mailbox:
    """The 256-byte fast transfer mode buffer, reached as ``tag.mailbox``.

    Two rules bite immediately. The buffer may only be turned on when
    ``MB_MODE`` is 1 in EEPROM, which needs the security session; and while it
    *is* on, every EEPROM write is NACKed, because writes transit this same
    buffer (section 6.4). The driver enforces both rather than letting them
    surface as unexplained NACKs.
    """

    def __init__(self, tag):
        self._tag = tag

    # -- configuration ---------------------------------------------------

    @property
    def allowed(self):
        """``MB_MODE``: may fast transfer mode be enabled at all (Table 15)."""
        return bool(self._tag._sys(REG_MB_MODE) & 0x01)

    @allowed.setter
    def allowed(self, value):
        self._tag._set_sys(REG_MB_MODE, 0x01 if value else 0x00)

    @property
    def watchdog(self):
        """``MB_WDG``, 0 to 7. 0 means the message is never auto-released;
        otherwise the timeout is ``2 ** (MB_WDG - 1) * 30 ms`` (Table 17)."""
        return self._tag._sys(REG_MB_WDG) & 0x07

    @watchdog.setter
    def watchdog(self, value):
        if not 0 <= value <= 7:
            raise ValueError("MB_WDG is a 3-bit field, 0 to 7")
        self._tag._set_sys(REG_MB_WDG, value)

    # -- runtime state ---------------------------------------------------

    @property
    def status(self):
        """Raw ``MB_CTRL_Dyn`` (Table 19)."""
        return self._tag._dyn(REG_MB_CTRL_DYN)

    @property
    def enabled(self):
        return bool(self.status & MB_EN)

    def enable(self):
        """Turn fast transfer mode on. Blocks EEPROM writes until disabled."""
        if not self.allowed:
            raise MailboxError(
                "MB_MODE is 0, so MB_EN cannot be set. Open the security "
                "session and set tag.mailbox.allowed = True first.")
        self._tag._set_dyn(REG_MB_CTRL_DYN, MB_EN)
        if not self.enabled:
            raise MailboxError("MB_EN did not stick; is VCC present?")

    def disable(self):
        """Turn fast transfer mode off, re-allowing EEPROM writes."""
        self._tag._set_dyn(REG_MB_CTRL_DYN, 0x00)

    @property
    def available(self):
        """Bytes waiting to be read, or 0 when the mailbox holds no message.

        ``MB_LEN_Dyn`` stores the length minus one (Table 21), which is why a
        naive driver reports one byte too few and an empty mailbox as one byte.
        """
        status = self.status
        if not status & (MB_HOST_PUT_MSG | MB_RF_PUT_MSG):
            return 0
        return self._tag._dyn(REG_MB_LEN_DYN) + 1

    @property
    def sender(self):
        """``"rf"``, ``"i2c"`` or ``None``: who put the current message."""
        status = self.status
        if status & MB_RF_CURRENT_MSG:
            return "rf"
        if status & MB_HOST_CURRENT_MSG:
            return "i2c"
        return None

    @property
    def missed(self):
        """Sides that failed to collect a message before the watchdog fired.

        A tuple drawn from ``"i2c"`` and ``"rf"``, empty when nothing was
        missed. The bits stay set until the mailbox is disabled.
        """
        status = self.status
        out = []
        if status & MB_HOST_MISS_MSG:
            out.append("i2c")
        if status & MB_RF_MISS_MSG:
            out.append("rf")
        return tuple(out)

    # -- traffic ---------------------------------------------------------

    def put(self, data):
        """Write a message for the RF side to collect.

        Writes always start at the first mailbox byte; the chip refuses any
        other starting address (section 6.4.1). The buffer is volatile, so
        there is no program time to wait out.
        """
        data = bytes(data)
        if not 1 <= len(data) <= MAILBOX_SIZE:
            raise MailboxError("a mailbox message is 1 to %d bytes, got %d"
                               % (MAILBOX_SIZE, len(data)))
        if not self.enabled:
            raise MailboxError("fast transfer mode is off; call enable()")
        self._tag._write_raw(self._tag.address, REG_MAILBOX, data)

    def get(self, length=None):
        """Read the waiting message, releasing the mailbox.

        Returns ``b""`` when nothing is waiting. Reading fewer bytes than the
        message holds leaves it in place, which is what ``length`` is for.
        """
        if length is None:
            length = self.available
        if not length:
            return b""
        if not self.enabled:
            raise MailboxError("fast transfer mode is off; call enable()")
        if length > MAILBOX_SIZE:
            raise MailboxError("the mailbox holds at most %d bytes"
                               % MAILBOX_SIZE)
        return self._tag._read_raw(self._tag.address, REG_MAILBOX, length)

    def __len__(self):
        return self.available

    def __repr__(self):
        try:
            return "<Mailbox %s, %d bytes from %s>" % (
                "on" if self.enabled else "off", self.available,
                self.sender or "nobody")
        except ST25DVError as err:
            return "<Mailbox unreadable: %s>" % err


# ----------------------------------------------------------------- the tag


class ST25DV:
    """An ST25DV04K, ST25DV16K or ST25DV64K on an I2C bus.

    ``i2c`` is a ``busio.I2C`` (or anything with the same four methods). The
    two device addresses are separate arguments because they are two device
    select codes for one chip, not two chips: 0x53 for user memory, the dynamic
    registers and the mailbox, 0x57 for the system area and the password.

    ``gpo`` is optional and takes a pin or a ready-made ``DigitalInOut``. The
    driver never needs it: field presence, RF writes and mailbox traffic are
    all readable over I2C alone.
    """

    def __init__(self, i2c, address=ADDRESS_USER,
                 system_address=ADDRESS_SYSTEM, gpo=None, debug=False,
                 busy_timeout=DEFAULT_BUSY_TIMEOUT, probe=True):
        self._i2c = i2c
        self.address = address
        self.system_address = system_address
        self.debug = debug
        #: How long a NACKed transfer keeps retrying before raising BusyError.
        #: It also bounds the wait for the bus lock on a shared bus.
        self.busy_timeout = busy_timeout
        self._bus = _Bus(i2c, lambda: self.busy_timeout)
        #: Check that fast transfer mode is off before each EEPROM write.
        #: Turning this off saves one register read per write and costs you the
        #: clear error message when the mailbox is on.
        self.check_fast_transfer = True
        self.mailbox = Mailbox(self)
        self.gpo_pin = None
        self._owns_gpo = False
        self._owns_i2c = False
        self._session_password = None
        self._identity = None
        self._areas = None
        if gpo is not None:
            self._setup_gpo(gpo)
        if probe:
            self.refresh()

    @classmethod
    def from_board(cls, board, **kwargs):
        """Build one on the board's shared STEMMA QT bus, or its default I2C.

        The shared bus runs at whatever frequency the board set up, usually
        100 kHz. The ST25DV is good for 1 MHz (Table 249), so for anything
        bulk-transfer heavy build a ``busio.I2C`` at 400 kHz yourself.
        """
        for name in ("STEMMA_I2C", "I2C"):
            maker = getattr(board, name, None)
            if maker is not None:
                return cls(maker(), **kwargs)
        raise NotFoundError("this board exposes neither STEMMA_I2C nor I2C")

    @classmethod
    def from_pins(cls, scl, sda, frequency=400000, **kwargs):
        """Build one on a dedicated bus at 400 kHz.

        The bus is created here, so it belongs to the object that comes back:
        :meth:`deinit` releases it along with the GPO pin. A bus passed to the
        constructor directly is the caller's and is left alone.
        """
        import busio
        tag = cls(busio.I2C(scl, sda, frequency=frequency), **kwargs)
        tag._owns_i2c = True
        return tag

    def _setup_gpo(self, gpo):
        from digitalio import DigitalInOut, Pull
        if isinstance(gpo, DigitalInOut):
            self.gpo_pin = gpo
        else:
            self.gpo_pin = DigitalInOut(gpo)
            self._owns_gpo = True
            # The -IE part drives GPO open drain, so it needs a pull-up. The
            # -JF part is push-pull, where the pull-up is harmless.
            self.gpo_pin.switch_to_input(Pull.UP)

    def deinit(self):
        """Release whatever this object created: the GPO pin, and the I2C bus
        if :meth:`from_pins` built it. Safe to call twice."""
        if self._owns_gpo and self.gpo_pin is not None:
            self.gpo_pin.deinit()
            self.gpo_pin = None
            self._owns_gpo = False
        if self._owns_i2c:
            self._i2c.deinit()
            self._owns_i2c = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.deinit()
        return False

    def _log(self, arrow, dev, addr, data):
        print("%s %02x @%04x %s" % (arrow, dev, addr, hexlify(data, " ")))

    # ---------------------------------------------------------- bus layer

    def _read_raw(self, dev, addr, length, timeout=None):
        """Random address read: address bytes, repeated start, then data.

        Retried through NACKs, because a NACK here usually means the RF side
        got in first (section 5.5) and will be gone in a moment.
        """
        if length <= 0:
            return b""
        out = bytes([(addr >> 8) & 0xFF, addr & 0xFF])
        buf = bytearray(length)
        deadline = _deadline(self.busy_timeout if timeout is None else timeout)
        delay = 0.0002
        tries = 0
        while True:
            tries += 1
            try:
                with self._bus:
                    self._i2c.writeto_then_readfrom(dev, out, buf)
                break
            except OSError as err:
                if _expired(deadline):
                    raise BusyError(
                        "no ACK reading %d bytes from 0x%04x on 0x%02x after "
                        "%d tries (%s). An RF reader may be holding the tag."
                        % (length, addr, dev, tries, err))
                sleep(delay)
                delay = min(delay * 2, 0.004)
        data = bytes(buf)
        if self.debug:
            self._log("<-", dev, addr, data)
        return data

    def _write_raw(self, dev, addr, data, timeout=None):
        """One write transaction, retried through NACKs. No program-time wait:
        callers that touch EEPROM use :meth:`_write_eeprom` instead."""
        payload = bytes([(addr >> 8) & 0xFF, addr & 0xFF]) + bytes(data)
        if self.debug:
            self._log("->", dev, addr, bytes(data))
        deadline = _deadline(self.busy_timeout if timeout is None else timeout)
        delay = 0.0002
        tries = 0
        while True:
            tries += 1
            try:
                with self._bus:
                    self._i2c.writeto(dev, payload)
                return tries
            except OSError as err:
                if _expired(deadline):
                    self._diagnose_write_failure(dev, addr, len(data), tries,
                                                 err)
                sleep(delay)
                delay = min(delay * 2, 0.004)

    def _diagnose_write_failure(self, dev, addr, length, tries, err):
        """Turn an exhausted write retry into the error that explains it.

        A NACK on a write means one of several very different things: the RF
        side is busy, the mailbox is on, the security session is closed, or the
        bytes are protected. All four look identical on the wire, so ask the
        chip which it was before giving up.
        """
        detail = ("no ACK writing %d bytes to 0x%04x on 0x%02x after %d tries "
                  "(%s)" % (length, addr, dev, tries, err))
        try:
            if self._dyn(REG_MB_CTRL_DYN, timeout=0.05) & MB_EN:
                raise MailboxError(
                    detail + ". Fast transfer mode is enabled and EEPROM "
                    "writes transit the mailbox buffer, so they are refused "
                    "(section 6.4). Call tag.mailbox.disable().")
            if dev == self.system_address and not self._sso(timeout=0.05):
                raise SessionRequired(
                    detail + ". The I2C security session is closed, and every "
                    "system register needs it open (Table 11).")
        except BusyError:
            raise BusyError(detail + ". The chip is not answering at all; an "
                            "RF reader may be holding it.")
        raise ProtectedError(
            detail + ". The session is open and the mailbox is off, so the "
            "bytes are write protected, read only, or the write crossed an "
            "area boundary.")

    def _wait_ready(self, dev, timeout):
        """Block until the internal write cycle finishes.

        ACK polling (section 6.4.3): the chip disconnects from the bus for tW
        per page and answers nothing at all, so poke it until it answers. The
        poke is a one-byte read of a read-only register rather than the
        zero-length write the datasheet describes, because not every I2C port
        will send a zero-length transfer and this one is guaranteed to work.
        """
        probe = REG_IC_REF if dev == self.system_address else REG_I2C_SSO_DYN
        out = bytes([(probe >> 8) & 0xFF, probe & 0xFF])
        buf = bytearray(1)
        deadline = _deadline(timeout)
        while True:
            try:
                with self._bus:
                    self._i2c.writeto_then_readfrom(dev, out, buf)
                return
            except OSError:
                if _expired(deadline):
                    raise BusyError(
                        "0x%02x did not finish its write cycle within %d ms"
                        % (dev, int(timeout * 1000)))
                sleep(0.0005)

    def _write_eeprom(self, dev, addr, data):
        """Write one already-chunked run of bytes to EEPROM and wait it out.

        Program time is tW per 4-byte page touched, partial pages included
        (section 6.4.2), so it is charged from the addresses, not the length.
        """
        pages = ((addr + len(data) - 1) // PAGE_SIZE) - (addr // PAGE_SIZE) + 1
        self._write_raw(dev, addr, data)
        self._wait_ready(dev, pages * PAGE_WRITE_TIME + 0.02)

    # ----------------------------------------------------- register access

    def _sys(self, reg, timeout=None):
        """One byte from the system configuration area (device select E2=1)."""
        return self._read_raw(self.system_address, reg, 1, timeout)[0]

    def _dyn(self, reg, timeout=None):
        """One byte from the dynamic registers (device select E2=0)."""
        return self._read_raw(self.address, reg, 1, timeout)[0]

    def _set_dyn(self, reg, value):
        """Write one dynamic register. Volatile, so no program time."""
        self._write_raw(self.address, reg, bytes([value & 0xFF]))

    def _set_sys(self, reg, value):
        """Write one system register, which needs the session open.

        Checked up front so the failure is named rather than being a NACK that
        looks exactly like RF contention.
        """
        if not self._sso():
            raise SessionRequired(
                "writing system register 0x%04x needs the I2C security "
                "session open; call open_session()" % reg)
        self._ensure_fast_transfer_off()
        self._write_eeprom(self.system_address, reg, bytes([value & 0xFF]))

    def _sso(self, timeout=None):
        """``I2C_SSO_Dyn``: is the security session open (Table 68)."""
        return bool(self._dyn(REG_I2C_SSO_DYN, timeout) & 0x01)

    def _ensure_fast_transfer_off(self):
        if not self.check_fast_transfer:
            return
        if self._dyn(REG_MB_CTRL_DYN) & MB_EN:
            raise MailboxError(
                "fast transfer mode is enabled. EEPROM writes transit the "
                "256-byte mailbox buffer, so the chip NACKs them and writes "
                "nothing (section 6.4). Call tag.mailbox.disable() first.")

    # ---------------------------------------------------------- identity

    def refresh(self):
        """Re-read the identity block. Called once at construction."""
        # 0x0014 to 0x0020 inclusive: MEM_SIZE, BLK_SIZE, IC_REF, UID, IC_REV.
        # System-area reads run in continuity, so this is one transaction.
        try:
            blob = self._read_raw(self.system_address, REG_MEM_SIZE, 13)
        except BusyError as err:
            raise NotFoundError(
                "no ST25DV answered on 0x%02x: %s" % (self.system_address, err))
        blocks = ((blob[1] << 8) | blob[0]) + 1
        block_size = blob[2] + 1
        capacity = blocks * block_size
        if blob[2] == 0xFF or capacity == 0 or capacity > 0x10000:
            raise NotFoundError(
                "0x%02x answered but MEM_SIZE/BLK_SIZE read back as %s, which "
                "is not an ST25DV" % (self.system_address, hexlify(blob[:3])))
        uid = bytearray(8)
        for i in range(8):                      # stored LSB first at 0x0018
            uid[7 - i] = blob[4 + i]
        self._areas = None
        self._identity = {
            "blocks": blocks,
            "block_size": block_size,
            "capacity": capacity,
            "ic_ref": blob[3],
            "uid": bytes(uid),
            "ic_revision": blob[12],
        }
        return self

    def _id(self, key):
        if self._identity is None:
            self.refresh()
        return self._identity[key]

    @property
    def memory_size(self):
        """User memory in bytes: ``(MEM_SIZE + 1) * (BLK_SIZE + 1)``.

        Read from the part, because IC_REF cannot tell a 16K from a 64K: both
        report 0x26 (Table 83).
        """
        return self._id("capacity")

    @property
    def block_count(self):
        """RF blocks of user memory. ``MEM_SIZE`` holds this minus one."""
        return self._id("blocks")

    @property
    def block_size(self):
        """Bytes per RF block, always 4 on shipping parts."""
        return self._id("block_size")

    @property
    def ic_ref(self):
        """``IC_REF``: 0x24 on the 4K, 0x26 on both the 16K and the 64K."""
        return self._id("ic_ref")

    @property
    def ic_revision(self):
        return self._id("ic_revision")

    @property
    def uid(self):
        """The 8-byte ISO/IEC 15693 UID, most significant byte first.

        It reads back least significant byte first from 0x0018; this is the
        order a reader prints, starting 0xE0 0x02 for an ST tag.
        """
        return self._id("uid")

    @property
    def uid_hex(self):
        return hexlify(self.uid)

    @property
    def part(self):
        """The part number, e.g. ``"ST25DV16K-IE"``.

        Capacity comes from MEM_SIZE and the package variant from UID byte 5,
        which is 0x24/0x26 for the -IE parts and 0x25/0x27 for the -JF
        (Table 85). Neither alone is enough. Byte 5 counting from the least
        significant end is index 2 of the most-significant-first UID.
        """
        kbit = self.memory_size * 8 // 1024
        variant = "JF" if self.uid[2] in (0x25, 0x27) else "IE"
        return "ST25DV%02dK-%s" % (kbit, variant)

    def __repr__(self):
        # Deliberately does not read IT_STS_Dyn: that register is read-to-clear
        # and printing an object must not eat the caller's events.
        try:
            return "<ST25DV %s %d bytes uid %s>" % (
                self.part, self.memory_size, self.uid_hex)
        except ST25DVError as err:
            return "<ST25DV unreadable: %s>" % err

    # ------------------------------------------------------- user memory

    def _check_range(self, addr, length):
        size = self.memory_size
        if addr < 0 or length < 0:
            raise ValueError("negative address or length")
        if addr + length > size:
            raise ValueError(
                "0x%04x + %d runs past the end of %d bytes of user memory. "
                "There is no rollover: the chip would return 0xff forever "
                "(section 6.5.3)." % (addr, length, size))

    def _area_end(self, addr):
        """The last byte address of the area containing ``addr``."""
        for area in self._cached_areas():
            if addr <= area.end:
                return area.end
        return self.memory_size - 1

    def read(self, addr, length):
        """Read user memory, in as few transactions as the chip allows.

        Split at area boundaries, because a sequential read that crosses one
        returns 0xFF from there on rather than the next area's contents
        (section 6.5.3).
        """
        self._check_range(addr, length)
        out = bytearray(length)
        done = 0
        while done < length:
            here = addr + done
            span = min(length - done, MAX_READ,
                       self._area_end(here) - here + 1)
            chunk = self._read_raw(self.address, here, span)
            out[done:done + span] = chunk
            done += span
        return bytes(out)

    def write(self, addr, data):
        """Write user memory, waiting out each page's program time.

        Split three ways: at 256 bytes, the largest sequential write the chip
        takes; at area boundaries, which a sequential write may not cross
        (section 6.4.2); and never at page boundaries, which cost time but are
        not a limit.
        """
        data = bytes(data)
        self._check_range(addr, len(data))
        if not data:
            return 0
        self._ensure_fast_transfer_off()
        done = 0
        while done < len(data):
            here = addr + done
            span = min(len(data) - done, MAX_WRITE,
                       self._area_end(here) - here + 1)
            self._write_eeprom(self.address, here, data[done:done + span])
            done += span
        return done

    def __len__(self):
        return self.memory_size

    def _slice_bounds(self, key):
        """``slice`` to ``(start, stop)``, clamped to memory.

        Spelled out rather than calling ``slice.indices``, which CircuitPython
        does not always provide.
        """
        if key.step is not None and key.step != 1:
            raise ValueError("stepped slices are not supported")
        size = self.memory_size
        start = 0 if key.start is None else key.start
        stop = size if key.stop is None else key.stop
        if start < 0:
            start += size
        if stop < 0:
            stop += size
        start = min(max(start, 0), size)
        stop = min(max(stop, 0), size)
        return start, max(stop, start)

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop = self._slice_bounds(key)
            return self.read(start, stop - start)
        if key < 0:
            key += self.memory_size
        return self.read(key, 1)[0]

    def __setitem__(self, key, value):
        if isinstance(key, slice):
            start, stop = self._slice_bounds(key)
            data = bytes(value)
            if len(data) != stop - start:
                raise ValueError(
                    "slice is %d bytes but %d were given; this is fixed-size "
                    "memory, not a list" % (stop - start, len(data)))
            self.write(start, data)
            return
        if key < 0:
            key += self.memory_size
        if isinstance(value, int):
            value = bytes([value])
        self.write(key, bytes(value))

    def dump(self, addr=0, length=None, width=16):
        """A hex dump of user memory as a list of lines. Handy in the REPL."""
        if length is None:
            length = self.memory_size - addr
        data = self.read(addr, length)
        lines = []
        for off in range(0, len(data), width):
            row = data[off:off + width]
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            cells = hexlify(row, " ")
            pad = " " * max(0, width * 3 - 1 - len(cells))
            lines.append("%04x  %s%s  %s" % (addr + off, cells, pad, text))
        return lines

    # -------------------------------------------------------------- areas

    @property
    def max_area_limit(self):
        """The ENDAi value that means "end of memory": 0x0F, 0x3F or 0xFF."""
        return self.memory_size // AREA_GRANULARITY - 1

    def _area_limits(self):
        """``(ENDA1, ENDA2, ENDA3)``, read in one transaction.

        They sit at 0x0005, 0x0007 and 0x0009 with the RFAiSS registers
        interleaved, so five bytes cover all three.
        """
        blob = self._read_raw(self.system_address, REG_ENDA1, 5)
        return (blob[0], blob[2], blob[4])

    def _cached_areas(self):
        """The area map, read once and kept.

        Every read and write consults it to find the next boundary, so it must
        not cost a transaction each time. Only the security session can change
        the layout, and this driver clears the cache when it does; call
        :meth:`refresh` if something else did.
        """
        if self._areas is None:
            self._areas = self.read_areas()
        return self._areas

    @property
    def areas(self):
        """The user memory areas as ``Area`` objects, in address order.

        Factory setting is one area covering everything. Splitting is what the
        RFAiSS and I2CSS protection registers act on, one setting per area.
        """
        return self._cached_areas()

    def read_areas(self):
        """Re-read the ENDAi registers and rebuild the area map."""
        limits = self._area_limits()
        last = self.memory_size - 1
        out = []
        start = 0
        for index in range(3):
            end = AREA_GRANULARITY * limits[index] + AREA_GRANULARITY - 1
            if end >= last:
                break
            out.append(Area(index + 1, start, end))
            start = end + 1
        out.append(Area(len(out) + 1, start, last))
        self._areas = tuple(out)
        return self._areas

    @areas.setter
    def areas(self, sizes):
        self.set_areas(sizes)

    def set_areas(self, sizes):
        """Split user memory into one to four areas of the given byte sizes.

        Sizes must be multiples of 32 and add up to the whole memory, because
        the areas are contiguous and the last one always ends at the last byte.
        The security session must be open.

        The ordering rule the chip enforces is handled here: an ENDAi can only
        be lowered once its successor already sits at end of memory, so the
        writes go out as ENDA3 and ENDA2 raised first, then ENDA1, ENDA2 and
        ENDA3 set ascending (section 4.2). Getting this order wrong returns a
        NACK and writes nothing.
        """
        sizes = list(sizes)
        if not 1 <= len(sizes) <= 4:
            raise AreaError("there are 1 to 4 areas, not %d" % len(sizes))
        total = self.memory_size
        for size in sizes:
            if size < AREA_GRANULARITY or size % AREA_GRANULARITY:
                raise AreaError(
                    "each area is a multiple of %d bytes and at least that "
                    "big; got %d" % (AREA_GRANULARITY, size))
        if sum(sizes) != total:
            raise AreaError(
                "the areas must fill user memory exactly: %d given, %d needed"
                % (sum(sizes), total))
        top = self.max_area_limit
        limits = []
        running = 0
        for size in sizes[:-1]:
            running += size
            limits.append(running // AREA_GRANULARITY - 1)
        while len(limits) < 3:
            limits.append(top)
        self.set_area_limits(limits[0], limits[1], limits[2])

    def set_area_limits(self, enda1, enda2, enda3):
        """Set the three ENDAi registers directly, in the order the chip wants.

        Each is a count of 32-byte units: the last byte of area *i* is
        ``32 * ENDAi + 31``.
        """
        top = self.max_area_limit
        for name, value in (("ENDA1", enda1), ("ENDA2", enda2),
                            ("ENDA3", enda3)):
            if not 0 <= value <= top:
                raise AreaError("%s is 0 to 0x%02x on this part, got 0x%02x"
                                % (name, top, value))
        if not enda1 <= enda2 <= enda3:
            raise AreaError("area limits must not decrease: %02x %02x %02x"
                            % (enda1, enda2, enda3))
        if enda2 < top and enda1 >= enda2:
            raise AreaError("area 2 exists (ENDA2 < end of memory) so ENDA1 "
                            "must be strictly below ENDA2")
        if enda3 < top and enda2 >= enda3:
            raise AreaError("area 3 exists (ENDA3 < end of memory) so ENDA2 "
                            "must be strictly below ENDA3")
        if not self._sso():
            raise SessionRequired(
                "the ENDAi registers are system registers; call open_session()")
        current = self._area_limits()
        # Raise the successors to end of memory first, highest first, skipping
        # any that is already there. Writing ENDA3 while ENDA2 already equals
        # it is exactly the case the datasheet calls out as an error.
        if current[2] != top:
            self._set_sys(REG_ENDA3, top)
        if current[1] != top:
            self._set_sys(REG_ENDA2, top)
        current = self._area_limits()
        if current[0] != enda1:
            self._set_sys(REG_ENDA1, enda1)
        if enda2 != top:
            self._set_sys(REG_ENDA2, enda2)
        if enda3 != top:
            self._set_sys(REG_ENDA3, enda3)
        self._areas = None
        return self.areas

    # --------------------------------------------------------------- NDEF

    @property
    def capability_container(self):
        """The Type 5 capability container, read fresh from the tag.

        Not cached: a reader can rewrite it, and a stale copy is how a driver
        ends up writing a message past the end of the declared area.
        """
        return CapabilityContainer.from_bytes(self.read(0, min(8, len(self))))

    def format(self, capacity=None, erase=True, flags=None,
               version_access=CC_VERSION_ACCESS):
        """Write a capability container that matches the actual tag.

        Worth doing on a board that arrives pre-formatted: the Adafruit 4701
        ships declaring 512 bytes, so on a 16K part a phone refuses to write
        past a quarter of the memory.

        ``erase=True`` also writes an empty NDEF message, which is what makes a
        phone report the tag as formatted and blank. Pass ``erase=False`` to
        keep the message the tag already holds: it is read first and written
        back afterwards, because correcting a 4-byte container to an 8-byte one
        moves the NDEF area and would otherwise leave the message stranded four
        bytes short of where a reader looks for it.
        """
        if capacity is None:
            capacity = self.memory_size
        old = None
        if flags is None or not erase:
            try:
                old = self.capability_container
            except (NDEFError, ST25DVError):
                old = None
        if flags is None:
            flags = CC_DEFAULT_FLAGS if old is None else old.flags
        cc = CapabilityContainer.for_capacity(capacity, flags, version_access)
        carried = None
        if not erase and old is not None and old.length != cc.length:
            try:
                carried = self.read_ndef()
            except (NDEFError, ST25DVError):
                carried = None
        payload = cc.to_bytes()
        if erase:
            payload += b"\x03\x00\xfe"        # a valid, empty NDEF message
        self.write(0, payload)
        if carried is not None:
            self.write_ndef(carried)
        return cc

    def read_ndef(self):
        """The NDEF message on the tag, or ``None`` if there is none.

        Walks the TLV chain from the end of the capability container, skipping
        proprietary (0xFD) and null (0x00) entries by length, and handles both
        the one-byte and the 0xFF-plus-two-byte length forms.
        """
        cc = self.capability_container
        limit = min(cc.length + cc.capacity, self.memory_size)
        pos = cc.length
        while pos < limit:
            head = self.read(pos, min(4, limit - pos))
            kind = head[0]
            if kind == 0x00:                       # null TLV, one byte
                pos += 1
                continue
            if kind == 0xFE:                       # terminator
                return None
            if len(head) < 2:
                raise NDEFError("TLV at 0x%04x runs off the end of the "
                                "declared NDEF area" % pos)
            if head[1] == 0xFF:
                if len(head) < 4:
                    raise NDEFError("three-byte TLV length at 0x%04x is cut "
                                    "off by the end of the area" % pos)
                length = (head[2] << 8) | head[3]
                value = pos + 4
            else:
                length = head[1]
                value = pos + 2
            if kind == 0x03:                       # the NDEF message
                if length == 0:
                    return None                    # formatted but empty
                if value + length > limit:
                    raise NDEFError(
                        "the NDEF TLV claims %d bytes at 0x%04x but the "
                        "declared area ends at 0x%04x. The capability "
                        "container is wrong; call format()."
                        % (length, value, limit))
                return NDEFMessage.from_bytes(self.read(value, length))
            pos = value + length                   # 0xFD and anything else
        return None

    def write_ndef(self, message):
        """Write an NDEF message, a record, or a string.

        A string containing ``://`` becomes a URI record, anything else a text
        record. The message is bounded by the capacity the capability container
        declares, so a tag that a reader will not read past is refused here
        rather than half written.
        """
        msg = _as_ndef_message(message)
        cc = self.capability_container
        if msg is None:
            payload = b"\x03\x00\xfe"
        else:
            payload = _ndef_to_tlv(msg)
        if len(payload) > cc.capacity:
            raise NDEFError(
                "%d bytes of NDEF do not fit the %d the capability container "
                "declares. The tag holds %d bytes; call format() if the "
                "container under-declares it."
                % (len(payload), cc.capacity, self.memory_size))
        if cc.length + len(payload) > self.memory_size:
            raise NDEFError("the declared NDEF area runs past the end of "
                            "memory; call format()")
        self.write(cc.length, payload)
        return len(payload)

    @property
    def ndef(self):
        """The tag's NDEF message, or ``None``.

        Assigning a string, a record or a message writes it::

            tag.ndef = "https://thefilip.com"
        """
        return self.read_ndef()

    @ndef.setter
    def ndef(self, message):
        self.write_ndef(message)

    # ---------------------------------------------------------- security

    @property
    def session_open(self):
        """``I2C_SSO_Dyn``: is the I2C security session open."""
        return self._sso()

    def open_session(self, password=b"\x00\x00\x00\x00\x00\x00\x00\x00"):
        """Present the I2C password, opening the session.

        The command is 17 bytes written to 0x0900 on the system address: the
        password, the validation byte 0x09, then the password again, sent twice
        so a corrupted transfer cannot open the session by accident
        (section 6.6.1). Factory password is eight zero bytes.
        """
        pwd = self._password_bytes(password)
        self._password_command(pwd, _PWD_PRESENT)
        self._session_password = pwd
        if not self._sso():
            raise SessionRequired(
                "the I2C password was rejected; the session is still closed")
        return True

    def close_session(self):
        """Close the session by presenting a password that cannot be right.

        There is no close command, so a wrong password goes in instead. The
        complement of whatever was last presented differs from it in every bit,
        so it always fails.

        When nothing was presented through this object there is nothing to
        complement, and the assumed factory password's complement, eight 0xFF
        bytes, might be the real one - which would *open* the session rather
        than close it. So a second, complementary guess follows. Two values
        that differ in every bit cannot both be the password, so one of the two
        always fails and the session always ends closed.

        The I2C password has no retry counter, unlike the RF ones, so a
        deliberately wrong presentation costs nothing.
        """
        pwd = self._session_password or b"\x00" * 8
        self._session_password = None
        # The second guess is only ever reached when the first one opened the
        # session, which means the first was the password and this one is not.
        for guess in (bytes([b ^ 0xFF for b in pwd]), bytes(pwd)):
            self._password_command(guess, _PWD_PRESENT)
            if not self._sso():
                return True
        raise ST25DVError(
            "the security session is still open after two complementary "
            "passwords, which cannot both have been right")

    def change_password(self, new_password):
        """Set a new I2C password. The session must already be open.

        Same 17-byte shape as presenting one, with validation byte 0x07
        (section 6.6.2). The new password takes effect immediately, so the
        session opened with the old one is no longer backed by a valid
        password; this re-presents the new one so the caller keeps their
        session.
        """
        pwd = self._password_bytes(new_password)
        if not self._sso():
            raise SessionRequired(
                "changing the I2C password needs the session open; present "
                "the current password with open_session() first")
        self._password_command(pwd, _PWD_WRITE)
        self._session_password = pwd
        if not self._sso():
            self.open_session(pwd)
        return True

    @staticmethod
    def _password_bytes(password):
        pwd = bytes(password)
        if len(pwd) != 8:
            raise ValueError("the I2C password is 8 bytes, got %d" % len(pwd))
        return pwd

    def _password_command(self, pwd, validation):
        """Send one 17-byte password command to 0x0900 on the system address."""
        self._ensure_fast_transfer_off()
        payload = pwd + bytes([validation]) + pwd
        self._write_raw(self.system_address, REG_I2C_PWD, payload)
        # Presenting only compares, but writing programs EEPROM, and both end
        # with a stop condition the chip may take tW to come back from.
        self._wait_ready(self.system_address, 2 * PAGE_WRITE_TIME + 0.02)

    # ----------------------------------------------------- configuration

    @property
    def gpo(self):
        """The static ``GPO`` register: which events pulse the pin (Table 26).

        Or it with the ``GPO_*`` constants. Writing needs the session. The
        constructor's ``gpo`` argument is the pin, and lives on as
        :attr:`gpo_pin`; this is the event mask.
        """
        return self._sys(REG_GPO)

    @gpo.setter
    def gpo(self, value):
        self._set_sys(REG_GPO, value)

    @property
    def gpo_enabled(self):
        """Bit 7 of ``GPO_CTRL_Dyn``: is the pin actually driving.

        This is the bit that matters. Events are reported in the static GPO
        register's mask, but nothing reaches the pin unless this is set, and it
        is the one GPO bit writable over I2C without the session (Table 30).
        """
        return bool(self._dyn(REG_GPO_CTRL_DYN) & GPO_ENABLE)

    @gpo_enabled.setter
    def gpo_enabled(self, value):
        current = self._dyn(REG_GPO_CTRL_DYN)
        if value:
            current |= GPO_ENABLE
        else:
            current &= ~GPO_ENABLE & 0xFF
        self._set_dyn(REG_GPO_CTRL_DYN, current)

    @property
    def gpo_value(self):
        """The GPO pin's level, or ``None`` when no pin was given."""
        if self.gpo_pin is None:
            return None
        return self.gpo_pin.value

    @property
    def interrupt_pulse_us(self):
        """GPO pulse width in microseconds: ``301 - IT_TIME * 37.65``.

        Ranges from 301 us at IT_TIME 0 down to about 37 us at 7 (Table 28).
        """
        return 301.0 - (self._sys(REG_IT_TIME) & 0x07) * 37.65

    @interrupt_pulse_us.setter
    def interrupt_pulse_us(self, microseconds):
        raw = int(round((301.0 - microseconds) / 37.65))
        if raw < 0 or raw > 7:
            raise ValueError(
                "the pulse is 37 to 301 us in eight steps; %.1f us is outside "
                "that" % microseconds)
        self._set_sys(REG_IT_TIME, raw)

    @property
    def energy_harvesting(self):
        """``EH_CTRL_Dyn`` bit 0: is harvesting on right now.

        Writable without the session, and forgotten at power off. For the
        power-on default see :attr:`energy_harvesting_after_boot`.
        """
        return bool(self._dyn(REG_EH_CTRL_DYN) & EH_EN)

    @energy_harvesting.setter
    def energy_harvesting(self, value):
        self._set_dyn(REG_EH_CTRL_DYN, EH_EN if value else 0x00)

    @property
    def energy_harvesting_active(self):
        """``EH_ON``: harvesting is actually being delivered."""
        return bool(self._dyn(REG_EH_CTRL_DYN) & EH_ON)

    @property
    def energy_harvesting_after_boot(self):
        """``EH_MODE`` read the way round people expect.

        True means harvesting comes up enabled after power on. The register
        itself is inverted: EH_MODE 0 is "forced after boot", 1 is "on demand"
        (Table 35), and 1 is the factory value.
        """
        return not self._sys(REG_EH_MODE) & 0x01

    @energy_harvesting_after_boot.setter
    def energy_harvesting_after_boot(self, value):
        self._set_sys(REG_EH_MODE, 0x00 if value else 0x01)

    @property
    def rf_disabled(self):
        """``RF_MNGT_Dyn`` bit 0: RF commands are answered with error 0x0F."""
        return bool(self._dyn(REG_RF_MNGT_DYN) & RF_DISABLE)

    @rf_disabled.setter
    def rf_disabled(self, value):
        self._update_rf_dyn(RF_DISABLE, value)

    @property
    def rf_sleep(self):
        """``RF_MNGT_Dyn`` bit 1: the tag stays silent to readers entirely."""
        return bool(self._dyn(REG_RF_MNGT_DYN) & RF_SLEEP)

    @rf_sleep.setter
    def rf_sleep(self, value):
        self._update_rf_dyn(RF_SLEEP, value)

    def _update_rf_dyn(self, bit, value):
        current = self._dyn(REG_RF_MNGT_DYN)
        if value:
            current |= bit
        else:
            current &= ~bit & 0xFF
        self._set_dyn(REG_RF_MNGT_DYN, current)

    @property
    def rf_management(self):
        """The static ``RF_MNGT`` byte, copied into the dynamic one at boot."""
        return self._sys(REG_RF_MNGT)

    @rf_management.setter
    def rf_management(self, value):
        self._set_sys(REG_RF_MNGT, value)

    @property
    def i2c_protection(self):
        """The raw ``I2CSS`` byte: two bits of I2C protection per area."""
        return self._sys(REG_I2CSS)

    @i2c_protection.setter
    def i2c_protection(self, value):
        self._set_sys(REG_I2CSS, value)

    @property
    def rf_protection(self):
        """The four ``RFAiSS`` bytes, area 1 first."""
        blob = self._read_raw(self.system_address, REG_RFA1SS, 7)
        return (blob[0], blob[2], blob[4], blob[6])

    @property
    def lock_cfg(self):
        """``LOCK_CFG``: is RF write access to system registers blocked."""
        return bool(self._sys(REG_LOCK_CFG) & 0x01)

    @lock_cfg.setter
    def lock_cfg(self, value):
        self._set_sys(REG_LOCK_CFG, 0x01 if value else 0x00)

    @property
    def lock_ccfile(self):
        """``LOCK_CCFILE``: RF write protection of blocks 0 and 1.

        Bit 0 locks block 0, bit 1 locks block 1. Both are one-way on the RF
        side; over I2C the register still reads and writes normally.
        """
        return self._sys(REG_LOCK_CCFILE) & 0x03

    @lock_ccfile.setter
    def lock_ccfile(self, value):
        self._set_sys(REG_LOCK_CCFILE, value & 0x03)

    @property
    def afi(self):
        """Application family identifier, read only over I2C (Table 77)."""
        return self._sys(REG_AFI)

    @property
    def dsfid(self):
        """Data storage format identifier, read only over I2C (Table 75)."""
        return self._sys(REG_DSFID)

    def system_dump(self):
        """The whole readable system area, 0x0000 to 0x0020, as bytes.

        Take one of these before changing anything: it is the only record of
        the factory register values, and there is no reset command.
        """
        return self._read_raw(self.system_address, 0x0000, 0x21)

    # ------------------------------------------------------- live RF state

    @property
    def field_present(self):
        """``FIELD_ON``: a reader's field is on the tag right now."""
        return bool(self._dyn(REG_EH_CTRL_DYN) & EH_FIELD_ON)

    @property
    def vcc_present(self):
        """``VCC_ON``: the tag sees its own supply and is not in low power."""
        return bool(self._dyn(REG_EH_CTRL_DYN) & EH_VCC_ON)

    def poll_events(self):
        """Drain ``IT_STS_Dyn`` once and return what it held.

        Reading the register clears it (section 5.2.3), so this is the only
        place in the driver that touches 0x2005, and calling it twice loses
        whatever arrived before the first call. Keep the returned object.
        """
        return Events(self._dyn(REG_IT_STS_DYN))

    def _wait_for(self, mask, timeout, interval, extra_field=False):
        """Poll IT_STS_Dyn until any bit in ``mask`` has been seen.

        Events are accumulated rather than replaced, so a field-rising that
        arrives while waiting for an RF write is still in the object that comes
        back instead of being silently eaten by an intermediate read.
        """
        deadline = _deadline(timeout)
        seen = 0
        while True:
            seen |= self.poll_events().raw
            if extra_field and self.field_present:
                seen |= IT_FIELD_RISING
            if seen & mask:
                return Events(seen)
            if _expired(deadline):
                return None
            sleep(interval)

    def wait_for_field(self, timeout=10, interval=0.01):
        """Wait for a reader's field. Returns the events seen, or ``None``.

        Both a FIELD_RISING event and a field that is already present count, so
        a phone held on the tag before the call still registers. In that second
        case FIELD_RISING is set in the returned object to say why it matched.
        """
        return self._wait_for(IT_FIELD_RISING, timeout, interval,
                              extra_field=True)

    def wait_for_rf_write(self, timeout=10, interval=0.01):
        """Wait for a reader to write EEPROM. Returns the events, or ``None``.

        This is the "a phone just changed the tag" signal: read the NDEF back
        afterwards to see what it wrote.
        """
        return self._wait_for(IT_RF_WRITE, timeout, interval)

    def wait_for_mailbox(self, timeout=10, interval=0.01):
        """Wait for a reader to put a mailbox message. Returns events or None."""
        return self._wait_for(IT_RF_PUT_MSG, timeout, interval)
