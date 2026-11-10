"""Steam on the PC: where the game is, which build is installed, and waiting for an update.

The installed build is read from ``steamapps/appmanifest_<appid>.acf`` (``buildid`` and the
``StateFlags`` bit field). Steam must run, be logged in, and be allowed to update the game.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from ...common import proc, vdf

__all__ = ["AppStatus", "SteamLocator", "is_process_running", "kill_process", "STATE_FULLY_INSTALLED", "BUSY_MASK"]

log = logging.getLogger("cs2mmt.steam.local")

STATE_FULLY_INSTALLED = 4
# Update required / running / paused / started, validating, adding files, preallocating,
# downloading, staging, committing.
BUSY_MASK = 2 | 256 | 512 | 1024 | 131072 | 262144 | 524288 | 1048576 | 2097152 | 4194304


@dataclass
class AppStatus:
    appid: int
    buildid: str
    state_flags: int
    installdir: str
    library: Path
    manifest: Path
    bytes_to_download: int = 0
    bytes_downloaded: int = 0
    target_buildid: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.state_flags & STATE_FULLY_INSTALLED) and not (self.state_flags & BUSY_MASK)

    @property
    def game_dir(self) -> Path:
        return self.library / "steamapps" / "common" / self.installdir

    def progress(self) -> str:
        if self.bytes_to_download:
            return "%.0f%% of %.1f GB" % (100.0 * self.bytes_downloaded / max(1, self.bytes_to_download), self.bytes_to_download / 1e9)
        return "flags %d" % self.state_flags


def _posix_pids(image: str) -> List[int]:
    """Processes one of whose arguments is a file named exactly ``image`` (tests run on Linux)."""
    pids: List[int] = []
    me = os.getpid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as f:
                args = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if any(os.path.basename(a) == image for a in args[:3]):
            pids.append(int(entry))
    return pids


def is_process_running(image: str, run=proc.run) -> bool:
    if os.name != "nt":
        return bool(_posix_pids(image))
    r = run(["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq %s" % image], timeout=30)
    return ('"%s"' % image.lower()) in r.stdout.lower()


def kill_process(image: str, run=proc.run) -> None:
    if os.name == "nt":
        run(["taskkill", "/IM", image, "/T", "/F"], timeout=60)
        return
    import signal as _signal

    for pid in _posix_pids(image):
        try:
            os.kill(pid, _signal.SIGKILL)
        except OSError:
            pass


class SteamLocator:
    def __init__(self, root: Optional[Path] = None, exe: Optional[Path] = None, appid: int = 949230, run=proc.run):
        self.root = root or self._find_root()
        self.exe = exe or (self.root / "steam.exe" if self.root else None)
        self.appid = appid
        self._run = run

    @staticmethod
    def _find_root() -> Optional[Path]:
        if os.name == "nt":
            try:
                import winreg  # type: ignore

                for hive, key, name in (
                    (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                ):
                    try:
                        with winreg.OpenKey(hive, key) as k:
                            value, _ = winreg.QueryValueEx(k, name)
                            if value:
                                return Path(value)
                    except OSError:
                        continue
            except ImportError:
                pass
            default = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam"
            return default if default.is_dir() else None
        for p in (Path.home() / ".steam" / "steam", Path.home() / ".local" / "share" / "Steam"):
            if p.is_dir():
                return p
        return None

    def libraries(self) -> List[Path]:
        if not self.root:
            return []
        libs = [self.root]
        lf = self.root / "steamapps" / "libraryfolders.vdf"
        if lf.is_file():
            try:
                data = vdf.loads(lf.read_text(encoding="utf-8", errors="replace"))
            except vdf.VdfError as e:
                log.warning("libraryfolders.vdf: %s", e)
                data = {}
            folders = vdf.get_ci(data, "libraryfolders") or {}
            for key, entry in folders.items():
                if isinstance(entry, dict) and entry.get("path"):
                    libs.append(Path(entry["path"]))
                elif isinstance(entry, str) and key.isdigit():
                    libs.append(Path(entry))  # very old format
        seen, out = set(), []
        for l in libs:
            k = str(l).lower().rstrip("\\/")
            if k not in seen:
                seen.add(k)
                out.append(l)
        return out

    def manifest_path(self) -> Optional[Path]:
        for lib in self.libraries():
            p = lib / "steamapps" / ("appmanifest_%d.acf" % self.appid)
            if p.is_file():
                return p
        return None

    def status(self) -> Optional[AppStatus]:
        mp = self.manifest_path()
        if mp is None:
            return None
        return parse_manifest(mp, self.appid)

    def start(self, args: Optional[List[str]] = None) -> None:
        if self.exe is None or not Path(self.exe).exists():
            raise RuntimeError("steam.exe not found; set [steam].exe in agent.toml")
        if is_process_running("steam.exe", self._run):
            return
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(self.exe)] + list(args or ["-silent"]), creationflags=flags, close_fds=True)

    def restart(self, args: Optional[List[str]] = None, sleep: Callable[[float], None] = time.sleep) -> None:
        if self.exe is not None and Path(self.exe).exists() and is_process_running("steam.exe", self._run):
            self._run([str(self.exe), "-shutdown"], timeout=60)
            for _ in range(24):
                if not is_process_running("steam.exe", self._run):
                    break
                sleep(5)
        self.start(args)

    def open_url(self, url: str) -> None:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]


def parse_manifest(path: Path, appid: int) -> AppStatus:
    data = vdf.loads(path.read_text(encoding="utf-8", errors="replace"))
    st = vdf.get_ci(data, "AppState") or {}

    def num(key: str) -> int:
        v = str(vdf.get_ci(st, key, "0") or "0")
        return int(v) if v.isdigit() else 0

    return AppStatus(
        appid=appid,
        buildid=str(vdf.get_ci(st, "buildid", "") or ""),
        state_flags=num("StateFlags"),
        installdir=str(vdf.get_ci(st, "installdir", "") or ""),
        library=path.parent.parent,
        manifest=path,
        bytes_to_download=num("BytesToDownload"),
        bytes_downloaded=num("BytesDownloaded"),
        target_buildid=str(vdf.get_ci(st, "TargetBuildID", "") or ""),
    )
