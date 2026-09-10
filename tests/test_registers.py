"""Security session, configuration registers and the event register."""
import pytest

from fake_st25dv import FakeST25DV
from st25dv import (EH_FIELD_ON, Events, GPO_FIELD_CHANGE, GPO_RF_WRITE,
                    IT_FIELD_RISING, IT_RF_PUT_MSG, IT_RF_WRITE,
                    SessionRequired, ST25DV)


@pytest.fixture
def chip():
    return FakeST25DV(2048)


@pytest.fixture
def tag(chip):
    return ST25DV(chip)


# -- session ----------------------------------------------------------------

def test_the_factory_password_opens_the_session(tag):
    assert not tag.session_open
    tag.open_session()
    assert tag.session_open


def test_a_wrong_password_is_reported_not_silently_ignored(tag):
    with pytest.raises(SessionRequired) as err:
        tag.open_session(b"12345678")
    assert "rejected" in str(err.value)
    assert not tag.session_open


def test_the_present_command_is_password_validation_password(chip, tag):
    """17 bytes: the password, 0x09, then the password again (section 6.6.1)."""
    chip.log.clear()
    tag.open_session()
    payload = [e[3] for e in chip.log if e[0] == "write" and e[2] == 0x0900][0]
    assert len(payload) == 17
    assert payload[8] == 0x09
    assert payload[:8] == payload[9:]


def test_closing_the_session_presents_something_that_cannot_match(tag):
    tag.open_session()
    tag.close_session()
    assert not tag.session_open


def test_closing_works_even_with_an_all_ones_password(tag):
    tag.open_session()
    tag.change_password(b"\xff" * 8)
    tag.close_session()
    assert not tag.session_open


def test_closing_a_session_this_object_did_not_open(chip):
    """The case the single-guess close got wrong.

    There is no close command, only a wrong password, and with nothing
    presented through this object there is nothing to take the complement of.
    The assumed factory password complements to eight 0xFF bytes -- which, if
    that is the real password, *opens* the session instead of closing it. A
    second, complementary guess has to follow.
    """
    chip.password[:] = b"\xff" * 8
    chip.dynamic[0x04] = 0x01                     # opened out of band
    tag = ST25DV(chip)
    assert tag.session_open
    assert tag._session_password is None
    tag.close_session()
    assert not tag.session_open


def test_that_close_takes_two_guesses_only_when_it_has_to(chip):
    """One presentation in the ordinary case, two only in the awkward one."""
    tag = ST25DV(chip)
    tag.open_session()
    chip.log.clear()
    tag.close_session()
    known = [e for e in chip.log if e[0] == "write" and e[2] == 0x0900]
    assert len(known) == 1

    chip.password[:] = b"\xff" * 8
    chip.dynamic[0x04] = 0x01
    blind = ST25DV(chip)
    chip.log.clear()
    blind.close_session()
    guesses = [bytes(e[3][:8]) for e in chip.log
               if e[0] == "write" and e[2] == 0x0900]
    assert guesses == [b"\xff" * 8, b"\x00" * 8]


def test_closing_never_presents_the_password_it_was_given(chip):
    """A known password is only ever complemented, never sent back."""
    tag = ST25DV(chip)
    tag.open_session()
    tag.change_password(b"secret!!")
    chip.log.clear()
    tag.close_session()
    guesses = [bytes(e[3][:8]) for e in chip.log
               if e[0] == "write" and e[2] == 0x0900]
    assert guesses == [bytes(b ^ 0xFF for b in b"secret!!")]
    tag.open_session(b"secret!!")
    tag.change_password(b"\x00" * 8)             # leave it as it was found


def test_changing_the_password_uses_validation_byte_7(chip, tag):
    tag.open_session()
    chip.log.clear()
    tag.change_password(b"secret!!")
    payload = [e[3] for e in chip.log if e[0] == "write" and e[2] == 0x0900][0]
    assert payload[8] == 0x07
    assert bytes(chip.password) == b"secret!!"


