"""Doctor subsystem: network diagnostics, PLC health interpretation, and subnet hints."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import os
import platform
import re
import socket
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from deltactl.errors import UnknownModelCodeError
from deltactl.profiles import decode_d1001, get_profile
from deltactl.transport import AsyncDeltaPLC


@dataclass
class CheckResult:
    """Individual diagnostic check result."""

    name: str
    status: str
    detail: str
    latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert check result to dictionary matching JSON schema."""
        d: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }
        if self.latency_ms is not None:
            d["latency_ms"] = self.latency_ms
        return d


@dataclass
class DoctorReport:
    """Full doctor diagnostic report."""

    ts: str
    target: dict[str, Any] | None
    verdict: str
    checks: list[CheckResult]
    plc: dict[str, Any] | None
    hints: list[str]

    def exit_code(self, strict: bool = False) -> int:
        """Return process exit code based on report verdict."""
        if self.verdict == "FAIL":
            return 1
        if self.verdict == "WARN" and strict:
            return 1
        return 0

    def to_dict(self) -> dict[str, Any]:
        """Convert doctor report to dictionary matching JSON schema."""
        return {
            "ts": self.ts,
            "target": self.target,
            "verdict": self.verdict,
            "checks": [c.to_dict() for c in self.checks],
            "plc": self.plc,
            "hints": list(self.hints),
        }


