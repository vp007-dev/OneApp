#!/usr/bin/env bash
# start.sh — launch TractorCare FastAPI app with Gunicorn + Uvicorn worker

# Fail on first error
set -e

# Use PORT from environment (Render sets this), default to 8000 for local runs
PORT=${PORT:-8000}

echo "Starting TractorCare on port ${PORT}..."

# Run Gunicorn with Uvicorn worker
exec gunicorn main:app \
  -w 3 \
  -k uvicorn.workers.UvicornWorker \
  -b 0.0.0.0:${PORT}
