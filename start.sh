#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p data data/logs

GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || true)"
TODAY="$(date '+%a %d %b %Y')"

# ── Banner ────────────────────────────────────────────────────────────────
clear
echo ""
echo "  ╔══════════════════════════════════════════════════════════════════════╗"
echo "  ║                        [ Farhan's DevFleet™ ]                        ║"
echo "  ╚══════════════════════════════════════════════════════════════════════╝"
echo ""
echo "  ███████╗ █████╗ ██████╗ ██╗  ██╗ █████╗ ███╗   ██╗"
echo "  ██╔════╝██╔══██╗██╔══██╗██║  ██║██╔══██╗████╗  ██║"
echo "  █████╗  ███████║██████╔╝███████║███████║██╔██╗ ██║"
echo "  ██╔══╝  ██╔══██║██╔══██╗██╔══██║██╔══██║██║╚██╗██║"
echo "  ██║     ██║  ██║██║  ██║██║  ██║██║  ██║██║ ╚████║"
echo "  ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝'s"
echo ""
echo "  ██████╗ ███████╗██╗   ██╗███████╗██╗     ███████╗███████╗████████╗"
echo "  ██╔══██╗██╔════╝██║   ██║██╔════╝██║     ██╔════╝██╔════╝╚══██╔══╝"
echo "  ██║  ██║█████╗  ██║   ██║█████╗  ██║     █████╗  █████╗     ██║   "
echo "  ██║  ██║██╔══╝  ╚██╗ ██╔╝██╔══╝  ██║     ██╔══╝  ██╔══╝     ██║   "
echo "  ██████╔╝███████╗ ╚████╔╝ ██║     ███████╗███████╗███████╗   ██║   "
echo "  ╚═════╝ ╚══════╝  ╚═══╝  ╚═╝     ╚══════╝╚══════╝╚══════╝   ╚═╝   "
echo ""
echo "  Welcome back, Farhan  ·  ${TODAY}  ·  ${GIT_BRANCH} @ ${GIT_SHA}"
echo ""

# ── Fleet topology ────────────────────────────────────────────────────────
echo "  ┌─ Fleet ─ 18 slots · 10 lanes ──────────────────────────────────────┐"
echo "  │  🧠 orchestrator×3  🛠 coder×3  🔍 reviewer×2  🔒 security×1      │"
echo "  │  🧪 tester×2  🌐 e2e×2  ✅ qa×1  ⚡ dyn-test×1                    │"
echo "  │  🔬 researcher×2  🔭 explorer×1                                     │"
echo "  └─────────────────────────────────────────────────────────────────────┘"
echo ""

