#!/usr/bin/env bash
# Bump the PAID package version in lock-step across all files.
#
# Usage:
#   bin/bump-version.sh <new-version>
#
# Updates:
#   - paid/_version.py  (canonical source — pyproject.toml reads via dynamic)
#   - plugin.yaml       (Hermes plugin manifest)
#
# Then runs test_version_sync.py to verify nothing drifted, and prints a
# checklist for the rest of the release flow (CHANGELOG, git tag, push).

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <new-version>" >&2
    echo "Example: $0 1.7.1" >&2
    exit 1
fi

NEW="$1"

# Basic semver shape check (X.Y.Z, optional -prerelease).
if ! [[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]]; then
    echo "Error: '$NEW' does not look like semver (X.Y.Z)" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

OLD="$(python3 -c 'from paid._version import __version__; print(__version__)')"

if [[ "$OLD" == "$NEW" ]]; then
    echo "Already at $NEW — nothing to do." >&2
    exit 0
fi

echo "Bumping $OLD → $NEW"

# 1. paid/_version.py — canonical source.
python3 - <<PY
import pathlib, re
p = pathlib.Path("paid/_version.py")
text = p.read_text()
new_text = re.sub(
    r'^__version__\s*=\s*"[^"]+"',
    f'__version__ = "$NEW"',
    text,
    count=1,
    flags=re.MULTILINE,
)
assert new_text != text, "paid/_version.py: __version__ line not found"
p.write_text(new_text)
PY

# 2. plugin.yaml — Hermes manifest.
python3 - <<PY
import pathlib, re
p = pathlib.Path("plugin.yaml")
text = p.read_text()
new_text = re.sub(
    r'^version:\s*\S+',
    f'version: $NEW',
    text,
    count=1,
    flags=re.MULTILINE,
)
assert new_text != text, "plugin.yaml: version: line not found"
p.write_text(new_text)
PY

echo "Updated paid/_version.py + plugin.yaml"

# 3. Run sync test to verify.
echo "Running version-sync test..."
python3 -m pytest tests/test_version_sync.py -q

echo ""
echo "✅ Version bumped to $NEW. Remaining release checklist:"
echo "  1. Update CHANGELOG.md with v$NEW section"
echo "  2. git add -A && git commit -m 'chore: bump to v$NEW'"
echo "  3. git tag -a v$NEW -m 'Release v$NEW'"
echo "  4. Open PR (don't push to main directly — see"
echo "     feedback_github_workflow_discipline memory)"
echo "  5. After PR merge: git push origin v$NEW"
