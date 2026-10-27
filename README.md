# Delta DVP PLC Driver & CLI (deltactl)

A production-ready, cross-platform Python library and bench CLI for Delta DVP PLCs over Modbus TCP.

Tested against physical **Delta DVP-12SE** hardware and designed for SCADA/HMI backends (FastAPI, WebSockets, PyQt, Tkinter) and automation bench diagnostics.

---

## Features

- **Symbolic Addressing:** Full support for `D`, `M`, `X`, `Y` registers, octal I/O indices (`X0–X377`, `Y0–Y377`), and bit-of-word syntax (`D100.3`).
- **Data Types & Delta CDAB Byte Order:** 16-bit (`u16`, `i16`) and 32-bit (`i32`, `u32`, `f32`) typed access using Delta's low-word-first (CDAB) register order.
- **Strict Write Safety:** Read-only `X` inputs (`FC02`), full-span guards on critical registers (`D1000–D1002`, `D1010–D1012`, `D1067`, `M1000–M1003`, `M1067`), fail-fast writes, socket closure on write timeouts, and atomic client-side read-modify-write for bit-of-word updates.
- **Hardware-Aware Planning:** Automatic coalescing and chunking conforming to Delta's 100-register / 256-bit PDU limits and split memory block boundaries (`D4095/D4096`, `M1535/M1536`).
- **Telemetry Poller:** Multi-tag polling with coalescing, deadband filtering, report-by-exception (RBE), configurable heartbeat snapshots, and NDJSON/CSV streaming.
- **Bench Diagnostic CLI (`deltactl`):** Register reads, writes, live watching, passive/active network link diagnostics (`deltactl doctor`), and an accurate local mock simulator (`deltactl mock`).

---

## Installation

Requires **Python ≥ 3.10** and pins `pymodbus>=3.16.1,<4` (Modbus datastore and client API stability).

```bash
# Clone the repository
git clone https://github.com/deltactl/deltactl.git
cd deltactl

# Install in editable mode with development dependencies
pip install -e ".[dev]"
```

Verify installation:

```bash
deltactl --version
```

---

## 60-Second Quickstart

### 1. Asynchronous Client (`AsyncDeltaPLC`)

```python
import asyncio
from deltactl import AsyncDeltaPLC


async def main():
    async with AsyncDeltaPLC("192.168.1.5", port=502, unit=1) as plc:
        # Read watchdog setpoint (D1000) and CPU RUN state (M1000)
        wdt = await plc.read("D1000")
        running = await plc.read("M1000")
        print(f"Watchdog: {wdt} ms, CPU running: {running}")

        # Typed 32-bit float read & write
        temp = await plc.read_f32("D200")
        await plc.write_f32("D9000", 23.5)


asyncio.run(main())
```

### 2. Synchronous Client (`DeltaPLC`)

```python
from deltactl import DeltaPLC

with DeltaPLC("192.168.1.5", port=502, unit=1) as plc:
    val = plc.read("D100")
    plc.write("D9000", 42)
    print(f"D100={val}, D9000 updated")
```

### 3. Bench CLI (`deltactl`)

```bash
# Read registers (single, multiple, typed)
deltactl read D1000 M1000 X0 Y0 --host 192.168.1.5
deltactl read D100:f32 D104:i32 --host 192.168.1.5
deltactl read D0 --count 8 --host 192.168.1.5

# Write registers (single, 32-bit float, bit-of-word, dry-run inspection)
deltactl write D9000 1234 --host 192.168.1.5
deltactl write --dtype f32 D9000 -12.5 --host 192.168.1.5
deltactl write D100.3 true --host 192.168.1.5
deltactl write D9000 42 --dry-run --host 192.168.1.5

# Stream telemetry with deadband and change detection
deltactl watch D1000 D1010 M1000 --on-change --deadband 0.5 --format ndjson --host 192.168.1.5
```

---

