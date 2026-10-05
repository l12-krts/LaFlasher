"""Start LaFlasher: `python LaFlasher.py [package.laflash]` (also the PyInstaller entry point)."""

import sys

from laflasher.app import main

if __name__ == "__main__":
    sys.exit(main())
