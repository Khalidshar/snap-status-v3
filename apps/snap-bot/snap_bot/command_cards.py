"""Compatibility import for the shared studio-core media implementation."""

import sys
from typing import TYPE_CHECKING

from studio_core.presentation import command_cards as _shared

sys.modules[__name__] = _shared

if TYPE_CHECKING:
    from studio_core.presentation.command_cards import *  # noqa: F403
