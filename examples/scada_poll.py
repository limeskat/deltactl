"""Example: polling D100:f32 and M0 to NDJSON on stdout for 10 seconds."""

import asyncio
import os
import sys

from deltactl import AsyncDeltaPLC, TagPoller
from deltactl.poller import NdjsonWriter


async def main() -> None:
    host = os.environ.get("DELTA_PLC_HOST", "127.0.0.1")
    port = int(os.environ.get("DELTA_PLC_PORT", "502"))
    unit = int(os.environ.get("DELTA_PLC_UNIT", "1"))

    client = AsyncDeltaPLC(host, port=port, unit=unit)
    await client.connect()
    writer = NdjsonWriter(sys.stdout)
    poller = TagPoller(client, ["D100:f32", "M0"], interval=0.5)
    try:
        await poller.run(duration=10.0, writer=writer)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
