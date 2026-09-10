"""The fast transfer mode mailbox, and the rules it imposes on everything else."""
import pytest

from fake_st25dv import FakeST25DV
from st25dv import MailboxError, SessionRequired, ST25DV


@pytest.fixture
def chip():
    return FakeST25DV(2048)


@pytest.fixture
def tag(chip):
    return ST25DV(chip)


@pytest.fixture
def live(tag):
    tag.open_session()
    tag.mailbox.allowed = True
    tag.close_session()
    tag.mailbox.enable()
    return tag


def test_mailbox_is_off_and_not_even_allowed_from_the_factory(tag):
    assert not tag.mailbox.allowed
    assert not tag.mailbox.enabled


def test_enabling_without_mb_mode_says_what_is_missing(tag):
    with pytest.raises(MailboxError) as err:
        tag.mailbox.enable()
    assert "MB_MODE is 0" in str(err.value)


def test_allowing_the_mailbox_needs_the_session(tag):
    with pytest.raises(SessionRequired):
        tag.mailbox.allowed = True


def test_enable_and_disable_round_trip(live):
    assert live.mailbox.enabled
    live.mailbox.disable()
    assert not live.mailbox.enabled


def test_host_side_round_trip(live):
    live.mailbox.put(b"hello over i2c")
    assert live.mailbox.available == len(b"hello over i2c")
    assert live.mailbox.sender == "i2c"
    assert live.mailbox.get() == b"hello over i2c"


def test_length_register_holds_one_less_than_the_message(chip, live):
    """MB_LEN_Dyn is size minus one. A driver that forgets loses the last byte
    and reports an empty mailbox as holding one."""
    live.mailbox.put(b"AB")
    assert chip.dynamic[0x07] == 1
    assert live.mailbox.available == 2


def test_an_empty_mailbox_reports_zero_not_one(live):
    assert live.mailbox.available == 0
    assert live.mailbox.get() == b""
    assert len(live.mailbox) == 0


def test_reading_the_whole_message_frees_the_mailbox(live):
    live.mailbox.put(b"first")
    live.mailbox.get()
    live.mailbox.put(b"second")
    assert live.mailbox.get() == b"second"


def test_a_message_from_the_rf_side_is_labelled_as_such(chip, live):
    chip.rf_put_mailbox(b"from the phone")
    assert live.mailbox.sender == "rf"
    assert live.mailbox.get() == b"from the phone"


def test_waiting_for_a_reader_to_put_a_message(chip, live):
    chip.rf_put_mailbox(b"ping")
    assert live.wait_for_mailbox(timeout=1).rf_put_msg


def test_put_is_bounded_by_the_buffer(live):
    with pytest.raises(MailboxError):
        live.mailbox.put(b"x" * 257)
    with pytest.raises(MailboxError):
        live.mailbox.put(b"")


def test_put_with_the_mailbox_off_is_named(tag):
    with pytest.raises(MailboxError) as err:
        tag.mailbox.put(b"x")
    assert "fast transfer mode is off" in str(err.value)


def test_a_full_length_message_fits(live):
    payload = bytes(range(256))
    live.mailbox.put(payload)
    assert live.mailbox.get() == payload


def test_the_watchdog_is_a_three_bit_field(tag):
    tag.open_session()
    assert tag.mailbox.watchdog == 7
    tag.mailbox.watchdog = 3
    assert tag.mailbox.watchdog == 3
    with pytest.raises(ValueError):
        tag.mailbox.watchdog = 8


def test_missed_messages_are_reported_by_side(chip, live):
    chip.dynamic[0x06] |= 0x10 | 0x20
    assert live.mailbox.missed == ("i2c", "rf")


def test_repr_says_the_state(live):
    live.mailbox.put(b"hi")
    assert "on" in repr(live.mailbox)
