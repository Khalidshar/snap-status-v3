"""Compatibility import for the shared studio-core media implementation."""

import sys
from typing import TYPE_CHECKING

from studio_core.presentation import riddle_card as _shared

sys.modules[__name__] = _shared

if TYPE_CHECKING:
    from studio_core.presentation.riddle_card import *  # noqa: F403
