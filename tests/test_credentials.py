"""Phone numbers, Wi-Fi, contacts, Bluetooth and HomeKit.

These are all payload formats rather than chip behaviour, so they are checked
on both sides: the bytes that go out are pinned field by field against the
format's own rules, and every builder is read back through the same decoder a
tag's contents would go through.
"""
import pytest

from fake_st25dv import FakeST25DV
from st25dv import (BLE_CENTRAL_ONLY, BLE_PERIPHERAL_ONLY, HOMEKIT_SCHEME,
                    MIME_BLUETOOTH, MIME_BLUETOOTH_LE, MIME_VCARD, MIME_WIFI,
                    NDEFError, NDEFMessage, NDEFRecord, ST25DV, WIFI_ENC_AES,
                    WIFI_ENC_NONE, WIFI_OPEN, WIFI_WPA2_PSK, _eir_walk,
                    _wsc_walk)


@pytest.fixture
def tag():
    chip = FakeST25DV(2048)
    tag = ST25DV(chip)
    tag.format()
    return tag


def roundtrip(tag, record):
    """Through the TLV, the EEPROM and the parser, the way a phone would."""
    tag.ndef = record
    return tag.ndef.records[0]


# -- phone numbers ----------------------------------------------------------

def test_tel_builds_a_uri_record_with_the_tel_prefix_code():
    rec = NDEFRecord.tel("+441632960961")
    assert rec.kind == "uri"
    assert rec.payload[0] == 5, "tel: is prefix code 5"
    assert rec.payload[1:] == b"+441632960961"
    assert rec.value == "tel:+441632960961"


def test_tel_accepts_the_scheme_already_there_and_does_not_double_it():
    assert NDEFRecord.tel("tel:+1234").value == "tel:+1234"
    assert NDEFRecord.tel("  +1234  ").value == "tel:+1234"


def test_tel_costs_one_byte_more_than_the_digits():
    """The point of the prefix table: the scheme is a single byte."""
    rec = NDEFRecord.tel("+441632960961")
    assert len(rec.payload) == len("+441632960961") + 1


def test_sms_percent_encodes_the_body():
    rec = NDEFRecord.sms("+1234", "on my way!")
    assert rec.value == "sms:+1234?body=on%20my%20way%21"


def test_sms_without_a_body_is_just_the_number():
    assert NDEFRecord.sms("+1234").value == "sms:+1234"


def test_a_tel_string_assigned_to_the_tag_is_stored_as_a_uri(tag):
    tag.ndef = "tel:+441632960961"
    assert tag.ndef.uri == "tel:+441632960961"


# -- email ------------------------------------------------------------------

def test_email_builds_a_mailto_uri_with_the_right_prefix_code():
    rec = NDEFRecord.email("grace@example.com")
    assert rec.kind == "uri"
    assert rec.payload[0] == 6, "mailto: is prefix code 6"
    assert rec.value == "mailto:grace@example.com"


def test_email_does_not_encode_the_address_itself():
    """@ and . are legal in a mailto: address; encoding them helps nobody."""
    assert "@" in NDEFRecord.email("grace@example.com").value


def test_email_percent_encodes_subject_and_body():
    rec = NDEFRecord.email("a@b.c", "Hello there", "see you at 6")
    assert rec.value == ("mailto:a@b.c?subject=Hello%20there"
                         "&body=see%20you%20at%206")


def test_email_takes_either_half_of_the_query_alone():
    assert NDEFRecord.email("a@b.c", subject="Hi").value == \
        "mailto:a@b.c?subject=Hi"
    assert NDEFRecord.email("a@b.c", body="Hi").value == \
        "mailto:a@b.c?body=Hi"


def test_email_scheme_is_not_doubled():
    assert NDEFRecord.email("mailto:a@b.c").value == "mailto:a@b.c"


def test_email_round_trips_through_the_tag(tag):
    rec = roundtrip(tag, NDEFRecord.email("grace@example.com", "Hi"))
    assert rec.value == "mailto:grace@example.com?subject=Hi"


