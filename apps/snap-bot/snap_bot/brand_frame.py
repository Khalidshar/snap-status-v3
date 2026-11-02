"""Compatibility import for the shared studio-core media implementation."""

import sys
from typing import TYPE_CHECKING

from studio_core.presentation import brand_frame as _shared

if __name__ == "__main__":
    _shared.main()
else:
    sys.modules[__name__] = _shared

if TYPE_CHECKING:
    from studio_core.presentation.brand_frame import *  # noqa: F403
