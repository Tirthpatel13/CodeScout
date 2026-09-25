#!/bin/sh
# Render's dockerCommand override doesn't run through a shell, so chaining
# with && there silently breaks. This script is the workaround: run
# migrations, then start the app.
set -e
alembic upgrade head

# Render's free tier has no background-worker plan, so the ARQ worker runs
# alongside the API in this one container instead of as a separate service.
# Trade-off: the whole container (API + worker) sleeps after 15 min idle on
# the free web-service plan, and both wake together on the next request. If
# a paid worker plan gets added later, drop this and go back to running
# `arq app.worker.WorkerSettings` as its own service.
arq app.worker.WorkerSettings &
WORKER_PID=$!
trap 'kill -TERM $WORKER_PID 2>/dev/null' TERM INT

uvicorn app.main:app --host 0.0.0.0 --port 8000 &
API_PID=$!

wait $API_PID
kill -TERM $WORKER_PID 2>/dev/null
wait $WORKER_PID 2>/dev/null
