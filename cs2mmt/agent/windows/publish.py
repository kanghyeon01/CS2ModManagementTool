"""Uploading to Paradox Mods with the game's own ModPublisher.

``ModPublisher.exe <Publish|NewVersion|Update> <PublishConfiguration.xml> -c <content folder> -v``

* ``NewVersion`` uploads a new version (needs a new ModVersion and a ChangeLog);
* ``Update`` only rewrites the page data from the config (description, GameVersion, ...);
* ``Publish`` creates a new mod. The tool never does that: a first upload is the owner's call.

ModPublisher logs in with the Paradox account the game last signed in with, so the game must
have been started and signed in on this PC (the smoke test does that), and the game must not be
running while ModPublisher works (it locks the Mods folder). The content is staged into a clean
folder first, so only the mod's own files are uploaded.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ...common import proc

__all__ = ["PublishResult", "find_modpublisher", "stage_content", "run_modpublisher", "KNOWN_FAILURES"]

log = logging.getLogger("cs2mmt.publish")

KNOWN_FAILURES = [
    (r"Could not auto log in|not logged in|You were not logged in|login failed", "pdx_login",
     "Paradox 계정 자동 로그인 실패: 이 PC에서 게임을 켜 Paradox 계정에 로그인한 뒤 종료해야 합니다."),
    (r"ACCESS_DENIED|IOERR_\d+|being used by another process", "locked", "파일이 잠겨 있습니다 (게임이 실행 중?)."),
    (r"Must be a string between \d+ and \d+ length", "field_length", "설명 등의 글자 수 제한을 넘었습니다."),
    (r"\bnot found\b.*\bmod\b|\bmod\b.*\bnot found\b|No mod with id", "mod_not_found", "ModId에 해당하는 모드를 찾지 못했습니다."),
    (r"Unhandled exception|System\.\w+Exception", "exception", "ModPublisher가 예외로 멈췄습니다."),
]


@dataclass
class PublishResult:
    ok: bool
    code: int
    kind: str
    message: str
    output_tail: str
    mod_id: Optional[str] = None


def find_modpublisher(configured: Optional[str], game_dir: Optional[Path]) -> Optional[Path]:
    candidates: List[Path] = []
    if configured:
        candidates.append(Path(os.path.expandvars(configured)))
    env = os.environ.get("CSII_MODPUBLISHERPATH")
    if env:
        candidates.append(Path(env))
    if os.name == "nt":
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                value, _ = winreg.QueryValueEx(k, "CSII_MODPUBLISHERPATH")
                if value:
                    candidates.append(Path(value))
        except OSError:
            pass
        except ImportError:
            pass
    if game_dir is not None:
        candidates.append(game_dir / "Cities2_Data" / "StreamingAssets" / "~Tooling~" / "ModPublisher" / "ModPublisher.exe")
    for c in candidates:
        if c.is_file():
            return c
    return None


def stage_content(source: Path, dest: Path, exclude_suffixes=(".pdb", ".user", ".log")) -> Path:
    if not source.is_dir():
        raise FileNotFoundError("content folder %s does not exist (did the build deploy?)" % source)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, ignore=lambda d, names: [n for n in names if n.lower().endswith(exclude_suffixes)])
    if not any(dest.rglob("*.dll")):
        raise FileNotFoundError("no DLL in %s" % source)
    return dest


def run_modpublisher(
    exe: Path,
    verb: str,
    config_path: Path,
    content_dir: Path,
    verbose: bool = True,
    timeout_s: float = 1200,
    log_file: Optional[Path] = None,
    run=proc.run,
) -> PublishResult:
    if verb not in ("NewVersion", "Update"):
        raise ValueError("the tool only runs NewVersion and Update (got %r)" % verb)
    argv = [str(exe), verb, str(config_path), "-c", str(content_dir)]
    if verbose:
        argv.append("-v")
    r = run(argv, cwd=exe.parent, timeout=timeout_s, log_file=log_file)
    out = r.stdout + "\n" + r.stderr
    for rx, kind, message in KNOWN_FAILURES:
        if re.search(rx, out, re.I):
            return PublishResult(False, r.code, kind, message, r.tail(25))
    if r.timed_out:
        return PublishResult(False, r.code, "timeout", "ModPublisher가 시간 안에 끝나지 않았습니다.", r.tail(25))
    if r.code != 0:
        return PublishResult(False, r.code, "exit_code", "ModPublisher 종료 코드 %d" % r.code, r.tail(25))
    m = re.search(r"\bId\s*[=:]\s*(\d+)", out)
    return PublishResult(True, 0, "ok", "업로드 완료", r.tail(15), m.group(1) if m else None)
