#!/usr/bin/env bash
# dev-bot watchdog — periodic health probe + crash notification.
#
# Two failure modes we guard against:
#   1) dev-bot crashes hard (Python exception during startup, broken
#      env, unable to connect to Telegram). systemd ``Restart=on-failure``
#      tries to bring it back, but if every restart fails the same way
#      we end up in a 5-second loop and nobody learns about it. After
#      ``StartLimitBurst=5`` failures in ``StartLimitIntervalSec=300``,
#      systemd gives up and the unit stays inactive — so this watchdog
#      checks ``systemctl is-active`` directly.
#   2) dev-bot is "active" but stuck (deadlocked event loop, frozen on
#      a network call). We can't easily probe liveness from outside —
#      that needs an in-process keepalive. Out of scope for this script;
#      see ROADMAP item "progress messages for long tasks" — same
#      mechanism would emit a "still alive" heartbeat we could check.
#
# This script ONLY catches case 1, but case 1 is what bit us today.
#
# Notifications go through the slot-hunter Telegram bot (different
# token) so we can still get alerts even if the dev-bot itself is
# the one that's down. Owner chat id and bot token are sourced from
# slot-hunter's .env (TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_CHAT_ID).

set -euo pipefail

UNIT="dev-bot.service"
SLOT_ENV="/opt/slot-hunter/.env"
STATE_FILE="/var/lib/slothunter-dev-bot/watchdog.state"

# Cooldown: don't spam notifications. After alerting, wait this long
# before re-alerting on the same continuous downtime.
NOTIFY_COOLDOWN_SEC=900   # 15 min

# Don't pull the wolf-cried-wolf trigger on a brief blip. Wait this long
# of continuous "inactive" before sending the first alert.
INACTIVE_GRACE_SEC=120    # 2 min

mkdir -p "$(dirname "$STATE_FILE")"

now=$(date +%s)

# ─────────── Notify helper ───────────
notify() {
    local text="$1"
    [[ -f "$SLOT_ENV" ]] || return 0
    # shellcheck disable=SC1090
    set +u; source "$SLOT_ENV"; set -u
    [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] && return 0
    [[ -z "${TELEGRAM_OWNER_CHAT_ID:-}" ]] && return 0
    curl -fsS --max-time 10 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_OWNER_CHAT_ID}" \
        -d "parse_mode=HTML" \
        --data-urlencode "text=${text}" >/dev/null 2>&1 || true
}

# ─────────── State helpers ───────────
read_state() {
    # Format: ``<status> <since_ts> <last_notified_ts>``
    if [[ -f "$STATE_FILE" ]]; then
        cat "$STATE_FILE"
    else
        echo "active $now 0"
    fi
}

write_state() {
    echo "$1 $2 $3" > "$STATE_FILE"
}

# ─────────── Probe ───────────
if systemctl is-active --quiet "$UNIT"; then
    current="active"
else
    current="inactive"
fi

read prev_status prev_since prev_notified <<< "$(read_state)"

if [[ "$current" == "active" ]]; then
    # Recovery — only notify if we previously alerted about a crash.
    if [[ "$prev_status" == "inactive" && "$prev_notified" != "0" ]]; then
        notify "✅ <b>dev-bot recovered</b> (был down с $(date -d "@$prev_since" '+%H:%M %d.%m'))"
    fi
    write_state "active" "$now" "0"
    exit 0
fi

# current=inactive
since="$prev_since"
if [[ "$prev_status" == "active" ]]; then
    since="$now"
fi

down_for=$(( now - since ))
last_notified="$prev_notified"

if (( down_for < INACTIVE_GRACE_SEC )); then
    write_state "inactive" "$since" "$last_notified"
    exit 0
fi

# Past the grace window. Notify if we haven't recently.
since_last_notify=$(( now - last_notified ))
if (( last_notified == 0 || since_last_notify >= NOTIFY_COOLDOWN_SEC )); then
    short_log=$(journalctl -u "$UNIT" -n 8 --no-pager 2>/dev/null \
        | tail -8 | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g' \
        | head -c 1500)
    body=$(printf '⚠️ <b>dev-bot down for %d min</b>\nHost: %s\nUnit: %s\n\n<pre>%s</pre>' \
        "$(( down_for / 60 ))" "$(hostname)" "$UNIT" "$short_log")
    notify "$body"
    last_notified="$now"
fi

write_state "inactive" "$since" "$last_notified"
