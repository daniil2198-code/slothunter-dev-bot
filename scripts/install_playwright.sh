#!/usr/bin/env bash
# One-time install for Playwright MCP on the VPS.
#
# What this does:
#   1. Pre-fetches the @playwright/mcp npm package + chromium browser
#      so the first ``npx @playwright/mcp`` invocation isn't a 30-second
#      cold start. Otherwise the dev-bot's first browser query times out.
#   2. Installs system shared libraries Chromium needs on Ubuntu (fonts,
#      codecs, libnss). Without these the browser launches but pages
#      render without text or crash on protected media.
#
# Idempotent — safe to run again.

set -euo pipefail

ts() { date "+[%Y-%m-%dT%H:%M:%S%z]"; }
log() { echo "$(ts) $*"; }

# ─────────── 1. Node + npx ───────────
if ! command -v node >/dev/null 2>&1; then
  log "ERROR: Node.js not installed. Install Node 20+ first."
  exit 1
fi
NODE_MAJOR=$(node -v | sed 's/^v//' | cut -d. -f1)
if [ "$NODE_MAJOR" -lt 20 ]; then
  log "ERROR: Node $NODE_MAJOR is too old. Need ≥ 20."
  exit 1
fi
log "node $(node -v)"

# ─────────── 2. Cache @playwright/mcp ───────────
# `npx -y` will re-fetch on cache miss; pre-warm so the first dev-bot
# query has a cached package.
log "warming @playwright/mcp npm cache..."
npx -y @playwright/mcp@latest --help >/dev/null 2>&1 || {
  log "npx @playwright/mcp invocation returned non-zero — that's expected for --help on some versions; continuing"
}

# ─────────── 3. Chromium + system deps ───────────
# Use Playwright's own installer; ``--with-deps`` pulls apt packages on
# Debian/Ubuntu. Idempotent — Playwright skips already-installed.
log "installing chromium + system deps via playwright..."
npx -y playwright@latest install --with-deps chromium

log "===== install_playwright DONE ====="
log "next: set PLAYWRIGHT_MCP_ENABLED=true in /opt/slothunter-dev-bot/.env"
log "      and restart the service: systemctl restart dev-bot"
