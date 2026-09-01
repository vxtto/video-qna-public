#!/usr/bin/env bash
# Bring up this worktree's docker-compose stack (db + api) on host ports
# that won't collide with any other worktree's stack already running on
# this box.
#
# Why this exists: docker-compose.yml hardcoded 5432/8000 originally, so
# only one worktree's stack could run at a time — see PLAN.md's "Running
# several worktrees in parallel". Those ports are now read from
# ${DB_PORT}/${API_PORT} (compose defaults still fall back to 5432/8000 for
# a single-worktree setup). This script's only job is to make sure THIS
# worktree's .env has a port pair nothing else on the box is using, then
# hand off to `docker compose up` as normal.
#
# Safe to re-run: it never touches DB_PORT/APIPORT once they're already
# set in .env, so a worktree keeps the same ports across restarts.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -f .env ]; then
  echo "no .env yet — copying .env.example"
  cp .env.example .env
fi

# Ask the OS for a currently-free ephemeral port. This is a point-in-time
# check (TOCTOU race with whatever binds it next is possible in theory),
# which is an acceptable tradeoff for local dev tooling, not production
# orchestration.
free_port() {
  python3 - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

ensure_port() {
  local var="$1"
  if grep -qE "^${var}=[0-9]+" .env; then
    return  # already assigned in a previous run — leave it alone
  fi
  # Wipe any commented-out / empty placeholder line from .env.example so we
  # don't end up with two conflicting lines for the same var.
  sed -i -E "/^#? ?${var}=/d" .env
  local port
  port="$(free_port)"
  echo "${var}=${port}" >> .env
  echo "assigned ${var}=${port}"
}

ensure_port DB_PORT
ensure_port API_PORT

# Pull in just the port assignments, not the whole file — a blind `source
# .env` would clobber OPENROUTER_API_KEY (blank in .env by design) if it's
# already exported from ../scripts/with-secrets.sh, breaking the secrets
# flow silently. See ../PLAN.md "Secrets management".
DB_PORT="$(grep -E '^DB_PORT=' .env | tail -1 | cut -d= -f2)"
API_PORT="$(grep -E '^API_PORT=' .env | tail -1 | cut -d= -f2)"
export DB_PORT API_PORT
echo "db:  127.0.0.1:${DB_PORT}"
echo "api: http://localhost:${API_PORT}"

docker compose up -d --build
