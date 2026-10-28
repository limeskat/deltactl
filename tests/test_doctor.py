# tests/test_doctor.py
import pytest

from deltactl.doctor import (
    CheckResult,
    _check_icmp_sync,
    _check_interface_sync,
    plc_warnings,
    run_doctor,
    scan_subnet,
    subnet_hints,
    verdict,
)
from deltactl.mock import MockServer


def test_verdict_priority():
    assert verdict([CheckResult("a", "PASS", "")]) == "PASS"
    assert verdict([CheckResult("a", "PASS", ""), CheckResult("b", "SKIP", "")]) == "PASS"
    assert verdict([CheckResult("a", "WARN", "")]) == "WARN"
    assert verdict([CheckResult("a", "WARN", ""), CheckResult("b", "FAIL", "")]) == "FAIL"


def test_plc_warnings_thresholds():
    base = {
        "run": True,
        "wdt_ms": 200,
        "scan_ms": {"current": 0.4, "min": 0.1, "max": 159.9},
        "error": {"flag": False, "code": 0},
        "firmware": "0x7C21",
    }
    assert plc_warnings(base) == []
    assert plc_warnings({**base, "scan_ms": {**base["scan_ms"], "max": 160.0}})
    assert plc_warnings({**base, "run": False})
    assert plc_warnings({**base, "error": {"flag": True, "code": 5}})
    assert not any(
        "firmware" in w or "0x7C21" in w for w in plc_warnings({**base, "firmware": "0xDEAD"})
    )


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
    assert report.plc["station_id"] == 1
    payload = report.to_dict()
    assert payload["target"] == {"host": "127.0.0.1", "port": mock.address[1], "unit": 1}
    assert {c["name"] for c in payload["checks"]} >= {"tcp502", "modbus", "plc"}
    plc_check = next(c for c in report.checks if c.name == "plc")
    assert "station ID 1" in plc_check.detail


async def test_doctor_custom_station_id():
    server = MockServer(port=0, station_id=42)
    server.start()
    try:
        report = await run_doctor("127.0.0.1", port=server.address[1], timeout=1)
        assert report.verdict == "PASS"
        assert report.plc is not None
        assert report.plc["station_id"] == 42
    finally:
        server.stop()


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
        calls.append(host)
        return host.endswith(".5")

    monkeypatch.setattr("deltactl.doctor._probe_tcp", fake_probe)
    hosts = await scan_subnet("192.168.1.0/29")
    assert "192.168.1.5" in hosts and calls


async def test_doctor_passive_check_set():
    report = await run_doctor()
    names = {c.name for c in report.checks}
    assert names == {"interface", "address", "arp"}


def test_powershell_parameter_binding(monkeypatch):
    recorded_cmds = []

    class FakeResult:
        returncode = 0
        stdout = "Ethernet0\n"

    def fake_run(cmd, *args, **kwargs):
        recorded_cmds.append(list(cmd))
        return FakeResult()

    monkeypatch.setattr("deltactl.doctor.platform.system", lambda: "Windows")
    monkeypatch.setattr("deltactl.doctor.subprocess.run", fake_run)

    _res, dev = _check_interface_sync("192.168.1.50; injection")
    assert dev == "Ethernet0"
    assert len(recorded_cmds) == 2

    route_cmd = recorded_cmds[0]
    assert "192.168.1.50; injection" not in route_cmd[3]
    assert route_cmd[-1] == "192.168.1.50; injection"
    assert "param($addr)" in route_cmd[3]

    adapter_cmd = recorded_cmds[1]
    assert "Ethernet0" not in adapter_cmd[3]
    assert adapter_cmd[-1] == "Ethernet0"
    assert "param($name)" in adapter_cmd[3]


def test_interface_undeterminable_returns_skip(monkeypatch):
    monkeypatch.setattr("deltactl.doctor.platform.system", lambda: "Linux")

    class RouteResult:
        returncode = 0
        stdout = "192.168.1.50 dev eth0 src 192.168.1.1\n"

    monkeypatch.setattr("deltactl.doctor.subprocess.run", lambda *a, **kw: RouteResult())
    res, dev = _check_interface_sync("192.168.1.50")
    assert dev == "eth0"
    assert res.status == "SKIP"
    assert "cannot determine link carrier for eth0" in res.detail


def test_icmp_fallback_timeout_returns_warn(monkeypatch):
    import subprocess

    monkeypatch.setattr("deltactl.doctor.platform.system", lambda: "Linux")

    first_call = True

    def fake_run(cmd, *args, **kwargs):
        nonlocal first_call
        if first_call:
            first_call = False

            class InitialResult:
                returncode = 2
                stdout = ""

            return InitialResult()
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr("deltactl.doctor.subprocess.run", fake_run)
    res = _check_icmp_sync("192.168.1.50")
    assert res.status == "WARN"
    assert "ping timed out" in res.detail or "0/3 replies" in res.detail


def test_subnet_hints_matrix():
    h1 = subnet_hints("192.168.1.5", "eth0", system="Linux", profile="Wired")
    assert any("ip addr add" in h for h in h1)
    assert any("nmcli" in h for h in h1)

    h2 = subnet_hints("192.168.1.250", "eth0", system="Linux")
    assert "192.168.1.249" in h2[0]

    h3 = subnet_hints("192.168.1.5", "Ethernet", system="Windows")
    assert "netsh" in h3[0]

    h4 = subnet_hints("plc.local", "eth0", system="Linux")
    assert "plc.local/24" in h4[0]


async def test_probe_tcp_and_invalid_scan(mock):
    from deltactl.doctor import _probe_tcp

    assert await _probe_tcp("127.0.0.1", mock.address[1], timeout=1.0) is True
    assert await _probe_tcp("127.0.0.1", 1, timeout=0.1) is False
    assert await scan_subnet("not-a-cidr") == []
