"""Command line: ``python -m cs2mmt <command> ...``

  controller run      --config controller.toml        the always-on service (NAS / server)
  controller once     --config controller.toml        one round of checks, for testing
  agent run           --config agent.toml [--mode boot|watch|once]
  agent selftest      --config agent.toml             check the PC's setup
  agent linux-switch  --config agent-linux.toml [--watch] [--check]
  enqueue KIND        --config agent.toml|controller.toml   queue a job (update, rehearsal, ...)
  status              --config agent.toml|controller.toml
  tools policy-check  REPO [--base REF]               run the change gate on a repository
  tools apidiff       OLD NEW --mods DIR [DIR...] --out DIR
  tools validate      --mods mods.toml                check the mod registry and publish configs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional


def _controllers_from(cfg):
    from .agent.client import ControllerRef, Controllers

    refs = [ControllerRef(str(c["id"]), str(c["url"])) for c in cfg.get_list("controllers")]
    if not refs:
        port = int(cfg.get("controller.listen_port", 8765))
        refs = [ControllerRef("local", "http://127.0.0.1:%d" % port)]
    return Controllers(refs, str(cfg.require("secrets.api_token")))


def cmd_controller(args) -> int:
    from .common.config import load_config
    from .common.log import setup_logging
    from .common.registry import load_registry
    from .controller.service import Controller

    cfg = load_config(Path(args.config))
    data_dir = cfg.get_path("controller.data_dir", "data")
    setup_logging(data_dir / "logs" / "controller.log", str(cfg.get("controller.log_level", "INFO")), cfg.secrets())
    registry = load_registry(Path(args.mods) if args.mods else cfg.get_path("controller.mods_file", "mods.toml"))
    ctl = Controller(cfg, registry)
    if args.action == "once":
        for task in ctl.periodic:
            task.next_at = 0
        ctl.tick()
        print(ctl.status_text())
        return 0
    ctl.run_forever()
    return 0


def cmd_agent(args) -> int:
    if args.action == "linux-switch":
        from .common.config import load_config
        from .common.log import setup_logging
        from .agent.linux_switch import run_boot_switch

        cfg = load_config(Path(args.config))
        setup_logging(None, str(cfg.get("linux.log_level", "INFO")), cfg.secrets())
        if args.check:
            from .agent.linux_switch import describe

            print(describe(cfg))
            return 0
        print(run_boot_switch(cfg, watch=args.watch))
        return 0
    from .agent.runner import main_agent

    return main_agent(args.config, "selftest" if args.action == "selftest" else args.mode, no_update=args.no_update)


def cmd_enqueue(args) -> int:
    from .common.config import load_config

    cfg = load_config(Path(args.config))
    clients = _controllers_from(cfg)
    kind = {"update": "game_update", "rehearsal": "rehearsal"}.get(args.kind, args.kind)
    payload = json.loads(args.payload) if args.payload else {}
    for ref in clients.refs:
        try:
            status, data = clients._post(ref, "/api/v1/jobs", {"kind": kind, "payload": payload})
        except Exception as e:
            print("%s: %s" % (ref.id, e))
            continue
        print("%s: HTTP %d %s" % (ref.id, status, json.dumps(data.get("job", data), ensure_ascii=False)[:300]))
        if status == 200:
            return 0
    return 1


def cmd_status(args) -> int:
    from .common.config import load_config

    cfg = load_config(Path(args.config))
    clients = _controllers_from(cfg)
    rc = 1
    for ref in clients.refs:
        try:
            status, data = clients._get(ref, "/api/v1/status")
        except Exception as e:
            print("== %s: unreachable (%s)" % (ref.id, e))
            continue
        print("== %s (HTTP %d)" % (ref.id, status))
        print(data.get("text") or json.dumps(data, ensure_ascii=False, indent=2)[:3000])
        rc = 0
    return rc


def cmd_tools(args) -> int:
    if args.tool == "policy-check":
        from .common.policy import check_diff, parse_unified_diff
        from .common import proc

        repo = Path(args.repo)
        proc.run(["git", "-C", str(repo), "add", "-A", "-N"], timeout=60)
        r = proc.run(["git", "-C", str(repo), "diff", "--no-color", args.base], timeout=120)
        result = check_diff(parse_unified_diff(r.stdout))
        print(result.summary())
        return 0 if result.ok else 2
    if args.tool == "apidiff":
        from .common import apidiff

        ids = apidiff.collect_identifiers([Path(d) for d in args.mods])
        rep = apidiff.diff_trees(Path(args.old), Path(args.new), ids)
        path = apidiff.write_report(rep, Path(args.out), Path(args.old).name, Path(args.new).name, Path(args.old), Path(args.new))
        print(path.read_text(encoding="utf-8"))
        return 0
    if args.tool == "validate":
        from .common import publishconfig as pc
        from .common.registry import load_registry

        reg = load_registry(Path(args.mods))
        base = Path(args.mods).resolve().parent.parent if args.workspace is None else Path(args.workspace)
        bad = 0
        print("build order:", " -> ".join(m.id for m in reg.build_order()))
        for m in reg.mods:
            d = Path(m.dir) if Path(m.dir).is_absolute() else base / m.dir
            cfg_path = d / m.publish_config
            if not cfg_path.is_file():
                print("[%s] %s not found (checkout missing?)" % (m.id, cfg_path))
                continue
            info = pc.read_info(cfg_path.read_text(encoding="utf-8-sig"))
            problems = pc.validate(info, "NewVersion", d)
            for loc in m.version_files:
                p = d / loc.path
                if p.is_file():
                    mm = loc.compiled().search(p.read_text(encoding="utf-8-sig"))
                    if not mm:
                        problems.append("%s: version pattern not found" % loc.path)
                    elif mm.group("v") != info.mod_version:
                        problems.append("%s says %s, PublishConfiguration says %s" % (loc.path, mm.group("v"), info.mod_version))
            state = "ok" if not problems else "; ".join(problems)
            print("[%s] %s %s (GameVersion %s, ModId %s): %s" % (m.id, info.display_name, info.mod_version, info.game_version, info.mod_id or "-", state))
            bad += bool(problems)
        return 0 if bad == 0 else 2
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="cs2mmt", description="CS2 Mod Management Tool")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("controller")
    c.add_argument("action", choices=["run", "once"])
    c.add_argument("--config", required=True)
    c.add_argument("--mods")
    c.set_defaults(func=cmd_controller)

    a = sub.add_parser("agent")
    a.add_argument("action", choices=["run", "selftest", "linux-switch"])
    a.add_argument("--config", required=True)
    a.add_argument("--mode", default="boot", choices=["boot", "watch", "once"])
    a.add_argument("--watch", action="store_true", help="linux-switch: also switch when the PC is already on and nobody uses it")
    a.add_argument("--check", action="store_true", help="linux-switch: show how Windows would be booted, without rebooting")
    a.add_argument("--no-update", action="store_true", help="run: do not pull the latest tool version first")
    a.set_defaults(func=cmd_agent)

    e = sub.add_parser("enqueue")
    e.add_argument("kind", help="update | rehearsal | game_update | bugfix | triage")
    e.add_argument("--config", required=True)
    e.add_argument("--payload", help="JSON payload")
    e.set_defaults(func=cmd_enqueue)

    s = sub.add_parser("status")
    s.add_argument("--config", required=True)
    s.set_defaults(func=cmd_status)

    t = sub.add_parser("tools")
    tsub = t.add_subparsers(dest="tool", required=True)
    pc_ = tsub.add_parser("policy-check")
    pc_.add_argument("repo")
    pc_.add_argument("--base", default="HEAD")
    ad = tsub.add_parser("apidiff")
    ad.add_argument("old")
    ad.add_argument("new")
    ad.add_argument("--mods", nargs="+", required=True)
    ad.add_argument("--out", required=True)
    va = tsub.add_parser("validate")
    va.add_argument("--mods", required=True)
    va.add_argument("--workspace")
    t.set_defaults(func=cmd_tools)

    args = p.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
