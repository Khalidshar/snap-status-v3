"""The old free Edge voice, only for verified hadith and riddle answers.

Quran, dua and adhkar cannot enter this endpoint. No keys or paid AI APIs.
"""

import asyncio
import json
import socket
from typing import Any
from urllib.parse import parse_qs, urlsplit

from studio_core.network.egress import PolicyDenied, validate_target

HOST = "speech.platform.bing.com"


def approved(url: str) -> str:
    from studio_core.presentation import faith
    from studio_core.production.model import DATA

    parts = urlsplit(url)
    query = parse_qs(parts.query, strict_parsing=True)
    if (
        parts.scheme != "tts"
        or parts.hostname not in {"hadith", "riddle"}
        or parts.path
        or set(query) != {"text"}
        or len(query["text"]) != 1
    ):
        raise PolicyDenied("PRODUCTION_SPEECH_SCOPE_DENIED")
    text = query["text"][0]
    allowed = (
        [i["text"] for i in faith.load("hadith")]
        if parts.hostname == "hadith"
        else [i["a"] for i in json.loads((DATA / "riddles.json").read_bytes())]
    )
    if text not in allowed:
        raise PolicyDenied("PRODUCTION_SPEECH_TEXT_DENIED")
    return text


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


def fetch(url: str) -> bytes:
    return asyncio.run(speech(approved(url)))
