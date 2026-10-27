# tests/test_transport_detection.py
import pytest

from deltactl.errors import (
    ModelDetectionError,
    ModelMismatchError,
    UnknownModelCodeError,  # noqa: F401
)
from deltactl.mock import MockServer
from deltactl.profiles import (
    PROFILE_ES2_EX2,  # noqa: F401
    PROFILE_SE_EH3_SV2,
    PROFILE_SS2,
)
from deltactl.transport import AsyncDeltaPLC


@pytest.fixture
def mock_se():
    server = MockServer(port=0, seed={"D1001": 0x7C21})
    server.start()
    yield server
    server.stop()


@pytest.fixture
def mock_ss2():
    server = MockServer(port=0, seed={"D1001": 0x7210})
    server.start()
    yield server
    server.stop()


async def test_auto_detection_resolves_profile(mock_se):
    async with AsyncDeltaPLC("127.0.0.1", port=mock_se.address[1], series="auto") as plc:
        assert plc.detected_profile == PROFILE_SE_EH3_SV2
        assert plc.effective_profile == PROFILE_SE_EH3_SV2


async def test_strict_mismatch_aborts_with_zero_payload_frames(mock_se):
    # Configured SS2 on SE hardware under strict_series=True
    plc = AsyncDeltaPLC("127.0.0.1", port=mock_se.address[1], series="SS2", strict_series=True)
    with pytest.raises(ModelMismatchError):
        await plc.connect()
    # Verified: exactly 1 probe request was received by mock, 0 writes
    assert mock_se.stats.requests == 1
    assert mock_se.stats.writes == 0
    await plc.close()


async def test_non_strict_mismatch_clamps_to_intersection(mock_ss2):
    # Configured SE on SS2 hardware: intersection clamps to SS2 bounds (D max 4999)
    async with AsyncDeltaPLC(
        "127.0.0.1", port=mock_ss2.address[1], series="SE", strict_series=False
    ) as plc:
        assert plc.detected_profile == PROFILE_SS2
        assert plc.effective_profile.d_ranges == ((0, 4999),)
        # Reading D4999 succeeds
        await plc.read("D4999")
        # Reading D5000 is blocked client-side by effective profile
        with pytest.raises(Exception):  # noqa: B017
            await plc.read("D5000")


async def test_strict_detection_failure_aborts():
    # Dead port under strict_series=True
    plc = AsyncDeltaPLC("127.0.0.1", port=1, timeout=0.1, series="auto", strict_series=True)
    with pytest.raises(ModelDetectionError):
        await plc.connect()

