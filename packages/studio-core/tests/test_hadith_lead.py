from urllib.parse import urlencode

import pytest

from studio_core.network.egress import PolicyDenied
from studio_core.network.production_speech import approved
from studio_core.presentation import faith
from studio_core.production.model import HADITH_LEAD


def test_attribution_is_said_before_the_unchanged_hadith():
    text = faith.load("hadith")[0]["text"]
    spoken = HADITH_LEAD + " " + text
    assert approved("tts://hadith?" + urlencode({"text": spoken})) == spoken
    assert approved("tts://hadith?" + urlencode({"text": text})) == text
    for other in (HADITH_LEAD, HADITH_LEAD + " كلام آخر", text + " زيادة"):
        with pytest.raises(PolicyDenied):
            approved("tts://hadith?" + urlencode({"text": other}))
