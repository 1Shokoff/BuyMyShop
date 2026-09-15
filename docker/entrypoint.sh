#!/usr/bin/env bash
set -euo pipefail

# Ждём, пока Postgres начнёт принимать соединения: compose depends_on healthcheck
# закрывает основной случай, но при рестарте контейнера БД гонка всё ещё возможна.
python - <<'PY'
import os, sys, time
import psycopg

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
deadline = time.time() + 60
while True:
    try:
        with psycopg.connect(url, connect_timeout=3):
            break
    except Exception as exc:  # noqa: BLE001
        if time.time() > deadline:
            print(f"database is not reachable: {exc}", file=sys.stderr)
            raise
        time.sleep(1)
PY

echo "[entrypoint] applying migrations"
alembic upgrade head

echo "[entrypoint] starting: $*"
exec "$@"
