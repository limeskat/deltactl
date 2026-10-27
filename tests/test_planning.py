# tests/test_planning.py
import pytest

from delta_plc.errors import AddressRangeError, BlockBoundaryError, DeltaValueError
from delta_plc.packing import DType
from delta_plc.planning import make_tag, make_tags, plan_chunks, plan_reads


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
