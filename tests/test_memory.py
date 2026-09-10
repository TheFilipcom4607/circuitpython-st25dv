"""User memory: chunking, page timing, area boundaries and NACK retries."""
import pytest

from fake_st25dv import FakeST25DV
from st25dv import (Area, AreaError, BusyError, MailboxError, ProtectedError,
                    SessionRequired, ST25DV)


@pytest.fixture
def chip():
    return FakeST25DV(2048)


@pytest.fixture
def tag(chip):
    return ST25DV(chip)


def writes(chip):
    return [entry for entry in chip.log if entry[0] == "write"]


def reads(chip):
    return [entry for entry in chip.log if entry[0] == "read"]


# -- identity ---------------------------------------------------------------

@pytest.mark.parametrize("capacity,part,blocks", [
    (512, "ST25DV04K-IE", 128),
    (2048, "ST25DV16K-IE", 512),
    (8192, "ST25DV64K-IE", 2048),
])
def test_capacity_comes_from_mem_size_not_ic_ref(capacity, part, blocks):
    """IC_REF is 0x26 on both the 16K and the 64K, so it cannot size the part.
    MEM_SIZE and BLK_SIZE can."""
    tag = ST25DV(FakeST25DV(capacity))
    assert tag.memory_size == capacity
    assert tag.block_count == blocks
    assert tag.block_size == 4
    assert tag.part == part
    assert len(tag) == capacity


def test_uid_reads_back_most_significant_byte_first(tag):
    assert tag.uid[:2] == b"\xe0\x02"
    assert tag.uid_hex.startswith("e0:02:")


def test_a_bus_with_nothing_on_it_is_reported_as_such():
    import busio
    with pytest.raises(Exception) as err:
        ST25DV(busio.I2C())
    assert "no ST25DV" in str(err.value) or "NotFound" in type(err.value).__name__


# -- chunking ---------------------------------------------------------------

def test_write_is_split_at_256_bytes(chip, tag):
    chip.log.clear()
    tag.write(0, bytes(range(256)) * 3)
    lengths = [len(entry[3]) for entry in writes(chip)]
    assert lengths == [256, 256, 256]


def test_the_datasheet_worked_example_for_page_timing(chip, tag):
    """Section 6.4.2: 256 bytes starting at 0x0002 spans 65 pages, tW x 65.

    The datasheet picks a deliberately awkward example, and it doubles as
    proof that an unaligned 256-byte write is allowed at all: the byte address
    counter increments rather than wrapping inside an aligned buffer, so the
    chunker in write() has no reason to split on 256-byte alignment.
    """
    chip.programmed.clear()
    tag.write(0x0002, bytes(256))
    assert chip.programmed == [65]


def test_write_respects_page_timing(chip, tag):
    """tW is charged per 4-byte page touched, partial ones included, so a
    5-byte write starting at 2 costs two pages, not one."""
    chip.write_busy_ticks = 4
    chip.log.clear()
    tag.write(2, b"12345")
    assert chip._busy == 0                    # the driver waited it out
    # Two pages at four ticks each, so eight transfers are absorbed by the
    # write cycle before the chip answers again.
    assert len(chip.log) >= 1 + 2 * 4
    assert tag.read(2, 5) == b"12345"


def test_read_is_split_at_area_boundaries(chip, tag):
    tag.open_session()
    tag.set_areas([1024, 1024])
    chip.log.clear()
    tag.read(1000, 100)
    addresses = [entry[2] for entry in reads(chip)]
    assert 1024 in addresses                  # a fresh read starts at the seam


def test_read_across_areas_returns_the_real_bytes_not_ff(chip, tag):
    """A single sequential read would return 0xFF past the seam. Chunking is
    what makes the crossing invisible to the caller."""
    tag.open_session()
    tag.set_areas([1024, 1024])
    chip.user[1020:1028] = b"ABCDEFGH"
    assert tag.read(1020, 8) == b"ABCDEFGH"


def test_write_across_areas_is_split_rather_than_refused(chip, tag):
    tag.open_session()
    tag.set_areas([1024, 1024])
    chip.log.clear()
    tag.write(1020, b"ABCDEFGH")
    assert bytes(chip.user[1020:1028]) == b"ABCDEFGH"
    assert [len(entry[3]) for entry in writes(chip)] == [4, 4]


def test_reading_past_the_end_is_refused_before_the_bus(tag):
    with pytest.raises(ValueError) as err:
        tag.read(2040, 16)
    assert "no rollover" in str(err.value)


def test_writing_past_the_end_is_refused_before_the_bus(tag):
    with pytest.raises(ValueError):
        tag.write(2047, b"ab")


# -- the indexing sugar -----------------------------------------------------

def test_single_byte_indexing(tag):
    tag[100] = 0x5A
    assert tag[100] == 0x5A
    assert tag[-1] == tag[len(tag) - 1]


def test_slice_round_trip(tag):
    tag[10:14] = b"abcd"
    assert tag[10:14] == b"abcd"


