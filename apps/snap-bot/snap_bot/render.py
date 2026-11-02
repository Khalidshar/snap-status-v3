"""Compatibility import for the shared studio-core media implementation."""

import sys
from typing import TYPE_CHECKING

from studio_core.presentation import render as _shared

sys.modules[__name__] = _shared

if TYPE_CHECKING:
    from studio_core.presentation.render import *  # noqa: F403
