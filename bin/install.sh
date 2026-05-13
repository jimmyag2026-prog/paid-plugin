#!/bin/bash
# Install PAID into ~/.hermes/plugins/paid-v1/
#
# Idempotent — re-running overlays the latest plugin code without touching
# ~/.hermes/paid/ runtime state.

set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${HERMES_HOME:-$HOME/.hermes}/plugins/paid-v1"

echo "PAID install"
echo "  source: $SRC"
echo "  target: $DEST"

mkdir -p "$DEST"

# Sync code, exclude dev artefacts. --delete-excluded so stray pycache /
# pytest_cache from a previous install get cleaned up too.
rsync -a --delete --delete-excluded \
    --exclude '.git' \
    --exclude '.gitignore' \
    --exclude '.pytest_cache' \
    --exclude '__pycache__' \
    --exclude 'tests' \
    --exclude 'bin' \
    --exclude '*.pyc' \
    "$SRC/" "$DEST/"

echo
echo "Installed plugin files:"
find "$DEST" -maxdepth 2 -type f | sort | sed "s#$DEST/#  #"

echo
echo "Next steps:"
echo "  1. python3 -m paid setup --name 'Your Name' --identity telegram:YOUR_ID"
echo "     (edit ~/.hermes/paid/persona.md and sop.md after this)"
echo "  2. hermes plugins enable paid-v1"
echo "  3. hermes gateway restart"
echo "  4. tail -20 ~/.hermes/paid/plugin_runtime.log   # verify"
echo
echo "Optional (review skill — recommended for v0.1 dogfood):"
echo "  5. $SRC/bin/install_review_cron.sh        # hourly TTL sweep (R6)"
echo "  6. python3 $SRC/scripts/doctor.py         # health-check post-install"
