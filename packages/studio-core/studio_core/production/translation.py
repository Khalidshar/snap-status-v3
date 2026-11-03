"""Keyless sentence translation with retained upstream evidence, never sacred text."""

import json
import re
import unicodedata
from collections import Counter
from typing import Any
from urllib.parse import urlencode

ENDPOINT = "https://translate.googleapis.com/translate_a/single"
BRANDS = {
    "Louis Vuitton": "لويس فويتون",
    "Chanel": "شانيل",
    "Hermès": "هيرميس",
    "Hermes": "هيرميس",
    "Gucci": "غوتشي",
    "Dior": "ديور",
    "Cartier": "كارتييه",
    "Rolex": "رولكس",
    "Prada": "برادا",
    "Tiffany": "تيفاني",
    "Saint Laurent": "سان لوران",
}


def request_url(text: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError("TRANSLATION_INPUT_INVALID")
    return (
        ENDPOINT + "?" + urlencode({"client": "gtx", "sl": "en", "tl": "ar", "dt": "t", "q": text})
    )


def numbers(text: str) -> Counter[str]:
    text = "".join(str(unicodedata.digit(c)) if c.isdigit() else c for c in text)
    return Counter(re.findall(r"\d+(?:[.,٫٬]\d+)*", text.replace("٫", ".").replace("٬", ",")))


def decode(text: str, raw: bytes) -> str:
    """Verify source echoes and every number; reject empty/foreign or altered output."""
    data: Any = json.loads(raw)
    try:
        segments = data[0]
        if not isinstance(segments, list) or not segments:
            raise ValueError
        target = "".join(row[0] for row in segments)
        echoed = "".join(row[1] for row in segments)
        if re.sub(r"\s+", "", echoed) != re.sub(r"\s+", "", text):
            raise ValueError
    except (IndexError, TypeError, ValueError):
        raise ValueError("TRANSLATION_SOURCE_ECHO_CHANGED") from None
    for brand, name in BRANDS.items():
        target = re.sub(re.escape(brand), name, target, flags=re.I)
    if (
        not target.strip()
        or not re.search("[ء-ي]", target)
        or numbers(target) != numbers(text)
        or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} and c not in "\n\t" for c in target)
    ):
        raise ValueError("TRANSLATION_FACTS_OR_LANGUAGE_CHANGED")
    return target.strip()