def test_a_mailto_string_assigned_to_the_tag_is_stored_as_a_uri(tag):
    tag.ndef = "mailto:grace@example.com"
    assert tag.ndef.uri == "mailto:grace@example.com"


# -- Wi-Fi ------------------------------------------------------------------

def wsc_fields(record):
    """The credential's inner TLVs, flattened to {type: value}."""
    for kind, value in _wsc_walk(record.payload):
        if kind == 0x100E:
            return dict(_wsc_walk(value))
    raise AssertionError("no Credential TLV")


def test_wifi_uses_the_mime_type_android_dispatches_on():
    rec = NDEFRecord.wifi("MyNetwork", "hunter22")
    assert rec.type == MIME_WIFI.encode()
    assert rec.kind == "wifi"


def test_wifi_fields_are_big_endian_tlvs_in_a_credential():
    rec = NDEFRecord.wifi("MyNetwork", "hunter22")
    fields = wsc_fields(rec)
    assert fields[0x1045] == b"MyNetwork"          # SSID
    assert fields[0x1027] == b"hunter22"           # network key
    assert fields[0x1003] == b"\x00\x20"           # WPA2-Personal, big endian
    assert fields[0x100F] == b"\x00\x08"           # AES


def test_a_password_defaults_to_wpa2_with_aes():
    got = NDEFRecord.wifi("Net", "hunter22").value
    assert got["authentication"] == WIFI_WPA2_PSK
    assert got["encryption"] == WIFI_ENC_AES
    assert got["security"] == "wpa2"


def test_no_password_defaults_to_an_open_network():
    got = NDEFRecord.wifi("Cafe").value
    assert got["authentication"] == WIFI_OPEN
    assert got["encryption"] == WIFI_ENC_NONE
    assert got["security"] == "open"
    assert got["password"] == ""


def test_wifi_round_trips_through_the_tag(tag):
    rec = roundtrip(tag, NDEFRecord.wifi("Guest Wi-Fi", "correct horse"))
    got = rec.value
    assert got["ssid"] == "Guest Wi-Fi"
    assert got["password"] == "correct horse"
    assert got["security"] == "wpa2"
    assert tag.ndef.wifi["ssid"] == "Guest Wi-Fi"


def test_a_utf8_ssid_survives(tag):
    rec = roundtrip(tag, NDEFRecord.wifi("café ☕", "hunter22"))
    assert rec.value["ssid"] == "café ☕"


def test_an_optional_mac_is_carried_and_read_back():
    rec = NDEFRecord.wifi("Net", "hunter22", mac="a4:c1:38:01:02:03")
    assert rec.value["mac"] == "a4:c1:38:01:02:03"


def test_an_open_network_refuses_a_password():
    with pytest.raises(NDEFError) as err:
        NDEFRecord.wifi("Cafe", "hunter22", authentication=WIFI_OPEN)
    assert "open network" in str(err.value)


def test_naming_a_secured_type_without_a_password_is_refused():
    """Bare wifi("Net") means an open network; asking for WPA2 and giving no
    key is a mistake, and quietly writing "open" would be the wrong repair."""
    with pytest.raises(NDEFError) as err:
        NDEFRecord.wifi("Net", authentication=WIFI_WPA2_PSK)
    assert "needs a password" in str(err.value)


@pytest.mark.parametrize("ssid", ["", "x" * 33])
def test_an_ssid_has_to_fit_the_field(ssid):
    with pytest.raises(NDEFError):
        NDEFRecord.wifi(ssid, "hunter22")


@pytest.mark.parametrize("key", ["short", "x" * 65])
def test_a_wpa_key_has_to_be_a_plausible_length(key):
    with pytest.raises(NDEFError):
        NDEFRecord.wifi("Net", key)


def test_a_record_with_no_credential_is_named_not_guessed():
    rec = NDEFRecord(0x02, MIME_WIFI.encode(), b"\x10\x03\x00\x02\x00\x20")
    with pytest.raises(NDEFError) as err:
        rec.value
    assert "no Wi-Fi credential" in str(err.value)