def test_the_new_password_is_the_one_that_works(tag):
    tag.open_session()
    tag.change_password(b"secret!!")
    tag.close_session()
    with pytest.raises(SessionRequired):
        tag.open_session()                        # the factory one, now wrong
    tag.open_session(b"secret!!")
    assert tag.session_open
    tag.change_password(b"\x00" * 8)              # leave it as it was found


def test_changing_the_password_needs_the_session(tag):
    with pytest.raises(SessionRequired):
        tag.change_password(b"secret!!")


def test_passwords_are_eight_bytes(tag):
    with pytest.raises(ValueError):
        tag.open_session(b"short")


# -- system registers -------------------------------------------------------

def test_system_writes_are_refused_by_name_when_the_session_is_shut(tag):
    with pytest.raises(SessionRequired) as err:
        tag.gpo = GPO_FIELD_CHANGE
    assert "open_session" in str(err.value)


def test_gpo_event_mask_round_trips(tag):
    tag.open_session()
    tag.gpo = GPO_FIELD_CHANGE | GPO_RF_WRITE | 0x80
    assert tag.gpo == 0x88 | GPO_RF_WRITE


def test_gpo_output_bit_needs_no_session(chip, tag):
    """Bit 7 of GPO_CTRL_Dyn is the one GPO bit an I2C host may set with the
    session closed, and it is the one that actually drives the pin."""
    assert not tag.session_open
    tag.gpo_enabled = False
    assert not tag.gpo_enabled
    tag.gpo_enabled = True
    assert tag.gpo_enabled


def test_interrupt_pulse_maps_to_microseconds(tag):
    tag.open_session()
    tag.interrupt_pulse_us = 301
    assert round(tag.interrupt_pulse_us) == 301
    tag.interrupt_pulse_us = 150
    assert 130 < tag.interrupt_pulse_us < 170


def test_interrupt_pulse_outside_the_range_is_refused(tag):
    tag.open_session()
    with pytest.raises(ValueError):
        tag.interrupt_pulse_us = 1000


def test_energy_harvesting_dynamic_bit_needs_no_session(tag):
    assert not tag.energy_harvesting
    tag.energy_harvesting = True
    assert tag.energy_harvesting
    assert tag.energy_harvesting_active


def test_energy_harvesting_after_boot_is_the_inverted_static_bit(chip, tag):
    """EH_MODE 1 means "on demand only", so the property reads the other way
    round from the register."""
    assert chip.system[0x0002] == 0x01
    assert not tag.energy_harvesting_after_boot
    tag.open_session()
    tag.energy_harvesting_after_boot = True
    assert chip.system[0x0002] == 0x00


def test_rf_can_be_silenced_and_restored(tag):
    tag.rf_disabled = True
    assert tag.rf_disabled and not tag.rf_sleep
    tag.rf_sleep = True
    assert tag.rf_disabled and tag.rf_sleep
    tag.rf_disabled = False
    assert not tag.rf_disabled and tag.rf_sleep
    tag.rf_sleep = False
    assert not tag.rf_sleep


def test_static_and_dynamic_rf_management_are_separate(chip, tag):
    tag.rf_disabled = True
    assert tag.rf_management == 0x00           # the boot value is untouched
    tag.open_session()
    tag.rf_management = 0x01
    assert tag.rf_disabled                     # the static write copies down


def test_system_dump_covers_the_whole_readable_area(tag):
    blob = tag.system_dump()
    assert len(blob) == 0x21
    assert blob[0x17] == tag.ic_ref
    assert blob[0x20] == tag.ic_revision


def test_lock_registers_round_trip(tag):
    tag.open_session()
    tag.lock_cfg = True
    assert tag.lock_cfg
    tag.lock_ccfile = 0x03
    assert tag.lock_ccfile == 0x03


# -- events -----------------------------------------------------------------

def test_the_event_register_is_read_to_clear(chip, tag):
    chip.rf_event(IT_RF_WRITE)
    assert tag.poll_events().rf_write
    assert not tag.poll_events().rf_write       # gone, as the chip intends


def test_repr_does_not_eat_events(chip, tag):
    """A status property or __repr__ that touched 0x2005 would destroy exactly
    the events the caller is waiting for."""
    chip.rf_event(IT_RF_WRITE)
    repr(tag)
    _ = tag.field_present, tag.vcc_present, tag.session_open
    assert tag.poll_events().rf_write


