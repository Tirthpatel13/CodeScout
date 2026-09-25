#!/bin/sh
# Render's dockerCommand override doesn't run through a shell, so chaining
# with && there silently breaks. This script is the workaround: run
# migrations, then hand off to uvicorn.
set -e
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
