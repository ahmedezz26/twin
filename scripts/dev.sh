#!/bin/bash
# Run the twin locally: the backend in Docker (rebuilt when you save a file in backend/) and the frontend dev server.
# Usage: ./scripts/dev.sh        Ctrl+C stops both and removes the backend container (conversations stay in memory/).
set -euo pipefail

cd "$(dirname "$0")/.."        # project root

# Fail early with a clear message instead of a confusing error later
docker info > /dev/null 2>&1 || { echo "❌ Docker isn't running: start Docker Desktop first."; exit 1; }
[ -f .env ] || { echo "❌ No .env in the repo root: copy .env.example to .env and fill it in."; exit 1; }

# Runs on every exit (Ctrl+C, an error, or the frontend stopping): stop the watcher, remove the backend container
cleanup() {
    echo "🧹 Stopping the backend..."
    [ -n "${WATCH_PID:-}" ] && kill "$WATCH_PID" 2> /dev/null || true
    docker compose down
}
trap cleanup EXIT

# 1. Backend: build, start, and wait until its healthcheck says healthy
echo "🐳 Starting the backend on http://localhost:8000 ..."
docker compose up --build --detach --wait

# 2. Rebuild and restart the backend when backend/ changes (in the background, quietly)
docker compose watch --no-up --quiet &
WATCH_PID=$!

# 3. Frontend: runs in the foreground until Ctrl+C
echo "🌐 Starting the frontend (its URL is printed below; 3001 if port 3000 is busy)..."
cd frontend
[ -d node_modules ] || npm ci --ignore-scripts
npm run dev
