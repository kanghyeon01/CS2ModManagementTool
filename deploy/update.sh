#!/bin/sh
# Updates the tool on a NAS or server by hand (the same thing Discord !update_tool does):
# fast-forward the checkout, check the new code, then restart the services. Safe to run again.
#   sh deploy/update.sh            (from the repository checkout)
set -eu
cd "$(dirname "$0")/.."
PY=${CS2MMT_PYTHON:-python3}
before=$(git rev-parse --short HEAD)
"$PY" - <<'PYEOF'
import sys
from cs2mmt import selfupdate
r = selfupdate.self_update(selfupdate.repo_root(), "main", check=selfupdate.default_check())
print(r.short())
sys.exit(0 if r.ok else 1)
PYEOF
after=$(git rev-parse --short HEAD)
if [ "$before" = "$after" ]; then echo "nothing to restart"; exit 0; fi
if command -v docker >/dev/null 2>&1 && docker compose -f deploy/docker-compose.yml ps -q controller >/dev/null 2>&1 \
        && [ -n "$(docker compose -f deploy/docker-compose.yml ps -q controller 2>/dev/null)" ]; then
    # Dockerfile changes need a rebuild; code-only changes just a restart (the code is mounted).
    if git diff --name-only "$before" "$after" | grep -qE '(^deploy/Dockerfile|^sentinel/Dockerfile|docker-compose)'; then
        docker compose -f deploy/docker-compose.yml up -d --build
    else
        docker compose -f deploy/docker-compose.yml restart controller sentinel
    fi
elif command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files | grep -q cs2mmt-controller; then
    sudo systemctl restart cs2mmt-controller cs2mmt-sentinel
else
    pkill -f "cs2mmt controller run" || true
    pkill -f "cs2mmt-sentinel --config" || true
    echo "stopped; the keepalive task starts the new version within 10 minutes (or run deploy/synology/keepalive.sh)"
fi
echo "updated $before -> $after"