# -- contacts ---------------------------------------------------------------

def test_contact_is_a_vcard_with_the_mandatory_fields():
    rec = NDEFRecord.contact("Ada Lovelace", phone="+441632960961",
                             email="ada@example.com")
    text = rec.value["text"]
    assert rec.type == MIME_VCARD.encode()
    assert text.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\n")
    assert text.endswith("END:VCARD\r\n")
    assert "FN:Ada Lovelace" in text
    assert "N:Lovelace;Ada;;;" in text
    assert "TEL;TYPE=CELL:+441632960961" in text
    assert "EMAIL;TYPE=INTERNET:ada@example.com" in text


def test_a_name_is_split_on_the_last_space():
    text = NDEFRecord.contact("Ada King Lovelace").value["text"]
    assert "N:Lovelace;Ada King;;;" in text


def test_an_explicit_first_and_last_win_over_splitting():
    text = NDEFRecord.contact(first="Ada", last="Lovelace").value["text"]
    assert "N:Lovelace;Ada;;;" in text
    assert "FN:Ada Lovelace" in text


def test_several_numbers_and_addresses():
    rec = NDEFRecord.contact("A B", phone=["+1", "+2"],
                             email=["a@b.c", "d@e.f"])
    got = rec.value
    assert got["phone"] == ["+1", "+2"]
    assert got["email"] == ["a@b.c", "d@e.f"]


def test_vcard_separators_in_a_value_are_escaped():
    """A comma or semicolon would otherwise end the field early."""
    text = NDEFRecord.contact("A B", organization="Bakery, Ltd; Bread")\
        .value["text"]
    assert "ORG:Bakery\\, Ltd\\; Bread" in text


def test_a_contact_needs_a_name():
    with pytest.raises(NDEFError):
        NDEFRecord.contact(phone="+1234")


def test_contact_round_trips_through_the_tag(tag):
    rec = roundtrip(tag, NDEFRecord.contact(
        "Grace Hopper", phone="+441632960961", email="grace@example.com",
        organization="US Navy", title="Rear Admiral",
        url="https://example.com", note="knows where the bug is"))
    got = rec.value
    assert rec.kind == "contact"
    assert got["name"] == "Grace Hopper"
    assert got["phone"] == ["+441632960961"]
    assert got["organization"] == "US Navy"
    assert "TITLE:Rear Admiral" in got["text"]
    assert tag.ndef.contact["name"] == "Grace Hopper"


def test_the_older_x_vcard_type_is_recognised_on_read():
    rec = NDEFRecord(0x02, b"text/x-vCard",
                     b"BEGIN:VCARD\r\nFN:Old Style\r\nEND:VCARD\r\n")
    assert rec.kind == "contact"
    assert rec.value["name"] == "Old Style"


# -- Bluetooth --------------------------------------------------------------

def test_bluetooth_address_goes_out_little_endian():
    """BD_ADDR is reversed on the wire; people write it the other way."""
    rec = NDEFRecord.bluetooth("a4:c1:38:01:02:03", name="Speaker")
    assert rec.type == MIME_BLUETOOTH.encode()
    assert rec.payload[2:8] == b"\x03\x02\x01\x38\xc1\xa4"


def test_the_bluetooth_length_prefix_counts_itself():
    rec = NDEFRecord.bluetooth("a4:c1:38:01:02:03", name="Speaker")
    declared = rec.payload[0] | (rec.payload[1] << 8)
    assert declared == len(rec.payload)


def test_bluetooth_reads_back_the_way_it_was_written():
    got = NDEFRecord.bluetooth("a4:c1:38:01:02:03", name="Speaker").value
    assert got["address"] == "a4:c1:38:01:02:03"
    assert got["name"] == "Speaker"
    assert got["low_energy"] is False


