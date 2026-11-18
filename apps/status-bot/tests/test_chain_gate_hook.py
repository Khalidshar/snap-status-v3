import inspect

import pytest

from status_bot import content_clips


@pytest.mark.real_chain_gate
def test_whatsapp_checks_the_eighteen_before_upload():
    source = inspect.getsource(content_clips.deliver_built)
    gate = source.index("chain_gate.verify(")
    assert gate < source.index("UPLOAD_INTENT")