def verdict(checks: Sequence[CheckResult], *, strict: bool = False) -> str:
    """Compute overall verdict from individual check results.

    FAIL > WARN > PASS. SKIP results are ignored.
    """
    statuses = {c.status for c in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


def plc_warnings(plc: dict[str, Any]) -> list[str]:
    """Inspect interpreted PLC diagnostic values and return warnings.

    Order: run, error, scan. Firmware (including 0x7C21) never produces a warning.
    """
    warnings: list[str] = []

    # 1. run mode
    if not plc.get("run", True):
        warnings.append("PLC is in STOP mode (M1000 is OFF)")

    # 2. error flag
    err = plc.get("error")
    if isinstance(err, dict) and err.get("flag", False):
        code = err.get("code", 0)
        warnings.append(f"PLC error flag active (M1067 ON, fault code {code})")

    # 3. scan time vs watchdog
    wdt_ms = plc.get("wdt_ms")
    scan_ms = plc.get("scan_ms")
    if isinstance(wdt_ms, (int, float)) and isinstance(scan_ms, dict):
        max_scan = scan_ms.get("max")
        if isinstance(max_scan, (int, float)) and max_scan >= 0.8 * wdt_ms:
            warnings.append(f"Max scan time ({max_scan} ms) >= 80% of watchdog ({wdt_ms} ms)")

    return warnings


def subnet_hints(
    target: str,
    interface: str,
    *,
    profile: str | None = None,
    system: str = platform.system(),
) -> list[str]:
    """Generate copy-pasteable network configuration commands for subnet mismatches."""
    parts = target.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        prefix = f"{parts[0]}.{parts[1]}.{parts[2]}"
        alias = f"{prefix}.249" if parts[3] == "250" else f"{prefix}.250"
    else:
        alias = target

    if system == "Windows":
        return [f'netsh interface ipv4 add address "{interface}" {alias} 255.255.255.0']

    hints = [f"sudo ip addr add {alias}/24 dev {interface}"]
    if profile is not None:
        hints.append(f'nmcli connection modify "{profile}" +ipv4.addresses {alias}/24')
        hints.append(f'nmcli connection up "{profile}"')
    return hints


async def _probe_tcp(host: str, port: int, timeout: float) -> bool:
    """Test TCP port availability on a target host."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    except Exception:  # noqa: BLE001
        return False


async def scan_subnet(
    cidr: str,
    *,
    port: int = 502,
    timeout: float = 0.2,
    concurrency: int = 64,
) -> list[str]:
    """Scan a CIDR block for hosts with open TCP ports."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
        targets = [str(h) for h in net.hosts()] or [str(net.network_address)]
    except ValueError:
        return []

    sem = asyncio.Semaphore(concurrency)

    async def _worker(ip_str: str) -> str | None:
        async with sem:
            ok = await _probe_tcp(ip_str, port, timeout)
            if ok:
                return ip_str
            return None

    tasks = [_worker(ip_str) for ip_str in targets]
    results = await asyncio.gather(*tasks)
    found = [ip for ip in results if ip is not None]
    return sorted(found, key=lambda ip: ipaddress.ip_address(ip))


def _check_interface_sync(target: str | None) -> tuple[CheckResult, str | None]:
    """Check physical link and routing interface synchronously."""
    system = platform.system()
    if target is None:
        # Passive mode: list available interfaces
        if system == "Linux":
            try:
                ifaces = os.listdir("/sys/class/net")
                summaries: list[str] = []
                for iface in sorted(ifaces):
                    state = "unknown"
                    try:
                        with open(f"/sys/class/net/{iface}/operstate", encoding="utf-8") as f:
                            state = f.read().strip()
                    except OSError:
                        pass
                    summaries.append(f"{iface}: {state}")
                if summaries:
                    return (
                        CheckResult("interface", "PASS", f"Interfaces: {', '.join(summaries)}"),
                        None,
                    )
            except OSError:
                pass
            return CheckResult("interface", "PASS", "No network interfaces listed"), None
        if system == "Windows":
            try:
                cmd = [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-NetAdapter | Select-Object -ExpandProperty Name",
                ]
                res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
                names = [line.strip() for line in res.stdout.splitlines() if line.strip()]
                if names:
                    return CheckResult("interface", "PASS", f"Adapters: {', '.join(names)}"), None
            except (subprocess.SubprocessError, OSError):
                pass
            return CheckResult("interface", "SKIP", "cannot list network adapters on Windows"), None
        return CheckResult("interface", "PASS", "Passive interface check"), None

    # Host mode: find routing interface
    dev: str | None = None
    if system == "Linux":
        try:
            res = subprocess.run(
                ["ip", "route", "get", target],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if res.returncode == 0:
                m = re.search(r"\bdev\s+(\S+)", res.stdout)
                if m:
                    dev = m.group(1)
        except (subprocess.SubprocessError, OSError):
            pass
    elif system == "Windows":
        try:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "& { param($addr) Find-NetRoute -RemoteIPAddress $addr | Select-Object -ExpandProperty InterfaceAlias }",
                target,
            ]
            res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
            for line in res.stdout.splitlines():
                if line.strip():
                    dev = line.strip()
                    break
        except (subprocess.SubprocessError, OSError):
            pass

    if not dev:
        return CheckResult("interface", "SKIP", "cannot determine routing interface"), None

    # Inspect carrier/state
    if system == "Linux":
        try:
            with open(f"/sys/class/net/{dev}/carrier", encoding="utf-8") as f:
                carrier = f.read().strip()
            if carrier == "0":
                return (
                    CheckResult("interface", "FAIL", f"Interface {dev} has carrier=0 (link down)"),
                    dev,
                )
            if carrier == "1":
                return (
                    CheckResult("interface", "PASS", f"Interface {dev} has carrier=1 (link up)"),
                    dev,
                )
        except OSError:
            pass

        try:
            with open(f"/sys/class/net/{dev}/operstate", encoding="utf-8") as f:
                operstate = f.read().strip()
            if operstate == "down":
                return CheckResult("interface", "FAIL", f"Interface {dev} is down"), dev
            return CheckResult("interface", "PASS", f"Interface {dev} state is {operstate}"), dev
        except OSError:
            pass
        return CheckResult("interface", "SKIP", f"cannot determine link carrier for {dev}"), dev
    if system == "Windows":
        try:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "& { param($name) Get-NetAdapter -Name $name | Select-Object -ExpandProperty Status }",
                dev,
            ]
            res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
            status_out = res.stdout.strip().lower()
            if status_out in ("disabled", "disconnected"):
                return (
                    CheckResult(
                        "interface", "FAIL", f"Interface {dev} status: {res.stdout.strip()}"
                    ),
                    dev,
                )
            if status_out:
                return (
                    CheckResult(
                        "interface", "PASS", f"Interface {dev} status: {res.stdout.strip()}"
                    ),
                    dev,
                )
        except (subprocess.SubprocessError, OSError):
            pass
        return CheckResult("interface", "SKIP", f"cannot determine link carrier for {dev}"), dev

    return CheckResult("interface", "SKIP", f"cannot determine carrier on {system}"), dev


