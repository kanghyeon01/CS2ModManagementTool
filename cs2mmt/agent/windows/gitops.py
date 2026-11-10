"""Git operations on the mod repositories (through the git command line)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Optional, Sequence

from ...common import proc

__all__ = ["GitError", "Git"]

log = logging.getLogger("cs2mmt.git")


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, repo: Path, author_name: str = "", author_email: str = "", log_file: Optional[Path] = None, run=proc.run, sleep=time.sleep):
        self.repo = Path(repo)
        self.author_name = author_name
        self.author_email = author_email
        self.log_file = log_file
        self._run = run
        self._sleep = sleep

    def _git(self, *args: str, timeout: float = 300, check: bool = True, retries: int = 0) -> proc.ProcResult:
        argv = ["git", "-C", str(self.repo)]
        if self.author_name:
            argv += ["-c", "user.name=%s" % self.author_name]
        if self.author_email:
            argv += ["-c", "user.email=%s" % self.author_email]
        argv += ["-c", "core.autocrlf=false", "-c", "core.quotepath=false"]
        argv += list(args)
        delay = 2.0
        for attempt in range(retries + 1):
            r = self._run(argv, timeout=timeout, log_file=self.log_file, extra_env={"GIT_TERMINAL_PROMPT": "0"})
            if r.ok or attempt == retries:
                break
            self._sleep(delay)
            delay *= 2
        if check and not r.ok:
            raise GitError("git %s failed in %s: %s" % (" ".join(args), self.repo, r.tail(12)))
        return r

    # ---- reading ----
    def exists(self) -> bool:
        return (self.repo / ".git").exists()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def is_clean(self) -> bool:
        return self._git("status", "--porcelain").stdout.strip() == ""

    def status_porcelain(self) -> str:
        return self._git("status", "--porcelain").stdout

    def diff_staged(self, base: str) -> str:
        """Everything that differs from ``base``, new files included (stages the work tree)."""
        self._git("add", "-A")
        return self._git("diff", "--cached", "--no-color", "--no-ext-diff", "-U3", base).stdout

    def diff_stat(self, base: str) -> str:
        self._git("add", "-A")
        return self._git("diff", "--cached", "--stat", base).stdout

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self._git("merge-base", "--is-ancestor", older, newer, check=False).code == 0

    def rev(self, ref: str) -> str:
        return self._git("rev-parse", ref).stdout.strip()

    def remote_url(self) -> str:
        return self._git("remote", "get-url", "origin", check=False).stdout.strip()

    def tags_at(self, ref: str = "HEAD") -> List[str]:
        return [t for t in self._git("tag", "--points-at", ref, check=False).stdout.split() if t]

    # ---- changing ----
    def clone(self, url: str, branch: str) -> None:
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        r = self._run(["git", "clone", "--branch", branch, url, str(self.repo)], timeout=900, log_file=self.log_file,
                      extra_env={"GIT_TERMINAL_PROMPT": "0"})
        if not r.ok:
            raise GitError("git clone %s failed: %s" % (url, r.tail(10)))

    def sync(self, branch: str) -> str:
        """Fetches and fast-forwards ``branch``; refuses to touch a dirty work tree."""
        if not self.is_clean():
            raise GitError("%s has uncommitted changes; leaving it alone:\n%s" % (self.repo, self.status_porcelain()[:800]))
        self._git("fetch", "origin", "--prune", "--tags", retries=3, timeout=600)
        self._git("checkout", branch)
        self._git("merge", "--ff-only", "origin/%s" % branch)
        return self.head()

    def checkout(self, ref: str) -> None:
        self._git("checkout", ref)

    def fetch(self) -> None:
        self._git("fetch", "origin", "--prune", "--tags", retries=3, timeout=600)

    def new_branch(self, name: str, start: str = "HEAD") -> None:
        self._git("checkout", "-B", name, start)

    def reset_hard(self, ref: str) -> None:
        self._git("reset", "--hard", ref)
        self._git("clean", "-fd")

    def commit(self, message: str, paths: Sequence[str] = ()) -> Optional[str]:
        if paths:
            self._git("add", "--", *paths)
        else:
            self._git("add", "-A")
        if self._git("diff", "--cached", "--quiet", check=False).code == 0:
            return None
        msg_file = self.repo / ".git" / "CS2MMT_COMMIT_MSG"
        msg_file.write_text(message, encoding="utf-8")
        try:
            self._git("commit", "-q", "-F", str(msg_file))
        finally:
            try:
                msg_file.unlink()
            except OSError:
                pass
        return self.head()

    def tag(self, name: str, message: str) -> None:
        self._git("tag", "-a", name, "-m", message)

    def push(self, refspec: str, set_upstream: bool = False, force_with_lease: bool = False) -> None:
        """``force_with_lease`` only for the tool's own job branches (a re-run replaces them)."""
        args = ["push"] + (["-u"] if set_upstream else []) + (["--force-with-lease"] if force_with_lease else []) + ["origin", refspec]
        self._git(*args, retries=4, timeout=600)

    def push_tag(self, name: str) -> None:
        self._git("push", "origin", "refs/tags/%s" % name, retries=4, timeout=600)

    def fast_forward(self, branch: str, to: str) -> None:
        """Moves ``branch`` to ``to`` only if that is a fast-forward."""
        self._git("checkout", branch)
        self._git("merge", "--ff-only", to)
