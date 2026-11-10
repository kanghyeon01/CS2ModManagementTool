"""Updating the tool itself from its git repository, with an automatic rollback.

Every machine runs the tool from a git checkout of this repository. An update is a fast-forward
of that checkout to ``origin/<branch>``; then a check runs the new code in a fresh process (it
must import and load the mod registry). If the check fails, the checkout goes back to the
previous commit, so a broken push never takes a machine down. Local changes block the update
(nothing is overwritten); files git ignores (configs, secrets, mods/, cache/, work/) are never
touched.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .common import proc

__all__ = ["UpdateResult", "repo_root", "self_update", "current_commit", "IMPORT_CHECK"]

log = logging.getLogger("cs2mmt.update")

IMPORT_CHECK = (
    "import cs2mmt.controller.service, cs2mmt.agent.pipeline, cs2mmt.agent.runner, cs2mmt.agent.linux_switch;"
    "from pathlib import Path; from cs2mmt.common.registry import load_registry;"
    "load_registry(Path('config/mods.toml'))"
)


@dataclass
class UpdateResult:
    changed: bool
    old: str
    new: str
    message: str
    rolled_back: bool = False
    ok: bool = True

    def short(self) -> str:
        if self.rolled_back:
            return "새 버전 검사 실패로 %s(으)로 되돌림: %s" % (self.old[:8], self.message)
        if self.changed:
            return "%s → %s" % (self.old[:8], self.new[:8])
        return self.message


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def current_commit(repo: Optional[Path] = None, run=proc.run) -> str:
    r = run(["git", "-C", str(repo or repo_root()), "rev-parse", "--short=8", "HEAD"], timeout=30)
    return r.stdout.strip() if r.ok else "unknown"


def self_update(repo: Path, branch: str = "main", check: Optional[List[str]] = None, remote: str = "origin", run=proc.run) -> UpdateResult:
    repo = Path(repo)
    env = {"GIT_TERMINAL_PROMPT": "0"}

    def git(*args: str, timeout: float = 300) -> proc.ProcResult:
        return run(["git", "-C", str(repo), "-c", "safe.directory=*"] + list(args), timeout=timeout, extra_env=env)

    if not (repo / ".git").exists():
        return UpdateResult(False, "", "", "git 저장소가 아니라 업데이트하지 않음 (%s)" % repo, ok=False)
    old = git("rev-parse", "HEAD").stdout.strip()
    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        return UpdateResult(False, old, old, "추적 중인 파일에 로컬 변경이 있어 업데이트하지 않음:\n" + dirty[:500], ok=False)
    f = git("fetch", "--quiet", remote, branch, timeout=600)
    if not f.ok:
        return UpdateResult(False, old, old, "fetch 실패: %s" % f.tail(3), ok=False)
    target = git("rev-parse", "FETCH_HEAD").stdout.strip()
    if not target or target == old:
        return UpdateResult(False, old, old, "이미 최신 (%s)" % old[:8])
    if git("merge-base", "--is-ancestor", old, target).code != 0:
        return UpdateResult(False, old, target, "%s/%s가 현재 커밋에서 이어지지 않아(fast-forward 아님) 업데이트하지 않음" % (remote, branch), ok=False)
    m = git("merge", "--ff-only", "--quiet", target)
    if not m.ok:
        return UpdateResult(False, old, target, "merge 실패: %s" % m.tail(3), ok=False)
    if check:
        # A fresh bytecode cache: the check must compile the new sources, never reuse old .pyc files.
        with tempfile.TemporaryDirectory(prefix="cs2mmt-check-") as cache:
            c = run(check, cwd=repo, timeout=300, extra_env={"PYTHONPYCACHEPREFIX": cache, "PYTHONDONTWRITEBYTECODE": "1"})
        if not c.ok:
            git("reset", "--hard", "--quiet", old)
            log.error("update to %s failed its check; rolled back to %s: %s", target[:8], old[:8], c.tail(5))
            return UpdateResult(False, old, target, c.tail(5), rolled_back=True, ok=False)
    log.info("updated %s -> %s", old[:8], target[:8])
    return UpdateResult(True, old, target, "업데이트됨")


def default_check() -> List[str]:
    return [sys.executable, "-c", IMPORT_CHECK]
