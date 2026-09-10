"""The Type 5 capability container, in both its 4-byte and 8-byte forms."""
import pytest

from fake_st25dv import FakeST25DV
from st25dv import CapabilityContainer, NDEFError, ST25DV


def test_short_form_parses():
    cc = CapabilityContainer.from_bytes(b"\xe1\x40\x40\x05")
    assert cc.length == 4
    assert cc.capacity == 512
    assert cc.major_version == 1 and cc.minor_version == 0
    assert cc.writable
    assert not cc.extended


def test_extended_form_parses():
    """A zero third byte is what says the real length lives in bytes 6 and 7."""
    cc = CapabilityContainer.from_bytes(b"\xe2\x40\x00\x01\x00\x00\x03\xff")
    assert cc.length == 8
    assert cc.capacity == 0x3FF * 8
    assert cc.extended


def test_extended_form_needs_all_eight_bytes():
    with pytest.raises(NDEFError) as err:
        CapabilityContainer.from_bytes(b"\xe2\x40\x00\x01")
    assert "8 bytes" in str(err.value)


def test_unformatted_tag_is_named_as_such():
    with pytest.raises(NDEFError) as err:
        CapabilityContainer.from_bytes(b"\xff\xff\xff\xff")
    assert "not a formatted Type 5 tag" in str(err.value)


def test_round_trip_through_bytes():
    for raw in (b"\xe1\x40\x40\x05", b"\xe2\x40\x00\x01\x00\x00\x03\xff"):
        assert CapabilityContainer.from_bytes(raw).to_bytes() == raw


def test_access_bits_are_split_out():
    cc = CapabilityContainer.from_bytes(b"\xe1\x4f\x40\x05")
    assert cc.read_access == 3 and cc.write_access == 3
    assert not cc.writable


@pytest.mark.parametrize("capacity,length,capacity_declared", [
    (512, 4, 504),          # the 4K: one byte of MLEN still covers it
    (2044, 4, 2040),        # the largest tag the short form can describe
    (2048, 8, 2040),        # the 16K: one byte short by four, so extended
    (8192, 8, 8184),        # the 64K
])
def test_form_is_chosen_by_capacity(capacity, length, capacity_declared):
    cc = CapabilityContainer.for_capacity(capacity)
    assert cc.length == length
    assert cc.capacity == capacity_declared
    assert cc.length + cc.capacity <= capacity


def test_a_16k_gets_the_extended_form_not_a_truncated_short_one():
    """The bug this guards: a short container on a 16K declares 2040 of 2048
    bytes and strands the rest, and Adafruit's shipped one declares 512."""
    cc = CapabilityContainer.for_capacity(2048)
    assert cc.magic == 0xE2
    assert cc.to_bytes() == b"\xe2\x40\x00\x05\x00\x00\x00\xff"


@pytest.mark.parametrize("capacity", [512, 2048, 8192])
def test_format_then_read_back_on_a_tag(capacity):
    tag = ST25DV(FakeST25DV(capacity))
    written = tag.format()
    assert tag.capability_container == written
    assert tag.capability_container.capacity == written.capacity


def test_format_carries_the_existing_feature_flags():
    chip = FakeST25DV(2048)
    tag = ST25DV(chip)
    tag.write(0, b"\xe1\x40\x40\x0f")
    assert tag.format().flags == 0x0F


def test_format_without_erase_leaves_the_message_alone():
    """Fixing an under-declared container should not throw away the URL."""
    chip = FakeST25DV(2048)
    tag = ST25DV(chip)
    tag.write(0, b"\xe2\x40\x00\x05\x00\x00\x00\x40")     # declares 512 bytes
    tag.ndef = "https://example.com"
    tag.format(erase=False)
    assert tag.capability_container.capacity == 2040
    assert tag.ndef.uri == "https://example.com"


def test_zero_length_declaration_is_rejected():
    with pytest.raises(NDEFError):
        CapabilityContainer.from_bytes(b"\xe2\x40\x00\x05\x00\x00\x00\x00")


def test_fixing_a_short_container_moves_the_message_with_it():
    """The Adafruit case: a 16K shipped with a 4-byte container. Correcting it
    to the 8-byte form moves the NDEF area, so the message has to move too or
    a reader looks four bytes past where it now sits."""
    tag = ST25DV(FakeST25DV(2048))
    tag.write(0, b"\xe1\x40\x40\x05")
    tag.ndef = "https://www.adafruit.com/product/4701"
    assert tag.capability_container.length == 4

    tag.format(erase=False)
    assert tag.capability_container.length == 8
    assert tag.capability_container.capacity == 2040
    assert tag.ndef.uri == "https://www.adafruit.com/product/4701"

    tag.ndef = "y" * 600                      # would not have fitted before
    assert len(tag.ndef.text) == 600


def test_shrinking_a_container_below_the_message_is_refused():
    tag = ST25DV(FakeST25DV(2048))
    tag.format()
    tag.ndef = "y" * 600
    with pytest.raises(NDEFError):
        tag.format(capacity=512, erase=False)
