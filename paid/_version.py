"""Single source of truth for the PAID package version.

All other version strings (pyproject.toml via dynamic, plugin.yaml via
test_version_sync.py, paid.__init__.__version__) derive from this file.

Bump with `bin/bump-version.sh <new-version>` — never edit version strings
by hand in multiple places.
"""

__version__ = "1.6.11"
