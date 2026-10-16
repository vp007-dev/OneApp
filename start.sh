#!/usr/bin/env bash
# start.sh — Launch TractorCare FastAPI app in production mode
set -e

PORT=${PORT:-8000}
WORKERS=${WEB_CONCURRENCY:-3}

echo "🚜 Starting TractorCare v2.0 on port ${PORT} with ${WORKERS} workers..."

# Find gunicorn executable (virtualenv or global)
if [ -f "./.venv/bin/gunicorn" ]; then
  GUNICORN_BIN="./.venv/bin/gunicorn"
else
  GUNICORN_BIN="gunicorn"
fi

exec ${GUNICORN_BIN} main:app \
  -w ${WORKERS} \
  -k uvicorn.workers.UvicornWorker \
  -b 0.0.0.0:${PORT} \
  --access-logfile - \
  --error-logfile -
