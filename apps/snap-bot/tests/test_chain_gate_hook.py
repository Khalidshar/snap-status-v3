import inspect

import pytest

from snap_bot import server_preview


@pytest.mark.real_chain_gate
def test_snap_checks_the_eighteen_before_any_approval_or_send():
    source = inspect.getsource(server_preview.Runtime.verify)
    assert 'chain_gate.verify(directory / "state/jobs.sqlite")' in source
    # approve() and send() both go through verify().
    assert "self.verify(" in inspect.getsource(server_preview.Runtime.approve)
