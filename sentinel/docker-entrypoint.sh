#!/bin/sh
# Builds the sentinel from the repository mounted at /app, so updating the tool is a git pull
# and a restart. Falls back to the binary baked into the image if the source is missing or does
# not compile (a broken update never stops the watcher).
BIN=/usr/local/bin/cs2mmt-sentinel
SRC=/app/sentinel/src
if [ -f "$SRC/sentinel.c" ] && command -v cc >/dev/null 2>&1; then
    if cc -O2 -std=c11 -D_FORTIFY_SOURCE=2 -fstack-protector-strong -o /tmp/cs2mmt-sentinel \
        "$SRC/sentinel.c" "$SRC/json.c" "$SRC/util.c" 2>/tmp/sentinel-build.log; then
        BIN=/tmp/cs2mmt-sentinel
    else
        echo "sentinel: the updated source did not build; running the image's binary" >&2
        cat /tmp/sentinel-build.log >&2
    fi
fi
exec "$BIN" "$@"
