#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Local settings (night window, ports, ...): see .env.example
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PORT="${DEVFLEET_PORT:-18801}"
UI_PORT="${DEVFLEET_UI_PORT:-3100}"
export DEVFLEET_API_URL="${DEVFLEET_API_URL:-http://localhost:$PORT}"  # agents' MCP tools + UI proxy

mkdir -p data

echo ""
echo "  ██████╗ ███████╗██╗   ██╗███████╗██╗     ███████╗███████╗████████╗"
echo "  ██╔══██╗██╔════╝██║   ██║██╔════╝██║     ██╔════╝██╔════╝╚══██╔══╝"
echo "  ██║  ██║█████╗  ██║   ██║█████╗  ██║     █████╗  █████╗     ██║   "
echo "  ██║  ██║██╔══╝  ╚██╗ ██╔╝██╔══╝  ██║     ██╔══╝  ██╔══╝     ██║   "
echo "  ██████╔╝███████╗ ╚████╔╝ ██║     ███████╗███████╗███████╗   ██║   "
echo "  ╚═════╝ ╚══════╝  ╚═══╝  ╚═╝     ╚══════╝╚══════╝╚══════╝   ╚═╝   "
echo ""
echo "  Coding Team Orchestrator"
echo ""

# Load fnm if available (for Node 22)
if [ -d "$HOME/.local/share/fnm" ]; then
  export PATH="$HOME/.local/share/fnm:$PATH"
  eval "$(fnm env)" 2>/dev/null
  fnm use 22 2>/dev/null || true
fi

# Check dependencies
command -v node >/dev/null 2>&1 || { echo "Error: node not found"; exit 1; }
command -v claude >/dev/null 2>&1 || { echo "Warning: claude CLI not found — agent dispatch won't work"; }

UV="$HOME/.local/bin/uv"
if ! command -v "$UV" &>/dev/null; then
  UV="$(command -v uv 2>/dev/null || true)"
fi
if [ -z "$UV" ]; then
  echo "  Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
fi

# Setup backend venv
echo "  Setting up backend venv..."
cd backend
if [ ! -d .venv ]; then
  "$UV" venv .venv
fi
source .venv/bin/activate
"$UV" pip install -q -r requirements.txt 2>/dev/null
cd ..

# Install the /nightshift Claude Code skill (repo-owned; re-copied on every start so updates land)
mkdir -p "$HOME/.claude/skills"
cp -R integrations/claude-code/skills/nightshift "$HOME/.claude/skills/"

# Install frontend deps
echo "  Installing frontend dependencies..."
cd frontend
npm install --silent 2>/dev/null
cd ..

# Start backend (in venv)
echo "  Starting Claude DevFleet API on port $PORT..."
cd backend
source .venv/bin/activate
# Loopback by default: the API can start coding agents, so don't expose it to the network
python3 -m uvicorn app:app --host "${DEVFLEET_HOST:-127.0.0.1}" --port "$PORT" --reload &
API_PID=$!
cd ..

# Start frontend
echo "  Starting Claude DevFleet UI on port $UI_PORT..."
cd frontend
npx vite --port "$UI_PORT" --strictPort &
UI_PID=$!
cd ..

echo ""
echo "  +-----------------------------------------+"
echo "  |  Claude DevFleet UI   -> http://localhost:$UI_PORT"
echo "  |  Claude DevFleet API  -> $DEVFLEET_API_URL"
echo "  +-----------------------------------------+"
echo ""
echo "  Press Ctrl+C to stop all services."
echo ""

cleanup() {
    echo ""
    echo "  Shutting down Claude DevFleet..."
    kill $API_PID $UI_PID 2>/dev/null
    wait $API_PID $UI_PID 2>/dev/null
    echo "  Done."
}

trap cleanup EXIT INT TERM
wait
