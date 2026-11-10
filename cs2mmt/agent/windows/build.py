"""Building a mod with ``dotnet build`` against the installed game's assemblies.

The mods' project files compile straight against ``Cities2_Data/Managed`` and copy the result
into the local Mods folder (their ``DeployToLocalMods`` target), so a successful build also
installs the mod for the smoke test.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...common import proc
from ...common.buildlog import Diagnostic, format_diagnostics, parse_msbuild

__all__ = ["BuildResult", "dotnet_build", "add_game_references"]

log = logging.getLogger("cs2mmt.build")


@dataclass
class BuildResult:
    ok: bool
    diagnostics: List[Diagnostic] = field(default_factory=list)
    output_tail: str = ""
    duration_s: float = 0.0
    timed_out: bool = False

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.kind == "error"]

    def error_text(self, limit: int = 80) -> str:
        text = format_diagnostics(self.diagnostics, limit=limit, kind="error")
        return text or self.output_tail


def dotnet_build(
    project: Path,
    configuration: str = "Release",
    properties: Optional[Dict[str, str]] = None,
    dotnet: str = "dotnet",
    log_file: Optional[Path] = None,
    timeout_s: float = 900,
    run=proc.run,
) -> BuildResult:
    argv = [dotnet, "build", str(project), "-c", configuration, "-nologo", "-v:minimal", "-p:TreatWarningsAsErrors=false"]
    for k, v in (properties or {}).items():
        argv.append("-p:%s=%s" % (k, v))
    r = run(argv, cwd=project.parent, timeout=timeout_s, log_file=log_file,
            extra_env={"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1", "MSBUILDDISABLENODEREUSE": "1"})
    diags = parse_msbuild(r.stdout + "\n" + r.stderr)
    ok = r.ok and not any(d.kind == "error" for d in diags)
    return BuildResult(ok=ok, diagnostics=diags, output_tail=r.tail(30), duration_s=r.duration_s, timed_out=r.timed_out)


def add_game_references(csproj_text: str, names: List[str], managed_dir: Path) -> Tuple[str, List[str]]:
    """Adds game assemblies to the project's ``GameReference`` list (done by the tool, never by Claude).

    Only names that exist as ``<name>.dll`` in the game's Managed folder and look like assembly
    names are accepted. Returns the new text and the names actually added.
    """
    added: List[str] = []
    for name in names:
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{1,80}", name):
            continue
        if not (managed_dir / (name + ".dll")).is_file():
            continue
        if re.search(r"[\";]%s[\";]" % re.escape(name), csproj_text):
            continue
        m = re.search(r'(<GameReference\s+Include=")([^"]*)("\s*/>)', csproj_text)
        if not m:
            break
        csproj_text = csproj_text[: m.start(2)] + m.group(2) + ";" + name + csproj_text[m.end(2):]
        added.append(name)
    return csproj_text, added
