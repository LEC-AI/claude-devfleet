#!/bin/bash
# Install DevFleet as launchd services (auto-restart on crash/SIGKILL).
# Run once. After install, use: launchctl start/stop com.farhan.devfleet-api

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS_DIR"

stop_old() {
    local label="$1"
    launchctl list "$label" &>/dev/null && launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
}

install_plist() {
    local plist="$1"
    local label
    label=$(basename "$plist" .plist)
    stop_old "$label"
    cp "$plist" "$AGENTS_DIR/"
    launchctl bootstrap "gui/$(id -u)" "$AGENTS_DIR/$(basename "$plist")"
    echo "  ✓ $label installed and running"
}

echo ""
echo "  Installing Farhan's DevFleet™ as launchd services..."
echo ""

# Kill any processes currently on the ports so launchd can bind them
lsof -ti :18801 | xargs kill -9 2>/dev/null || true
lsof -ti :3100  | xargs kill -9 2>/dev/null || true

install_plist "$SCRIPT_DIR/com.farhan.devfleet-api.plist"
install_plist "$SCRIPT_DIR/com.farhan.devfleet-ui.plist"

echo ""
echo "  Services are now supervised by launchd."
echo "  They will restart automatically after any crash or SIGKILL."
echo ""
echo "  Commands:"
echo "    launchctl start  com.farhan.devfleet-api   # manual start"
echo "    launchctl stop   com.farhan.devfleet-api   # manual stop (restarts itself)"
echo "    launchctl kill   SIGTERM com.farhan.devfleet-api  # graceful stop without restart"
echo ""
echo "  Logs:"
echo "    tail -f $(dirname "$SCRIPT_DIR")/data/logs/api.log"
echo "    tail -f $(dirname "$SCRIPT_DIR")/data/logs/ui.log"
echo ""
