"""Tests for the public DVP wire map and the mock's runtime setter."""

import asyncio

import pytest
from pymodbus.client import AsyncModbusTcpClient

from deltactl.addressing import Device, parse_tag
from deltactl.dvp_map import (
    D_COUNT,
    D_SEGMENT_SPANS,
    D_WIRE0,
    M_COUNT,
    M_SEGMENT_SPANS,
    M_WIRE0,
    PDU_CAPS,
    READ_RANGES,
    WRITE_GUARDS,
    X_WIRE0,
    XY_COUNT,
    Y_WIRE0,
    function_code_for,
    wire_for,
)
from deltactl.mock import MockServer


def test_wire_map_matches_phase_one():
    assert wire_for(Device.D, 500) == 0x11F4
    assert wire_for(Device.D, 4096) == 0x9000
    assert wire_for(Device.M, 1000) == 0x0BE8
    assert wire_for(Device.X, 0o17) == 0x040F
    assert wire_for(Device.Y, 0o7) == 0x0507


def test_counts_and_caps():
    assert D_COUNT == 12000
    assert M_COUNT == 4096
    assert PDU_CAPS[3] == 100
    assert READ_RANGES[3][0] == (0x1000, 0x1FFF)


def test_wire_anchors_segments_and_guards():
    assert (D_WIRE0, M_WIRE0, X_WIRE0, Y_WIRE0) == (0x1000, 0x0800, 0x0400, 0x0500)
    assert XY_COUNT == 0o400
    assert D_SEGMENT_SPANS[0] == (0, 1000, False)
    assert D_SEGMENT_SPANS[-1] == (4096, 12000, False)
    assert M_SEGMENT_SPANS[-1] == (1536, 4096, False)
    assert WRITE_GUARDS[16] == ((0x13E8, 0x13EA), (0x13F2, 0x13F4), (0x142B, 0x142B))


def test_function_code_for_matches_read_map():
    assert function_code_for(Device.D) == 3
    assert function_code_for(Device.X) == 2
    assert function_code_for(Device.M) == 1
    assert function_code_for(Device.Y) == 1


async def _read(server, tag):
    """Read one tag from a running mock with the deltactl tag grammar."""
    address = parse_tag(tag).address
    client = AsyncModbusTcpClient("127.0.0.1", port=server.address[1], timeout=1, retries=0)
    assert await client.connect()
    try:
        if address.device is Device.D:
            response = await client.read_holding_registers(address.wire, count=1, device_id=1)
            return response.registers[0]
        if address.device is Device.X:
            response = await client.read_discrete_inputs(address.wire, count=1, device_id=1)
            return response.bits[0]
        response = await client.read_coils(address.wire, count=1, device_id=1)
        return response.bits[0]
    finally:
        client.close()


def test_set_values_updates_mock():
    server = MockServer(port=0)
    server.start()
    try:
        server.set_values("D500", 39)
        assert asyncio.run(_read(server, "D500")) == 39
    finally:
        server.stop()


def test_set_values_bit_devices():
    server = MockServer(port=0)
    server.start()
    try:
        server.set_values("M100", True)
        server.set_values("Y7", True)
        assert asyncio.run(_read(server, "M100")) is True
        assert asyncio.run(_read(server, "Y7")) is True
        server.set_values("M100", False)
        assert asyncio.run(_read(server, "M100")) is False
    finally:
        server.stop()


def test_set_values_leaves_counters_alone():
    server = MockServer(port=0)
    server.start()
    try:
        before = (server.stats.requests, server.stats.writes)
        server.set_values("D500", 1)
        assert (server.stats.requests, server.stats.writes) == before
    finally:
        server.stop()


def test_set_values_seeds_guarded_bits_but_not_guarded_registers():
    server = MockServer(port=0)
    server.start()
    try:
        server.set_values("M1000", False)
        server.set_values("D1000", 111)
        assert asyncio.run(_read(server, "M1000")) is False
        assert asyncio.run(_read(server, "D1000")) == 200  # read-only, unchanged
    finally:
        server.stop()


def test_set_values_requires_running_server():
    server = MockServer(port=0)
    with pytest.raises(RuntimeError, match="not running"):
        server.set_values("D500", 1)


def test_set_values_rejects_suffixed_tags():
    server = MockServer(port=0)
    server.start()
    try:
        with pytest.raises(ValueError, match="bare tag"):
            server.set_values("D500.1", 1)
        with pytest.raises(ValueError, match="bare tag"):
            server.set_values("D500:f32", 1)
    finally:
        server.stop()


def test_set_values_rejects_out_of_range_register():
    server = MockServer(port=0)
    server.start()
    try:
        with pytest.raises(ValueError, match="0..65535"):
            server.set_values("D500", 70000)
    finally:
        server.stop()
