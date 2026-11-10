#!/bin/sh
# Synology without Container Manager: start the controller and the sentinel if they are not
# running. Register in DSM Task Scheduler twice: at boot-up, and every 10 minutes.
#   sh /volume1/cs2mmt/deploy/synology/keepalive.sh
BASE=${CS2MMT_BASE:-/volume1/cs2mmt}
PY=${CS2MMT_PYTHON:-/var/packages/Python3.9/target/usr/bin/python3.9}
mkdir -p "$BASE/data"

if ! pgrep -f "cs2mmt controller run" >/dev/null 2>&1; then
    cd "$BASE" && nohup "$PY" -m cs2mmt controller run --config "$BASE/config/controller.toml" >> "$BASE/data/controller.out" 2>&1 &
    echo "$(date) started controller" >> "$BASE/data/keepalive.log"
fi
if [ -x "$BASE/bin/cs2mmt-sentinel" ] && ! pgrep -f "cs2mmt-sentinel --config" >/dev/null 2>&1; then
    nohup "$BASE/bin/cs2mmt-sentinel" --config "$BASE/config/sentinel.conf" >> "$BASE/data/sentinel.out" 2>&1 &
    echo "$(date) started sentinel" >> "$BASE/data/keepalive.log"
fi
