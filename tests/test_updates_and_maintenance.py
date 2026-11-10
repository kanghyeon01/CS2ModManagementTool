"""Self-update with rollback, remote maintenance commands, boot-method detection."""

import subprocess
import sys
from pathlib import Path

import pytest

from cs2mmt import selfupdate
from cs2mmt.agent.linux_switch import detect_method, grub_default_is_saved, grub_windows_entry, set_next_boot
from cs2mmt.common import proc

from test_controller import Clock, client_for, make_controller

GIT = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com"]


def git(*args, cwd=None):
    return subprocess.run(GIT + list(args), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", str(seed))
    (seed / "app.py").write_text("VALUE = 1\n")
    (seed / ".gitignore").write_text("local.cfg\n")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "v1", cwd=seed)
    origin = tmp_path / "origin.git"
    git("clone", "-q", "--bare", str(seed), str(origin))
    work = tmp_path / "work"
    git("clone", "-q", str(origin), str(work))
    (work / "local.cfg").write_text("secret = 1\n")  # ignored files never block or get touched

    def push(content):
        git("-C", str(seed), "pull", "-q", str(origin), "main")
        (seed / "app.py").write_text(content)
        git("commit", "-qam", "change", cwd=seed)
        git("push", "-q", str(origin), "main", cwd=seed)
        return git("rev-parse", "HEAD", cwd=seed)

    return {"work": work, "push": push, "origin": origin, "seed": seed}


CHECK = [sys.executable, "-c", "import app; assert app.VALUE != 0"]


def test_update_fast_forwards_and_checks(repos):
    old = git("rev-parse", "HEAD", cwd=repos["work"])
    assert not selfupdate.self_update(repos["work"], check=CHECK).changed  # nothing new
    new = repos["push"]("VALUE = 2\n")
    r = selfupdate.self_update(repos["work"], check=CHECK)
    assert r.changed and r.ok and r.old == old and r.new == new
    assert git("rev-parse", "HEAD", cwd=repos["work"]) == new
    assert (repos["work"] / "local.cfg").read_text() == "secret = 1\n"


def test_broken_update_is_rolled_back(repos):
    old = git("rev-parse", "HEAD", cwd=repos["work"])
    repos["push"]("VALUE = 0\n")  # the check fails on this version
    r = selfupdate.self_update(repos["work"], check=CHECK)
    assert r.rolled_back and not r.ok and not r.changed
    assert git("rev-parse", "HEAD", cwd=repos["work"]) == old
    assert (repos["work"] / "app.py").read_text() == "VALUE = 1\n"
    assert "되돌림" in r.short()


def test_update_refuses_local_changes_and_diverged_history(repos, tmp_path):
    (repos["work"] / "app.py").write_text("VALUE = 99\n")
    repos["push"]("VALUE = 2\n")
    r = selfupdate.self_update(repos["work"], check=CHECK)
    assert not r.ok and "로컬 변경" in r.message
    assert (repos["work"] / "app.py").read_text() == "VALUE = 99\n"
    git("checkout", "-q", "--", "app.py", cwd=repos["work"])
    git("commit", "-q", "--allow-empty", "-m", "local only", cwd=repos["work"])
    r = selfupdate.self_update(repos["work"], check=CHECK)
    assert not r.ok and "fast-forward" in r.message
    assert not selfupdate.self_update(tmp_path, check=CHECK).ok  # not a checkout


# ---------------------------------------------------------------- maintenance commands
@pytest.fixture
def ctl(tmp_path):
    c = make_controller(tmp_path, "nas", "primary")
    yield c
    c.stop()


def test_maintenance_wakes_even_when_paused_and_done_ends_it(ctl):
    ctl.command("!pause")
    ctl._enqueue_game_update({"old_build": "1", "new_build": "2"})
    reply = ctl.command("!maintenance 3")
    assert "3.0시간" in reply
    assert "이미 예약" in ctl.command("!maintenance")
    job = next(j for j in ctl.store.jobs(["queued"]) if j.kind == "maintenance")
    assert job.priority == 1 and job.payload["hours"] == 3.0
    ctl.dispatcher.tick()
    assert ctl.wakes and ctl.store.get_job(job.id).state == "dispatched"  # paused, but the owner asked for this one
    c = client_for(ctl)
    claimed = c.claim("main-pc")
    assert claimed.kind == "maintenance"
    assert c.heartbeat(claimed, "maintenance") is False
    assert "끝냅니다" in ctl.command("!done")
    assert c.heartbeat(claimed, "maintenance") is True
    c.complete(claimed, "succeeded", {}, "원격 점검을 마쳤습니다")
    assert "진행 중인 원격 점검이 없습니다" in ctl.command("!done")
    assert ctl.command("!maintenance x").startswith("사용법")


