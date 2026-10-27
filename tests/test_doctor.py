# tests/test_doctor.py
import pytest
from delta_plc.doctor import (
    CheckResult, plc_warnings, scan_subnet, subnet_hints, verdict, run_doctor,
)
from delta_plc.mock import MockServer

def test_verdict_priority():
    assert verdict([CheckResult("a", "PASS", "")]) == "PASS"
    assert verdict([CheckResult("a", "PASS", ""), CheckResult("b", "SKIP", "")]) == "PASS"
    assert verdict([CheckResult("a", "WARN", "")]) == "WARN"
    assert verdict([CheckResult("a", "WARN", ""), CheckResult("b", "FAIL", "")]) == "FAIL"

def test_plc_warnings_thresholds():
    base = {"run": True, "wdt_ms": 200, "scan_ms": {"current": 0.4, "min": 0.1, "max": 159.9},
            "error": {"flag": False, "code": 0}, "firmware": "0x7C21"}
    assert plc_warnings(base) == []
    assert plc_warnings({**base, "scan_ms": {**base["scan_ms"], "max": 160.0}})
    assert plc_warnings({**base, "run": False})
    assert plc_warnings({**base, "error": {"flag": True, "code": 5}})
    assert not any("firmware" in w or "0x7C21" in w for w in plc_warnings({**base, "firmware": "0xDEAD"}))

def test_subnet_hints_are_copy_pasteable():
    linux = subnet_hints("192.168.1.5", "enp1s0", profile="PLC-DVP12SE", system="Linux")
    assert any(h.startswith("sudo ip addr add 192.168.1.250/24 dev enp1s0") for h in linux)
    assert any("nmcli connection modify" in h for h in linux)
    win = subnet_hints("192.168.1.250", "Ethernet", system="Windows")
    assert win == ['netsh interface ipv4 add address "Ethernet" 192.168.1.249 255.255.255.0']

@pytest.fixture(scope="module")
def mock():
    server = MockServer(port=0)
    server.start()
    yield server
    server.stop()

async def test_doctor_pass_and_json(mock):
    report = await run_doctor("127.0.0.1", port=mock.address[1], timeout=1)
    assert report.verdict == "PASS" and report.exit_code() == 0
    assert report.plc["wdt_ms"] == 200
    assert report.plc["firmware"] == "0x7C21"
    assert report.plc["scan_ms"] == {"current": 0.4, "min": 0.1, "max": 1.2}
    payload = report.to_dict()
    assert payload["target"] == {"host": "127.0.0.1", "port": mock.address[1], "unit": 1}
    assert {c["name"] for c in payload["checks"]} >= {"tcp502", "modbus", "plc"}

async def test_doctor_warn_on_fault_and_strict():
    server = MockServer(port=0, fault_code=5)
    server.start()
    try:
        report = await run_doctor("127.0.0.1", port=server.address[1], timeout=1)
        assert report.verdict == "WARN"
        assert report.exit_code() == 0 and report.exit_code(strict=True) == 1
    finally:
        server.stop()

async def test_doctor_tcp_failure():
    report = await run_doctor("127.0.0.1", port=1, timeout=0.2)
    assert report.verdict == "FAIL" and report.exit_code() == 1
    assert any("WPLSoft" in h for h in report.hints)

async def test_doctor_passive():
    report = await run_doctor()
    assert report.verdict == "PASS" and report.target is None
    assert report.plc is None
    assert any("--host" in h for h in report.hints)

async def test_scan_opt_in(monkeypatch):
    calls = []
    async def fake_probe(host, port, timeout):
        calls.append(host); return host.endswith(".5")
    monkeypatch.setattr("delta_plc.doctor._probe_tcp", fake_probe)
    hosts = await scan_subnet("192.168.1.0/29")
    assert "192.168.1.5" in hosts and calls