def test_slice_length_must_match(tag):
    with pytest.raises(ValueError) as err:
        tag[10:14] = b"abcde"
    assert "fixed-size memory" in str(err.value)


def test_open_ended_and_negative_slices(tag):
    tag.write(0, b"abcd")
    tag.write(len(tag) - 4, b"wxyz")
    assert tag[:4] == b"abcd"
    assert tag[-4:] == b"wxyz"
    assert len(tag[:]) == len(tag)
    assert tag[10:5] == b""


def test_stepped_slices_are_refused(tag):
    with pytest.raises(ValueError):
        _ = tag[0:10:2]


def test_dump_lines_look_like_a_hex_dump(chip, tag):
    chip.user[0:4] = b"ABCD"
    line = tag.dump(0, 16)[0]
    assert line.startswith("0000  41 42 43 44")
    assert line.endswith("ABCD............")


# -- retries ----------------------------------------------------------------

def test_a_transient_nack_is_retried_not_raised(chip, tag):
    """A NACK usually means the RF side got in first, so it is worth another
    go rather than an exception."""
    chip.nack_next = 5
    assert tag.read(0, 4) == bytes(chip.user[0:4])


def test_a_chip_that_never_answers_raises_busy(chip, tag):
    chip.rf_busy = True
    tag.busy_timeout = 0.05
    with pytest.raises(BusyError) as err:
        tag.read(0, 4)
    assert "RF reader" in str(err.value)


def test_a_write_that_never_lands_says_why(chip, tag):
    chip.rf_busy = True
    tag.busy_timeout = 0.05
    with pytest.raises(BusyError):
        tag.write(0, b"x")


def test_a_write_refused_by_protection_is_not_reported_as_busy(chip, tag):
    """The point of the diagnosis step: a protected write and a busy chip look
    identical on the wire, and only one of them is worth retrying."""
    tag.open_session()
    tag.i2c_protection = 0x01                 # area 1 needs the session to write
    tag.close_session()
    tag.busy_timeout = 0.05
    with pytest.raises(ProtectedError) as err:
        tag.write(0, b"x")
    assert "write protected" in str(err.value)


def test_writes_while_the_mailbox_is_on_are_named(chip, tag):
    tag.open_session()
    tag.mailbox.allowed = True
    tag.mailbox.enable()
    with pytest.raises(MailboxError) as err:
        tag.write(0, b"x")
    assert "fast transfer mode" in str(err.value)


def test_the_mailbox_check_can_be_turned_off(chip, tag):
    """Skipping the check saves a register read per write; it costs the clear
    message, and the chip still refuses the write."""
    tag.open_session()
    tag.mailbox.allowed = True
    tag.mailbox.enable()
    tag.check_fast_transfer = False
    tag.busy_timeout = 0.05
    with pytest.raises(MailboxError):         # diagnosed after the fact
        tag.write(0, b"x")


# -- areas ------------------------------------------------------------------

def test_factory_layout_is_one_area(tag):
    assert tag.areas == (Area(1, 0, 2047),)


def test_splitting_into_four(chip, tag):
    tag.open_session()
    tag.areas = [512, 512, 512, 512]
    assert [area.size for area in tag.areas] == [512, 512, 512, 512]
    assert tag.areas[0].start == 0 and tag.areas[3].end == 2047


def test_area_sizes_must_fill_memory(tag):
    tag.open_session()
    with pytest.raises(AreaError) as err:
        tag.areas = [512, 512]
    assert "fill user memory exactly" in str(err.value)


def test_area_sizes_must_be_multiples_of_32(tag):
    tag.open_session()
    with pytest.raises(AreaError):
        tag.areas = [500, 1548]


def test_areas_need_the_session(tag):
    with pytest.raises(SessionRequired):
        tag.areas = [1024, 1024]


def test_successors_are_raised_before_limits_are_lowered(chip, tag):
    """The chip NACKs an ENDAi write whose successor is not already at the end
    of memory, so the driver has to walk them in the right order."""
    tag.open_session()
    tag.areas = [512, 512, 512, 512]
    chip.log.clear()
    tag.areas = [1024, 1024]
    order = [entry[2] for entry in writes(chip) if entry[2] in (5, 7, 9)]
    assert order == [9, 7, 5]                 # ENDA3, ENDA2, then ENDA1


def test_shrinking_area_one_alone_still_works(chip, tag):
    tag.open_session()
    tag.areas = [1024, 1024]
    tag.areas = [32, 2016]
    assert tag.areas[0].size == 32


def test_area_limits_may_not_decrease(tag):
    tag.open_session()
    with pytest.raises(AreaError):
        tag.set_area_limits(0x20, 0x10, 0x3F)


def test_area_two_needs_room_below_it(tag):
    tag.open_session()
    with pytest.raises(AreaError) as err:
        tag.set_area_limits(0x20, 0x20, 0x3F)
    assert "strictly below" in str(err.value)


def test_area_limit_past_end_of_memory_is_refused(tag):
    tag.open_session()
    with pytest.raises(AreaError):
        tag.set_area_limits(0x40, 0x40, 0x40)
