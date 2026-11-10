"""Running Claude Code non-interactively (``claude -p``).

Each job step is one headless session started in the mod's folder. The session gets:

* the instructions on standard input (no quoting problems on Windows, no length limit),
* a JSON Schema, so the final answer arrives in ``structured_output`` and can be checked,
* a locked-down permission set: ``dontAsk`` mode denies everything that is not explicitly
  allowed, and ``--permission-prompts none`` makes sure nothing waits for a person.

Authentication comes from the environment: ``CLAUDE_CODE_OAUTH_TOKEN`` (a one-year token from
``claude setup-token``, Pro/Max subscription) or ``ANTHROPIC_API_KEY`` (Console, pay per use).
"""

from __future__ import annotations

import json
import logging
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import proc

__all__ = ["ClaudeTask", "ClaudeResult", "ClaudeRunner", "render_template", "parse_result_json", "FIX_TOOLS", "FIX_DENY",
           "SECRET_READ_DENY", "mod_session_rules"]

log = logging.getLogger("cs2mmt.claude")

# What every fix session may do without asking. The pipeline adds, per session, edits inside the
# one mod's folder, building the registered projects, and read-only git commands on that mod.
FIX_TOOLS: List[str] = [
    "Read",
    "Glob",
    "Grep",
    "TodoWrite",
]


def mod_session_rules(mod_rel: str, projects: List[str]) -> List[str]:
    """Allow rules for a session working on the mod in ``mod_rel`` (workspace-relative path)."""
    rules = ["Edit(%s/**)" % mod_rel, "Write(%s/**)" % mod_rel]
    for p in projects:
        rules.append("Bash(dotnet build %s *)" % p)
        rules.append("Bash(dotnet build %s)" % p)
    for sub in ("diff", "status", "log", "show"):
        rules.append("Bash(git -C %s %s *)" % (mod_rel, sub))
        rules.append("Bash(git -C %s %s)" % (mod_rel, sub))
    return rules


# Never readable by a session: credentials anywhere they might be.
SECRET_READ_DENY: List[str] = [
    "Read(**/secrets.toml)",
    "Read(**/*secret*)",
    "Read(**/.credentials.json)",
    "Read(**/.git-credentials)",
    "Read(**/*.pem)",
    "Read(**/*.key)",
    "Read(**/id_rsa*)",
    "Read(**/id_ed25519*)",
    "Read(~/.ssh/**)",
    "Read(~/.claude/**)",
    "Read(~/.config/**)",
    "Read(~/AppData/**)",
]

# Denied outright, whatever the mode: network, publishing, history rewriting, build files.
FIX_DENY: List[str] = SECRET_READ_DENY + [
    "WebFetch",
    "WebSearch",
    "PowerShell",
    "Bash(git -C * push *)",
    "Bash(git -C * commit *)",
    "Bash(git -C * reset *)",
    "Bash(git -C * checkout *)",
    "Bash(git -C * config *)",
    "Bash(git -C * remote *)",
    "Bash(env)",
    "Bash(env *)",
    "Bash(printenv *)",
    "Bash(printenv)",
    "Bash(set)",
    "Bash(echo *)",
    "Bash(curl *)",
    "Bash(wget *)",
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git reset *)",
    "Bash(git checkout *)",
    "Bash(git rebase *)",
    "Bash(git remote *)",
    "Bash(git config *)",
    "Bash(rm *)",
    "Bash(del *)",
    "Bash(powershell *)",
    "Bash(pwsh *)",
    "Bash(cmd *)",
    "Bash(dotnet publish *)",
    "Bash(dotnet run *)",
    "Bash(dotnet tool *)",
    "Bash(dotnet nuget *)",
    "Bash(dotnet add *)",
    "Edit(**/*.csproj)",
    "Write(**/*.csproj)",
    "Edit(**/*.*proj)",
    "Write(**/*.*proj)",
    "Edit(**/*.proj)",
    "Write(**/*.proj)",
    "Edit(**/*.props)",
    "Write(**/*.props)",
    "Edit(**/*.targets)",
    "Write(**/*.targets)",
    "Edit(**/*.rsp)",
    "Write(**/*.rsp)",
    "Edit(**/*.sln)",
    "Write(**/*.sln)",
    "Edit(**/Directory.Build.*)",
    "Write(**/Directory.Build.*)",
    "Edit(**/nuget.config)",
    "Write(**/nuget.config)",
    "Edit(**/global.json)",
    "Write(**/global.json)",
    "Edit(**/*.ps1)",
    "Write(**/*.ps1)",
    "Edit(**/*.cmd)",
    "Write(**/*.cmd)",
    "Edit(**/*.bat)",
    "Write(**/*.bat)",
    "Edit(**/*.sh)",
    "Write(**/*.sh)",
    "Edit(**/PublishConfiguration.xml)",
    "Write(**/PublishConfiguration.xml)",
    "Edit(**/.claude/**)",
    "Write(**/.claude/**)",
    "Edit(**/.git/**)",
    "Write(**/.git/**)",
    "Edit(**/.github/**)",
    "Write(**/.github/**)",
]


