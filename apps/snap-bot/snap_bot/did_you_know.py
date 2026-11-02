"""Compatibility import for the shared studio-core media implementation."""

import sys
from typing import TYPE_CHECKING

from studio_core.presentation import did_you_know as _shared

sys.modules[__name__] = _shared

if TYPE_CHECKING:
    from studio_core.presentation.did_you_know import *  # noqa: F403
