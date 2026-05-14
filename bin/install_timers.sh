#!/bin/bash
# Install PAID's systemd --user timer suite:
#
#   paid-sweep.timer            (every 5 min)  — auto-defer stale pending approvals
#   paid-review-sweep.timer     (hourly)       — close inactive review sessions
#   paid-daily-snapshot.timer   (00:30 UTC)    — roll today's activity into daily/<date>.md
#
# Idempotent — re-running re-copies units + reload + re-enable.
#
# Assumes the plugin lives at ~/.hermes/plugins/paid-v1/ (default install path).
# Assumes the hermes venv is at ~/.hermes/hermes-agent/venv/.
#
# After install: `systemctl --user list-timers` will show 3 paid-* entries.

set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
USER_SYSTEMD="${HOME}/.config/systemd/user"
HERMES_VENV_PY="${HOME}/.hermes/hermes-agent/venv/bin/python"

# ---- preflight ----------------------------------------------------------

if [[ ! -d "${HOME}/.hermes/plugins/paid-v1" ]]; then
    echo "ERROR: PAID plugin not found at ~/.hermes/plugins/paid-v1/" >&2
    echo "       Run bin/install.sh first." >&2
    exit 2
fi

if [[ ! -x "$HERMES_VENV_PY" ]]; then
    echo "ERROR: hermes venv python not found at $HERMES_VENV_PY" >&2
    echo "       Install hermes-agent first; PAID timers shell out to that venv." >&2
    exit 2
fi

mkdir -p "$USER_SYSTEMD"

# ---- copy unit files ----------------------------------------------------

UNITS=(
    "paid-sweep.service"
    "paid-sweep.timer"
    "paid-review-sweep.service"
    "paid-review-sweep.timer"
    "paid-daily-snapshot.service"
    "paid-daily-snapshot.timer"
)

echo "Installing timer units to $USER_SYSTEMD/"
for u in "${UNITS[@]}"; do
    if [[ ! -f "$SRC/$u" ]]; then
        echo "  ⚠ missing $SRC/$u — skip"
        continue
    fi
    cp "$SRC/$u" "$USER_SYSTEMD/$u"
    echo "  ✓ $u"
done

# ---- enable + start -----------------------------------------------------

# linger lets timers fire even when the user isn't logged in.
if ! loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q "Linger=yes"; then
    echo
    echo "⚠ Linger is OFF for user '$USER'."
    echo "  Timers will only fire while you're logged in. To enable forever:"
    echo "    sudo loginctl enable-linger $USER"
fi

echo
echo "Reloading systemd --user daemon..."
systemctl --user daemon-reload

echo "Enabling + starting timers..."
for t in paid-sweep.timer paid-review-sweep.timer paid-daily-snapshot.timer; do
    if systemctl --user enable --now "$t" 2>&1 | tail -1; then
        echo "  ✓ $t"
    else
        echo "  ✗ $t failed — check 'systemctl --user status $t'"
    fi
done

echo
echo "Active timers:"
systemctl --user list-timers --no-pager | grep -E "paid-(sweep|review-sweep|daily-snapshot)" || \
    echo "  (none — investigate 'systemctl --user list-units --all --type=timer')"

echo
echo "Logs:"
echo "  ~/.hermes/paid/sweep_pending.log"
echo "  ~/.hermes/paid/sweep_review.log"
echo "  ~/.hermes/paid/daily_snapshot.log"
echo
echo "Next: tail the logs after the first scheduled firing to verify."
