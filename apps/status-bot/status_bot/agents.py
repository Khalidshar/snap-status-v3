"""Owned Status weather adapters. Local rules only, never publication authority."""

import math
import re
import time
from typing import Any

from studio_core.observability.agent_runs import AgentContract, digest

from .weather import content_hash

PLAN_SHA = digest(
    {
        "app": "status-bot-v2",
        "command": "weather",
        "version": "1.0.0",
        "roles": {"fact_source": [], "arabic": ["fact_source"], "quality": ["arabic"]},
        "source": "weather-v1",
        "arabic": "weather-labels-v1",
        "quality": "owned-content-and-local-video-hash-v1",
    }
)


def contract(role: str, budget: int = 25) -> AgentContract:
    dependencies = {"fact_source": (), "arabic": ("fact_source",), "quality": ("arabic",)}
    return AgentContract(
        "status-bot-v2", "weather", role, "1.0.0", PLAN_SHA, dependencies[role], budget
    )


def source_request(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"command", "synthetic"}
        and value["command"] == "weather"
        and type(value["synthetic"]) is bool
    )


def weather_payload(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"content", "failures"}:
        return False
    content = value["content"]
    if not isinstance(content, dict) or not isinstance(value["failures"], list):
        return False
    if (
        content.get("type") != "weather"
        or content.get("sacred") is not False
        or content.get("language") != "ar"
        or content.get("content_hash") != content_hash(content)
    ):
        return False
    for key, limit in (("title", 100), ("body", 1000), ("attribution", 110)):
        text = content.get(key)
        if (
            not isinstance(text, str)
            or not 1 <= len(text) <= limit
            or any(ord(char) < 32 and not (char == "\n" and key == "body") for char in text)
            or any(
                char in text
                for char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b\ufeff"
            )
        ):
            return False
    if not re.search("[\u0600-\u06ff]", content["title"]) or not re.search(
        "[\u0600-\u06ff]", content["body"]
    ):
        return False
    for key, low, high in (("temperature_c", -90, 65), ("wind_kmh", 0, 500)):
        number: Any = content.get(key)
        if (
            type(number) not in (int, float)
            or not math.isfinite(number)
            or not low <= number <= high
        ):
            return False
    expiry: Any = content.get("expires_at")
    return type(expiry) in (int, float) and math.isfinite(expiry) and expiry > time.time()


def source_output(value: Any, request: Any) -> bool:
    return weather_payload(value) and (
        not request["synthetic"] or value["content"]["source"] == "synthetic-fixture"
    )


def arabic_output(value: Any, original: Any) -> bool:
    # Check the weather labels without rewriting or asserting full proofreading.
    return weather_payload(value) and digest(value) == digest(original)
