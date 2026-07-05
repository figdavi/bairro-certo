#!/usr/bin/env bash
# One-shot startup: mint (or reuse) a Claude OAuth token, save it to .env, launch the server.
#
# Usage:
#   ./start.sh                 # reuse token from .env if present, else run `claude setup-token`
#   ./start.sh --new-token     # force minting a fresh token (re-runs the browser auth flow)
#   ./start.sh --port 8080     # serve on a different port (default 8000)
#   ./start.sh --app app2      # run the guided finder (app2.py) instead of the free-text app
set -euo pipefail

cd "$(dirname "$0")"

ENV_FILE=".env"
PORT=8000
FORCE_NEW=0
APP_MODULE="app"

while [ $# -gt 0 ]; do
  case "$1" in
    --new-token) FORCE_NEW=1 ;;
    --port) PORT="$2"; shift ;;
    --app) APP_MODULE="$2"; shift ;;
    *) echo "unknown option: $1  (known: --new-token, --port N, --app MODULE)" >&2; exit 1 ;;
  esac
  shift
done

# Locate the claude CLI (same resolution app.py uses).
if command -v claude >/dev/null 2>&1; then
  CLAUDE_BIN=$(command -v claude)
elif [ -x "$HOME/.local/bin/claude" ]; then
  CLAUDE_BIN="$HOME/.local/bin/claude"
else
  echo "error: claude CLI not found — install Claude Code first (https://claude.com/claude-code)" >&2
  exit 1
fi

# Reuse an existing token unless --new-token was passed.
TOKEN=""
if [ "$FORCE_NEW" -eq 0 ] && [ -f "$ENV_FILE" ]; then
  TOKEN=$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)
fi

if [ -n "$TOKEN" ]; then
  echo "→ Reusing OAuth token from $ENV_FILE (run with --new-token to mint a fresh one)"
else
  echo "→ Minting a new OAuth token via 'claude setup-token' (a browser window will open)…"
  # tee to stderr so the interactive instructions stay visible while we capture the output
  RAW=$("$CLAUDE_BIN" setup-token | tee /dev/stderr)
  TOKEN=$(grep -oE 'sk-ant-oat[0-9]+-[A-Za-z0-9_-]+' <<<"$RAW" | tail -1 || true)
  if [ -z "$TOKEN" ]; then
    echo "error: could not find a token (sk-ant-oat…) in 'claude setup-token' output" >&2
    exit 1
  fi
  touch "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  if grep -qE '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE"; then
    sed -i "s|^CLAUDE_CODE_OAUTH_TOKEN=.*|CLAUDE_CODE_OAUTH_TOKEN=$TOKEN|" "$ENV_FILE"
  else
    printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$TOKEN" >> "$ENV_FILE"
  fi
  echo "→ Token saved to $ENV_FILE"
fi

export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"

# Keep the token out of version control.
if ! grep -qxF '.env' .gitignore 2>/dev/null; then
  echo '.env' >> .gitignore
fi

# Prefer the project venv's uvicorn.
if [ -x ".venv/bin/uvicorn" ]; then
  UVICORN=".venv/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
  UVICORN=$(command -v uvicorn)
else
  echo "error: uvicorn not found — run: pip install -r requirements.txt" >&2
  exit 1
fi

# Ensure the claude CLI's directory is on PATH for app.py's subprocess calls.
export PATH="$(dirname "$CLAUDE_BIN"):$PATH"

echo "→ Starting server ($APP_MODULE) on http://localhost:$PORT"
exec "$UVICORN" "$APP_MODULE:app" --reload --port "$PORT"
