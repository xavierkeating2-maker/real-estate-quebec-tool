#!/usr/bin/env bash
# Unload + remove the qc-screener nightly launchd agent.
# Leaves data/logs/ and ~/.qc-screener.env untouched.
set -euo pipefail

LABEL="com.qc-screener.nightly"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm "$PLIST_DST"
    echo "OK  Uninstalled $LABEL (plist removed, logs kept)"
else
    echo "$LABEL is not installed (no plist at $PLIST_DST)."
fi
