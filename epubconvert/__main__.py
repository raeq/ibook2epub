"""Entry point for ``python -m epubconvert``."""

import sys

from .convert import main

if __name__ == "__main__":
    sys.exit(main())
