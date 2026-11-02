"""Compatibility import for the shared studio-core media implementation."""

import sys
from typing import TYPE_CHECKING

from studio_core.presentation import faith as _shared

sys.modules[__name__] = _shared

if TYPE_CHECKING:
    from studio_core.presentation.faith import *  # noqa: F403
