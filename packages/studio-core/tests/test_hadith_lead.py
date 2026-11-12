from urllib.parse import urlencode

import pytest

from studio_core.network.egress import PolicyDenied
from studio_core.network.production_speech import approved
from studio_core.presentation import faith
from studio_core.production.model import HADITH_LEAD


def test_hadith_never_returns_to_generated_voice_even_with_exact_approved_text():
    text = faith.load("hadith")[0]["text"]
    spoken = HADITH_LEAD + " " + text
    for other in (text, spoken, HADITH_LEAD, HADITH_LEAD + " كلام آخر", text + " زيادة"):
        with pytest.raises(PolicyDenied):
            approved("tts://hadith?" + urlencode({"text": other}))
