import pytest

from deltactl.addressing import parse_tag
from deltactl.errors import BlockBoundaryError
from deltactl.planning import plan_chunks
from deltactl.profiles import PROFILE_ES_EX_SS, PROFILE_SA_SX_SC, PROFILE_SS2


def test_split_block_planning():
    tag_d = parse_tag("D4095", profile=PROFILE_SS2)
    chunks = plan_chunks(tag_d, value_width=1, value_count=2, profile=PROFILE_SS2)
    assert len(chunks) == 2
    assert chunks[0].start == 0x1FFF and chunks[1].start == 0x9000

    # 32-bit spanning D4095-D4096 raises BlockBoundaryError
    with pytest.raises(BlockBoundaryError):
        plan_chunks(tag_d, value_width=2, value_count=1, profile=PROFILE_SS2)


def test_sa_sx_sc_split_planning():
    tag_m = parse_tag("M1535", profile=PROFILE_SA_SX_SC)
    chunks = plan_chunks(tag_m, value_width=1, value_count=2, profile=PROFILE_SA_SX_SC)
    assert len(chunks) == 2
    assert chunks[0].start == 0x0DFF and chunks[1].start == 0xB000


def test_single_block_no_split():
    tag = parse_tag("D500", profile=PROFILE_ES_EX_SS)
    chunks = plan_chunks(tag, value_width=1, value_count=10, profile=PROFILE_ES_EX_SS)
    assert len(chunks) == 1
