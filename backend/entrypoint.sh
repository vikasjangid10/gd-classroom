#!/usr/bin/env bash
set -euo pipefail

echo "[entrypoint] applying migrations"
alembic upgrade head

echo "[entrypoint] seeding reference data"
python -m scripts.seed

# The Piper voice is ~60 MB and lives in a volume, so this is a no-op after the first
# boot. It never fails the container: a missing voice degrades the moderator to the
# scripted one, which is a worse discussion, not a broken deployment.
if [ "${TTS_BACKEND:-auto}" = "piper" ]; then
  echo "[entrypoint] ensuring the Piper voice is present"
  python -m scripts.fetch_piper_voice || true
fi

echo "[entrypoint] starting API"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
