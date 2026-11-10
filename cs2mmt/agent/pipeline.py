"""What the agent does for each kind of job.

``game_update``      Steam installs the new build; every mod is built; Claude fixes what no longer
                     compiles (with the decompiled API diff); the in-game smoke test runs; Claude
                     fixes runtime errors; changes that pass the policy gate get a new version and
                     are published (or wait for approval, as configured).
``bugfix``           a triaged bug report: Claude attempts a minimal fix; build, smoke test,
                     policy; then approval (by default) before anything is published.
``publish_approved`` publishes exactly the commits the owner approved.
``rehearsal``        the whole path without publishing, to find expired logins and broken
                     setups while the owner can still fix them.
``triage``           classifies new posts (when the controller has no Claude of its own).

Deterministic steps (git, build, version, upload) are done by this code; Claude only edits
source files, inside a permission sandbox, and its result is checked before it can ship.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..common import apidiff, proc
from ..common import publishconfig as pc
from ..common.claude import FIX_DENY, FIX_TOOLS, ClaudeResult, ClaudeRunner, ClaudeTask, mod_session_rules, render_template
from ..common.config import Config
from ..common.policy import PolicyResult, check_diff, parse_unified_diff, policy_from_dict
from ..common.registry import ModSpec, Registry
from ..common.triage import run_triage
from ..common.versioning import GameVersion, parse_game_version, wildcard_matches
from .client import ClaimedJob, Controllers
from .state import AgentState
from .versionedit import ReleaseEdit, prepare_release, read_text, update_game_version_only
from .windows import decompile as dc
from .windows.build import BuildResult, add_game_references, dotnet_build
from .windows.game import HARNESS_NAME, SmokeConfig, SmokeResult, SmokeTester
from .windows.gitops import Git, GitError
from .windows.publish import find_modpublisher, run_modpublisher, stage_content
from .windows.steam import BUSY_MASK, AppStatus, SteamLocator, is_process_running, kill_process

__all__ = ["Cancelled", "JobFailed", "FIX_SCHEMA", "ModOutcome", "Agent", "JobRun"]

log = logging.getLogger("cs2mmt.pipeline")


class Cancelled(Exception):
    pass


class JobFailed(Exception):
    pass


FIX_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["fixed", "no_change_needed", "cannot_fix", "needs_owner"]},
        "summary_en": {"type": "string"},
        "summary_ko": {"type": "string"},
        "changes": {"type": "array", "items": {"type": "object", "properties": {"file": {"type": "string"}, "what": {"type": "string"}}, "required": ["file", "what"]}},
        "changelog_en": {"type": "array", "items": {"type": "string"}},
        "changelog_ko": {"type": "array", "items": {"type": "string"}},
        "behavior_changed": {"type": "boolean"},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "build_passed": {"type": "boolean"},
        "add_game_references": {"type": "array", "items": {"type": "string"}},
        "notes_for_owner": {"type": "string"},
        "reply_en": {"type": "string"},
    },
    "required": ["status", "summary_en", "summary_ko", "changelog_en", "changelog_ko", "behavior_changed", "risk", "build_passed"],
}

PING_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


@dataclass
class ModOutcome:
    mod_id: str
    base_commit: str = ""
    built_before: Optional[bool] = None
    build_ok: bool = False
    smoke_ok: Optional[bool] = None
    claude_sessions: List[Dict[str, Any]] = field(default_factory=list)
    changed: bool = False
    policy: Optional[str] = None
    policy_ok: Optional[bool] = None
    release: Optional[Dict[str, Any]] = None
    branch: Optional[str] = None
    commit: Optional[str] = None
    published: Optional[bool] = None
    status: str = "pending"
    notes: List[str] = field(default_factory=list)
    changelog_en: List[str] = field(default_factory=list)
    changelog_ko: List[str] = field(default_factory=list)
    metadata_game_version: Optional[str] = None
    reply_en: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


STATUS_KO = {
    "ok_unchanged": "변경 없음, 정상 동작",
    "released": "수정 후 배포 완료",
    "awaiting_approval": "수정 완료, 승인 대기",
    "metadata_updated": "게임 버전 표기만 갱신",
    "failed": "실패",
    "skipped": "건너뜀",
    "blocked": "정책 검사에 걸려 보류",
    "no_fix": "수정하지 않음",
    "pending": "미완료",
    "built": "빌드·테스트 통과",
}


class Agent:
    """Everything a job needs: config, registry, paths and the tools, created once per agent run."""

    def __init__(self, cfg: Config, registry: Registry, clients: Controllers, state: AgentState, run=proc.run, sleep=time.sleep):
        self.cfg = cfg
        self.registry = registry
        self.clients = clients
        self.state = state
        self._run = run
        self._sleep = sleep
        self.host_id = str(cfg.get("agent.host_id", "main-pc"))
        self.workspace = cfg.get_path("agent.workspace", ".") or Path(".")
        self.mods_root = self.workspace / str(cfg.get("agent.mods_root", "mods"))
        self.work_root = cfg.get_path("agent.work_dir") or (self.workspace / "work")
        self.cache_dir = cfg.get_path("agent.cache_dir") or (self.workspace / "cache")
        self.prompts_dir = cfg.get_path("agent.prompts_dir") or (self.workspace / "prompts")
        user_data = cfg.get_path("game.user_data_dir", "%USERPROFILE%/AppData/LocalLow/Colossal Order/Cities Skylines II")
        self.user_data = user_data or Path(".")
        self.appid = int(cfg.get("game.appid", 949230))
        steam_root = cfg.get_path("steam.root")
        steam_exe = cfg.get_path("steam.exe")
        self.steam = SteamLocator(root=steam_root, exe=steam_exe, appid=self.appid, run=run)
        self.policy_cfg = cfg.section("policy")

    # ---------------------------------------------------------------- paths & tools
    def mod_dir(self, mod: ModSpec) -> Path:
        p = Path(mod.dir)
        return p if p.is_absolute() else self.workspace / p

    def rel(self, p: Path) -> str:
        try:
            return Path(p).resolve().relative_to(self.workspace.resolve()).as_posix()
        except ValueError:
            return Path(p).as_posix()

    def git(self, mod: ModSpec, log_file: Optional[Path] = None) -> Git:
        return Git(self.mod_dir(mod), str(self.cfg.get("git.author_name", "")), str(self.cfg.get("git.author_email", "")), log_file=log_file, run=self._run, sleep=self._sleep)

    def game_dir(self) -> Path:
        configured = self.cfg.get_path("game.install_dir")
        if configured:
            return configured
        st = self.steam.status()
        if st is None:
            raise JobFailed("Cities: Skylines II is not installed in any Steam library (set [game].install_dir)")
        return st.game_dir

    def managed_dir(self) -> Path:
        return dc.managed_dir_of(self.game_dir())

    def build_props(self, mod: ModSpec) -> Dict[str, str]:
        props = {"CS2ManagedPath": str(self.managed_dir()), "CS2LocalModsPath": str(self.user_data / "Mods")}
        props.update(mod.extra_build_props)
        return props

    def build(self, mod: ModSpec, log_file: Optional[Path]) -> BuildResult:
        project = self.mod_dir(mod) / mod.project
        if not project.is_file():
            raise JobFailed("%s: project %s not found" % (mod.id, project))
        return dotnet_build(project, str(self.cfg.get("build.configuration", "Release")), self.build_props(mod),
                            dotnet=str(self.cfg.get("build.dotnet", "dotnet")), log_file=log_file,
                            timeout_s=float(self.cfg.get("build.timeout_minutes", 15)) * 60, run=self._run)

    def build_harness(self, log_file: Optional[Path]) -> BuildResult:
        project = self.workspace / "harness" / HARNESS_NAME / (HARNESS_NAME + ".csproj")
        props = {"CS2ManagedPath": str(self.managed_dir()), "CS2LocalModsPath": str(self.user_data / "Mods")}
        return dotnet_build(project, "Release", props, dotnet=str(self.cfg.get("build.dotnet", "dotnet")), log_file=log_file, run=self._run)

    def claude(self) -> ClaudeRunner:
        s = self.cfg.section("secrets")
        env: Dict[str, str] = {}
        if s.get("claude_oauth_token"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = str(s["claude_oauth_token"])
        if s.get("anthropic_api_key"):
            env["ANTHROPIC_API_KEY"] = str(s["anthropic_api_key"])
        for k, v in dict(self.cfg.get("claude.env", {}) or {}).items():
            env[str(k)] = str(v)
        return ClaudeRunner(
            executable=str(self.cfg.get("claude.executable", "claude")),
            env=env,
            work_dir=self.work_root,
            run=self._run,
            default_model=self.cfg.get("claude.model", "opus"),
            default_fallback_model=self.cfg.get("claude.fallback_model", "sonnet"),
            default_effort=self.cfg.get("claude.effort") or None,
        )

    def prompt(self, name: str) -> str:
        return (self.prompts_dir / name).read_text(encoding="utf-8")

    def smoke_tester(self) -> SmokeTester:
        g = self.cfg
        sc = SmokeConfig(
            user_data_dir=self.user_data,
            game_dir=self.game_dir(),
            launch=[str(a) for a in g.get_list("game.launch", ["{game_dir}/Cities2.exe", "-continuelastsave"])],
            launch_env={str(k): str(v) for k, v in dict(g.get("game.launch_env", {"SteamAppId": str(self.appid), "SteamGameId": str(self.appid), "SteamOverlayGameId": str(self.appid)})).items()},
            run_seconds=int(float(g.get("game.smoke_minutes", 6)) * 60),
            load_timeout_s=int(float(g.get("game.load_timeout_minutes", 12)) * 60),
            test_save=str(g.get("game.test_save", "")),
            sim_speed=int(g.get("game.sim_speed", 3)),
            process_name=str(g.get("game.process_name", "Cities2.exe")),
            ignore_patterns=[str(p) for p in g.get_list("game.ignore_log_patterns")],
            appid=self.appid,
        )
        return SmokeTester(sc, run=self._run, sleep=self._sleep)

    # ---------------------------------------------------------------- checks
    def preflight(self) -> List[str]:
        problems: List[str] = []
        for exe, what in ((str(self.cfg.get("build.dotnet", "dotnet")), "the .NET SDK"), ("git", "Git"), (str(self.cfg.get("claude.executable", "claude")), "Claude Code")):
            r = self._run([exe, "--version"], timeout=60)
            if not r.ok:
                problems.append("%s (%s) is not working: %s" % (what, exe, r.tail(3)))
        s = self.cfg.section("secrets")
        if not (s.get("claude_oauth_token") or s.get("anthropic_api_key")):
            problems.append("no Claude credential in secrets (claude_oauth_token or anthropic_api_key)")
        try:
            free = _free_gb(self.workspace)
            if free is not None and free < float(self.cfg.get("agent.min_free_gb", 10)):
                problems.append("only %.1f GB free on the workspace drive" % free)
        except OSError:
            pass
        return problems


def _free_gb(path: Path) -> Optional[float]:
    import shutil as _sh

    try:
        return _sh.disk_usage(str(path)).free / 1e9
    except OSError:
        return None


class JobRun:
    def __init__(self, agent: Agent, claimed: ClaimedJob):
        self.a = agent
        self.claimed = claimed
        self.job = claimed.job
        self.payload: Dict[str, Any] = dict(claimed.payload)
        self.work = agent.work_root / claimed.id
        self.work.mkdir(parents=True, exist_ok=True)
        self.log_file = self.work / "job.log"
        self.outcomes: Dict[str, ModOutcome] = {}
        self.notes: List[str] = []
        self.costs: List[float] = []
        self.game_version: Optional[GameVersion] = parse_game_version(self.payload.get("game_version_hint"))
        self.installed_build: Optional[str] = None
        self.smoke: Optional[SmokeResult] = None
        self._file_logger = logging.FileHandler(self.log_file, encoding="utf-8")
        self._file_logger.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    # ---------------------------------------------------------------- plumbing
    def stage(self, name: str, message: str = "") -> None:
        log.info("[%s] %s %s", self.claimed.id, name, message)
        if self.a.clients.heartbeat(self.claimed, name, message):
            raise Cancelled("cancelled by the owner during '%s'" % name)

    def outcome(self, mod: ModSpec) -> ModOutcome:
        if mod.id not in self.outcomes:
            self.outcomes[mod.id] = ModOutcome(mod.id)
        return self.outcomes[mod.id]

    def run(self) -> Tuple[str, Dict[str, Any], str]:
        logging.getLogger().addHandler(self._file_logger)
        kind = self.claimed.kind
        try:
            handler = getattr(self, "run_" + kind, None)
            if handler is None:
                raise JobFailed("this agent does not know job kind %r" % kind)
            summary = handler()
            # A change held back by the policy gate leaves the mod broken for players: that is a failure.
            state = "failed" if any(o.status in ("failed", "blocked") for o in self.outcomes.values()) else "succeeded"
            return state, self.result(), summary
        except Cancelled as e:
            return "cancelled", self.result(error=str(e)), "취소됨: %s" % e
        except Exception as e:
            log.exception("job %s failed", self.claimed.id)
            err = "%s: %s" % (type(e).__name__, e)
            return "failed", self.result(error=err, trace=traceback.format_exc()[-4000:]), "실패: %s\n%s" % (err, self.mod_lines())
        finally:
            logging.getLogger().removeHandler(self._file_logger)
            self._file_logger.close()

    def result(self, **extra: Any) -> Dict[str, Any]:
        out = {
            "mods": {k: v.to_dict() for k, v in self.outcomes.items()},
            "notes": self.notes,
            "installed_build": self.installed_build,
            "game_version": str(self.game_version) if self.game_version else None,
            "cost_usd": round(sum(self.costs), 2) if self.costs else None,
            "work_dir": str(self.work),
        }
        if self.smoke is not None:
            out["smoke"] = self.smoke.summary()
        if getattr(self, "retry_later", False):
            out["retry"] = True
        out.update(getattr(self, "extra_result", {}) or {})
        out.update(extra)
        return out

    def mod_lines(self) -> str:
        lines = []
        for mod in self.a.registry.mods:
            o = self.outcomes.get(mod.id)
            if not o:
                continue
            text = "- %s: %s" % (mod.display_name, STATUS_KO.get(o.status, o.status))
            if o.release:
                text += " %s → %s" % (o.release.get("old_version"), o.release.get("new_version"))
            if o.notes:
                text += " (%s)" % "; ".join(o.notes[-3:])
            lines.append(text)
        if self.costs:
            lines.append("Claude 사용량 추정: $%.2f" % sum(self.costs))
        return "\n".join(lines)

    # ---------------------------------------------------------------- shared steps
    def ensure_steam_and_build(self, target: Optional[str], settle_s: Optional[float] = None) -> AppStatus:
        """Starts Steam and waits until the game is installed at ``target`` (or newer), ready to run.

        Without a target (bug fixes, publishing, rehearsals) the installed build is fine once
        Steam has had ``settle_s`` seconds to start any pending update.
        """
        if settle_s is None:
            settle_s = float(self.a.cfg.get("steam.idle_settle_seconds", 120))
        st = self.a.steam.status()
        if st is None:
            raise JobFailed("CS2 is not installed (no appmanifest_%d.acf)" % self.a.appid)
        assemblies = [str(x) for x in self.a.cfg.get_list("decompile.assemblies", DEFAULT_ASSEMBLIES)]
        # Copy the current build's DLLs before Steam touches them: the API diff needs the old ones.
        try:
            dc.backup_managed(dc.managed_dir_of(st.game_dir), st.buildid, self.a.cache_dir, assemblies)
        except OSError as e:
            self.notes.append("could not back up the game DLLs: %s" % e)
        self.stage("steam", "installed build %s, waiting for %s" % (st.buildid, target or "the latest"))
        self.a.steam.start([str(x) for x in self.a.cfg.get_list("steam.start_args", ["-silent"])])
        timeout = float(self.a.cfg.get("steam.update_timeout_minutes", 120)) * 60
        start = time.time()
        restarted = False
        settled_since: Optional[float] = None
        while True:
            st = self.a.steam.status() or st
            reached = target is None or (st.buildid.isdigit() and str(target).isdigit() and int(st.buildid) >= int(target)) or st.buildid == str(target)
            if reached and st.ready:
                if target is not None:
                    break
                # No target build known: wait until Steam has been idle for a while.
                settled_since = settled_since or time.time()
                if time.time() - settled_since >= settle_s:
                    break
            else:
                settled_since = None
            elapsed = time.time() - start
            if elapsed > timeout:
                self.retry_later = True
                raise JobFailed("Steam did not install build %s within %d min (installed %s, %s). Is Steam logged in and allowed to update?"
                                % (target, timeout // 60, st.buildid, st.progress()))
            if not restarted and elapsed > 900 and not reached and not (st.state_flags & BUSY_MASK):
                # Nothing is happening: restart Steam once so it checks for the update.
                restarted = True
                self.notes.append("Steam restarted to pick up the update")
                self.a.steam.restart()
            self.stage("steam", "build %s, %s" % (st.buildid, st.progress()))
            self.a._sleep(30)
        self.installed_build = st.buildid
        try:
            dc.backup_managed(dc.managed_dir_of(st.game_dir), st.buildid, self.a.cache_dir, assemblies)
        except OSError:
            pass
        return st

    def sync_mods(self, mods: List[ModSpec]) -> None:
        for mod in mods:
            o = self.outcome(mod)
            g = self.a.git(mod, self.log_file)
            if not g.exists():
                self.stage("clone", mod.id)
                g.clone(mod.repo if mod.repo.endswith(".git") else mod.repo + ".git", mod.default_branch)
            elif not g.is_clean():
                # Leftovers of an interrupted job: keep them, but out of the way.
                self.a._run(["git", "-C", str(self.a.mod_dir(mod)), "stash", "push", "-u", "-m", "cs2mmt %s leftover" % self.claimed.id],
                            timeout=120, log_file=self.log_file)
                o.notes.append("작업 폴더에 남은 변경을 git stash로 보관함")
            try:
                if g.current_branch() != mod.default_branch:
                    g.checkout(mod.default_branch)
            except GitError:
                pass
            o.base_commit = g.sync(mod.default_branch)

    def api_diff(self, old_build: Optional[str], new_build: Optional[str], mods: List[ModSpec]) -> Optional[Path]:
        if not self.a.cfg.get("decompile.enabled", True) or not old_build or not new_build or str(old_build) == str(new_build):
            return None
        old_dlls = self.a.cache_dir / "managed" / str(old_build)
        if not (old_dlls / "MANIFEST.json").is_file():
            self.notes.append("no copy of the old build's DLLs; Claude works from the compiler errors only")
            return None
        assemblies = [str(x) for x in self.a.cfg.get_list("decompile.assemblies", DEFAULT_ASSEMBLIES)]
        ilspy = str(self.a.cfg.get("decompile.ilspycmd", "ilspycmd"))
        timeout = float(self.a.cfg.get("decompile.timeout_minutes", 30)) * 60
        self.stage("decompile", "build %s and %s" % (old_build, new_build))
        old_tree = dc.decompile(old_dlls, str(old_build), self.a.cache_dir, assemblies, ilspy, reference_dir=self.a.managed_dir(), timeout_s=timeout, log_file=self.log_file, run=self.a._run)
        new_tree = dc.decompile(self.a.managed_dir(), str(new_build), self.a.cache_dir, assemblies, ilspy, reference_dir=self.a.managed_dir(), timeout_s=timeout, log_file=self.log_file, run=self.a._run)
        if old_tree is None or new_tree is None:
            self.notes.append("decompiling failed (is ilspycmd installed?); no API diff")
            return None
        ids = apidiff.collect_identifiers([self.a.mod_dir(m) for m in mods])
        rep = apidiff.diff_trees(old_tree, new_tree, ids)
        out = self.work / "api-diff"
        apidiff.write_report(rep, out, str(old_build), str(new_build), old_tree, new_tree)
        self.notes.append("API diff: %d relevant changed types, %d removed" % (len(rep.relevant_changed), len(rep.relevant_removed)))
        return out

    def claude_fix(self, mod: ModSpec, template: str, values: Dict[str, Any], label: str, max_changed_lines: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """One Claude session that edits only ``mod``'s folder. Returns its structured result."""
        o = self.outcome(mod)
        mod_rel = self.a.rel(self.a.mod_dir(mod))
        projects = ["%s/%s" % (self.a.rel(self.a.mod_dir(m)), m.project) for m in self.a.registry.mods]
        allowed = list(FIX_TOOLS) + mod_session_rules(mod_rel, projects)
        values = dict(values)
        values.setdefault("MOD_ID", mod.id)
        values.setdefault("MOD_NAME", mod.display_name)
        values.setdefault("MOD_DIR", mod_rel)
        values.setdefault("PROJECT", "%s/%s" % (mod_rel, mod.project))
        values.setdefault("BUILD_COMMAND", "dotnet build %s/%s -c Release -nologo -v:minimal %s" % (
            mod_rel, mod.project, " ".join('"-p:%s=%s"' % kv for kv in self.a.build_props(mod).items())))
        values.setdefault("MOD_NOTES", mod.notes or "(none)")
        values.setdefault("DEPENDENCIES", ", ".join("%s (%s)" % (d, self.a.rel(self.a.mod_dir(self.a.registry.get(d)))) for d in mod.depends_on) or "(none)")
        values.setdefault("DEPENDENTS", ", ".join(self.a.registry.dependents_of(mod.id)) or "(none)")
        values.setdefault("MAX_CHANGED_LINES", max_changed_lines or int(self.a.policy_cfg.get("max_changed_lines", 1500)))
        values.setdefault("API_DIFF", "(not available in this run)")
        values.setdefault("DECOMPILED_NEW", "(not available in this run)")
        values.setdefault("ATTEMPT", 1)
        prompt = render_template(template, values)
        if values.get("BUILD_ERRORS") and "${BUILD_ERRORS}" not in template:
            prompt += ("\n\n## The build fails after the previous attempt\n\n```\n%s\n```\n\nFix these errors as part of this task." % values["BUILD_ERRORS"])
        name = "cs2mmt-%s-%s-%s" % (self.claimed.id, mod.id, label)
        resume = None
        if str(self.a.cfg.get("claude.session_mode", "fresh")) == "resume":
            resume = self.a.state.session_for("mod:" + mod.id)
        task = ClaudeTask(
            name=name,
            prompt=prompt,
            cwd=self.a.workspace,
            schema=FIX_SCHEMA,
            allowed_tools=allowed,
            disallowed_tools=list(FIX_DENY),
            max_turns=int(self.a.cfg.get("claude.max_turns", 150)),
            timeout_s=int(float(self.a.cfg.get("claude.fix_timeout_minutes", 90)) * 60),
            system_prompt_file=self.a.prompts_dir / "system_appendix.md",
            resume_session=resume,
            max_budget_usd=float(self.a.cfg.get("claude.max_budget_usd", 0)) or None,
            log_file=self.work / ("claude-%s-%s.log" % (mod.id, label)),
        )
        self.stage("claude " + label, mod.id)
        res: ClaudeResult = self.a.claude().run(task)
        if res.cost_usd:
            self.costs.append(float(res.cost_usd))
        if str(self.a.cfg.get("claude.session_mode", "fresh")) == "resume":
            self.a.state.set_session("mod:" + mod.id, res.session_id)
        if not res.ok and res.error and re.search(r"rate.?limit|overloaded|usage limit|429|529", res.error, re.I):
            self.retry_later = True
        entry = {"label": label, "ok": res.ok, "error": res.error, "session_id": res.session_id, "turns": res.num_turns,
                 "cost_usd": res.cost_usd, "result": res.structured, "denied": len(res.permission_denials)}
        o.claude_sessions.append(entry)
        if not res.ok:
            o.notes.append("Claude 세션 실패: %s" % (res.error or "?")[:200])
            return None
        data = res.structured or {}
        o.changelog_en = list(data.get("changelog_en") or o.changelog_en)
        o.changelog_ko = list(data.get("changelog_ko") or o.changelog_ko)
        if data.get("reply_en"):
            o.reply_en = data["reply_en"]
        refs = [str(x) for x in data.get("add_game_references") or []]
        if refs:
            self.apply_game_references(mod, refs)
        return data

    def apply_game_references(self, mod: ModSpec, names: List[str]) -> None:
        project = self.a.mod_dir(mod) / mod.project
        text = read_text(project)
        new_text, added = add_game_references(text, names, self.a.managed_dir())
        if added:
            project.write_text(new_text, encoding="utf-8")
            self.outcome(mod).notes.append("게임 어셈블리 참조 추가: %s" % ", ".join(added))
            self.tool_edited.setdefault(mod.id, set()).add(mod.project)

    @property
    def tool_edited(self) -> Dict[str, set]:
        if not hasattr(self, "_tool_edited"):
            self._tool_edited: Dict[str, set] = {}
        return self._tool_edited

    def fix_until_builds(self, mod: ModSpec, br: BuildResult, template: str, values: Dict[str, Any], rounds: int) -> BuildResult:
        for i in range(rounds):
            values = dict(values)
            values["BUILD_ERRORS"] = br.error_text()
            values["ATTEMPT"] = i + 1
            data = self.claude_fix(mod, template, values, "compile%d" % (i + 1))
            br = self.a.build(mod, self.log_file)
            if br.ok:
                return br
            if data is not None and data.get("status") in ("cannot_fix", "needs_owner"):
                self.outcome(mod).notes.append("Claude: %s" % (data.get("summary_ko") or data.get("summary_en") or "")[:300])
                break
        return br

    def run_smoke(self, mods: List[ModSpec]) -> SmokeResult:
        self.stage("harness")
        hb = self.a.build_harness(self.log_file)
        if not hb.ok:
            self.notes.append("the test harness did not build: %s" % hb.error_text(5)[:300])
        self.stage("smoke test", ", ".join(m.id for m in mods))
        tester = self.a.smoke_tester()
        try:
            sm = tester.run(mods, self.claimed.id, heartbeat=lambda s: self.stage("smoke test", s))
        finally:
            tester.remove_harness()
        (self.work / ("smoke-%d.txt" % int(time.time()))).write_text(sm.summary() + "\n\n" + "\n\n".join(
            "\n".join(v.errors) for v in sm.verdicts if v.errors), encoding="utf-8")
        if sm.harness and sm.harness.get("game_version"):
            gv = parse_game_version(sm.harness.get("game_version"))
            if gv:
                self.game_version = gv
        self.smoke = sm
        return sm

    def check_policy(self, mod: ModSpec, max_lines: Optional[int] = None) -> PolicyResult:
        o = self.outcome(mod)
        g = self.a.git(mod, self.log_file)
        diff = g.diff_staged(o.base_commit)
        changes = parse_unified_diff(diff)
        tool_paths = self.tool_edited.get(mod.id, set())
        claude_changes = [c for c in changes if c.path not in tool_paths]
        cfg = policy_from_dict(self.a.policy_cfg)
        if max_lines:
            cfg.max_changed_lines = max_lines
        result = check_diff(claude_changes, cfg, secrets=self.a.cfg.secrets())
        o.changed = bool(changes)
        o.policy = result.summary()
        o.policy_ok = result.ok
        (self.work / ("diff-%s.patch" % mod.id)).write_text(diff, encoding="utf-8")
        return result

    def commit_release(self, mod: ModSpec, kind_label: str, subject_extra: str = "") -> None:
        o = self.outcome(mod)
        mod_dir = self.a.mod_dir(mod)
        rel: ReleaseEdit = prepare_release(mod_dir, mod, o.changelog_en, o.changelog_ko, self.game_version, bump="patch")
        o.release = {"old_version": rel.old_version, "new_version": rel.new_version, "files": rel.files_changed, "game_version_value": rel.game_version_value}
        br = self.a.build(mod, self.log_file)  # the UI module header changed with the version
        if not br.ok:
            raise JobFailed("%s does not build after the version change: %s" % (mod.id, br.error_text(5)))
        g = self.a.git(mod, self.log_file)
        branch = "%s%s-%s" % (self.a.cfg.get("git.branch_prefix", "cs2mmt/"), self.claimed.id, mod.id)
        g.new_branch(branch)
        lines = ["Version %s: %s%s" % (rel.new_version, kind_label, subject_extra), ""]
        lines += ["- " + re.sub(r"^[-*]\s*", "", l) for l in o.changelog_en] or ["- Maintenance update."]
        lines += ["", "Automated by CS2ModManagementTool (job %s)." % self.claimed.id]
        if self.installed_build:
            lines.append("Tested in game on Steam build %s%s." % (self.installed_build, (" (game %s)" % self.game_version) if self.game_version else ""))
        co = str(self.a.cfg.get("git.co_author", "Claude <noreply@anthropic.com>"))
        if co and any(s.get("ok") for s in o.claude_sessions):
            lines += ["", "Co-Authored-By: %s" % co]
        o.commit = g.commit("\n".join(lines))
        o.branch = branch
        if self.a.cfg.get("git.push", True):
            self.stage("push branch", branch)
            g.push(branch, set_upstream=True, force_with_lease=True)

    def publish_ready(self, mods: List[ModSpec], verb: str = "NewVersion") -> bool:
        """Uploads each mod (dependency order), then moves the default branch and tags it."""
        if not mods:
            return True
        if is_process_running(str(self.a.cfg.get("game.process_name", "Cities2.exe")), self.a._run):
            kill_process(str(self.a.cfg.get("game.process_name", "Cities2.exe")), self.a._run)
            self.a._sleep(10)
        exe = find_modpublisher(self.a.cfg.get("publish.modpublisher"), self.a.game_dir())
        if exe is None:
            for m in mods:
                self.outcome(m).notes.append("ModPublisher.exe를 찾지 못해 배포하지 못함")
                self.outcome(m).status = "failed"
            return False
        all_ok = True
        for mod in self.a.registry.build_order([m.id for m in mods]):
            if mod not in mods:
                continue
            o = self.outcome(mod)
            mod_dir = self.a.mod_dir(mod)
            info = pc.read_info(read_text(mod_dir / mod.publish_config))
            problems = pc.validate(info, verb, mod_dir)
            if problems:
                o.status, o.published = "failed", False
                o.notes.append("배포 설정 문제: " + "; ".join(problems))
                all_ok = False
                continue
            content_src = Path(os.path.expandvars(mod.content_dir)) if mod.content_dir else self.a.user_data / "Mods" / mod.assembly
            self.stage("publish", "%s %s" % (mod.id, verb))
            staged = stage_content(content_src, self.work / "publish" / mod.id)
            res = run_modpublisher(exe, verb, mod_dir / mod.publish_config, staged, verbose=bool(self.a.cfg.get("publish.verbose", True)),
                                   timeout_s=float(self.a.cfg.get("publish.timeout_minutes", 20)) * 60, log_file=self.log_file, run=self.a._run)
            if not res.ok:
                o.published = False
                o.status = "failed"
                o.notes.append("업로드 실패: %s" % res.message)
                all_ok = False
                if res.kind == "pdx_login":
                    self.a.clients.notify("Paradox 계정 로그인이 풀려 업로드하지 못했습니다. 휴가 때 PC에서 게임을 켜 로그인해 주세요. (%s)" % mod.display_name, "error")
                    break
                continue
            o.published = True
            self.finish_git_after_publish(mod, verb)
        return all_ok

    def finish_git_after_publish(self, mod: ModSpec, verb: str) -> None:
        o = self.outcome(mod)
        g = self.a.git(mod, self.log_file)
        if not g.is_clean():
            # ModPublisher may write back to the publish configuration.
            g.commit("PublishConfiguration after ModPublisher %s" % verb)
        if o.branch:
            head = g.head()
            g.fast_forward(mod.default_branch, head)
        version = (o.release or {}).get("new_version")
        if self.a.cfg.get("git.push", True):
            self.stage("push", "%s %s" % (mod.id, mod.default_branch))
            g.push(mod.default_branch)
            if version and verb == "NewVersion":
                tag = "v%s" % version
                if tag not in g.tags_at("HEAD"):
                    g.tag(tag, "%s %s" % (mod.display_name, version))
                g.push_tag(tag)
        o.status = "released" if verb == "NewVersion" else "metadata_updated"

    def request_approval(self, mods: List[ModSpec], kind: str, title: str, body: str) -> str:
        aid = "ap-%s-%s" % (time.strftime("%m%d"), secrets.token_hex(3))
        entries = []
        for mod in mods:
            o = self.outcome(mod)
            slug = mod.github_slug
            compare = "https://github.com/%s/compare/%s...%s" % (slug, mod.default_branch, o.branch) if slug and o.branch else ""
            entries.append({"id": mod.id, "branch": o.branch, "commit": o.commit, "version": (o.release or {}).get("new_version"),
                            "metadata_game_version": o.metadata_game_version, "compare": compare})
            if compare:
                body += "\n- %s 변경 보기: %s" % (mod.display_name, compare)
        payload = {"mods": entries, "source_job": self.claimed.id, "game_build": self.installed_build,
                   "game_version": str(self.game_version) if self.game_version else None, "kind": kind}
        sent = self.a.clients.request_approval({"id": aid, "job_id": self.claimed.id, "kind": kind, "title": title, "body": body,
                                                "payload": payload, "policy": str(self.payload.get("approval", "require"))})
        if not sent:
            raise JobFailed("could not reach any controller to ask for approval")
        for mod in mods:
            self.outcome(mod).status = "awaiting_approval"
        return aid

    # ---------------------------------------------------------------- job kinds
    def run_game_update(self) -> str:
        self.stage("preflight")
        problems = self.a.preflight()
        if problems:
            raise JobFailed("; ".join(problems))
        target = self.payload.get("new_build")
        old = self.payload.get("old_build")
        covered = self.a.state.game_update_covered(target)
        if covered and not self.payload.get("manual"):
            self.notes.append("already handled by %s" % covered)
            return "이미 처리된 업데이트입니다 (%s)." % covered
        # A game update without a known build (detected from the patch notes) waits longer for Steam.
        st = self.ensure_steam_and_build(target, settle_s=None if target else 600)
        mods = self.a.registry.build_order()
        self.stage("sync")
        self.sync_mods(mods)
        diff_dir = self.api_diff(old, st.buildid, mods)
        template = self.a.prompt("game_update_fix.md")
        notes_text = "\n\n".join("## %s\n%s" % (n.get("title"), (n.get("contents") or "")[:6000]) for n in self.payload.get("patch_notes", [])) or "(no patch notes found)"
        base_values = {
            "OLD_BUILD": old or "?",
            "NEW_BUILD": st.buildid,
            "GAME_VERSION": str(self.game_version or "unknown"),
            "API_DIFF": self.a.rel(diff_dir) + "/summary.md" if diff_dir else "(not available)",
            "DECOMPILED_NEW": self.a.rel(self.a.cache_dir / "decompiled" / st.buildid) if diff_dir else "(not available)",
            "PATCH_NOTES": notes_text,
            "JOB_ID": self.claimed.id,
        }
        failed: set = set()
        rounds = int(self.a.cfg.get("claude.compile_rounds", 2))
        for mod in mods:
            o = self.outcome(mod)
            if any(d in failed for d in mod.depends_on):
                o.status = "skipped"
                o.notes.append("의존 모드가 실패해 건너뜀")
                failed.add(mod.id)
                continue
            self.stage("build", mod.id)
            br = self.a.build(mod, self.log_file)
            o.built_before = br.ok
            if not br.ok:
                br = self.fix_until_builds(mod, br, template, base_values, rounds)
            o.build_ok = br.ok
            if not br.ok:
                o.status = "failed"
                o.notes.append("빌드 실패: %s" % br.error_text(3)[:300])
                failed.add(mod.id)
        testable = [m for m in mods if self.outcome(m).build_ok]
        if testable:
            self.smoke_and_fix(testable, base_values)
        self.settle_and_release(mods, "game update", " (game build %s)" % st.buildid)
        for mod in mods:
            o = self.outcome(mod)
            if o.build_ok and o.smoke_ok:
                self.a.state.set_tested(mod.id, st.buildid, (o.release or {}).get("new_version") or "")
        dc.prune(self.a.cache_dir, keep=int(self.a.cfg.get("decompile.keep_builds", 3)))
        head = "게임 업데이트 대응: 빌드 %s → %s%s" % (old or "?", st.buildid, (" (게임 %s)" % self.game_version) if self.game_version else "")
        return head + "\n" + self.mod_lines() + ("\n" + "\n".join(self.notes[-5:]) if self.notes else "")

    def smoke_and_fix(self, testable: List[ModSpec], base_values: Dict[str, Any]) -> None:
        sm = self.run_smoke(testable)
        require_city = bool(self.a.cfg.get("game.require_city_load", True))
        fix_rounds = int(self.a.cfg.get("claude.smoke_rounds", 1))
        template = self.a.prompt("smoke_fix.md")
        for rnd in range(fix_rounds):
            if not sm.city_loaded:
                break
            failing = [m for m in testable if not sm.mod_ok(m.id)]
            if not failing:
                break
            for mod in failing:
                v = sm.verdict(mod.id)
                values = dict(base_values)
                values["SMOKE_SUMMARY"] = v.summary() if v else "no verdict"
                values["SMOKE_ERRORS"] = "\n\n".join((v.errors if v else [])[:12])[:20000] or "(no error lines; see the missing expectations)"
                values["MISSING"] = ", ".join(k for k, ok in (v.expectations.items() if v else []) if not ok) or "(none)"
                self.claude_fix(mod, template, values, "runtime%d" % (rnd + 1))
                br = self.a.build(mod, self.log_file)
                self.outcome(mod).build_ok = br.ok
            sm = self.run_smoke([m for m in testable if self.outcome(m).build_ok])
        for mod in testable:
            o = self.outcome(mod)
            if not sm.city_loaded:
                o.smoke_ok = None if not require_city else False
                o.notes.append("스모크 테스트에서 도시가 로드되지 않음 (%s)" % (sm.note or "?"))
                if require_city:
                    o.status = "failed"
            else:
                o.smoke_ok = sm.mod_ok(mod.id)
                if not o.smoke_ok:
                    o.status = "failed"
                    v = sm.verdict(mod.id)
                    o.notes.append("스모크 테스트 실패: %s" % (v.summary() if v else "?"))

    def settle_and_release(self, mods: List[ModSpec], kind_label: str, subject_extra: str, max_lines: Optional[int] = None) -> None:
        """Policy gate, version bump, commit, then publish or ask for approval."""
        mode = str(self.payload.get("approval", "auto"))
        ready: List[ModSpec] = []
        metadata: List[ModSpec] = []
        for mod in mods:
            o = self.outcome(mod)
            if o.status in ("failed", "skipped") or not o.build_ok or o.smoke_ok is False:
                continue
            # It was built against its dependency's DLL as it is on this PC: if that one did not
            # pass, this build may contain changes that will never ship.
            bad_deps = [d for d in mod.depends_on if self.outcomes.get(d) and self.outcomes[d].status in ("failed", "blocked", "skipped")]
            if bad_deps:
                o.status = "blocked"
                o.notes.append("의존 모드 %s가 통과하지 못해 보류" % ", ".join(bad_deps))
                self.keep_for_review(mod)
                continue
            res = self.check_policy(mod, max_lines)
            if not o.changed:
                o.status = "ok_unchanged"
                if self.game_version is not None:
                    info = pc.read_info(read_text(self.a.mod_dir(mod) / mod.publish_config))
                    if info.game_version and not wildcard_matches(info.game_version, self.game_version) and info.mod_id:
                        metadata.append(mod)
                continue
            if not res.ok:
                o.status = "blocked"
                o.notes.append("정책 위반: " + "; ".join(res.violations[:5]))
                self.keep_for_review(mod)
                continue
            if not mod.publish:
                o.status = "built"
                o.notes.append("배포 꺼짐(publish = false)")
                self.keep_for_review(mod)
                continue
            self.commit_release(mod, kind_label, subject_extra)
            info = pc.read_info(read_text(self.a.mod_dir(mod) / mod.publish_config))
            if not info.mod_id:
                o.status = "built"
                o.notes.append("Paradox Mods에 아직 없는 모드라(ModId 없음) 배포 생략; 첫 등록은 직접 해야 함")
                continue
            ready.append(mod)
        # A mod released together with others (an API it offers changed) waits for its group.
        for mod in list(ready):
            group = self.a.registry.release_group(mod.id)
            blocked = [g for g in group if g != mod.id and self.outcomes.get(g) and self.outcomes[g].status in ("failed", "blocked")]
            if blocked:
                ready.remove(mod)
                self.outcome(mod).status = "blocked"
                self.outcome(mod).notes.append("함께 배포해야 하는 %s 실패로 보류" % ", ".join(blocked))
        for mod in metadata:
            o = self.outcome(mod)
            value = update_game_version_only(self.a.mod_dir(mod), mod, self.game_version)
            if value:
                o.metadata_game_version = value
                g = self.a.git(mod, self.log_file)
                branch = "%s%s-%s" % (self.a.cfg.get("git.branch_prefix", "cs2mmt/"), self.claimed.id, mod.id)
                g.new_branch(branch)
                o.commit = g.commit("GameVersion %s: checked with game %s\n\nAutomated by CS2ModManagementTool (job %s)." % (value, self.game_version, self.claimed.id))
                o.branch = branch
                o.status = "metadata"
        policy_mode = mode.split(":")[0]
        if policy_mode == "auto":
            if ready:
                self.publish_ready(ready, "NewVersion")
            meta_ok = [m for m in metadata if self.outcome(m).status == "metadata"]
            if meta_ok:
                self.publish_ready(meta_ok, "Update")
        else:
            pending = ready + [m for m in metadata if self.outcome(m).status == "metadata"]
            if pending:
                for m in pending:
                    if self.a.cfg.get("git.push", True) and self.outcome(m).branch and self.outcome(m).status == "metadata":
                        self.a.git(m, self.log_file).push(self.outcome(m).branch, set_upstream=True, force_with_lease=True)
                lines = []
                for m in pending:
                    o = self.outcome(m)
                    if o.release:
                        lines.append("- %s %s → %s: %s" % (m.display_name, o.release["old_version"], o.release["new_version"], "; ".join(o.changelog_ko) or "-"))
                    else:
                        lines.append("- %s: GameVersion %s" % (m.display_name, o.metadata_game_version))
                title = "%s 배포 승인 요청 (%s)" % (kind_label, ", ".join(m.display_name for m in pending))
                self.request_approval(pending, self.claimed.kind, title, "\n".join(lines))

    def keep_for_review(self, mod: ModSpec) -> None:
        """Pushes a change that will not ship to a review branch, then restores the default branch."""
        o = self.outcome(mod)
        g = self.a.git(mod, self.log_file)
        if g.is_clean():
            return
        branch = "%sreview-%s-%s" % (self.a.cfg.get("git.branch_prefix", "cs2mmt/"), self.claimed.id, mod.id)
        g.new_branch(branch)
        g.commit("Not published: needs the owner's review\n\n%s\n\nAutomated by CS2ModManagementTool (job %s)." % (o.policy or "", self.claimed.id))
        if self.a.cfg.get("git.push", True):
            try:
                g.push(branch, set_upstream=True, force_with_lease=True)
                o.notes.append("검토용 브랜치 %s" % branch)
            except GitError as e:
                o.notes.append("검토용 브랜치 푸시 실패: %s" % e)
        g.checkout(mod.default_branch)

    def run_bugfix(self) -> str:
        mod = self.a.registry.get(str(self.payload.get("mod")))
        reports = list(self.payload.get("reports") or [])
        ids = [r["report"]["id"] for r in reports]
        new_ids = self.a.state.unseen(ids)
        if not new_ids:
            return "이미 처리한 제보입니다."
        reports = [r for r in reports if r["report"]["id"] in new_ids]
        self.stage("preflight")
        problems = self.a.preflight()
        if problems:
            raise JobFailed("; ".join(problems))
        st = self.ensure_steam_and_build(None)
        group = self.a.registry.build_order([mod.id] + self.a.registry.dependents_of(mod.id))
        self.stage("sync")
        self.sync_mods(group)
        for m in group:
            br = self.a.build(m, self.log_file)
            self.outcome(m).built_before = br.ok
            self.outcome(m).build_ok = br.ok
            if not br.ok:
                raise JobFailed("%s does not build as it is (a game update fix may be needed first): %s" % (m.id, br.error_text(3)))
        boundary = "REPORT-" + secrets.token_hex(6)
        blocks = []
        for r in reports:
            rep, tri = r["report"], r.get("triage", {})
            blocks.append("<<<%s id=%s source=%s author=%s url=%s>>>\n%s\n<<<END %s>>>\nTriage: %s" % (
                boundary, rep["id"], rep.get("source"), rep.get("author"), rep.get("url"), (rep.get("text") or "")[:8000], boundary,
                json.dumps({k: tri.get(k) for k in ("kind", "summary_en", "repro_steps", "suspected_area", "game_version", "mod_version", "other_mods")}, ensure_ascii=False)))
        values = {
            "REPORTS": "\n\n".join(blocks),
            "BOUNDARY": boundary,
            "NEW_BUILD": st.buildid,
            "GAME_VERSION": str(self.game_version or "unknown"),
            "JOB_ID": self.claimed.id,
        }
        max_lines = int(self.a.policy_cfg.get("bugfix_max_changed_lines", 400))
        data = self.claude_fix(mod, self.a.prompt("bugfix.md"), values, "bugfix", max_changed_lines=max_lines)
        o = self.outcome(mod)
        if data is None or data.get("status") != "fixed":
            o.status = "no_fix"
            reason = (data or {}).get("summary_ko") or (data or {}).get("summary_en") or "Claude 세션 실패"
            o.notes.append(reason[:400])
            g = self.a.git(mod, self.log_file)
            if not g.is_clean():
                g.reset_hard(o.base_commit)  # a half-done edit is not wanted
            self.a.state.mark_seen(new_ids)
            reply = ("\n답글 초안: " + o.reply_en[:800]) if o.reply_en else ""
            return "버그 제보 검토 (%s): 자동 수정하지 않음\n%s%s" % (mod.display_name, reason[:800], reply)
        br = self.a.build(mod, self.log_file)
        if not br.ok:
            br = self.fix_until_builds(mod, br, self.a.prompt("bugfix.md"), values, 1)
        o.build_ok = br.ok
        if not br.ok:
            o.status = "failed"
            self.keep_for_review(mod)
            self.a.state.mark_seen(new_ids)
            return "버그 수정 실패 (%s): 빌드되지 않음" % mod.display_name
        for dep in group:
            if dep.id != mod.id:
                bd = self.a.build(dep, self.log_file)
                self.outcome(dep).build_ok = bd.ok
                if not bd.ok:
                    o.status = "failed"
                    o.notes.append("%s가 이 수정으로 빌드되지 않음" % dep.id)
                    self.keep_for_review(mod)
                    self.a.state.mark_seen(new_ids)
                    return "버그 수정 실패 (%s): 의존 모드가 깨짐" % mod.display_name
        self.smoke_and_fix(group, values)
        for dep in group:
            if dep.id != mod.id and self.outcome(dep).status != "failed":
                self.outcome(dep).status = "ok_unchanged"
        self.settle_and_release([mod], "bug fix", "", max_lines=max_lines)
        self.a.state.mark_seen(new_ids)
        reply = ("\n답글 초안: " + o.reply_en[:800]) if o.reply_en else ""
        return "버그 수정 (%s)\n%s%s" % (mod.display_name, self.mod_lines(), reply)

    def run_publish_approved(self) -> str:
        aid = str(self.payload.get("approval_id") or "")
        state = self.a.clients.approval_state(aid)
        if state != "approved":
            raise JobFailed("approval %s is %s, not approved" % (aid, state))
        entries = list(self.payload.get("mods") or [])
        mods = [self.a.registry.get(e["id"]) for e in entries]
        self.stage("preflight")
        st = self.ensure_steam_and_build(None)
        for e in entries:
            mod = self.a.registry.get(e["id"])
            o = self.outcome(mod)
            g = self.a.git(mod, self.log_file)
            g.fetch()
            remote = g.rev("origin/%s" % e["branch"])
            if remote != e["commit"]:
                raise JobFailed("%s: branch %s is at %s, but %s was approved; refusing to publish anything else" % (mod.id, e["branch"], remote[:10], str(e["commit"])[:10]))
            if not g.is_ancestor("origin/%s" % mod.default_branch, e["commit"]):
                raise JobFailed("%s: %s has moved on since the approval; the change needs a new run" % (mod.id, mod.default_branch))
            g.checkout(mod.default_branch)
            g.sync(mod.default_branch)
            g.new_branch(e["branch"], e["commit"])
            o.branch, o.commit = e["branch"], e["commit"]
            o.base_commit = g.rev("origin/%s" % mod.default_branch)
            o.release = {"new_version": e.get("version")} if e.get("version") else None
            o.metadata_game_version = e.get("metadata_game_version")
        for mod in self.a.registry.build_order([m.id for m in mods]):
            o = self.outcome(mod)
            if mod not in mods:
                # A dependency that is not part of the release: build it from its default branch.
                self.a.git(mod, self.log_file).sync(mod.default_branch)
            br = self.a.build(mod, self.log_file)
            o.build_ok = br.ok
            if not br.ok:
                raise JobFailed("%s does not build at the approved commit: %s" % (mod.id, br.error_text(3)))
        if str(st.buildid) != str(self.payload.get("game_build")):
            self.notes.append("the game changed since the test (build %s -> %s): testing again" % (self.payload.get("game_build"), st.buildid))
            self.smoke_and_fix_disabled(mods)
            if any(self.outcome(m).smoke_ok is False for m in mods):
                raise JobFailed("the approved change fails the smoke test on the current game build")
        new_versions = [m for m in mods if self.outcome(m).release]
        metadata = [m for m in mods if not self.outcome(m).release]
        self.publish_ready(new_versions, "NewVersion")
        self.publish_ready(metadata, "Update")
        return "승인된 배포 (%s)\n%s" % (aid, self.mod_lines())

    def smoke_and_fix_disabled(self, mods: List[ModSpec]) -> None:
        """Smoke test without letting Claude change anything (the code was already approved)."""
        sm = self.run_smoke(mods)
        for mod in mods:
            o = self.outcome(mod)
            o.smoke_ok = sm.mod_ok(mod.id) if sm.city_loaded else None

    def run_maintenance(self) -> str:
        """Keeps Windows on for a remote session (e.g. Jump Desktop) until !done or the time is up."""
        from .windows import power

        hours = max(0.0, float(self.payload.get("hours", 2)))
        end = time.time() + hours * 3600
        self.a.clients.notify("PC가 Windows로 켜졌습니다 (%s). 원격 접속하시면 됩니다.\n끝나면 디스코드에 !done — 안 하시면 %d분 뒤 자동으로 꺼집니다."
                              % (self.a.host_id, int(hours * 60)))
        power.prevent_sleep()
        try:
            while True:
                left = end - time.time()
                if left <= 0:
                    break
                self.stage("maintenance", "남은 시간 %d분" % int(left // 60))
                self.a._sleep(min(60.0, left))
        except Cancelled:
            return "원격 점검을 마쳤습니다 (!done). PC를 끕니다."
        finally:
            power.allow_sleep()
        return "원격 점검 시간이 끝나 PC를 끕니다."

    def run_rehearsal(self) -> str:
        checks: List[Tuple[str, bool, str]] = []
        problems = self.a.preflight()
        checks.append(("도구(dotnet, git, claude) 및 디스크", not problems, "; ".join(problems)))
        self.stage("claude ping")
        try:
            with_tmp = self.work / "ping"
            with_tmp.mkdir(exist_ok=True)
            res = self.a.claude().run(ClaudeTask(name="cs2mmt-ping", prompt='Reply with {"ok": true} and nothing else.', cwd=with_tmp,
                                                 schema=PING_SCHEMA, disallowed_tools=["Bash", "Edit", "Write", "WebFetch", "WebSearch", "PowerShell"],
                                                 max_turns=3, timeout_s=300, model=str(self.a.cfg.get("claude.ping_model", "haiku"))))
            checks.append(("Claude 인증", res.ok, res.error or ""))
        except Exception as e:
            checks.append(("Claude 인증", False, str(e)))
        st = None
        try:
            st = self.ensure_steam_and_build(None)
            checks.append(("Steam·게임 상태 (빌드 %s)" % st.buildid, True, ""))
        except Exception as e:
            checks.append(("Steam·게임 상태", False, str(e)))
        mods = self.a.registry.build_order()
        try:
            self.sync_mods(mods)
            checks.append(("GitHub 동기화", True, ""))
        except Exception as e:
            checks.append(("GitHub 동기화", False, str(e)))
        for mod in mods:
            g = self.a.git(mod, self.log_file)
            r = self.a._run(["git", "-C", str(self.a.mod_dir(mod)), "push", "--dry-run", "origin", mod.default_branch], timeout=120, log_file=self.log_file)
            checks.append(("GitHub 푸시 권한 (%s)" % mod.id, r.ok, r.tail(2) if not r.ok else ""))
        built = []
        for mod in mods:
            try:
                br = self.a.build(mod, self.log_file)
            except Exception as e:
                checks.append(("빌드 %s" % mod.id, False, str(e)))
                continue
            self.outcome(mod).build_ok = br.ok
            checks.append(("빌드 %s" % mod.id, br.ok, br.error_text(2) if not br.ok else ""))
            if br.ok:
                built.append(mod)
        if built and st is not None:
            sm = self.run_smoke(built)
            checks.append(("스모크 테스트: 도시 로드", sm.city_loaded, sm.note))
            for mod in built:
                checks.append(("스모크 테스트 %s" % mod.id, sm.mod_ok(mod.id), (sm.verdict(mod.id).summary() if sm.verdict(mod.id) else "")))
        try:
            exe = find_modpublisher(self.a.cfg.get("publish.modpublisher"), self.a.game_dir())
            checks.append(("ModPublisher 위치", exe is not None, str(exe or "없음")))
        except Exception as e:
            checks.append(("ModPublisher 위치", False, str(e)))
        for mod in mods:
            o = self.outcome(mod)
            o.status = "ok_unchanged" if o.build_ok else "failed"
        lines = ["점검 리허설 결과 (배포 없음)"]
        for name, ok, detail in checks:
            lines.append("%s %s%s" % ("✅" if ok else "❌", name, (" — " + detail[:200]) if detail and not ok else ""))
        self.notes.extend("%s: %s" % (n, d) for n, ok, d in checks if not ok)
        failed = [c for c in checks if not c[1]]
        if failed:
            # The rehearsal itself succeeded in finding problems; mark the job failed so it stands out.
            self.outcomes.setdefault("_rehearsal", ModOutcome("_rehearsal", status="failed"))
        return "\n".join(lines)

    def run_triage(self) -> str:
        items = list(self.payload.get("items") or [])
        fresh = self.a.state.unseen([i["id"] for i in items])
        items = [i for i in items if i["id"] in fresh]
        if not items:
            self.payload["classified"] = []
            return ""
        self.stage("triage", "%d item(s)" % len(items))
        classified = run_triage(self.a.claude(), self.a.prompt("triage.md"), items, self.a.registry, log_file=self.work / "triage.log")
        self.a.state.mark_seen([i["id"] for i in items if not any(c["id"] == i["id"] and c.get("kind") in ("bug", "crash", "compatibility") for c in classified)])
        self.extra_result = {"classified": classified}
        return ""


DEFAULT_ASSEMBLIES = [
    "Game",
    "Colossal.Core",
    "Colossal.Collections",
    "Colossal.IO.AssetDatabase",
    "Colossal.Localization",
    "Colossal.Logging",
    "Colossal.Mathematics",
    "Colossal.UI.Binding",
    "Colossal.UI",
]