## Address Map Table

| Device | Notation / Address Range | Wire Modbus Address | FC Read | FC Write | Notes |
|---|---|---|---|---|---|
| **D** (Data Registers) | `D0–D4095` (Block 1)<br>`D4096–D11999` (Block 2) | `0x1000 + n`<br>`0x9000 + (n - 4096)` | 03 (Holding) | 06 (Single) / 16 (Multiple) | 16-bit words. D4095/D4096 split block boundary is never crossed by multi-register or 32-bit access. |
| **M** (Internal Relays) | `M0–M1535` (Block 1)<br>`M1536–M4095` (Block 2) | `0x0800 + n`<br>`0xB000 + (n - 1536)` | 01 (Coil) | 05 (Single) / 15 (Multiple) | Booleans. M1535/M1536 split block boundary is never merged into a single PDU. |
| **X** (Discrete Inputs) | `X0–X377` (octal) | `0x0400 + oct2dec(n)` | 02 (Discrete) | *None (Read-Only)* | Physical inputs. Read via **FC02 only** (`FC01` returns exception 02). Writes are rejected client-side. |
| **Y** (Discrete Outputs) | `Y0–Y377` (octal) | `0x0500 + oct2dec(n)` | 01 (Coil) | 05 (Single) / 15 (Multiple) | Physical outputs. **`Y0–Y377` is writable**. |
| **Bit-of-word** | `D<n>.<bit>` (bit: 0..15) | Read `0x1000 + n` | 03 (Holding) | 06 (Single) | Read-modify-write on D register under transport lock. |

*Note: String/ASCII registers are out of scope for v0.1.*

---

## Data Types & Delta CDAB Byte Order

Delta DVP PLCs store 32-bit values in **CDAB word order** (least-significant 16-bit word at lower address, most-significant 16-bit word at higher address). In `pymodbus`, this corresponds to `word_order="little"`.

| DType | Bit Width | Registers | Range / Format | Example Value | Register Words `[reg0, reg1]` |
|---|---|---|---|---|---|
| `u16` | 16 | 1 | `0..65535` (default for D) | `42` | `[0x002A]` |
| `i16` | 16 | 1 | `-32768..32767` | `-1` | `[0xFFFF]` |
| `u32` | 32 | 2 | `0..4294967295` (CDAB) | `0xDEADBEEF` | `[0xBEEF, 0xDEAD]` |
| `i32` | 32 | 2 | `-2147483648..2147483647` (CDAB) | `0x12345678` | `[0x5678, 0x1234]` |
| `f32` | 32 | 2 | IEEE 754 single-precision (CDAB) | `12.5`<br>`-12.5` | `[0x0000, 0x4148]`<br>`[0x0000, 0xC148]` |

### Specifying Data Types

You can specify data types inline on any D tag or via the `--dtype` CLI flag:
- **Inline Tag Syntax:** `deltactl read D100:f32 D104:i32`
- **CLI Flag:** `deltactl read D100 --dtype f32` or `deltactl write --dtype f32 D200 -12.5`

---

## Write Safety & System Guards

To prevent accidental hardware lockups, watchdog timeouts, and machine disruption, the driver enforces strict write safety rules:

1. **Full-Span Guarded Registers:**
   - **X Inputs:** `X0–X377` are physical inputs; all writes raise `ReadOnlyAddressError` client-side before any network packet is dispatched.
   - **System Special Relays:** `M1000–M1003` (RUN status/system flags) and `M1067` (program error flag) are guarded against writes.
   - **System Special Registers:** `D1000–D1002` (watchdog setpoint, firmware ID, capacity), `D1010–D1012` (scan time measurements), and `D1067` (error fault code) are guarded against writes.
   - **Span-Aware Rejection:** A multi-register or 32-bit write whose span overlaps any guarded register (e.g. writing a 2-register `f32` to `D999`) is rejected client-side before I/O.