def test_class_of_device_is_three_bytes_little_endian():
    rec = NDEFRecord.bluetooth("a4:c1:38:01:02:03", class_of_device=0x240404)
    assert rec.value["class_of_device"] == 0x240404
    assert dict(_eir_walk(rec.payload[8:]))[0x0D] == b"\x04\x04\x24"


def test_bluetooth_le_carries_the_address_as_an_eir_structure():
    rec = NDEFRecord.bluetooth_le("a4:c1:38:01:02:03", name="Sensor")
    assert rec.type == MIME_BLUETOOTH_LE.encode()
    fields = dict(_eir_walk(rec.payload))
    assert fields[0x1B] == b"\x03\x02\x01\x38\xc1\xa4\x00"
    assert fields[0x1C] == bytes([BLE_PERIPHERAL_ONLY])
    assert fields[0x09] == b"Sensor"


def test_bluetooth_le_reads_back():
    got = NDEFRecord.bluetooth_le("a4:c1:38:01:02:03", address_type=1,
                                  role=BLE_CENTRAL_ONLY, name="Sensor").value
    assert got["address"] == "a4:c1:38:01:02:03"
    assert got["address_type"] == "random"
    assert got["role"] == "central"
    assert got["low_energy"] is True


def test_bluetooth_round_trips_through_the_tag(tag):
    rec = roundtrip(tag, NDEFRecord.bluetooth_le("a4:c1:38:01:02:03",
                                                 name="Sensor"))
    assert rec.kind == "bluetooth_le"
    assert rec.value["name"] == "Sensor"


@pytest.mark.parametrize("address", ["a4:c1:38:01:02", "zz:c1:38:01:02:03",
                                     b"\x01\x02\x03"])
def test_a_malformed_address_is_refused(address):
    with pytest.raises(NDEFError):
        NDEFRecord.bluetooth(address)


def test_addresses_may_be_written_without_separators_or_as_bytes():
    plain = NDEFRecord.bluetooth("a4c138010203").value["address"]
    raw = NDEFRecord.bluetooth(b"\xa4\xc1\x38\x01\x02\x03").value["address"]
    assert plain == raw == "a4:c1:38:01:02:03"


def test_a_truncated_bluetooth_record_is_named():
    rec = NDEFRecord(0x02, MIME_BLUETOOTH.encode(), b"\x03\x00\x01")
    with pytest.raises(NDEFError) as err:
        rec.value
    assert "at least 8 bytes" in str(err.value)


# -- HomeKit ----------------------------------------------------------------

def test_homekit_writes_the_setup_payload_verbatim():
    rec = NDEFRecord.homekit("X-HM://0024K0M6P00HB")
    assert rec.kind == "uri"
    assert rec.value == "X-HM://0024K0M6P00HB"


def test_homekit_adds_the_scheme_when_only_the_payload_is_given():
    assert NDEFRecord.homekit("0024K0M6P00HB").value == \
        HOMEKIT_SCHEME + "0024K0M6P00HB"


def test_homekit_round_trips_through_the_tag(tag):
    rec = roundtrip(tag, NDEFRecord.homekit("0024K0M6P00HB"))
    assert rec.value.startswith(HOMEKIT_SCHEME)


# -- several records in one message -----------------------------------------

def test_a_message_can_carry_a_url_and_a_credential_together(tag):
    tag.ndef = NDEFMessage([
        NDEFRecord.uri("https://example.com"),
        NDEFRecord.wifi("Guest", "hunter22"),
        NDEFRecord.tel("+441632960961"),
    ])
    message = tag.ndef
    assert len(message) == 3
    assert message.uri == "https://example.com"
    assert message.wifi["ssid"] == "Guest"
    assert message.first("uri") == "https://example.com"
    assert [r.kind for r in message] == ["uri", "wifi", "uri"]


def test_first_returns_none_for_a_kind_that_is_not_there(tag):
    tag.ndef = "https://example.com"
    assert tag.ndef.first("wifi") is None
    assert tag.ndef.contact is None