@dataclass
class ClaudeTask:
    name: str
    prompt: str
    cwd: Path
    schema: Optional[Dict[str, Any]] = None
    allowed_tools: List[str] = field(default_factory=list)
    disallowed_tools: List[str] = field(default_factory=list)
    tools: Optional[str] = None  # value for --tools; "" disables every tool (triage)
    add_dirs: List[Path] = field(default_factory=list)
    max_turns: int = 120
    timeout_s: int = 3600
    model: Optional[str] = None
    fallback_model: Optional[str] = None
    effort: Optional[str] = None
    system_prompt_file: Optional[Path] = None
    resume_session: Optional[str] = None
    max_budget_usd: Optional[float] = None
    permission_mode: str = "dontAsk"
    log_file: Optional[Path] = None


@dataclass
class ClaudeResult:
    ok: bool
    exit_code: int
    structured: Optional[Dict[str, Any]]
    text: str
    session_id: Optional[str]
    cost_usd: Optional[float]
    num_turns: Optional[int]
    subtype: Optional[str]
    error: Optional[str]
    stdout: str
    stderr: str
    duration_s: float
    permission_denials: List[Any] = field(default_factory=list)


def render_template(template: str, values: Mapping[str, Any]) -> str:
    """Fills ``$name`` / ``${name}`` placeholders; anything else (C# braces, ``$"..."``) is left alone."""
    return string.Template(template).safe_substitute({k: ("" if v is None else str(v)) for k, v in values.items()})


def parse_result_json(stdout: str) -> Optional[Dict[str, Any]]:
    """The result object from ``--output-format json`` (the last JSON object on stdout)."""
    text = stdout.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            for item in reversed(obj):
                if isinstance(item, dict) and item.get("type") == "result":
                    return item
    except json.JSONDecodeError:
        pass
    # Something printed before the JSON: try each line from the end, then each '{' at a line start.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    for m in reversed(list(re.finditer(r"(?m)^\{", text))):
        try:
            obj = json.loads(text[m.start():])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


