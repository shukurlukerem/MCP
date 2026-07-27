#!/bin/sh
# Container entrypoint: wait for Postgres, migrate (api only), then exec the command.
set -e

python - <<'PY'
import os, sys, time
from urllib.parse import urlparse

url = urlparse(os.environ.get("DATABASE_URL", ""))
host, port = url.hostname or "postgres", url.port or 5432

import socket
for attempt in range(60):
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"database reachable at {host}:{port}")
            sys.exit(0)
    except OSError:
        time.sleep(1)
print(f"database unreachable at {host}:{port} after 60s", file=sys.stderr)
sys.exit(1)
PY

# Only the api container migrates — running Alembic from every worker at once
# races on the version table.
if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    echo "running alembic upgrade head"
    alembic upgrade head
fi

exec "$@"