def _check_address_sync(
    target: str | None, _detected_interface: str | None
) -> tuple[CheckResult, bool]:
    """Check IP configuration and subnet containment synchronously."""
    system = platform.system()
    networks: list[ipaddress.IPv4Network] = []

    if system == "Linux":
        try:
            res = subprocess.run(
                ["ip", "-o", "-4", "addr", "show"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)", line)
                    if m:
                        try:
                            networks.append(ipaddress.IPv4Network(m.group(1), strict=False))
                        except ValueError:
                            pass
        except (subprocess.SubprocessError, OSError):
            pass
    elif system == "Windows":
        try:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress, PrefixLength | ConvertTo-Json",
            ]
            res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    ip = item.get("IPAddress")
                    prefix = item.get("PrefixLength")
                    if ip and prefix:
                        try:
                            networks.append(ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False))
                        except ValueError:
                            pass
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            pass

    if target is None:
        if networks:
            net_strs = [str(n) for n in networks]
            return CheckResult("address", "PASS", f"Local subnets: {', '.join(net_strs)}"), False
        return CheckResult("address", "SKIP", "no local subnets discovered"), False

    src_ip = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target, 80))
            src_ip = s.getsockname()[0]
    except OSError:
        pass

    if not networks:
        return CheckResult("address", "SKIP", "unable to determine local subnets"), False

    try:
        tgt_ip = ipaddress.ip_address(target)
    except ValueError as e:
        return CheckResult("address", "SKIP", f"invalid target IP: {e}"), False

    in_local = any(tgt_ip in net for net in networks)
    if in_local:
        detail = f"Target {target} within local subnet"
        if src_ip:
            detail += f" (source IP {src_ip})"
        return CheckResult("address", "PASS", detail), False

    detail = f"Target {target} is outside local subnets"
    if src_ip:
        detail += f" (source IP {src_ip})"
    return CheckResult("address", "WARN", detail), True


def _check_icmp_sync(target: str | None) -> CheckResult:
    """Run ICMP ping probes synchronously."""
    if target is None:
        return CheckResult("icmp", "SKIP", "no target host specified")

    is_windows = platform.system() == "Windows"
    if is_windows:
        cmd = ["ping", "-n", "3", "-w", "1000", target]
    else:
        cmd = ["ping", "-c", "3", "-i", "0.2", "-W", "1", target]

    try:
        res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return CheckResult("icmp", "SKIP", "ping binary not found")
    except subprocess.TimeoutExpired:
        return CheckResult("icmp", "WARN", "ping timed out (0/3 replies)")
    except OSError as e:
        return CheckResult("icmp", "SKIP", f"ping error: {e}")

    # Fallback if -i 0.2 was rejected by ping implementation
    if not is_windows and res.returncode == 2 and "-i" in cmd:
        try:
            cmd = ["ping", "-c", "3", "-W", "1", target]
            res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            return CheckResult("icmp", "WARN", "ping timed out (0/3 replies)")
        except (subprocess.SubprocessError, OSError) as e:
            return CheckResult("icmp", "SKIP", f"ping error: {e}")

    stdout = res.stdout
    received = 0
    m_recv = re.search(r"(\d+)\s+(?:packets\s+)?received", stdout, re.IGNORECASE)
    if not m_recv:
        m_recv = re.search(r"Received\s*=\s*(\d+)", stdout, re.IGNORECASE)
    if m_recv:
        received = int(m_recv.group(1))

    latency_ms: float | None = None
    m_lat = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", stdout)
    if not m_lat:
        m_lat = re.search(r"Average\s*=\s*(\d+)ms", stdout, re.IGNORECASE)
    if m_lat:
        try:
            latency_ms = float(m_lat.group(1))
        except ValueError:
            pass

    if received > 0:
        if latency_ms is not None:
            detail = f"{received}/3 replies, avg {latency_ms:.2f} ms"
        else:
            detail = f"{received}/3 replies"
        return CheckResult("icmp", "PASS", detail, latency_ms=latency_ms)
    return CheckResult("icmp", "WARN", "0/3 replies")


