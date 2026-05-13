#!/bin/bash
# Install the hourly TTL sweep cron entry for paid-review.
#
# Linux: writes /etc/cron.d/paid-review-sweep (requires sudo).
# macOS: prints the line to add manually with `crontab -e`.
#
# Idempotent: re-running on Linux replaces the existing entry; on macOS a
# duplicate entry warning is printed if an identical line is detected.

set -euo pipefail

USER_NAME="${PAID_REVIEW_CRON_USER:-${USER}}"
PYTHON_BIN="${PAID_REVIEW_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python3}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$SRC/bin/sweep_review_sessions.py"
LOG="${HERMES_HOME:-$HOME/.hermes}/paid/review_sweep_cron.log"
CRON_LINE="0 * * * * $USER_NAME $PYTHON_BIN $SCRIPT >> $LOG 2>&1"

if [[ ! -f "$SCRIPT" ]]; then
    echo "ERROR: sweep script not found at $SCRIPT" >&2
    exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "WARN: python interpreter not found/executable at $PYTHON_BIN"
    echo "      override with PAID_REVIEW_PYTHON=/path/to/python3"
fi

mkdir -p "$(dirname "$LOG")"

case "$(uname -s)" in
    Linux*)
        TARGET="/etc/cron.d/paid-review-sweep"
        echo "Installing $TARGET"
        echo "  user:   $USER_NAME"
        echo "  python: $PYTHON_BIN"
        echo "  script: $SCRIPT"
        echo "  log:    $LOG"
        TMP="$(mktemp)"
        cat > "$TMP" <<EOF
# /etc/cron.d/paid-review-sweep — managed by install_review_cron.sh
# Hourly TTL check on review sessions; force_close any inactive > TTL hours.
$CRON_LINE
EOF
        if [[ "$EUID" -ne 0 ]]; then
            sudo install -m 0644 -o root -g root "$TMP" "$TARGET"
        else
            install -m 0644 -o root -g root "$TMP" "$TARGET"
        fi
        rm -f "$TMP"
        echo "Done. Verify with: cat $TARGET"
        ;;
    Darwin*)
        # macOS doesn't have /etc/cron.d/; user crontab is the path of least
        # resistance (launchd would be cleaner but adds plist authoring).
        USER_LINE="0 * * * * $PYTHON_BIN $SCRIPT >> $LOG 2>&1"
        echo "macOS detected. Add this line to your user crontab:"
        echo
        echo "    $USER_LINE"
        echo
        if crontab -l 2>/dev/null | grep -Fq "sweep_review_sessions.py"; then
            echo "(WARN: an existing crontab line already references sweep_review_sessions.py — skipping append)"
            exit 0
        fi
        echo "Run: crontab -e   then paste the line above. Or run:"
        echo "    (crontab -l 2>/dev/null; echo \"$USER_LINE\") | crontab -"
        ;;
    *)
        echo "Unsupported OS $(uname -s); add this cron line manually:"
        echo "    $CRON_LINE"
        ;;
esac
