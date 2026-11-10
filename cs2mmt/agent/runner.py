"""The agent's main loop on Windows.

``boot``  (Task Scheduler, at log-on): if a controller woke the PC for a job, take every job
          that is waiting, run them one after another, then shut the PC down (or go back to
          Linux). A boot that was not for a job does nothing, so the owner's own use is never
          disturbed.
``watch`` (optional, while the PC is on anyway): take waiting jobs when nobody has touched the
          keyboard or mouse for a while and the game is not running.
``once``  take one waiting job now (manual use).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

from ..common.config import Config, load_config
from ..common.http import HttpClient
from ..common.log import setup_logging
from ..common.registry import Registry, load_registry
from .client import ClaimedJob, ControllerRef, Controllers
from .pipeline import Agent, JobRun
from .state import AgentState
from .windows import power
from .windows.steam import is_process_running

__all__ = ["SingleInstance", "build_agent", "run_claimed", "run_agent", "selftest"]

log = logging.getLogger("cs2mmt.agent")


class SingleInstance:
    """A lock file so two agents never work at the same time (e.g. log-on task + manual run)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fh = None

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise RuntimeError("another agent is already running (%s)" % self.path)
        return self

    def __exit__(self, *exc) -> None:
        if self.fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.fh.close()


def build_agent(cfg: Config) -> Agent:
    mods_file = cfg.get_path("agent.mods_file", "mods.toml")
    registry: Registry = load_registry(mods_file)
    refs = [ControllerRef(str(c["id"]), str(c["url"])) for c in cfg.get_list("controllers")]
    clients = Controllers(refs, str(cfg.require("secrets.api_token")), HttpClient(timeout=20, retries=2))
    work = cfg.get_path("agent.work_dir") or (cfg.get_path("agent.workspace", ".") / "work")
    state = AgentState(work / "agent-state.json")
    return Agent(cfg, registry, clients, state)


def _duplicate_of(agent: Agent, claimed: ClaimedJob) -> Optional[str]:
    if agent.state.done(claimed.work_key):
        return claimed.work_key
    if claimed.kind == "game_update" and not claimed.payload.get("manual"):
        return agent.state.game_update_covered(claimed.payload.get("new_build"))
    return None


def run_claimed(agent: Agent, claimed: ClaimedJob) -> Tuple[str, str]:
    dup = _duplicate_of(agent, claimed)
    if dup:
        log.info("job %s duplicates %s; closing it", claimed.id, dup)
        agent.clients.complete(claimed, "succeeded", {"duplicate_of": dup}, "")
        return "succeeded", ""
    run = JobRun(agent, claimed)
    state, result, summary = run.run()
    agent.clients.complete(claimed, state, result, summary)
    if not result.get("retry"):
        agent.state.mark_done(claimed.work_key, state, summary)
    return state, summary


def _after_jobs(cfg: Config) -> None:
    action = str(cfg.get("agent.after_job", "shutdown"))
    delay = int(cfg.get("agent.shutdown_delay_s", 120))
    if action == "shutdown":
        log.info("all jobs done; shutting down in %ds (cancel: shutdown /a)", delay)
        power.shutdown(delay)
    elif action == "reboot_linux":
        pattern = str(cfg.get("agent.linux_entry_pattern", r"ubuntu|debian|fedora|linux|grub"))
        try:
            power.reboot_to_firmware_entry(pattern, delay)
        except Exception as e:
            log.warning("could not set the next boot to Linux (%s); shutting down instead", e)
            power.shutdown(delay)


def _drain(agent: Agent, opportunistic: bool, max_jobs: int = 10) -> int:
    done = 0
    while done < max_jobs:
        claimed = agent.clients.claim(agent.host_id, opportunistic=opportunistic)
        if claimed is None:
            break
        log.info("claimed %s (%s) from %s%s", claimed.id, claimed.kind, claimed.controller.id, " (resumed)" if claimed.resumed else "")
        power.prevent_sleep()
        try:
            run_claimed(agent, claimed)
        finally:
            power.allow_sleep()
        done += 1
        opportunistic = True  # the PC is up anyway: take whatever else is queued
    return done


