"""The in-game smoke test.

1. The harness mod (``harness/CS2MMTHarness``) and the freshly built mods are in the local Mods
   folder; a request file tells the harness what to do.
2. The game starts (by default ``Cities2.exe -continuelastsave`` with Steam's environment, which
   skips the Paradox launcher); the harness waits for a city, loading the test save itself if
   the game stays in the main menu, lets the simulation run, records every error Unity logs
   (with stack traces), writes ``result.json`` and quits.
3. The game's logs are read and every error is attributed to the mod whose namespace appears in
   it. A mod passes when it loaded, its expected log lines are there, and no error names it.

If the game hangs, it is killed after the time limit; the logs are judged all the same.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from ...common import proc
from ...common.logscan import LogFile, ModVerdict, read_logs, scan_logs
from ...common.registry import ModSpec
from .steam import is_process_running, kill_process

__all__ = ["SmokeConfig", "SmokeResult", "SmokeTester", "HARNESS_NAME"]

log = logging.getLogger("cs2mmt.smoke")

HARNESS_NAME = "CS2MMTHarness"


@dataclass
class SmokeConfig:
    user_data_dir: Path
    game_dir: Path
    launch: List[str]
    launch_env: Dict[str, str] = field(default_factory=dict)
    run_seconds: int = 360
    load_timeout_s: int = 900
    test_save: str = ""
    sim_speed: int = 3
    process_name: str = "Cities2.exe"
    keep_harness: bool = False
    ignore_patterns: List[str] = field(default_factory=list)
    appid: int = 949230


@dataclass
class SmokeResult:
    ran: bool
    city_loaded: bool
    harness: Optional[Dict[str, Any]]
    verdicts: List[ModVerdict]
    note: str = ""
    duration_s: float = 0.0
    log_files: List[str] = field(default_factory=list)

    def verdict(self, mod_id: str) -> Optional[ModVerdict]:
        return next((v for v in self.verdicts if v.mod_id == mod_id), None)

    def mod_ok(self, mod_id: str) -> bool:
        v = self.verdict(mod_id)
        return bool(self.ran and self.city_loaded and v is not None and v.ok)

    def summary(self) -> str:
        head = "city loaded" if self.city_loaded else "CITY NOT LOADED"
        lines = ["smoke test: %s (%.0fs)%s" % (head, self.duration_s, (" — " + self.note) if self.note else "")]
        lines.extend("  " + v.summary() for v in self.verdicts)
        return "\n".join(lines)


class SmokeTester:
    def __init__(self, cfg: SmokeConfig, run=proc.run, popen=subprocess.Popen, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self._run = run
        self._popen = popen
        self._sleep = sleep
        self._clock = clock

    @property
    def harness_data_dir(self) -> Path:
        return self.cfg.user_data_dir / "ModsData" / HARNESS_NAME

    @property
    def mods_dir(self) -> Path:
        return self.cfg.user_data_dir / "Mods"

    @property
    def logs_dir(self) -> Path:
        return self.cfg.user_data_dir / "Logs"

    def harness_installed(self) -> bool:
        return (self.mods_dir / HARNESS_NAME / (HARNESS_NAME + ".dll")).is_file()

    def remove_harness(self) -> None:
        shutil.rmtree(self.mods_dir / HARNESS_NAME, ignore_errors=True)
        try:
            (self.harness_data_dir / "request.json").unlink()
        except OSError:
            pass

    def _argv(self) -> List[str]:
        values = {"game_dir": str(self.cfg.game_dir), "appid": str(self.cfg.appid), "user_data": str(self.cfg.user_data_dir)}
        return [a.format(**values) for a in self.cfg.launch]

    def _write_request(self, job_id: str) -> Path:
        d = self.harness_data_dir
        d.mkdir(parents=True, exist_ok=True)
        for name in ("result.json", "result.partial.json"):
            try:
                (d / name).unlink()
            except OSError:
                pass
        req = {
            "version": 1,
            "job": job_id,
            "save": self.cfg.test_save,
            "run_seconds": self.cfg.run_seconds,
            "load_timeout_seconds": self.cfg.load_timeout_s,
            "menu_wait_seconds": 90,
            "sim_speed": self.cfg.sim_speed,
            "quit": True,
            "written_at": self._clock(),
        }
        path = d / "request.json"
        path.write_text(json.dumps(req, indent=2), encoding="utf-8")
        return path

    def _read_result(self) -> Optional[Dict[str, Any]]:
        p = self.harness_data_dir / "result.json"
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def run(self, mods: Sequence[ModSpec], job_id: str, heartbeat: Callable[[str], Any] = lambda s: None) -> SmokeResult:
        if is_process_running(self.cfg.process_name, self._run):
            kill_process(self.cfg.process_name, self._run)
            self._sleep(10)
        if not self.harness_installed():
            note = "harness not installed; judging from the logs only"
        else:
            note = ""
            self._write_request(job_id)
        start = self._clock()
        env = dict(os.environ)
        env.update({k: str(v) for k, v in self.cfg.launch_env.items()})
        argv = self._argv()
        log.info("starting the game: %s", " ".join(argv))
        try:
            p = self._popen(argv, cwd=str(self.cfg.game_dir), env=env)
        except OSError as e:
            return SmokeResult(False, False, None, [], note="could not start the game: %s" % e)
        limit = self.cfg.load_timeout_s + self.cfg.run_seconds + 180
        result: Optional[Dict[str, Any]] = None
        last_beat = 0.0
        while True:
            self._sleep(10)
            elapsed = self._clock() - start
            result = self._read_result()
            if result and result.get("final"):
                break
            if p.poll() is not None and not is_process_running(self.cfg.process_name, self._run):
                note = (note + "; " if note else "") + "game exited after %.0fs" % elapsed
                break
            if elapsed > limit:
                note = (note + "; " if note else "") + "time limit reached; game killed"
                break
            if elapsed - last_beat >= 60:
                last_beat = elapsed
                partial = self._read_partial()
                heartbeat("smoke test %.0f min%s" % (elapsed / 60, (", " + partial) if partial else ""))
        # Give the game a minute to quit on its own, then make sure it is gone.
        for _ in range(6):
            if p.poll() is not None and not is_process_running(self.cfg.process_name, self._run):
                break
            self._sleep(10)
        if is_process_running(self.cfg.process_name, self._run):
            kill_process(self.cfg.process_name, self._run)
            self._sleep(5)
        duration = self._clock() - start
        result = result or self._read_result()
        logs = self._collect_logs(start)
        verdicts = scan_logs(
            logs,
            [{"id": m.id, "identifiers": m.all_identifiers(), "log_names": m.all_log_names(), "smoke_expect": m.smoke_expect} for m in mods],
            harness_errors=(result or {}).get("errors", []),
            ignore=self.cfg.ignore_patterns,
        )
        city_loaded = bool(result and result.get("city_loaded"))
        if not result:
            note = (note + "; " if note else "") + "no harness result"
        if not self.cfg.keep_harness:
            try:
                (self.harness_data_dir / "request.json").unlink()
            except OSError:
                pass
        return SmokeResult(True, city_loaded, result, verdicts, note=note, duration_s=duration, log_files=[l.name for l in logs])

    def _read_partial(self) -> str:
        p = self.harness_data_dir / "result.partial.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if data.get("city_loaded"):
            return "city running, %d error(s) so far" % int(data.get("error_count", 0))
        return str(data.get("phase", ""))

    def _collect_logs(self, since: float) -> List[LogFile]:
        paths: List[Path] = []
        if self.logs_dir.is_dir():
            paths.extend(sorted(self.logs_dir.glob("*.log")))
        for name in ("Player.log",):
            p = self.cfg.user_data_dir / name
            if p.is_file():
                paths.append(p)
        return read_logs(paths, since_epoch=since)
