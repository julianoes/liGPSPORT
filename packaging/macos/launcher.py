"""PyInstaller entry point for the macOS app bundle.

ligpsport/gui.py uses package-relative imports, so it can't be the
script PyInstaller freezes directly.
"""

import sys

from ligpsport.gui import main

sys.exit(main())
