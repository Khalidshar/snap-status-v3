import hashlib
import json
import math
from datetime import UTC, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from studio_core.jobs import canonical
from studio_core.network import NetworkError


class SourceInvalid(ValueError):
    pass


def content_hash(content: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical(
            {
                k: v
                for k, v in content.items()
                if k not in {"content_hash", "fetched_at", "expires_at"}
            }
        ).encode()
    ).hexdigest()


def numeric(value: float, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise SourceInvalid("Invalid weather value")
    return round(value, 1)


def parse(
    source: dict[str, Any], raw: str | bytes, city: str, now: float, ttl: float
) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        if source["id"] == "open-meteo":
            current = value["current"]
            units = value["current_units"]
            if units["temperature_2m"] != "°C" or units["wind_speed_10m"] != "km/h":
                raise SourceInvalid("Unexpected units")
            temperature = numeric(current["temperature_2m"], -90, 65)
            wind = numeric(current["wind_speed_10m"], 0, 500)
            issued = datetime.fromisoformat(current["time"]).replace(tzinfo=ZoneInfo("Asia/Riyadh"))
        elif source["id"] == "met-no":
            units = value["properties"]["meta"]["units"]
            if units["air_temperature"] != "celsius" or units["wind_speed"] != "m/s":
                raise SourceInvalid("Unexpected units")
            series = value["properties"]["timeseries"]
            # Closest model timestamp; do not silently use the first old forecast period.
            point = min(
                series,
                key=lambda p: abs(
                    datetime.fromisoformat(p["time"].replace("Z", "+00:00")).timestamp() - now
                ),
            )
            issued = datetime.fromisoformat(point["time"].replace("Z", "+00:00"))
            details = point["data"]["instant"]["details"]
            temperature = numeric(details["air_temperature"], -90, 65)
            wind = numeric(numeric(details["wind_speed"], 0, 140) * 3.6, 0, 500)
        else:
            raise SourceInvalid("Unsupported provider")
        if abs(issued.timestamp() - now) > ttl:
            raise SourceInvalid("Stale model timestamp")
    except (KeyError, TypeError, json.JSONDecodeError, IndexError, ValueError) as exc:
        raise SourceInvalid("Weather response rejected") from None
    body = f"الحرارة {temperature:.1f} درجة مئوية\nالرياح {wind:.1f} كم في الساعة"
    content: dict[str, Any] = {
        "type": "weather",
        "title": "طقس " + city,
        "body": body,
        "language": "ar",
        "sacred": False,
        "source": source["id"],
        "source_url": source["url"],
        "license": source["license"],
        "attribution": source["attribution"],
        "temperature_c": temperature,
        "wind_kmh": wind,
        "model_time": issued.astimezone(ZoneInfo("Asia/Riyadh")).isoformat(),
        "fetched_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "expires_at": now + ttl,
    }
    content["content_hash"] = content_hash(content)
    return content


def fetch_weather(
    client: Any, settings: dict[str, Any], now: float
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    failures: list[dict[str, str]] = []
    for source in settings["sources"]:
        try:
            response = client.get(source["url"])
            if "json" not in response.content_type:
                raise SourceInvalid("Invalid content type")
            content = parse(source, response.body, settings["city"], now, settings["ttl_seconds"])
            return content, failures
        except (NetworkError, SourceInvalid) as exc:
            failures.append({"source": source["id"], "error": type(exc).__name__})
    raise SourceInvalid("All weather sources failed")
