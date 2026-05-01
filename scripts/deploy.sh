#!/usr/bin/env bash
# Slot Hunter dev-bot — VPS deploy script.
#
# Usage: bash scripts/deploy.sh
# Runs on the VPS in /opt/slothunter-dev-bot. Pulls latest, syncs deps,
# restarts the systemd service.

set -euo pipefail

APP_DIR="/opt/slothunter-dev-bot"
SERVICE="dev-bot"

cd "$APP_DIR"

ts() { date "+[%Y-%m-%dT%H:%M:%S%z]"; }
log() { echo "$(ts) $*"; }

# ─────────── 1. Pull latest ───────────
log "[1/4] git pull..."
git fetch origin main
git reset --hard origin/main
SHORT="$(git rev-parse --short HEAD)"

# ─────────── 2. Ensure uv ───────────
if ! command -v uv >/dev/null 2>&1; then
  if [ -x "/root/.local/bin/uv" ]; then
    export PATH="/root/.local/bin:$PATH"
  else
    log "uv not installed — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="/root/.local/bin:$PATH"
  fi
fi

# ─────────── 3. Sync deps ───────────
log "[2/4] uv sync..."
uv sync --no-dev

# ─────────── 4. Ensure runtime dirs ───────────
log "[3/4] ensuring state dir /var/lib/slothunter-dev-bot..."
mkdir -p /var/lib/slothunter-dev-bot
chmod 700 /var/lib/slothunter-dev-bot

# ─────────── 5. Install/refresh systemd unit ───────────
UNIT_SRC="$APP_DIR/scripts/dev-bot.service"
UNIT_DST="/etc/systemd/system/${SERVICE}.service"
if ! cmp -s "$UNIT_SRC" "$UNIT_DST"; then
  log "[4/4] installing systemd unit..."
  cp "$UNIT_SRC" "$UNIT_DST"
  systemctl daemon-reload
fi

# ─────────── 6. Restart ───────────
log "restarting ${SERVICE}..."
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

sleep 2
if systemctl is-active --quiet "$SERVICE"; then
  log "===== deploy DONE — ${SHORT} ====="
  log "  follow logs: journalctl -u ${SERVICE} -f"
else
  log "ERROR: service failed to start. Recent logs:"
  journalctl -u "$SERVICE" --no-pager -n 30
  exit 1
fi
