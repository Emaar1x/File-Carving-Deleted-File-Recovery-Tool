#!/usr/bin/env python3
"""
Convenience launcher for the carver desktop GUI (Phase 4).

    python gui.py [optional-image-path]

Equivalent to `python -m carver gui`. On Windows, run with `pythonw gui.py`
to launch without a console window.
"""

import sys

from carver.gui import launch

if __name__ == "__main__":
    sys.exit(launch(sys.argv[1:] or None))
