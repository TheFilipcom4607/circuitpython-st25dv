"""NDEF encoding, decoding and the TLV wrapper the tag stores it in."""
import pytest

from fake_st25dv import FakeST25DV
from st25dv import (ST25DV, NDEFError, NDEFMessage, NDEFRecord, _ndef_to_tlv,
                    _ndef_from_tlv)


@pytest.fixture
def tag():
    chip = FakeST25DV(2048)
    tag = ST25DV(chip)
    tag.format()
    tag.chip = chip
    return tag


def test_uri_round_trip(tag):
    tag.ndef = "https://thefilip.com"
    assert tag.ndef.uri == "https://thefilip.com"


def test_text_round_trip(tag):
    tag.ndef = "just some words"
    assert tag.ndef.text == "just some words"
    assert tag.ndef.records[0].language == "en"


def test_string_with_scheme_becomes_a_uri_record(tag):
    """A scheme the prefix table knows is enough; "://" is not required.

    This used to turn on "://" alone, which made `mailto:` and `tel:` prose.
    """
    tag.ndef = "mailto:someone@example.com"
    assert tag.ndef.records[0].kind == "uri"
    assert tag.ndef.uri == "mailto:someone@example.com"
    tag.ndef = "tel:+441632960961"
    assert tag.ndef.uri == "tel:+441632960961"
    tag.ndef = "sms:+441632960961"
    assert tag.ndef.records[0].kind == "uri"
    tag.ndef = "tel://12345"
    assert tag.ndef.records[0].kind == "uri"


def test_prose_that_merely_contains_a_colon_is_still_text(tag):
    """The reason the check is a prefix table and not "is there a colon"."""
    for prose in ("Note: buy milk", "Warning: hot", "12:30 tomorrow"):
        tag.ndef = prose
        assert tag.ndef.records[0].kind == "text", prose
        assert tag.ndef.text == prose


@pytest.mark.parametrize("uri,code", [
    ("https://www.example.com", 2),
    ("http://example.com", 3),
    ("https://example.com", 4),
    ("tel:12345", 5),
    ("mailto:a@b.c", 6),
    ("urn:nfc:x", 35),
    ("gopher://example.com", 0),
])
def test_shortest_uri_prefix_is_chosen(uri, code):
    record = NDEFRecord.uri(uri)
    assert record.payload[0] == code
    assert record.value == uri


def test_multiple_records_round_trip(tag):
    message = NDEFMessage([NDEFRecord.uri("https://example.com"),
                           NDEFRecord.text("hello", "fr")])
    tag.ndef = message
    back = tag.ndef
    assert len(back) == 2
    assert back.uri == "https://example.com"
    assert back.text == "hello"
    assert back.records[1].language == "fr"


def test_short_tlv_length_form():
    message = NDEFMessage.from_text("x" * 100)
    tlv = _ndef_to_tlv(message)
    assert tlv[0] == 0x03 and tlv[1] == len(message.to_bytes())
    assert tlv[-1] == 0xFE


def test_three_byte_tlv_length_form():
    message = NDEFMessage.from_text("x" * 400)
    payload = message.to_bytes()
    tlv = _ndef_to_tlv(message)
    assert tlv[0] == 0x03 and tlv[1] == 0xFF
    assert (tlv[2] << 8 | tlv[3]) == len(payload)
    assert _ndef_from_tlv(tlv).text == "x" * 400


def test_message_over_255_bytes_round_trips_on_the_tag(tag):
    """The TLV length form changes at 255 bytes, which is where a tag that
    only ever saw short messages starts returning nonsense."""
    text = "y" * 400
    tag.ndef = text
    assert tag.ndef.text == text
    assert tag.chip.user[tag.capability_container.length + 1] == 0xFF


def test_proprietary_tlv_is_skipped(tag):
    """A 0xFD TLV may precede the NDEF one and must be walked past by length."""
    cc_len = tag.capability_container.length
    tag.write(cc_len, b"\xfd\x04\x01\x02\x03\x04")
    tag.write(cc_len + 6, _ndef_to_tlv(NDEFMessage.from_uri("https://a.co")))
    assert tag.ndef.uri == "https://a.co"


def test_terminator_before_any_message_reads_as_none(tag):
    tag.write(tag.capability_container.length, b"\xfe")
    assert tag.ndef is None


def test_formatted_but_empty_tag_reads_as_none(tag):
    assert tag.ndef is None                        # format() wrote 03 00 FE


def test_message_too_large_for_the_declared_area_is_refused(tag):
    tag.format(capacity=64)                        # declares 56 usable bytes
    with pytest.raises(NDEFError) as err:
        tag.ndef = "z" * 200
    assert "do not fit" in str(err.value)


def test_truncated_tlv_claims_more_than_the_area_holds(tag):
    cc_len = tag.capability_container.length
    tag.write(cc_len, b"\x03\xff\x07\xff")         # claims 2047 bytes
    with pytest.raises(NDEFError) as err:
        tag.read_ndef()
    assert "capability container is wrong" in str(err.value)


def test_records_compare_by_value():
    assert NDEFRecord.uri("https://a.co") == NDEFRecord.uri("https://a.co")
    assert NDEFRecord.uri("https://a.co") != NDEFRecord.text("a")
    assert NDEFMessage.from_uri("https://a.co") == NDEFMessage.from_uri(
        "https://a.co")


def test_empty_message_raises_rather_than_returning_nothing():
    with pytest.raises(NDEFError):
        NDEFMessage.from_bytes(b"")
