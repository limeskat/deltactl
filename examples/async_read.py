"""Example: asynchronous read of D1000, M1000, and X0 using AsyncDeltaPLC."""

import asyncio
import os

from delta_plc import AsyncDeltaPLC


async def main() -> None:
    host = os.environ.get("DELTA_PLC_HOST", "127.0.0.1")
    port = int(os.environ.get("DELTA_PLC_PORT", "502"))
    unit = int(os.environ.get("DELTA_PLC_UNIT", "1"))

    client = AsyncDeltaPLC(host, port=port, unit=unit)
    await client.connect()
    try:
        d1000 = await client.read("D1000")
        m1000 = await client.read("M1000")
        x0 = await client.read("X0")

        print(f"D1000: {d1000}")
        print(f"M1000: {m1000}")
        print(f"X0:    {x0}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
