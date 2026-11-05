"""Free legacy voice broker child. Installed in the separate hash-locked voice venv."""

import asyncio
import json
import socket
import sys
from typing import Any

from studio_core.network.egress import PolicyDenied, validate_target
from studio_core.network.production_speech import HOST, approved


async def speech(text: str) -> bytes:
    import aiohttp
    import edge_tts

    class PinnedResolver(aiohttp.abc.AbstractResolver):
        async def resolve(
            self, host: str, port: int = 0, family: int = socket.AF_INET
        ) -> list[Any]:
            if host != HOST or port != 443:
                raise PolicyDenied("PRODUCTION_SPEECH_HOST_DENIED")
            _, address, _ = await asyncio.to_thread(
                validate_target, "https://" + HOST + "/", (HOST,)
            )
            return [
                {
                    "hostname": HOST,
                    "host": address,
                    "port": 443,
                    "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
                    "proto": socket.IPPROTO_TCP,
                    "flags": socket.AI_NUMERICHOST,
                }
            ]

        async def close(self) -> None:
            pass

    async with aiohttp.TCPConnector(resolver=PinnedResolver()) as connector:
        voice = edge_tts.Communicate(text, "ar-SA-HamedNeural", rate="-8%", connector=connector)
        chunks = []
        count = 0
        async for chunk in voice.stream():
            if chunk["type"] == "audio":
                data = chunk["data"]
                count += len(data)
                if count > 4_000_000:
                    raise PolicyDenied("PRODUCTION_SPEECH_SIZE_DENIED")
                chunks.append(data)
        if not chunks:
            raise PolicyDenied("PRODUCTION_SPEECH_EMPTY")
        return b"".join(chunks)


if __name__ == "__main__":
    request = json.loads(sys.stdin.buffer.read(8193))
    raw = asyncio.run(speech(approved(request["url"])))
    sys.stdout.buffer.write(raw)
