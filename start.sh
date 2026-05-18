#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p data

GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || true)"
TODAY="$(date '+%a %d %b %Y')"
if [ -n "$GIT_BRANCH" ] && [ -n "$GIT_SHA" ]; then
  SUBTITLE="Welcome back, Farhan.  ·  ${TODAY}  ·  ${GIT_BRANCH} @ ${GIT_SHA}"
else
  SUBTITLE="Welcome back, Farhan.  ·  ${TODAY}"
fi

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
echo "  $SUBTITLE"
echo ""
echo "  ┌─ Fleet ─ 18 slots · 10 lanes ──────────────────────────────────────┐"
echo "  │  orchestrator×3  coder×3  reviewer×2  security×1                   │"
echo "  │  tester×2  e2e×2  qa×1  dyn-test×1  researcher×2  explorer×1       │"
echo "  └─────────────────────────────────────────────────────────────────────┘"
echo ""
echo "  ┌─ Recently shipped ──────────────────────────────────────────────────┐"
git log --oneline -4 2>/dev/null | while IFS= read -r gitline; do
  sha="${gitline:0:7}"
  msg="${gitline:8}"
  if [ ${#msg} -gt 58 ]; then msg="${msg:0:55}..."; fi
  printf "  │  %s  %-58s│\n" "$sha" "$msg"
done
echo "  └─────────────────────────────────────────────────────────────────────┘"
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

# Install frontend deps
echo "  Installing frontend dependencies..."
cd frontend
npm install --silent 2>/dev/null
cd ..

# Clear any zombie processes holding the ports before starting
lsof -ti :18801 | xargs kill -9 2>/dev/null || true
lsof -ti :3100  | xargs kill -9 2>/dev/null || true

# Start backend (in venv)
echo "  Starting Farhan's DevFleet™ API on port 18801..."
cd backend
source .venv/bin/activate
python3 -m uvicorn app:app --host 0.0.0.0 --port 18801 --reload &
API_PID=$!
cd ..

# Start frontend
echo "  Starting Farhan's DevFleet™ UI on port 3100..."
cd frontend
npx vite --port 3100 --host &
UI_PID=$!
cd ..

echo ""
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 'YOUR_MAC_IP')"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║  UI   →  http://localhost:3100                           ║"
echo "  ║  UI   →  http://${LAN_IP}:3100   (phone/tablet)         ║"
echo "  ║  API  →  http://localhost:18801                          ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo ""
echo "  Press Ctrl+C to stop all services."
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
