"""Compatibility import for the shared studio-core media implementation."""

import sys
from typing import TYPE_CHECKING

from studio_core.presentation import content_agents as _shared

sys.modules[__name__] = _shared

if TYPE_CHECKING:
    from studio_core.presentation.content_agents import *  # noqa: F403
