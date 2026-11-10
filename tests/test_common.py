"""Unit tests for the shared helpers: VDF, versions, PublishConfiguration, registry, TOML, logs."""

from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10: compare against nothing, the fallback is all there is
    tomllib = None

needs_tomllib = pytest.mark.skipif(tomllib is None, reason="tomllib needs Python 3.11+")

from cs2mmt.common import minitoml, publishconfig as pc, vdf
from cs2mmt.common.buildlog import parse_msbuild
from cs2mmt.common.config import load_config
from cs2mmt.common.logscan import LogFile, scan_logs
from cs2mmt.common.registry import RegistryError, load_registry
from cs2mmt.common.versioning import (
    GameVersion,
    VersionError,
    bump_mod_version,
    game_version_wildcard,
    is_numeric_version,
    parse_game_version,
    wildcard_matches,
)

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"


# ---------------------------------------------------------------- VDF
def test_acf_manifest():
    data = vdf.loads((FIX / "appmanifest_949230.acf").read_text())
    st = data["AppState"]
    assert st["buildid"] == "20512345"
    assert vdf.get_ci(st, "stateflags") == "4"
    assert st["LauncherPath"] == "C:\\Program Files (x86)\\Steam\\steam.exe"
    assert st["InstalledDepots"]["949231"]["manifest"] == "1234567890123456789"


def test_libraryfolders():
    data = vdf.loads((FIX / "libraryfolders.vdf").read_text())
    folders = data["libraryfolders"]
    assert folders["1"]["path"] == "D:\\SteamLibrary"
    assert "949230" in folders["1"]["apps"]


def test_app_info_print_block_among_log_lines():
    block = vdf.find_block((FIX / "steamcmd_app_info_print.txt").read_text(), "949230")
    assert block["depots"]["branches"]["public"]["buildid"] == "20512345"
    assert block["common"]["name"] == "Cities: Skylines II"


def test_vdf_comments_conditionals_and_errors():
    text = '"a"\n{\n // comment\n "b" "1" [$WIN32]\n "c" { "d" "x\\"y" }\n}\n'
    d = vdf.loads(text)
    assert d["a"]["b"] == "1" and d["a"]["c"]["d"] == 'x"y'
    for bad in ('"a" {', '"a" { "b" }', '}', '"unterminated'):
        with pytest.raises(vdf.VdfError):
            vdf.loads(bad)


# ---------------------------------------------------------------- versions
def test_bump_versions():
    assert bump_mod_version("1.3.0") == "1.3.1"
    assert bump_mod_version("1.1") == "1.1.1"
    assert bump_mod_version("1.2.9") == "1.2.10"
    assert bump_mod_version("1.2.3", "minor") == "1.3.0"
    assert bump_mod_version("1.2.3", "major") == "2.0.0"
    assert bump_mod_version("1.2.3.4") == "1.2.4.0"
    with pytest.raises(VersionError):
        bump_mod_version("1.1a")
    assert is_numeric_version("1.1.1") and not is_numeric_version("1.1a")


def test_game_versions():
    v = parse_game_version("Patch 1.6.3f1 is live")
    assert v == GameVersion(1, 6, 3, "f1") and str(v) == "1.6.3f1"
    assert game_version_wildcard(v) == "1.6.*"
    assert wildcard_matches("1.6.*", v)
    assert not wildcard_matches("1.5.*", v)
    assert wildcard_matches("1.*", v)
    assert wildcard_matches("1.6.3", v) and not wildcard_matches("1.6.30", v)
    assert not wildcard_matches("1.6.2", v)
    assert wildcard_matches("1.6.3f1", v) and not wildcard_matches("1.6.3f2", v)
    assert parse_game_version("") is None and parse_game_version("no digits") is None


# ---------------------------------------------------------------- PublishConfiguration
def test_read_real_publish_configuration():
    text = (FIX / "RealisticAviation.PublishConfiguration.xml").read_text(encoding="utf-8")
    info = pc.read_info(text)
    assert info.mod_id == "164022"
    assert info.mod_version == "1.3.0"
    assert info.game_version == "1.6.*"
    assert info.change_log.startswith("1.3.0\n- Added the IFR chart infoview.")
    assert "Aircraft" in info.tags
    assert pc.validate(info, "NewVersion") == []
    addon = pc.read_info((FIX / "AviationWeatherAddon.PublishConfiguration.xml").read_text(encoding="utf-8"))
    assert addon.dependencies == ["164022"]