def _check_arp_sync() -> CheckResult:
    """Inspect local ARP/neighbor cache for Delta PLC MAC addresses."""
    delta_ouis = {"5C:8D:E5", "00:18:23", "00:06:6E", "20:F8:5E"}
    norm_ouis = {oui.lower().replace("-", ":") for oui in delta_ouis}

    cmd = ["ip", "neigh", "show"] if platform.system() != "Windows" else ["arp", "-a"]
    try:
        res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        try:
            res = subprocess.run(
                ["arp", "-a"], check=False, capture_output=True, text=True, timeout=5
            )
        except (subprocess.SubprocessError, OSError):
            return CheckResult("arp", "SKIP", "ARP tool not available")
    except (subprocess.SubprocessError, OSError) as e:
        return CheckResult("arp", "SKIP", f"ARP check failed: {e}")

    stdout = res.stdout
    mac_pattern = re.compile(
        r"([0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-]"
        r"[0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2})"
    )
    matches: list[str] = []
    for line in stdout.splitlines():
        m = mac_pattern.search(line)
        if m:
            mac = m.group(1)
            mac_norm = mac.lower().replace("-", ":")
            if any(mac_norm.startswith(prefix) for prefix in norm_ouis):
                matches.append(line.strip())

    if matches:
        return CheckResult("arp", "PASS", f"Found Delta PLC: {'; '.join(matches)}")
    return CheckResult("arp", "PASS", "No Delta PLC MAC found in ARP cache")


def _get_nmcli_profile(interface: str) -> str | None:
    """Detect active NetworkManager connection profile name for an interface."""
    if platform.system() != "Linux":
        return None
    try:
        res = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[1].strip() == interface:
                    return parts[0].strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return None


async def _check_tcp502(host: str, port: int, timeout: float) -> CheckResult:
    """Probe TCP port 502 connection status."""
    start = time.perf_counter()
    connect_timeout = min(3.0, timeout) if timeout > 0 else 3.0
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=connect_timeout,
        )
        latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            pass
        return CheckResult(
            "tcp502",
            "PASS",
            f"Connected to {host}:{port} in {latency_ms} ms",
            latency_ms=latency_ms,
        )
    except (OSError, asyncio.TimeoutError) as e:
        return CheckResult("tcp502", "FAIL", f"Connection to {host}:{port} failed: {e}")
    except Exception as e:  # noqa: BLE001
        return CheckResult("tcp502", "FAIL", f"Connection to {host}:{port} failed: {e}")


async def _check_modbus_and_plc(
    host: str,
    port: int,
    unit: int,
    timeout: float,
    series: str = "auto",
) -> tuple[CheckResult, CheckResult, CheckResult, dict[str, Any] | None]:
    """Perform Modbus diagnostic reads and interpret PLC state."""
    start = time.perf_counter()
    tags = [
        "M1000",
        "M1067",
        "D1000",
        "D1001",
        "D1002",
        "D1010",
        "D1011",
        "D1012",
        "D1067",
        "D1121",
    ]
    try:
        async with AsyncDeltaPLC(host, port=port, unit=unit, timeout=timeout) as plc_client:
            data = await plc_client.read_many(tags)
        latency_ms = round((time.perf_counter() - start) * 1000.0, 2)

        raw_d1001 = int(data["D1001"])
        plc: dict[str, Any] = {
            "run": bool(data["M1000"]),
            "station_id": int(data["D1121"]),
            "firmware": f"0x{raw_d1001:04X}",
            "capacity_steps": int(data["D1002"]),
            "wdt_ms": int(data["D1000"]),
            "scan_ms": {
                "current": round(float(data["D1010"]) / 10.0, 1),
                "min": round(float(data["D1011"]) / 10.0, 1),
                "max": round(float(data["D1012"]) / 10.0, 1),
            },
            "error": {
                "flag": bool(data["M1067"]),
                "code": int(data["D1067"]),
            },
        }
        modbus_res = CheckResult(
            "modbus",
            "PASS",
            f"Read {len(tags)} diagnostic tags in {latency_ms} ms",
            latency_ms=latency_ms,
        )
        warns = plc_warnings(plc)
        mode_str = "RUN mode" if plc["run"] else "STOP mode"
        if warns:
            plc_res = CheckResult("plc", "WARN", "; ".join(warns))
        else:
            plc_res = CheckResult(
                "plc",
                "PASS",
                f"{mode_str}, station ID {plc['station_id']}, no faults, scan max {plc['scan_ms']['max']} ms / WDT {plc['wdt_ms']} ms",
            )

        try:
            det_profile, _fw = decode_d1001(raw_d1001)
            if series == "auto":
                model_res = CheckResult(
                    "model",
                    "PASS",
                    f"Detected model {det_profile.name} (firmware 0x{raw_d1001:04X})",
                )
            else:
                conf_profile = get_profile(series)
                if conf_profile == det_profile:
                    model_res = CheckResult(
                        "model",
                        "PASS",
                        f"Model matches configured {conf_profile.name} (firmware 0x{raw_d1001:04X})",
                    )
                else:
                    model_res = CheckResult(
                        "model",
                        "WARN",
                        f"Configured {conf_profile.name} differs from detected {det_profile.name} (firmware 0x{raw_d1001:04X})",
                    )
        except UnknownModelCodeError:
            model_res = CheckResult(
                "model",
                "WARN",
                f"Unrecognized model code (firmware 0x{raw_d1001:04X})",
            )
        except Exception as e:  # noqa: BLE001
            model_res = CheckResult(
                "model",
                "WARN",
                f"Model check failed: {e}",
            )

        return modbus_res, plc_res, model_res, plc
    except Exception as e:  # noqa: BLE001
        modbus_res = CheckResult("modbus", "FAIL", f"Modbus read error: {e}")
        plc_res = CheckResult("plc", "SKIP", "PLC check skipped due to Modbus failure")
        model_res = CheckResult("model", "SKIP", f"Model check skipped due to Modbus failure: {e}")
        return modbus_res, plc_res, model_res, None