class ClaudeRunner:
    def __init__(
        self,
        executable: str = "claude",
        env: Optional[Mapping[str, str]] = None,
        work_dir: Optional[Path] = None,
        run: Callable[..., proc.ProcResult] = proc.run,
        default_model: Optional[str] = None,
        default_fallback_model: Optional[str] = None,
        default_effort: Optional[str] = None,
    ):
        self.executable = executable
        self.env = dict(env or {})
        self.work_dir = work_dir
        self._run = run
        self.default_model = default_model
        self.default_fallback_model = default_fallback_model
        self.default_effort = default_effort

    def settings_for(self, task: ClaudeTask) -> Dict[str, Any]:
        return {
            "permissions": {
                "allow": list(task.allowed_tools),
                "deny": list(task.disallowed_tools),
                "defaultMode": task.permission_mode,
            },
            # Nothing in a mod repository may add hooks or MCP servers to an unattended run.
            "enableAllProjectMcpServers": False,
            "disableAllHooks": True,
        }

    def build_argv(self, task: ClaudeTask, settings_path: Optional[Path]) -> List[str]:
        argv: List[str] = [self.executable, "-p", "Follow the instructions given on standard input."]
        argv += ["--output-format", "json"]
        argv += ["--permission-mode", task.permission_mode, "--permission-prompts", "none"]
        # No MCP servers at all: --strict-mcp-config without --mcp-config ignores every .mcp.json.
        argv += ["--strict-mcp-config"]
        if task.schema is not None:
            argv += ["--json-schema", json.dumps(task.schema, separators=(",", ":"))]
        if settings_path is not None:
            argv += ["--settings", str(settings_path)]
        if task.tools is not None:
            argv += ["--tools", task.tools]
        if task.allowed_tools:
            argv += ["--allowedTools", ",".join(task.allowed_tools)]
        if task.disallowed_tools:
            argv += ["--disallowedTools", ",".join(task.disallowed_tools)]
        for d in task.add_dirs:
            argv += ["--add-dir", str(d)]
        if task.max_turns:
            argv += ["--max-turns", str(task.max_turns)]
        model = task.model or self.default_model
        if model:
            argv += ["--model", model]
        fallback = task.fallback_model or self.default_fallback_model
        if fallback and fallback != model:
            argv += ["--fallback-model", fallback]
        effort = task.effort or self.default_effort
        if effort:
            argv += ["--effort", effort]
        if task.system_prompt_file is not None:
            argv += ["--append-system-prompt-file", str(task.system_prompt_file)]
        if task.max_budget_usd:
            argv += ["--max-budget-usd", "%.2f" % task.max_budget_usd]
        if task.resume_session:
            argv += ["--resume", task.resume_session]
        argv += ["--name", task.name]
        return argv

    def run(self, task: ClaudeTask) -> ClaudeResult:
        settings_path: Optional[Path] = None
        base = self.work_dir or task.cwd
        if task.allowed_tools or task.disallowed_tools:
            settings_path = Path(base) / (".claude-settings-%s.json" % re.sub(r"[^A-Za-z0-9_.-]", "_", task.name))
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(self.settings_for(task), indent=2), encoding="utf-8")
        argv = self.build_argv(task, settings_path)
        log.info("claude session %s in %s", task.name, task.cwd)
        if task.log_file:
            task.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(task.log_file, "a", encoding="utf-8") as f:
                f.write("\n===== prompt for %s =====\n%s\n" % (task.name, task.prompt))
        r = self._run(argv, cwd=task.cwd, timeout=task.timeout_s, input_text=task.prompt, extra_env=self.env, log_file=task.log_file)
        data = parse_result_json(r.stdout) or {}
        structured = data.get("structured_output")
        if isinstance(structured, str):
            try:
                structured = json.loads(structured)
            except json.JSONDecodeError:
                structured = None
        subtype = data.get("subtype")
        is_error = bool(data.get("is_error")) or r.code != 0 or r.timed_out
        error: Optional[str] = None
        if r.timed_out:
            error = "timed out after %ss" % task.timeout_s
        elif not data:
            error = "no JSON result (exit %s): %s" % (r.code, (r.stderr or r.stdout)[-800:])
        elif is_error:
            error = "%s: %s" % (subtype or "error", str(data.get("result") or r.stderr)[-800:])
        elif task.schema is not None and not isinstance(structured, dict):
            error = "the session ended without the structured result"
        return ClaudeResult(
            ok=error is None,
            exit_code=r.code,
            structured=structured if isinstance(structured, dict) else None,
            text=str(data.get("result") or ""),
            session_id=data.get("session_id"),
            cost_usd=data.get("total_cost_usd"),
            num_turns=data.get("num_turns"),
            subtype=subtype,
            error=error,
            stdout=r.stdout,
            stderr=r.stderr,
            duration_s=r.duration_s,
            permission_denials=list(data.get("permission_denials") or []),
        )