def test_events_decode_to_names():
    events = Events(IT_RF_WRITE | IT_FIELD_RISING)
    assert events.rf_write and events.field_rising and events.field_change
    assert events.names == ["field_rising", "rf_write"]
    assert bool(events) and not bool(Events(0))
    assert "field_rising" in repr(events)


def test_field_presence_comes_from_eh_ctrl(chip, tag):
    assert not tag.field_present
    assert tag.vcc_present
    chip.rf_field(True)
    assert tag.field_present


def test_wait_for_field_returns_the_events_it_saw(chip, tag):
    chip.rf_field(True)
    events = tag.wait_for_field(timeout=1)
    assert events.field_rising


def test_wait_for_field_matches_a_field_already_present(chip, tag):
    chip.rf_field(True)
    tag.poll_events()                           # drain the rising event
    events = tag.wait_for_field(timeout=1)
    assert events.field_rising                  # synthesised from FIELD_ON


def test_wait_for_field_times_out_to_none(tag):
    assert tag.wait_for_field(timeout=0.05) is None


def test_waiting_accumulates_rather_than_discards(chip, tag):
    """Every poll clears the register, so a wait that threw away what it read
    would lose the field event while looking for the write."""
    chip.rf_field(True)
    chip.rf_event(IT_RF_WRITE)
    events = tag.wait_for_rf_write(timeout=1)
    assert events.rf_write and events.field_rising


def test_wait_for_rf_write_sees_a_reader_writing(chip, tag):
    tag.format()
    chip.rf_write(8, b"\x03\x0bhello there")
    assert tag.wait_for_rf_write(timeout=1).rf_write


# -- waiting on events ------------------------------------------------------

def test_a_wait_reports_an_event_that_latched_before_the_call(chip, tag):
    """IT_STS_Dyn latches until read, so a tap that already happened satisfies
    the very first poll. Right for "did a reader visit", surprising for
    "wait for the next one"."""
    chip.dynamic[0x05] = IT_FIELD_RISING           # a field came and went
    events = tag.wait_for_field(timeout=0)
    assert events is not None
    assert events.field_rising


def test_drain_makes_a_wait_ignore_what_already_latched(chip, tag):
    chip.dynamic[0x05] = IT_FIELD_RISING
    assert tag.wait_for_field(timeout=0, drain=True) is None
    assert chip.dynamic[0x05] == 0, "the drain read should have cleared it"


def test_drain_still_sees_an_event_that_arrives_during_the_wait():
    """Draining must not blind the wait to what it is actually waiting for."""

    class LatchesLater(FakeST25DV):
        """A phone that turns up on the third read of IT_STS_Dyn."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.status_reads = 0

        def _read_dynamic(self, addr, length):
            if addr == 0x2005:
                self.status_reads += 1
                if self.status_reads > 2:
                    self.dynamic[0x05] |= IT_FIELD_RISING
            return super()._read_dynamic(addr, length)

    chip = LatchesLater(2048)
    chip.dynamic[0x05] = IT_FIELD_RISING          # stale, must be discarded
    tag = ST25DV(chip)
    events = tag.wait_for_field(timeout=5, interval=0, drain=True)
    assert events is not None and events.field_rising
    assert chip.status_reads > 2, "it returned before the new event arrived"


def test_drain_does_not_hide_a_field_resting_on_the_tag(chip, tag):
    """That comes from EH_CTRL_Dyn, not the latch, so draining cannot lose it."""
    chip.dynamic[0x05] = 0
    chip.dynamic[0x02] |= EH_FIELD_ON
    events = tag.wait_for_field(timeout=0, drain=True)
    assert events is not None and events.field_rising


def test_rf_write_and_mailbox_waits_take_drain_too(chip, tag):
    chip.dynamic[0x05] = IT_RF_WRITE | IT_RF_PUT_MSG
    assert tag.wait_for_rf_write(timeout=0, drain=True) is None
    chip.dynamic[0x05] = IT_RF_WRITE | IT_RF_PUT_MSG
    assert tag.wait_for_mailbox(timeout=0, drain=True) is None
