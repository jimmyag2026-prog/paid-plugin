#!/bin/bash
# Remove PAID plugin from Hermes. Does NOT touch ~/.hermes/paid/ state.

set -euo pipefail

DEST="${HERMES_HOME:-$HOME/.hermes}/plugins/paid-v1"

if [[ ! -d "$DEST" ]]; then
    echo "PAID is not installed at $DEST — nothing to do."
    exit 0
fi

echo "Removing $DEST"
rm -rf "$DEST"

echo "Done. Run 'hermes gateway restart' to drop hooks."
echo
echo "Your runtime state at ~/.hermes/paid/ is preserved."
echo "If you want a clean slate: rm -rf ~/.hermes/paid"
