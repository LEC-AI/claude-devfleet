#!/bin/bash
# Install DevFleet as launchd services (auto-restart on crash/SIGKILL).
# Run once. After install, use: launchctl start/stop com.farhan.devfleet-api

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS_DIR"

stop_old() {
    local label="$1"
    if launchctl list "$label" &>/dev/null; then
        launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
        # Give launchd a moment to settle after bootout before re-bootstrapping
        sleep 1
    fi
}

install_plist() {
    local plist="$1"
    local label
    label=$(basename "$plist" .plist)
    stop_old "$label"
    cp "$plist" "$AGENTS_DIR/"
    # Retry once on transient I/O error (5) — launchd race on rapid bootout→bootstrap
    if ! launchctl bootstrap "gui/$(id -u)" "$AGENTS_DIR/$(basename "$plist")" 2>/dev/null; then
        sleep 2
        launchctl bootstrap "gui/$(id -u)" "$AGENTS_DIR/$(basename "$plist")"
    fi
    echo "  ✓ $label installed and running"
}

echo ""
echo "  Installing Farhan's DevFleet™ as launchd services..."
echo ""

# Kill any processes currently on the ports so launchd can bind them cleanly
lsof -ti :18801 | xargs kill -9 2>/dev/null || true
lsof -ti :3100  | xargs kill -9 2>/dev/null || true
sleep 1  # let the OS release the ports before launchd tries to bind them

install_plist "$SCRIPT_DIR/com.farhan.devfleet-api.plist"
install_plist "$SCRIPT_DIR/com.farhan.devfleet-ui.plist"

echo ""
echo "  Services are now supervised by launchd."
echo "  They will restart automatically after any crash or SIGKILL."
echo ""
echo "  Commands:"
echo "    launchctl start  com.farhan.devfleet-api   # manual start"
echo "    launchctl stop   com.farhan.devfleet-api   # manual stop (restarts itself)"
echo "    launchctl kill   SIGTERM gui/$(id -u)/com.farhan.devfleet-api  # graceful stop without restart"
echo ""
echo "  Logs:"
echo "    tail -f $(dirname "$SCRIPT_DIR")/data/logs/api.log"
echo "    tail -f $(dirname "$SCRIPT_DIR")/data/logs/ui.log"
echo ""
