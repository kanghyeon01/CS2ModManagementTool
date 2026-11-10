#!/usr/bin/env python3
"""Stands in for the curl executable in the sentinel's integration test.

Understands the two ways the sentinel calls curl: options on stdin (``-K -``) and plain URL
arguments. Steam requests answer with the file named by FAKE_STEAM_FILE, Discord webhook posts
are appended to FAKE_DISCORD_LOG, controller health checks answer FAKE_CONTROLLER_STATUS (default 000).
"""

import json
import os
import re
import sys
import urllib.parse


def parse_config(text):
    opts = {"data-urlencode": []}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Za-z-]+)\s*(?:=\s*)?(.*)$', line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
            value = re.sub(r'\\(.)', lambda mm: {"n": "\n", "r": "\r", "t": "\t"}.get(mm.group(1), mm.group(1)), value)
        if key == "data-urlencode":
            opts[key].append(value)
        else:
            opts[key] = value
    return opts


def main():
    args = sys.argv[1:]
    opts = {"data-urlencode": []}
    if "-K" in args and args[args.index("-K") + 1] == "-":
        opts = parse_config(sys.stdin.read())
    url = opts.get("url") or next((a for a in args if a.startswith("http")), "")
    if "api.steamcmd.net" in url:
        path = os.environ.get("FAKE_STEAM_FILE", "")
        if not path or not os.path.exists(path):
            sys.exit(22)
        sys.stdout.write(open(path, encoding="utf-8").read())
        return
    if "discord.com/api/webhooks/" in url:
        body = json.loads(opts.get("data-raw") or opts.get("data") or "{}")
        with open(os.environ["FAKE_DISCORD_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"url": url, "header": opts.get("header"), "content": body.get("content"),
                                "allowed_mentions": body.get("allowed_mentions")}, ensure_ascii=False) + "\n")
        sys.stdout.write("204")
        return
    if url.endswith("/api/v1/health"):
        sys.stdout.write(os.environ.get("FAKE_CONTROLLER_STATUS", "000"))
        return
    # health-check pings and anything else: succeed quietly
    return


if __name__ == "__main__":
    main()
