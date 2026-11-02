"""Compatibility import for the pinned shared civil calendar."""

import sys
from typing import TYPE_CHECKING

from studio_core.presentation import civil_calendar as _shared

sys.modules[__name__] = _shared
if TYPE_CHECKING:
    from studio_core.presentation.civil_calendar import *  # noqa: F403
