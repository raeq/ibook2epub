"""Entry point for ``python -m epubconvert``."""

import sys

from .run import main

if __name__ == "__main__":
    sys.exit(main())
