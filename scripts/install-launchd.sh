#!/usr/bin/env bash
# Installs the qc-screener nightly launchd agent.
# Idempotent: safe to re-run to pick up path changes or plist edits.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.qc-screener.nightly"
PLIST_SRC="$PROJECT_DIR/scripts/${LABEL}.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
ENV_FILE="$HOME/.qc-screener.env"

# Sanity checks
if [ ! -x "$PROJECT_DIR/.venv/bin/qc-screener" ]; then
    echo "[FATAL] $PROJECT_DIR/.venv/bin/qc-screener is not executable."
    echo "        Run:  cd $PROJECT_DIR && python3 -m venv .venv && .venv/bin/pip install -e ."
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    cat <<EOF
[FATAL] $ENV_FILE not found.

Create it first (launchd won't source ~/.zshrc):

    cat > $ENV_FILE <<'ENVEOF'
    export NTFY_TOPIC=qc-screener-<your-random-slug>
    export ANTHROPIC_API_KEY=sk-ant-...
    ENVEOF
    chmod 600 $ENV_FILE

Then re-run this installer.
EOF
    exit 1
fi

# Warn (don't fail) if env vars look empty.
if ! grep -q "^export NTFY_TOPIC=" "$ENV_FILE"; then
    echo "[warn] NTFY_TOPIC not set in $ENV_FILE — notify step will fail nightly."
fi
if ! grep -q "^export ANTHROPIC_API_KEY=" "$ENV_FILE"; then
    echo "[warn] ANTHROPIC_API_KEY not set in $ENV_FILE — extract step will fail nightly."
fi

# Render template with the absolute project path.
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_DST"

# Unload if already loaded, then load. This picks up any changes.
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

# Confirm it's registered. Query the label directly rather than piping
# `launchctl list` into `grep -q`: grep exits on first match, launchctl then
# takes SIGPIPE (exit 141), and `set -o pipefail` would turn that successful
# match into a pipeline failure -> false FATAL.
if launchctl list "$LABEL" >/dev/null 2>&1; then
    echo "OK  Installed $LABEL"
    echo "    Schedule: nightly 03:00 (catches up on next wake if Mac was asleep)"
    echo "    Plist:    $PLIST_DST"
    echo "    Runner:   $PROJECT_DIR/scripts/nightly.sh"
    echo "    Logs:     $PROJECT_DIR/data/logs/nightly-YYYY-MM-DD.log"
    echo ""
    echo "Verify:"
    echo "    launchctl list | grep qc-screener"
    echo ""
    echo "Test-run now (does NOT wait for 03:00):"
    echo "    launchctl start $LABEL"
    echo "    tail -f $PROJECT_DIR/data/logs/nightly-\$(date +%Y-%m-%d).log"
else
    echo "[FATAL] launchctl did not register $LABEL. Check syntax:"
    echo "        plutil $PLIST_DST"
    exit 1
fi
