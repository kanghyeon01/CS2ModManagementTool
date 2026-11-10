"""The harness compiles (against API stubs shaped like the game's, see tests/harness_stubs)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from cs2mmt.agent.windows.build import dotnet_build

ROOT = Path(__file__).resolve().parents[1]
STUBS = ROOT / "tests" / "harness_stubs"

DOTNET = shutil.which("dotnet")
pytestmark = pytest.mark.skipif(DOTNET is None, reason="needs the .NET SDK")


def _ref_pack() -> Path:
    base = Path(DOTNET).resolve().parent
    for candidate in [base / "packs", Path("/usr/lib/dotnet/packs"), Path("/usr/share/dotnet/packs")]:
        hits = sorted(candidate.glob("NETStandard.Library.Ref/2.1*/ref/netstandard2.1"))
        if hits:
            return hits[-1]
    pytest.skip("NETStandard.Library.Ref 2.1 targeting pack not found")


def _csc() -> list:
    base = Path(DOTNET).resolve().parent
    hits = sorted(base.glob("sdk/*/Roslyn/bincore/csc.dll")) or sorted(Path("/usr/lib/dotnet").glob("sdk/*/Roslyn/bincore/csc.dll"))
    if not hits:
        pytest.skip("csc.dll not found")
    return [DOTNET, str(hits[-1]), "-nologo", "-target:library", "-nostdlib", "-langversion:9.0"]


def test_harness_compiles_against_stubs(tmp_path):
    managed = tmp_path / "Managed"
    managed.mkdir()
    ref = _ref_pack()
    for name in ("netstandard.dll", "mscorlib.dll", "System.dll", "System.Core.dll"):
        shutil.copy(ref / name, managed / name)
    csc = _csc() + ["-r:%s" % (managed / "netstandard.dll"), "-r:%s" % (managed / "mscorlib.dll")]

    def build(name, *refs):
        src = STUBS / (name + ".cs")
        args = csc + ["-out:%s" % (managed / (name + ".dll"))] + ["-r:%s" % (managed / (r + ".dll")) for r in refs] + [str(src)]
        r = subprocess.run(args, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr

    for name in ("UnityEngine.CoreModule", "Unity.Entities", "Colossal.Core", "Colossal.Logging", "Colossal.IO.AssetDatabase"):
        build(name)
    build("Game", "Unity.Entities", "Colossal.Core")
    empty = tmp_path / "empty.cs"
    empty.write_text("namespace Stub {}\n")
    for name in ("Colossal.Collections", "Colossal.Localization", "Colossal.Mathematics", "Colossal.UI.Binding", "Unity.Burst",
                 "Unity.Collections", "Unity.Mathematics"):
        r = subprocess.run(csc + ["-out:%s" % (managed / (name + ".dll")), str(empty)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
    project = tmp_path / "harness"
    shutil.copytree(ROOT / "harness" / "CS2MMTHarness", project, ignore=shutil.ignore_patterns("bin", "obj"))
    mods = tmp_path / "Mods"
    result = dotnet_build(project / "CS2MMTHarness.csproj", "Release", {"CS2ManagedPath": str(managed), "CS2LocalModsPath": str(mods)}, timeout_s=600)
    assert result.ok, result.error_text()
    assert not [d for d in result.diagnostics if d.kind == "warning"], [d.short() for d in result.diagnostics]
    assert (mods / "CS2MMTHarness" / "CS2MMTHarness.dll").is_file()
