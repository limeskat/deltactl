# tests/test_planning.py
import pytest

from deltactl.errors import AddressRangeError, BlockBoundaryError, DeltaValueError
from deltactl.packing import DType
from deltactl.planning import make_tag, make_tags, plan_chunks, plan_reads


def test_gap_merging_boundary():
    one = plan_reads(make_tags(["D100", "D101", "D105"]))
    assert [(g.function, g.start, g.count) for g in one] == [(3, 0x1064, 6)]
    assert len(plan_reads(make_tags(["D100", "D111"]))) == 1
    assert len(plan_reads(make_tags(["D100", "D112"]))) == 2


def test_overlapping_bit_tags_coalesce():
    bits = make_tags([f"D100.{i}" for i in range(16)])
    groups = plan_reads(bits)
    assert [(g.function, g.start, g.count) for g in groups] == [(3, 0x1064, 1)]
    groups = plan_reads(make_tags(["D100", "D100.3"]))
    assert len(groups) == 1 and groups[0].count == 1


def test_overlapping_32bit_tag_extends_group():
    groups = plan_reads(make_tags(["D100", "D100:f32", "D101"]))
    assert len(groups) == 1
    assert (groups[0].start, groups[0].count) == (0x1064, 2)


def test_function_separation():
    groups = plan_reads(make_tags(["D100", "M0", "X0", "Y0"]))
    assert sorted(g.function for g in groups) == [1, 1, 2, 3]


def test_cap_and_no_32bit_split():
    groups = plan_reads(make_tags([f"D{i}" for i in range(101)]))
    assert [g.count for g in groups] == [100, 1]
    groups = plan_reads(make_tags([f"D{i}" for i in range(99)] + ["D99:f32"]))
    assert [g.count for g in groups] == [99, 2]


def test_split_block_not_merged():
    groups = plan_reads(make_tags(["D4094", "D4096"]))
    assert len(groups) == 2
    assert groups[1].start == 0x9000
    groups = plan_reads(make_tags(["D4095", "D4096"]), max_gap=10000)
    assert len(groups) == 2
    groups = plan_reads(make_tags(["M1535", "M1536"]), max_gap=10000)
    assert len(groups) == 2


def test_chunks_whole_values_and_blocks():
    chunks = plan_chunks(make_tag("D0").address, 1, 101)
    assert [(c.first, c.values, c.count) for c in chunks] == [(0, 100, 100), (100, 1, 1)]
    chunks = plan_chunks(make_tag("D0").address, 2, 60)
    assert [c.values for c in chunks] == [50, 10]
    chunks = plan_chunks(make_tag("D4000").address, 1, 200)
    assert [c.values for c in chunks] == [96, 100, 4]
    chunks = plan_chunks(make_tag("D4095").address, 1, 2)
    assert [(c.start, c.count) for c in chunks] == [(0x1FFF, 1), (0x9000, 1)]


def test_chunk_rejects_bad_spans():
    with pytest.raises(BlockBoundaryError):
        plan_chunks(make_tag("D4095").address, 2, 1)
    with pytest.raises(AddressRangeError):
        plan_chunks(make_tag("D11999").address, 1, 2)
    for bad in (0, -1):
        with pytest.raises(DeltaValueError):
            plan_chunks(make_tag("D0").address, 1, bad)


def test_make_tag_defaults_and_conflicts():
    assert make_tag("D100").dtype is DType.U16
    assert make_tag("D100:f32").dtype is DType.F32
    assert make_tag("M0").dtype is DType.BIT
    assert make_tag("D100.3").dtype is DType.BIT
    with pytest.raises(DeltaValueError):
        make_tag("M0", DType.U16)
    with pytest.raises(DeltaValueError):
        make_tag("D100.3", DType.U16)


def test_slot_counters_are_never_merged():
    tags = make_tags(["C200.v", "C201.v"])
    groups = plan_reads(tags)
    assert [(g.start, g.count) for g in groups] == [(0x0EC8, 2), (0x0EC9, 2)]


def test_c16_neighbour_is_not_merged_into_slot():
    tags = make_tags(["C199.v", "C200.v"])
    groups = plan_reads(tags)
    assert len(groups) == 2


def test_plan_chunks_advances_slots_by_index():
    addr = make_tag("C200.v").address
    chunks = plan_chunks(addr, 2, 3)
    assert [(c.start, c.count, c.first, c.values) for c in chunks] == [
        (0x0EC8, 2, 0, 1),
        (0x0EC9, 2, 1, 1),
        (0x0ECA, 2, 2, 1),
    ]


def test_slot_counters_surrounded_by_coalescing_tags():
    tags = make_tags(["C198.v", "C199.v", "C200.v", "C201.v", "D0", "D1"])
    groups = plan_reads(tags)
    assert [(g.start, g.count) for g in groups] == [
        (0x0EC6, 2),
        (0x0EC8, 2),
        (0x0EC9, 2),
        (0x1000, 2),
    ]


def test_slot_chunks_out_of_bounds():
    addr = make_tag("C254.v").address
    with pytest.raises(AddressRangeError):
        plan_chunks(addr, 2, 2)


def test_slot_counters_duplicate_reads():
    tags = make_tags(["C200.v", "C200.v"])
    groups = plan_reads(tags)
    assert [(g.start, g.count) for g in groups] == [(0x0EC8, 2), (0x0EC8, 2)]


def test_address_coalesce_property():
    assert make_tag("C200.v").address.coalesce is False
    assert make_tag("D100").address.coalesce is True