def run_agent(cfg: Config, mode: str = "boot") -> int:
    agent = build_agent(cfg)
    lock = agent.work_root / "agent.lock"
    with SingleInstance(lock):
        if mode == "once":
            return _drain(agent, opportunistic=True, max_jobs=1)
        if mode == "boot":
            boot = power.boot_time()
            decision = {"action": "none"}
            deadline = time.time() + float(cfg.get("agent.network_wait_seconds", 300))
            while time.time() < deadline:
                decision = agent.clients.boot_decision(boot)
                if decision.get("action") == "windows" or agent.clients.reachable():
                    break
                time.sleep(15)
            if decision.get("action") != "windows":
                log.info("this boot was not for a job; nothing to do")
                if cfg.get("agent.watch_after_manual_boot", False):
                    return _watch(agent, cfg)
                return 0
            log.info("woken for job %s (%s)", decision.get("job_id"), decision.get("controller"))
            n = _drain(agent, opportunistic=False)
            if n == 0:
                # The job may have been claimed already or the controller restarted; try once more.
                time.sleep(30)
                n = _drain(agent, opportunistic=True)
            _after_jobs(cfg)
            return n
        if mode == "watch":
            return _watch(agent, cfg)
        raise ValueError("unknown mode %r" % mode)


def _watch(agent: Agent, cfg: Config) -> int:
    idle_needed = float(cfg.get("agent.only_when_idle_minutes", 15)) * 60
    every = float(cfg.get("agent.poll_minutes", 5)) * 60
    game = str(cfg.get("game.process_name", "Cities2.exe"))
    total = 0
    while True:
        if power.idle_seconds() >= idle_needed and not is_process_running(game):
            total += _drain(agent, opportunistic=True, max_jobs=3)
        time.sleep(every)


def selftest(cfg: Config) -> int:
    """Checks this PC's setup without running a job; prints what is missing."""
    agent = build_agent(cfg)
    ok = True

    def report(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print("%s %s%s" % ("[ OK ]" if good else "[FAIL]", name, (" — " + detail) if detail else ""))

    for p in agent.preflight():
        report("tools", False, p)
    reach = agent.clients.reachable()
    report("controllers reachable", bool(reach), ", ".join(r.id for r in reach) or "none")
    st = agent.steam.status()
    report("Steam / CS2 installed", st is not None, ("build %s, %s" % (st.buildid, "ready" if st.ready else st.progress())) if st else "no appmanifest")
    if st is not None:
        managed = agent.managed_dir()
        report("game assemblies", (managed / "Game.dll").is_file(), str(managed))
        from .windows.publish import find_modpublisher

        exe = find_modpublisher(cfg.get("publish.modpublisher"), agent.game_dir())
        report("ModPublisher.exe", exe is not None, str(exe or "not found (install the modding toolchain from the game's options)"))
    report("game user data", (agent.user_data / "Mods").is_dir() or agent.user_data.is_dir(), str(agent.user_data))
    for mod in agent.registry.mods:
        d = agent.mod_dir(mod)
        report("mod %s checkout" % mod.id, (d / mod.project).is_file(), str(d))
    for name in ("system_appendix.md", "game_update_fix.md", "smoke_fix.md", "bugfix.md", "triage.md"):
        report("prompt %s" % name, (agent.prompts_dir / name).is_file())
    harness = agent.workspace / "harness" / "CS2MMTHarness" / "CS2MMTHarness.csproj"
    report("test harness project", harness.is_file(), str(harness))
    return 0 if ok else 1


def _self_update(cfg: Config, config_path: str, mode: str) -> Optional[int]:
    """Pulls the tool's latest version before working; returns the exit code of the re-run, if any."""
    import subprocess
    import sys

    from .. import selfupdate

    if not cfg.get("update.auto", True):
        return None
    repo = cfg.get_path("update.repo_dir") or cfg.get_path("agent.workspace", ".")
    res = selfupdate.self_update(repo, str(cfg.get("update.branch", "main")), check=selfupdate.default_check())
    if res.rolled_back or (not res.ok and "fetch" not in res.message):
        log.warning("tool update: %s", res.short())
        try:
            build_agent(cfg).clients.notify("PC 에이전트 도구 업데이트 문제: %s" % res.short(), "warning")
        except Exception:
            pass
    if not res.changed:
        return None
    log.info("tool updated (%s); starting the new version", res.short())
    argv = [sys.executable, "-m", "cs2mmt", "agent", "run", "--config", str(Path(config_path).resolve()), "--mode", mode, "--no-update"]
    return subprocess.call(argv, cwd=str(repo))


def main_agent(config_path: str, mode: str, no_update: bool = False) -> int:
    cfg = load_config(Path(config_path))
    log_dir = cfg.get_path("agent.work_dir") or (cfg.get_path("agent.workspace", ".") / "work")
    setup_logging(log_dir / "agent.log", str(cfg.get("agent.log_level", "INFO")), cfg.secrets())
    if mode == "selftest":
        return selftest(cfg)
    if not no_update:
        try:
            rc = _self_update(cfg, config_path, mode)
            if rc is not None:
                return rc
        except Exception:
            log.exception("tool update failed; carrying on with this version")
    try:
        run_agent(cfg, mode)
        return 0
    except Exception:
        log.exception("agent stopped with an error")
        return 1
