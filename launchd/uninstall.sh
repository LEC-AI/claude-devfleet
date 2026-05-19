#!/bin/bash
# Remove DevFleet launchd services. Processes will stop and not restart.
set -e
AGENTS_DIR="$HOME/Library/LaunchAgents"

for label in com.farhan.devfleet-api com.farhan.devfleet-ui; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null && echo "  ✓ $label stopped and removed" || echo "  – $label not loaded"
    rm -f "$AGENTS_DIR/${label}.plist"
done

echo ""
echo "  DevFleet services uninstalled. Use ./start.sh for manual mode."
echo ""