def test_edits_touch_only_their_element():
    text = (FIX / "RealisticAviation.PublishConfiguration.xml").read_text(encoding="utf-8")
    new = pc.set_value(text, "ModVersion", "1.3.1")
    new = pc.set_text(new, "ChangeLog", "1.3.1\n- Fixed <this> & that.\n\n1.3.1\n- 고쳤습니다.")
    new = pc.set_value(new, "GameVersion", "1.7.*")
    info = pc.read_info(new)
    assert info.mod_version == "1.3.1" and info.game_version == "1.7.*"
    assert info.change_log == "1.3.1\n- Fixed <this> & that.\n\n1.3.1\n- 고쳤습니다."
    assert "&lt;this&gt; &amp; that" in new
    # Everything outside the three elements is byte-for-byte the same.
    import re
    strip = lambda s: re.sub(r"<ChangeLog>.*?</ChangeLog>|<ModVersion Value=\"[^\"]*\" />|<GameVersion Value=\"[^\"]*\" />", "", s, flags=re.S)
    assert strip(new) == strip(text)
    assert "<!-- Filled in after the first Publish" in new and info.long_description == pc.read_info(text).long_description


def test_set_text_creates_or_converts_elements():
    base = '<Publish>\n  <ModVersion Value="1.0" />\n  <ChangeLog />\n</Publish>\n'
    out = pc.set_text(base, "ChangeLog", "x")
    assert "<ChangeLog>x</ChangeLog>" in out
    base2 = '<Publish>\n  <ModVersion Value="1.0" />\n</Publish>\n'
    out2 = pc.set_text(base2, "ChangeLog", "y", insert_after="ModVersion")
    assert '  <ModVersion Value="1.0" />\n  <ChangeLog>y</ChangeLog>\n' in out2
    out3 = pc.set_value(base2, "GameVersion", "1.6.*", insert_after="ModVersion")
    assert pc.read_info(out3).game_version == "1.6.*"
    with pytest.raises(pc.PublishConfigError):
        pc.set_value(base2, "Missing", "x")


def test_validate_catches_publisher_limits(tmp_path):
    info = pc.PublishInfo(display_name="X", short_description="s" * 201, mod_version="1.1a", mod_id="", change_log="", thumbnail="t.png")
    problems = pc.validate(info, "NewVersion", tmp_path)
    joined = " | ".join(problems)
    assert "ShortDescription" in joined and "not numeric" in joined and "ModId is empty" in joined
    assert "ChangeLog is empty" in joined and "Thumbnail" in joined


# ---------------------------------------------------------------- registry
def test_real_registry():
    reg = load_registry(ROOT / "config" / "mods.toml")
    assert [m.id for m in reg.build_order()] == ["RealisticAviation", "AviationWeatherAddon", "NoJaywalking"]
    assert [m.id for m in reg.build_order(["AviationWeatherAddon"])] == ["RealisticAviation", "AviationWeatherAddon"]
    ra = reg.get("RealisticAviation")
    assert ra.github_slug == "kanghyeon01/RealisticAviation"
    assert ra.forum_threads and ra.owner_names == ["Boing787", "kanghyeon01"]
    assert reg.dependents_of("RealisticAviation") == ["AviationWeatherAddon"]
    assert reg.release_group("NoJaywalking") == ["NoJaywalking"]
    # The add-on's namespace starts with "RealisticAviation": the identifiers must keep them apart.
    assert not any(i in "RealisticAviationATIS.WeatherStationSystem" for i in ra.all_identifiers() if i.endswith("."))


def test_registry_errors(tmp_path):
    def write(body):
        p = tmp_path / "m.toml"
        p.write_text(body)
        return p

    with pytest.raises(RegistryError):
        load_registry(write('[[mod]]\nid="a"\nrepo="r"\nproject="p"\nassembly="a"\nbogus=1\n'))
    with pytest.raises(RegistryError):
        load_registry(write('[[mod]]\nid="a"\nrepo="r"\nproject="p"\nassembly="a"\ndepends_on=["b"]\n'))
    with pytest.raises(RegistryError):
        load_registry(write('[[mod]]\nid="a"\nrepo="r"\nproject="p"\nassembly="a"\ndepends_on=["b"]\n[[mod]]\nid="b"\nrepo="r"\nproject="p"\nassembly="b"\ndepends_on=["a"]\n'))
    with pytest.raises(RegistryError):
        load_registry(write('[[mod]]\nid="a"\nrepo="r"\nproject="p"\nassembly="a"\nversion_files=[{path="x", pattern="no group"}]\n'))
    with pytest.raises(RegistryError):
        load_registry(write('[[mod]]\nid="a b"\nrepo="r"\nproject="p"\nassembly="a"\n'))


# ---------------------------------------------------------------- TOML fallback (Python 3.9/3.10)
@needs_tomllib
@pytest.mark.parametrize("name", ["mods.toml", "controller.example.toml", "secrets.example.toml", "agent.example.toml",
                                  "agent-secrets.example.toml", "agent-linux.example.toml"])
def test_minitoml_matches_tomllib_on_our_configs(name):
    raw = (ROOT / "config" / name).read_bytes()
    assert minitoml.loads(raw.decode("utf-8")) == tomllib.loads(raw.decode("utf-8"))


