#!/usr/bin/env bash
# Nightly pipeline for qc-screener — invoked by launchd (see plist).
#
# Runs the four-step refresh (crawl --full → prune → extract → notify) and logs to
# data/logs/nightly-YYYY-MM-DD.log. Deliberately NOT set -e: we want each step to
# run even if a previous one failed, so a broken scraper doesn't silence the notify.
# Overall exit status = failure count.

set -uo pipefail

# Keep the Mac awake for the whole run so idle sleep can't freeze the crawl
# mid-flight. A frozen crawl drops its network connections and, on wake, throws
# RemoteProtocolError/ReadTimeout — which is how a ~90-min run got smeared across
# ~15h of hourly DarkWakes on 2026-08-19. Re-exec once under caffeinate.
# NOTE: caffeinate blocks *idle* sleep only. Closing the lid still sleeps unless
# the Mac is in clamshell mode (external display + power). For a reliable 03:00
# run: leave it plugged in with the lid open.
if [ -z "${QCS_CAFFEINATED:-}" ] && command -v caffeinate >/dev/null 2>&1; then
    export QCS_CAFFEINATED=1
    exec caffeinate -i "$0" "$@"
fi

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# Load secrets (NTFY_TOPIC, ANTHROPIC_API_KEY). launchd does not source ~/.zshrc.
if [ -f "$HOME/.qc-screener.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$HOME/.qc-screener.env"
    set +a
fi

LOG_DIR="$PROJECT_DIR/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/nightly-$(date +%Y-%m-%d).log"

# Redirect stdout+stderr to log file AND stdout (so launchd stderr log stays useful).
exec > >(tee -a "$LOG_FILE") 2>&1

# Rotate: drop logs older than 30 days.
find "$LOG_DIR" -maxdepth 1 -name "nightly-*.log" -type f -mtime +30 -delete 2>/dev/null || true

QCS="$PROJECT_DIR/.venv/bin/qc-screener"
if [ ! -x "$QCS" ]; then
    echo "[FATAL] $QCS not found or not executable. Run: pip install -e ."
    exit 2
fi

echo "==================================================="
echo "qc-screener nightly — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "==================================================="

failed_steps=()

run_step() {
    local name="$1"; shift
    echo ""
    echo "[$name] START — $(date +%H:%M:%S)"
    if "$@"; then
        echo "[$name] OK — $(date +%H:%M:%S)"
    else
        local rc=$?
        echo "[$name] FAILED (exit $rc) — $(date +%H:%M:%S)"
        failed_steps+=("$name")
    fi
}

run_step crawl   "$QCS" crawl --source all --full
run_step prune   "$QCS" prune --days 7
run_step extract "$QCS" extract --all
run_step notify  "$QCS" notify --scan

echo ""
echo "==================================================="
if [ ${#failed_steps[@]} -eq 0 ]; then
    echo "Nightly OK — all steps succeeded — $(date +%H:%M:%S)"
    exit 0
fi

echo "Nightly FAILED — ${#failed_steps[@]} step(s) errored: ${failed_steps[*]}"
# Push a failure notification if NTFY_TOPIC is configured. Silent on curl failure —
# we're already failing, no need to cascade.
if [ -n "${NTFY_TOPIC:-}" ]; then
    curl -s -X POST "https://ntfy.sh/$NTFY_TOPIC" \
        -H "Title: qc-screener nightly failed" \
        -H "Priority: high" \
        -H "Tags: warning" \
        -d "Failed steps: ${failed_steps[*]}. Log: $LOG_FILE" \
        > /dev/null || true
fi
exit 1
