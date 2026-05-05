#!/bin/bash
# Remove the hourly TTL sweep cron entry installed by install_review_cron.sh.

set -euo pipefail

case "$(uname -s)" in
    Linux*)
        TARGET="/etc/cron.d/paid-review-sweep"
        if [[ ! -f "$TARGET" ]]; then
            echo "$TARGET not present — nothing to remove."
            exit 0
        fi
        echo "Removing $TARGET"
        if [[ "$EUID" -ne 0 ]]; then
            sudo rm -f "$TARGET"
        else
            rm -f "$TARGET"
        fi
        echo "Done."
        ;;
    Darwin*)
        if ! crontab -l 2>/dev/null | grep -Fq "sweep_review_sessions.py"; then
            echo "No sweep_review_sessions.py crontab entry found."
            exit 0
        fi
        echo "Removing sweep_review_sessions.py from your user crontab..."
        crontab -l 2>/dev/null | grep -Fv "sweep_review_sessions.py" | crontab -
        echo "Done. Verify with: crontab -l"
        ;;
    *)
        echo "Unsupported OS $(uname -s); remove cron entry manually."
        ;;
esac
