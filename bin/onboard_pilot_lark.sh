#!/usr/bin/env bash
# onboard_pilot_lark.sh — provision a fresh PAID pilot instance on the VPS
# for an owner whose IM is Lark/Feishu.
#
# Run as root on the VPS. Idempotent for everything except `adduser` —
# if the pilot user already exists, the script aborts so you don't
# clobber an in-flight instance.
#
# Usage:
#   onboard_pilot_lark.sh \
#       <pilot_slug> \
#       <owner_display_name> \
#       <feishu_app_id> \
#       <feishu_app_secret> \
#       <owner_open_id> \
#       <feishu_domain> \
#       <openrouter_api_key>
#
# Example:
#   ./onboard_pilot_lark.sh jelabs "JE Labs Founder" \
#       cli_a1b2c3d4e5f6g7h8 \
#       xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
#       ou_aaaabbbbccccddddeeeeffffgggghhhh \
#       lark \
#       sk-or-v1-xxxxxxxxxxxxxx
#
# feishu_domain: 'lark' (international, larksuite.com) or 'feishu' (China)

set -euo pipefail

if [[ $# -ne 7 ]]; then
    cat >&2 <<EOF
usage: $0 <pilot_slug> <owner_name> <app_id> <app_secret> <owner_open_id> <domain> <openrouter_key>

  pilot_slug      : lowercase short name, e.g. 'jelabs' — used as Linux username
                    (will become user 'pilot_jelabs')
  owner_name      : display name in PAID outbound, e.g. "JE Labs Founder"
  app_id          : Lark/Feishu App ID, format cli_xxxxxxxxxxxx
  app_secret      : Lark/Feishu App Secret, 64-char random string
  owner_open_id   : pilot's own Lark Open ID, format ou_xxxxxxxxxxxxx
  domain          : 'lark' (open.larksuite.com) or 'feishu' (open.feishu.cn)
  openrouter_key  : sk-or-v1-xxx, OpenRouter API key (we recommend per-pilot keys)

If any value contains spaces, wrap in quotes. The script aborts on the
first failed step — partial state is left in place for inspection.
EOF
    exit 2
fi

PILOT_SLUG="$1"
OWNER_NAME="$2"
FEISHU_APP_ID="$3"
FEISHU_APP_SECRET="$4"
OWNER_OPEN_ID="$5"
FEISHU_DOMAIN="$6"
OPENROUTER_KEY="$7"

PILOT_USER="pilot_${PILOT_SLUG}"
PILOT_HOME="/home/${PILOT_USER}"

# ---- validation ---------------------------------------------------------

[[ "$PILOT_SLUG" =~ ^[a-z0-9_-]+$ ]] || { echo "ERR: pilot_slug must be [a-z0-9_-]"; exit 2; }
[[ "$FEISHU_APP_ID" =~ ^cli_ ]] || { echo "ERR: app_id must start with cli_"; exit 2; }
[[ "$OWNER_OPEN_ID" =~ ^ou_ ]] || { echo "ERR: owner_open_id must start with ou_"; exit 2; }
[[ "$FEISHU_DOMAIN" == "lark" || "$FEISHU_DOMAIN" == "feishu" ]] || \
    { echo "ERR: domain must be 'lark' or 'feishu'"; exit 2; }

if id "$PILOT_USER" &>/dev/null; then
    echo "ERR: user $PILOT_USER already exists — refusing to clobber. To re-onboard, remove the user manually first." >&2
    exit 2
fi

echo "=== [1/8] Creating Linux user $PILOT_USER ==="
adduser --disabled-password --gecos "" "$PILOT_USER"
loginctl enable-linger "$PILOT_USER"

echo "=== [2/8] Cloning hermes-agent into $PILOT_HOME/.hermes/hermes-agent ==="
sudo -u "$PILOT_USER" -H bash <<'INNER'
set -euo pipefail
mkdir -p ~/.hermes
git clone --depth 1 https://github.com/NousResearch/hermes-agent ~/.hermes/hermes-agent
cd ~/.hermes/hermes-agent
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip --quiet
pip install -e . --quiet
INNER

echo "=== [3/8] Cloning paid-plugin v1.3.8 ==="
sudo -u "$PILOT_USER" -H bash <<'INNER'
set -euo pipefail
git clone --branch v1.3.8 --depth 1 \
    https://github.com/jimmyag2026-prog/paid-plugin \
    ~/.hermes/plugins/paid-v1
INNER

echo "=== [4/8] Writing ~/.hermes/.env ==="
sudo -u "$PILOT_USER" -H tee "${PILOT_HOME}/.hermes/.env" >/dev/null <<EOF
# Pilot: ${OWNER_NAME} — provisioned $(date -u +%Y-%m-%dT%H:%M:%SZ)

FEISHU_APP_ID=${FEISHU_APP_ID}
FEISHU_APP_SECRET=${FEISHU_APP_SECRET}
FEISHU_DOMAIN=${FEISHU_DOMAIN}

# Suppress hermes "no home channel set" onboarding prompt for new cps
# (see paid-plugin/INSTALL.md §0.1). Pointed at owner's open_id —
# v1.2.4 of PAID routes owner DMs via ou_ open_id directly so this
# is safe even though older hermes code wanted a chat_id.
FEISHU_HOME_CHANNEL=${OWNER_OPEN_ID}

OPENROUTER_API_KEY=${OPENROUTER_KEY}
EOF
chmod 600 "${PILOT_HOME}/.hermes/.env"

echo "=== [5/8] Writing ~/.hermes/config.yaml ==="
sudo -u "$PILOT_USER" -H tee "${PILOT_HOME}/.hermes/config.yaml" >/dev/null <<EOF
platforms:
  feishu:
    enabled: true

llm:
  provider: openrouter
  model: anthropic/claude-sonnet-4-5
  api_key: \${OPENROUTER_API_KEY}
EOF

echo "=== [6/8] Initialising PAID owner profile ==="
sudo -u "$PILOT_USER" -H bash <<INNER
set -euo pipefail
source ~/.hermes/hermes-agent/venv/bin/activate
cd ~/.hermes/plugins/paid-v1
python3 -m paid setup \\
    --owner-id "owner_${PILOT_SLUG}" \\
    --name "${OWNER_NAME}" \\
    --identity "feishu:${OWNER_OPEN_ID}"
INNER

echo "=== [7/8] Installing systemd units (hermes-gateway + paid-sweep + paid-review-sweep) ==="
sudo -u "$PILOT_USER" -H mkdir -p "${PILOT_HOME}/.config/systemd/user"
cp /home/paid/.config/systemd/user/hermes-gateway.service \
   "${PILOT_HOME}/.config/systemd/user/"
cp /home/paid/.config/systemd/user/paid-sweep.service \
   "${PILOT_HOME}/.config/systemd/user/"
cp /home/paid/.config/systemd/user/paid-sweep.timer \
   "${PILOT_HOME}/.config/systemd/user/"
cp /home/paid/.config/systemd/user/paid-review-sweep.service \
   "${PILOT_HOME}/.config/systemd/user/"
cp /home/paid/.config/systemd/user/paid-review-sweep.timer \
   "${PILOT_HOME}/.config/systemd/user/"
chown -R "${PILOT_USER}:${PILOT_USER}" "${PILOT_HOME}/.config/systemd"

systemctl --user --machine="${PILOT_USER}@" daemon-reload
systemctl --user --machine="${PILOT_USER}@" enable --now \
    hermes-gateway.service paid-sweep.timer paid-review-sweep.timer

echo "=== [8/8] Verifying liftoff ==="
sleep 5
systemctl --user --machine="${PILOT_USER}@" is-active hermes-gateway.service \
    || { echo "ERR: hermes-gateway did not become active"; exit 3; }

sudo -u "$PILOT_USER" -H bash <<INNER
set -e
echo
echo --- plugin loaded version ---
grep ^version ~/.hermes/plugins/paid-v1/plugin.yaml
echo
echo --- runtime log first 10 lines after restart ---
tail -10 ~/.hermes/paid/plugin_runtime.log 2>/dev/null || echo "(log not written yet — give it 5s and re-check)"
INNER

cat <<EOF

====================================================================
✓ Pilot ${OWNER_NAME} (slug=${PILOT_SLUG}) provisioned successfully.

User account : ${PILOT_USER}
Plugin dir   : ${PILOT_HOME}/.hermes/plugins/paid-v1
Owner profile: ${PILOT_HOME}/.hermes/paid/owner.json
Runtime log  : ${PILOT_HOME}/.hermes/paid/plugin_runtime.log
Gateway log  : ${PILOT_HOME}/.hermes/logs/gateway.log

Next steps:
  1. Ask the pilot to DM their bot once ("hi"). Confirm hermes log shows
     the inbound — that verifies the Lark app is wired correctly.
  2. Schedule the 45-min briefing call (Lark voice).
  3. After the call, edit persona.md + sop.md on their behalf via:
        sudo -u ${PILOT_USER} -H \$EDITOR ${PILOT_HOME}/.hermes/paid/persona.md
        sudo -u ${PILOT_USER} -H \$EDITOR ${PILOT_HOME}/.hermes/paid/sop.md
  4. Add their first counterparty:
        sudo -u ${PILOT_USER} -H bash -c '
          cd ~/.hermes/plugins/paid-v1
          python3 -m paid add-counterparty feishu <friend_open_id> \\
              --name "<Friend Name>" --role junior \\
              --topic-allow logistics --topic-allow schedule
        '

To remove this pilot later:
  systemctl --user --machine=${PILOT_USER}@ stop hermes-gateway.service paid-sweep.timer paid-review-sweep.timer
  systemctl --user --machine=${PILOT_USER}@ disable hermes-gateway.service paid-sweep.timer paid-review-sweep.timer
  loginctl disable-linger ${PILOT_USER}
  pkill -u ${PILOT_USER} || true
  deluser --remove-home ${PILOT_USER}
====================================================================
EOF