@needs_tomllib
def test_minitoml_edge_cases():
    text = r'''
# comment
a = "x\ty\u00e9"  # trailing comment
b = 'C:\no\escape'
c = """
line1
line2 \
   joined"""
d = \'\'\'raw
text\'\'\'
e = [1, 2,
  3, # inside
]
f = { x = 1, "y z" = true, n.m = -2.5 }
g.h.i = 0x1F
"quoted key" = false
[t1]
k = 1_000
[[arr]]
v = 1
[[arr]]
v = 2
[arr.sub]
w = "s"
'''.replace("\\'", "'")
    assert minitoml.loads(text) == tomllib.loads(text)
    for bad in ('a = ', 'a = "x', '[t]\n[t]', 'a = 1\na = 2', 'a = 2026-01-01', 'a = 01'):
        with pytest.raises(minitoml.TOMLDecodeError):
            minitoml.loads(bad)


def test_config_secrets_and_env(tmp_path, monkeypatch):
    (tmp_path / "c.toml").write_text('[a]\nb = 1\npath = "sub/x"\n')
    (tmp_path / "secrets.toml").write_text('[secrets]\napi_token = "0123456789abcdef"\n')
    monkeypatch.setenv("CS2MMT__A__B", "5")
    cfg = load_config(tmp_path / "c.toml")
    assert cfg.get("a.b") == 5
    assert cfg.require("secrets.api_token") == "0123456789abcdef"
    assert cfg.get_path("a.path") == tmp_path / "sub" / "x"
    assert "0123456789abcdef" in cfg.secrets()


# ---------------------------------------------------------------- MSBuild and game logs
def test_parse_msbuild():
    out = """
  Determining projects to restore...
C:\\m\\RA\\AtcSystem.cs(123,45): error CS0117: 'AircraftNavigation' does not contain a definition for 'm_MaxSpeed' [C:\\m\\RA\\RealisticAviation.csproj]
C:\\m\\RA\\Mod.cs(10,1): warning CS0618: 'X' is obsolete [C:\\m\\RA\\RealisticAviation.csproj]
C:\\m\\RA\\RealisticAviation.csproj(57,5): error : Cities: Skylines II not found at 'X'. [C:\\m\\RA\\RealisticAviation.csproj]
MSBUILD : error MSB1009: Project file does not exist.
Build FAILED.
C:\\m\\RA\\AtcSystem.cs(123,45): error CS0117: 'AircraftNavigation' does not contain a definition for 'm_MaxSpeed' [C:\\m\\RA\\RealisticAviation.csproj]
"""
    diags = parse_msbuild(out)
    errors = [d for d in diags if d.kind == "error"]
    assert len(errors) == 3
    assert errors[0].code == "CS0117" and errors[0].line == 123 and errors[0].file.endswith("AtcSystem.cs")
    assert errors[1].code == "" and "not found" in errors[1].message
    assert errors[2].code == "MSB1009"
    assert [d.code for d in diags if d.kind == "warning"] == ["CS0618"]


def test_scan_logs_attribution():
    mods = [
        {"id": "RA", "identifiers": ["RealisticAviation."], "log_names": ["RealisticAviation"], "smoke_expect": [r"Game \S+, Realistic Aviation"]},
        {"id": "AWA", "identifiers": ["RealisticAviationATIS"], "log_names": ["RealisticAviationATIS"], "smoke_expect": ["Aviation Weather"]},
        {"id": "NJ", "identifiers": ["NoJaywalking."], "log_names": ["NoJaywalking"], "smoke_expect": ["OnLoad"]},
    ]
    player = LogFile("Player.log", "\n".join([
        "Loading city...",
        "NullReferenceException: Object reference not set to an instance of an object",
        "  at RealisticAviationATIS.WeatherStationSystem.OnUpdate () [0x00012] in <abc>:0",
        "  at Game.UpdateSystem.Update () [0x00000] in <def>:0",
        "Some game warning without a mod",
        "[ERROR] Game.Prefabs: missing prefab",
    ]))
    ra = LogFile("RealisticAviation.log", "[INFO] OnLoad\n[INFO] Game 1.6.2f1, Realistic Aviation 1.0.0.0\n")
    awa = LogFile("RealisticAviationATIS.log", "[INFO] OnLoad\n[INFO] Game 1.6.2f1, Aviation Weather Add-on 1.0, Realistic Aviation 1.0\n")
    v = {x.mod_id: x for x in scan_logs([player, ra, awa], mods, harness_errors=[
        {"type": "Exception", "message": "InvalidOperationException: boom", "stack": "NoJaywalking.CrossingRestrictionSystem.OnUpdate ()"}])}
    assert v["RA"].ok, v["RA"].errors
    assert not v["AWA"].ok and "WeatherStationSystem" in v["AWA"].errors[0]
    assert not v["NJ"].loaded and v["NJ"].errors and "harness" in v["NJ"].errors[0]
    ignored = {x.mod_id: x for x in scan_logs([player, awa], mods[1:2], ignore=["WeatherStationSystem"])}
    assert ignored["AWA"].ok
