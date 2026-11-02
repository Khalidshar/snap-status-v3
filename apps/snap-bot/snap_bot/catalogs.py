"""Compatibility import for the shared studio-core media implementation."""

import sys
from typing import TYPE_CHECKING

from studio_core.presentation import catalogs as _shared

sys.modules[__name__] = _shared

if TYPE_CHECKING:
    from studio_core.presentation.catalogs import *  # noqa: F403
