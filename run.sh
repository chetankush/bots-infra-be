#!/usr/bin/env bash
# One command to a working engine. Idempotent - safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf "\n\033[1;34m▸ %s\033[0m\n" "$1"; }

if [ ! -f .env ]; then
  say "creating .env"
  cp .env.example .env
  KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null \
        || uv run --with cryptography python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  sed -i.bak "s|^CREDENTIAL_ENCRYPTION_KEY=.*|CREDENTIAL_ENCRYPTION_KEY=${KEY}|" .env && rm -f .env.bak
  echo "  .env created — set FV_OPENROUTER_API_KEY in it before chatting"
fi

if ! grep -qE "^FV_OPENROUTER_API_KEY=.+" .env; then
  echo "  ⚠  FV_OPENROUTER_API_KEY is not set in .env"
fi

say "python env"
[ -d .venv ] || uv venv --python 3.12
uv pip install -q -e ".[dev]"

say "database"
docker compose up -d db
for i in $(seq 1 30); do
  docker compose exec -T db pg_isready -U engine >/dev/null 2>&1 && break
  sleep 1
done

say "schema + demo tenant"
.venv/bin/python scripts/seed.py
.venv/bin/python scripts/seed_evals.py northside-motors >/dev/null

say "widget"
if command -v npm >/dev/null 2>&1; then
  (cd widget && npm install --silent && npm run build >/dev/null) && echo "  widget/dist/widget.js built"
else
  echo "  npm not found — skipping widget build (console still works)"
fi

say "starting engine"
echo "  console : http://localhost:8000/console"
echo "  widget  : http://localhost:8000/demo"
echo "  docs    : http://localhost:8000/docs"
echo
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