# ── Recently shipped ──────────────────────────────────────────────────────
echo "  ┌─ Recently shipped ──────────────────────────────────────────────────┐"
git log --oneline -5 2>/dev/null | while IFS= read -r line; do
  sha="${line:0:7}"; msg="${line:8}"
  [ ${#msg} -gt 57 ] && msg="${msg:0:54}..."
  printf "  │  %s  %-57s│\n" "$sha" "$msg"
done
echo "  └─────────────────────────────────────────────────────────────────────┘"
echo ""

# ── URLs ──────────────────────────────────────────────────────────────────
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 'YOUR_MAC_IP')"
echo "  ┌─ Access ────────────────────────────────────────────────────────────┐"
printf "  │  %-68s│\n" "🌐  Dashboard   →  http://localhost:3100"
printf "  │  %-68s│\n" "📱  Mobile/LAN  →  http://${LAN_IP}:3100"
printf "  │  %-68s│\n" "⚙️   API         →  http://localhost:18801"
printf "  │  %-68s│\n" "📖  API Docs    →  http://localhost:18801/docs"
echo "  └─────────────────────────────────────────────────────────────────────┘"
echo ""

# ── Command reference ─────────────────────────────────────────────────────
echo "  ┌─ Commands ──────────────────────────────────────────────────────────┐"
echo "  │                                                                      │"
echo "  │  FLEET SUPERVISION (launchd — survives SIGKILL)                     │"
echo "  │  ─────────────────────────────────────────────                      │"
printf "  │  %-68s│\n" "bash launchd/install.sh          Install as supervised services"
printf "  │  %-68s│\n" "bash launchd/uninstall.sh        Remove launchd services"
printf "  │  %-68s│\n" "launchctl list | grep devfleet   Check service status"
printf "  │  %-68s│\n" "launchctl stop com.farhan.devfleet-api   Stop API (auto-restarts)"
printf "  │  %-68s│\n" "launchctl kill SIGTERM gui/\$(id -u)/com.farhan.devfleet-api  Graceful stop"
echo "  │                                                                      │"
echo "  │  LOGS                                                                │"
echo "  │  ────                                                                │"
printf "  │  %-68s│\n" "tail -f data/logs/api.log        Live API output"
printf "  │  %-68s│\n" "tail -f data/logs/ui.log         Live UI output"
printf "  │  %-68s│\n" "tail -f data/logs/api-error.log  API errors only"
echo "  │                                                                      │"
echo "  │  FLEET API (quick curl)                                             │"
echo "  │  ─────────────────────                                              │"
printf "  │  %-68s│\n" "curl localhost:18801/api/fleet/summary   Slots + cost today"
printf "  │  %-68s│\n" "curl localhost:18801/api/lanes           All 10 lanes + live counts"
printf "  │  %-68s│\n" "curl localhost:18801/api/missions        All missions"
printf "  │  %-68s│\n" "curl localhost:18801/api/projects        All projects"
printf "  │  %-68s│\n" "curl localhost:18801/api/sessions        All agent sessions"
printf "  │  %-68s│\n" "curl localhost:18801/api/reports         All filed reports"
echo "  │                                                                      │"
echo "  │  WEB DASHBOARD PAGES                                                │"
echo "  │  ───────────────────                                                │"
printf "  │  %-68s│\n" "/                 Mission Control — live fleet overview"
printf "  │  %-68s│\n" "/projects         All projects — create, view, dispatch"
printf "  │  %-68s│\n" "/missions         Full mission board — filter by status/lane"
printf "  │  %-68s│\n" "/reports          Filed agent reports — browse outcomes"
printf "  │  %-68s│\n" "/fleet-config     Lane capacity, models, tool presets"
printf "  │  %-68s│\n" "/prompt-studio    Edit lane prompts + MCP tool toggles (new)"
printf "  │  %-68s│\n" "/integrations     MCP server configs + external tool wiring"
printf "  │  %-68s│\n" "/status           Health monitor — services + incidents"
echo "  │                                                                      │"
echo "  │  GIT                                                                 │"
echo "  │  ───                                                                 │"
printf "  │  %-68s│\n" "git -C . log --oneline -10       Recent fleet commits"
printf "  │  %-68s│\n" "git -C . status                  Working tree state"
echo "  │                                                                      │"
echo "  └──────────────────────────────────────────────────────────────────────┘"
echo ""
echo "  ⚠️  NOTE: If launchd is installed, services auto-restart on crash."
echo "      This script starts in MANUAL mode (Ctrl+C stops everything)."
echo "      Run  bash launchd/install.sh  for persistent supervised mode."
echo ""

# ── Dependencies ──────────────────────────────────────────────────────────
if [ -d "$HOME/.local/share/fnm" ]; then
  export PATH="$HOME/.local/share/fnm:$PATH"
  eval "$(fnm env)" 2>/dev/null
  fnm use 22 2>/dev/null || true
fi

command -v node >/dev/null 2>&1 || { echo "  ✗ Error: node not found"; exit 1; }
command -v claude >/dev/null 2>&1 || echo "  ⚠  Warning: claude CLI not found — agent dispatch won't work"

UV="$HOME/.local/bin/uv"
if ! command -v "$UV" &>/dev/null; then UV="$(command -v uv 2>/dev/null || true)"; fi
if [ -z "$UV" ]; then
  echo "  Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
fi

echo "  Setting up backend venv..."
cd backend
[ ! -d .venv ] && "$UV" venv .venv
source .venv/bin/activate
"$UV" pip install -q -r requirements.txt 2>/dev/null
cd ..

echo "  Installing frontend deps..."
cd frontend && npm install --silent 2>/dev/null && cd ..

# Kill any zombies on the ports
lsof -ti :18801 | xargs kill -9 2>/dev/null || true
lsof -ti :3100  | xargs kill -9 2>/dev/null || true

# ── Start services ─────────────────────────────────────────────────────────
echo "  Starting API  → http://localhost:18801"
cd backend && source .venv/bin/activate
python3 -m uvicorn app:app --host 0.0.0.0 --port 18801 --reload >> ../data/logs/api.log 2>> ../data/logs/api-error.log &
API_PID=$!
cd ..

echo "  Starting UI   → http://localhost:3100"
cd frontend
npx vite --port 3100 --host >> ../data/logs/ui.log 2>&1 &
UI_PID=$!
cd ..

echo ""
echo "  ✓ Fleet is live. Logs → data/logs/  |  Ctrl+C to stop."
echo ""

cleanup() {
  echo ""
  echo "  Shutting down Farhan's DevFleet™..."
  kill $API_PID $UI_PID 2>/dev/null
  wait $API_PID $UI_PID 2>/dev/null
  echo "  Done."
}
trap cleanup EXIT INT TERM
wait
