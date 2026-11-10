"""Running external programs with a timeout that also stops their child processes."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

__all__ = ["ProcResult", "run", "kill_tree", "IS_WINDOWS"]

IS_WINDOWS = sys.platform == "win32"
log = logging.getLogger("cs2mmt.proc")


@dataclass
class ProcResult:
    argv: List[str]
    code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def tail(self, n: int = 40) -> str:
        lines = (self.stdout + ("\n" + self.stderr if self.stderr else "")).splitlines()
        return "\n".join(lines[-n:])


def kill_tree(pid: int) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=60)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception as e:  # already gone, or no permission
        log.debug("kill_tree(%s): %s", pid, e)


def run(
    argv: Sequence[str],
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
    input_text: Optional[str] = None,
    log_file: Optional[Path] = None,
    extra_env: Optional[Mapping[str, str]] = None,
) -> ProcResult:
    """Runs ``argv``; output goes to temporary files so large outputs never block the pipe.

    ``log_file``: stdout and stderr are also written there (appended) for the job log.
    """
    argv = [str(a) for a in argv]
    full_env: Optional[Dict[str, str]] = None
    if env is not None or extra_env:
        full_env = dict(os.environ if env is None else env)
        if extra_env:
            full_env.update({k: str(v) for k, v in extra_env.items()})
    kwargs: Dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    start = time.monotonic()
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        try:
            p = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd else None,
                env=full_env,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                **kwargs,
            )
        except FileNotFoundError as e:
            return ProcResult(argv, 127, "", "not found: %s" % e, False, 0.0)
        timed_out = False
        try:
            if input_text is not None:
                try:
                    p.stdin.write(input_text.encode("utf-8"))
                    p.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(p.pid)
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
        out_f.seek(0)
        err_f.seek(0)
        stdout = out_f.read().decode("utf-8", errors="replace")
        stderr = err_f.read().decode("utf-8", errors="replace")
    duration = time.monotonic() - start
    code = p.returncode if p.returncode is not None else -1
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("\n$ %s\n" % " ".join(argv))
                f.write(stdout)
                if stderr:
                    f.write("\n[stderr]\n" + stderr)
                f.write("\n[exit %s%s, %.1fs]\n" % (code, ", timed out" if timed_out else "", duration))
        except OSError as e:
            log.warning("could not write %s: %s", log_file, e)
    return ProcResult(argv, code, stdout, stderr, timed_out, duration)
