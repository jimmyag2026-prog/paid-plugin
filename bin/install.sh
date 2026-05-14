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

# ---------------------------------------------------------------------------
# v1.4.2: pre-install lark-oapi when feishu/lark platform is enabled.
#
# Why: hermes v0.13 installer treats lark-oapi as optional. First `hermes
# gateway run` after enabling feishu crashes with `'NoneType' object has no
# attribute 'Client'` — lark-oapi is async-pip-installed but the gateway
# process has already cached `lark = None` from the try/except import.
# Restart fixes it but pilots get a scary error first time. Pre-install
# here so first-run is clean. (backlog v1.4.5)
# ---------------------------------------------------------------------------
HERMES_CFG="${HERMES_HOME:-$HOME/.hermes}/config.yaml"
HERMES_VENV_PIP="${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/pip"

if [[ -f "$HERMES_CFG" ]] && grep -qE '^[[:space:]]*feishu:[[:space:]]*$' "$HERMES_CFG" 2>/dev/null \
   && grep -A1 -E '^[[:space:]]*feishu:[[:space:]]*$' "$HERMES_CFG" | grep -qE '^[[:space:]]*enabled:[[:space:]]*true'; then
    if [[ -x "$HERMES_VENV_PIP" ]]; then
        echo
        echo "Detected platforms.feishu.enabled — ensuring lark-oapi is installed in hermes venv..."
        if "$HERMES_VENV_PIP" show lark-oapi >/dev/null 2>&1; then
            echo "  lark-oapi already installed — OK"
        else
            "$HERMES_VENV_PIP" install --quiet "lark-oapi==1.5.5" \
                && echo "  lark-oapi==1.5.5 installed" \
                || echo "  ⚠️  pip install lark-oapi failed; first 'hermes gateway run' may crash with 'NoneType' .Client error (restart fixes it)"
        fi
    else
        echo
        echo "⚠️  feishu enabled but hermes venv pip not found at $HERMES_VENV_PIP"
        echo "    Manually: \$HERMES_VENV/bin/pip install lark-oapi==1.5.5"
    fi
fi

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