async def run_doctor(
    host: str | None = None,
    *,
    port: int = 502,
    unit: int = 1,
    scan: str | None = None,
    strict: bool = False,
    timeout: float = 2.0,
    series: str = "auto",
) -> DoctorReport:
    """Run system network and PLC diagnostics."""
    ts = datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")
    checks: list[CheckResult] = []
    hints: list[str] = []
    plc_dict: dict[str, Any] | None = None

    if host is None:
        # Passive mode: informational checks only
        iface_res, _ = await asyncio.to_thread(_check_interface_sync, None)
        checks.append(iface_res)

        addr_res, _ = await asyncio.to_thread(_check_address_sync, None, None)
        checks.append(addr_res)

        arp_res = await asyncio.to_thread(_check_arp_sync)
        checks.append(arp_res)

        if scan is not None:
            found = await scan_subnet(scan, port=port, timeout=0.2, concurrency=64)
            detail = (
                f"Discovered {len(found)} host(s): {', '.join(found)}"
                if found
                else f"No hosts responding on port {port}"
            )
            checks.append(CheckResult("scan", "PASS", detail))

        hints.append("Supply --host <ip> for Modbus/L7 checks.")
        return DoctorReport(
            ts=ts,
            target=None,
            verdict="PASS",
            checks=checks,
            plc=None,
            hints=hints,
        )

    # Host mode
    target = {"host": host, "port": port, "unit": unit}

    iface_res, dev = await asyncio.to_thread(_check_interface_sync, host)
    checks.append(iface_res)

    addr_res, outside_subnets = await asyncio.to_thread(_check_address_sync, host, dev)
    checks.append(addr_res)
    if outside_subnets:
        iface_name = dev or ("Ethernet" if platform.system() == "Windows" else "enp1s0")
        profile = _get_nmcli_profile(iface_name)
        hints.extend(subnet_hints(host, iface_name, profile=profile))

    icmp_res = await asyncio.to_thread(_check_icmp_sync, host)
    checks.append(icmp_res)

    tcp_res = await _check_tcp502(host, port, timeout)
    checks.append(tcp_res)

    if tcp_res.status == "PASS":
        modbus_res, plc_res, model_res, plc_dict = await _check_modbus_and_plc(
            host, port, unit, timeout, series=series
        )
        checks.append(modbus_res)
        checks.append(plc_res)
        checks.append(model_res)
    else:
        checks.append(
            CheckResult(
                "modbus",
                "SKIP",
                "Modbus check skipped because TCP 502 connection failed",
            )
        )
        checks.append(
            CheckResult(
                "plc",
                "SKIP",
                "PLC check skipped because TCP 502 connection failed",
            )
        )
        checks.append(
            CheckResult(
                "model",
                "SKIP",
                "Model check skipped because TCP 502 connection failed",
            )
        )
        if icmp_res.status == "PASS":
            hints.append(
                "TCP 502 connection failed while ICMP ping succeeded: "
                "close WPLSoft/ISPSoft and power-cycle the PLC."
            )

    if scan is not None:
        found = await scan_subnet(scan, port=port, timeout=0.2, concurrency=64)
        detail = (
            f"Discovered {len(found)} host(s): {', '.join(found)}"
            if found
            else f"No hosts responding on port {port}"
        )
        checks.append(CheckResult("scan", "PASS", detail))

    v = verdict(checks, strict=strict)
    return DoctorReport(
        ts=ts,
        target=target,
        verdict=v,
        checks=checks,
        plc=plc_dict,
        hints=hints,
    )
