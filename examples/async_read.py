"""Example: asynchronous read of D1000, M1000, and X0 using AsyncDeltaPLC.

Demonstrates hardware profile handling:
- `series="auto"` (default): queries D1001 on connect to automatically
  detect the PLC model (e.g. SE, SS2, ES2) and enforce its memory boundaries.
- Explicit series (e.g. `series="SS2"`, `series="ES2"`, `series="SE_EH3_SV2"`):
  validates addresses against a specific profile. In strict mode
  (`strict_series=True`), a mismatch with detected hardware aborts immediately
  with zero user payload frames sent.
"""

import asyncio
import os

from deltactl import AsyncDeltaPLC


async def main() -> None:
    host = os.environ.get("DELTA_PLC_HOST", "127.0.0.1")
    port = int(os.environ.get("DELTA_PLC_PORT", "502"))
    unit = int(os.environ.get("DELTA_PLC_UNIT", "1"))
    # Hardware series: "auto" probes D1001 on connect; can also be set to an
    # explicit canonical profile or model alias (e.g., "SS2", "ES2", "12SE", "SE_EH3_SV2").
    series = os.environ.get("DELTA_PLC_SERIES", "auto")

    # Connect with automatic hardware model detection (or explicit series)
    # Use strict_series=True to abort if the detected hardware differs from the configured series.
    client = AsyncDeltaPLC(
        host,
        port=port,
        unit=unit,
        series=series,
        strict_series=False,
    )
    await client.connect()
    try:
        print(f"Active hardware profile: {client.effective_profile.name}")
        if client.detected_profile:
            print(f"Detected hardware profile: {client.detected_profile.name}")

        d1000 = await client.read("D1000")
        m1000 = await client.read("M1000")
        x0 = await client.read("X0")

        print(f"D1000 (WDT):     {d1000}")
        print(f"M1000 (RUN):     {m1000}")
        print(f"X0 (Input):      {x0}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