2. **Fail-Fast Writes:**
   - Writes fail-fast by default (`write_retries=0`). Unlike reads, writing is not idempotently retried on timeout, avoiding duplicate actuation commands.
3. **Socket Closure on Write Timeout:**
   - If a write times out or encounters a network reset, the driver immediately closes the underlying TCP connection to prevent stale command pipelining in the PLC's TCP stack.
4. **Persistent Connection Policy:**
   - Delta DVP Ethernet interfaces (DVP-12SE / DVPEN01) support only 2 to 4 simultaneous TCP sockets. Each client instance maintains exactly one persistent socket.
5. **Read Retries:**
   - Read requests default to 2 retries with exponential backoff before surfacing an error.
6. **Bit-of-Word Read-Modify-Write:**
   - Delta PLCs do not support Modbus FC22 mask write. Writing `D<n>.<bit>` executes an atomic read-modify-write under the client's internal mutex lock.

---

## Hardware Notes (Delta DVP-12SE Bench Measurements)

- **PDU Caps:** Delta DVP firmware enforces strict packet caps:
  - Max **100 registers** per FC03 request (requesting 101 returns Modbus Exception 03).
  - Max **256 bits** per FC01/FC02 request (requesting 257 returns Modbus Exception 03).
  The driver's request planner automatically chunks large ranges into PDU-compliant blocks.
- **Split-Block Boundaries:**
  - `D0–D4095` and `D4096–D11999` are non-contiguous internal blocks. The planner never coalesces across the `D4095`/`D4096` boundary. Writing or reading a 32-bit value at `D4095` raises `BlockBoundaryError`.
  - `M0–M1535` and `M1536–M4095` are likewise kept in distinct request chunks.
- **Watchdog & Scan Registers:**
  - `D1000`: Watchdog Timer (WDT) setpoint in **1 ms** units (factory default 200 ms).
  - `D1001`: Hardware and firmware identifier (e.g. `0x7C21` where `0x7C` = SE model and `0x21` = v2.1). This value is **informational only** and is not flagged as an error by `doctor`.
  - `D1010 / D1011 / D1012`: Current, minimum, and maximum scan times measured in **0.1 ms** units (e.g. a value of 4 means 0.4 ms).
- **PLC Ethernet Stack Wedging & WPLSoft:**
  - The Delta DVP Ethernet stack can occasionally hang if flooded or left in half-open states, requiring a physical power cycle of the PLC.
  - If **WPLSoft** is connected or holding an online monitoring session, it may consume the available socket slots. Close WPLSoft before diagnosing driver connectivity issues.

---

## Network Diagnostics (`deltactl doctor`)

The `doctor` command provides non-intrusive network and PLC health diagnostics:

```bash
# Passive mode (no host required): lists local interfaces and IP routing
deltactl doctor

# Active check against a PLC
deltactl doctor --host 192.168.1.5

# Output machine-readable JSON
deltactl doctor --host 192.168.1.5 --json

# Strict mode: treat PLC warnings (e.g. M1067 set, scan time >= 80% WDT) as errors (exit code 1)
deltactl doctor --host 192.168.1.5 --strict

# Discover active PLCs across a subnet (opt-in concurrency probe)
deltactl doctor --scan 192.168.1.0/24
```

### Subnet Alignment Hints

If the PLC is physically reachable or routed on an interface with an incompatible subnet, `deltactl doctor` provides copy-pasteable configuration commands:
- **Linux:** `sudo ip addr add 192.168.1.250/24 dev eth0` or `nmcli connection modify ...`
- **Windows:** `netsh interface ipv4 add address "Ethernet" 192.168.1.250 255.255.255.0`

`doctor` **never executes these commands** automatically; it only suggests them for manual user execution.

---

## Hardware-Faithful Mock Server (`deltactl mock`)

For local testing, CI, and offline development, the built-in mock server replicates hardware-verified DVP behavior:

```bash
# Start mock on port 5020
deltactl mock --listen 127.0.0.1:5020

# Seed specific register values
deltactl mock --listen 127.0.0.1:5020 --seed D100=42 M0=true

# Simulate a fault code and CPU STOP state
deltactl mock --listen 127.0.0.1:5020 --fault-code 5 --plc-stop

# Enable dynamic scan time simulation (triangular wave on D1010-D1012)
deltactl mock --listen 127.0.0.1:5020 --live-scan
```

The mock strictly enforces:
- Maximum 4 concurrent client connections.
- 100-register and 256-bit PDU limits (returns Modbus Exception 03 on overflow).
- Read-only protection on `X0–X377` and system guards on `D1000–D1002`, `D1010–D1012`, `D1067`, `M1000–M1003`, `M1067`.
- FC02 requirement for `X` inputs (FC01 returns Exception 02).

---

## Hardware-in-the-Loop (HIL) Testing

The test suite includes dedicated hardware-in-the-loop tests under `tests/hil/test_hil.py`.

By default, HIL tests are skipped cleanly in CI and local mock runs. To run against a live PLC:

```bash
# Read-only tests against bench unit
export DELTA_PLC_HIL_HOST="192.168.1.5"
export DELTA_PLC_HIL_UNIT="1"
pytest tests/hil -v

# Opt into scratch writes (confined strictly to D9000–D9010 and M900–M910)
export DELTA_PLC_HIL_WRITES="1"
pytest tests/hil -v
```

HIL write tests will **never** touch inputs, outputs (`Y`), or system control registers.

---

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `DELTA_PLC_HOST` | Target PLC IP address or hostname | *(None, required for network commands)* |
| `DELTA_PLC_PORT` | Modbus TCP port | `502` |
| `DELTA_PLC_UNIT` | Modbus Unit ID / Slave ID | `1` |
| `DELTA_PLC_TIMEOUT` | Network socket timeout in seconds | `2.0` |
| `DELTA_PLC_HIL_HOST` | Host address for HIL test execution | *(Unset: HIL tests skipped)* |
| `DELTA_PLC_HIL_UNIT` | Unit ID for HIL test execution | `1` |
| `DELTA_PLC_HIL_WRITES` | Set to `1` to enable scratch writes in HIL tests | `0` (disabled) |

CLI flags (e.g. `--host`, `--port`) always take precedence over environment variables.

---

## Troubleshooting

- **`Connection refused` / `Cannot connect to host:502`:**
  - Verify physical cabling and link LED.
  - Check whether another client or WPLSoft session has occupied all 2–4 socket slots.
  - Run `deltactl doctor --host <IP>` to check interface, ICMP, and port availability.
  - Run `deltactl doctor --scan <CIDR>` if the PLC IP address may have changed.
- **`Timeout during write` / `Socket closed`:**
  - The driver closes the socket on write timeouts to avoid executing buffered stale writes. Ensure network latency is stable and the PLC cycle time is normal.
- **`ReadOnlyAddressError: X0 is read-only`:**
  - `X` addresses correspond to physical PLC inputs and cannot be written via Modbus. Write to internal relays (`M`) or physical outputs (`Y`) instead.
- **`DeltaAddressError: D1000 is guarded against writes`:**
  - System registers (`D1000–D1002`, `D1010–D1012`, `D1067`, `M1000–M1003`, `M1067`) are write-protected by driver policy to safeguard PLC operation.
- **Subnet Mismatch:**
  - If your PC is on `192.168.0.x` and the PLC is on `192.168.1.5`, run `deltactl doctor` to view the recommended `ip addr add` or `netsh` alias commands.
- **PLC Stops Answering Entirely:**
  - Delta DVP Ethernet stacks may crash when subjected to malformed raw packets or socket exhaustion. Power-cycle the PLC unit.

---

## License

MIT License. See `LICENSE` for details.