def test_done_before_the_pc_is_up_and_clear(ctl):
    ctl.command("!maintenance")
    assert "취소" in ctl.command("!done")
    ctl._enqueue_game_update({"old_build": "1", "new_build": "2"})
    ctl.store.enqueue("rehearsal", {"work_key": "r"})
    assert "2건" in ctl.command("!clear")
    assert ctl.store.jobs(["queued"]) == []


def test_update_tool_command(ctl, repos, monkeypatch):
    monkeypatch.setattr(selfupdate, "default_check", lambda: CHECK)
    ctl.cfg.data["update"] = {"repo_dir": str(repos["work"]), "restart": False, "sentinel_url": ""}
    assert "이미 최신" in ctl.command("!update_tool")
    repos["push"]("VALUE = 5\n")
    reply = ctl.command("!update_tool")
    assert "→" in reply and "다음에 켜질 때" in reply
    repos["push"]("VALUE = 0\n")
    assert "되돌림" in ctl.command("!update_tool")


# ---------------------------------------------------------------- boot method detection
EFI_WITH_WINDOWS = "BootCurrent: 0001\nBoot0001* ubuntu\tHD(1)/File(x)\nBoot0003* Windows Boot Manager\tHD(2)/File(y)\n"
EFI_WITHOUT = "BootCurrent: 0001\nBoot0001* ubuntu\tHD(1)/File(x)\n"
GRUB_CFG = """
menuentry 'Ubuntu' --class ubuntu {
    linux /vmlinuz
}
submenu 'Advanced options for Ubuntu' {
    menuentry 'Windows inside a submenu' { }
}
menuentry 'Windows Boot Manager (on /dev/nvme0n1p1)' --class windows --class os $menuentry_id_option 'osprober-efi-ABCD' {
    chainloader /EFI/Microsoft/Boot/bootmgfw.efi
}
"""


def fake(output, ok=True):
    calls = []

    def run(argv, timeout=None):
        calls.append(list(argv))
        return proc.ProcResult(list(argv), 0 if ok else 1, output if argv == ["efibootmgr"] else "", "", False, 0.1)

    run.calls = calls
    return run


def test_detect_prefers_uefi_bootnext(tmp_path):
    efi = tmp_path / "efi"
    efi.mkdir()
    m, t, notes = detect_method(fake(EFI_WITH_WINDOWS), efi_dir=efi, grub_cfgs=[])
    assert (m, t) == ("efibootmgr", "Windows Boot Manager")


def test_detect_falls_back_to_grub(tmp_path):
    efi = tmp_path / "efi"
    efi.mkdir()
    cfg = tmp_path / "grub.cfg"
    cfg.write_text(GRUB_CFG)
    default = tmp_path / "default-grub"
    default.write_text('GRUB_DEFAULT=0\nGRUB_TIMEOUT=5\n')
    m, t, notes = detect_method(fake(EFI_WITHOUT), efi_dir=efi, grub_cfgs=[cfg], default_grub=default)
    assert m in ("grub-reboot", "grub2-reboot") and t == "Windows Boot Manager (on /dev/nvme0n1p1)"
    assert any("GRUB_DEFAULT=saved" in n for n in notes)
    default.write_text('GRUB_DEFAULT="saved"\n')
    assert grub_default_is_saved(default) is True
    _, _, notes = detect_method(fake(EFI_WITHOUT), efi_dir=tmp_path / "no-efi", grub_cfgs=[cfg], default_grub=default)
    assert any("레거시 BIOS" in n for n in notes) and not any("주의" in n for n in notes)
    assert grub_windows_entry([cfg]) == "Windows Boot Manager (on /dev/nvme0n1p1)"  # not the one in the submenu


def test_detect_gives_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError) as e:
        detect_method(fake(EFI_WITHOUT), efi_dir=tmp_path / "none", grub_cfgs=[tmp_path / "missing.cfg"])
    assert "os-prober" in str(e.value) or "OS_PROBER" in str(e.value)


def test_set_next_boot_auto(monkeypatch, tmp_path):
    import cs2mmt.agent.linux_switch as ls

    monkeypatch.setattr(ls, "detect_method", lambda run: ("efibootmgr", "Windows Boot Manager", []))
    run = fake(EFI_WITH_WINDOWS)
    assert set_next_boot("auto", "", run) == "BootNext=0003"
    assert run.calls[-1] == ["efibootmgr", "--bootnext", "0003"]
