#!/bin/bash
# Run Prefect under launchd. The ownership marker is installed after DB verification.
set -euo pipefail
umask 077

python="$1"
state="$2"
port="$3"
[[ "$(<"$state/owner")" == mac ]]
[[ -f "$state/prefect/prefect.db" && -w "$state/prefect/prefect.db" ]]
cd "$state"

environment=(
    "HOME=$state"
    PATH=/usr/bin:/bin
    "PREFECT_HOME=$state/prefect"
    PREFECT_SERVER_ANALYTICS_ENABLED=false
    PREFECT_SERVER_DATABASE_MIGRATE_ON_START=false
)
exec /usr/bin/env -i "${environment[@]}" "$python" -I -m prefect server start --host 127.0.0.1 --port "$port"
