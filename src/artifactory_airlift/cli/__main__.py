"""Allow ``python -m artifactory_airlift.cli`` where the console script is not
on PATH (an older image, or a source tree on PYTHONPATH)."""

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
