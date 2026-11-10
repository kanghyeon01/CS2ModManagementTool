"""Linux side of the multi-boot PC: reboot into Windows when a controller woke the PC for a job.

Runs as root from a systemd unit at boot (and optionally on a timer). It asks the controllers
whether this boot is for a job; if so it sets a one-time boot entry and reboots:

* ``efibootmgr`` (default): sets the UEFI ``BootNext`` variable to the Windows Boot Manager
  entry; works whatever boot loader Linux uses, and only for the next boot;
* ``grub-reboot``: GRUB's one-time default (needs ``GRUB_DEFAULT=saved``);
* ``systemd-boot``: ``bootctl set-oneshot``.

A boot that is not for a job is left alone, so the owner using Linux at home is never
interrupted. Before rebooting, logged-in users get a message and a grace period to cancel
(``touch /run/cs2mmt-cancel`` or stop the unit).
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from ..common import proc
from ..common.config import Config
from .client import ControllerRef, Controllers

__all__ = ["parse_efibootmgr", "find_entry", "set_next_boot", "boot_time", "user_sessions_active", "run_boot_switch",
           "grub_windows_entry", "grub_default_is_saved", "detect_method", "describe"]

log = logging.getLogger("cs2mmt.linux")

CANCEL_FILE = Path("/run/cs2mmt-cancel")


def parse_efibootmgr(output: str) -> Tuple[Optional[str], List[Tuple[str, str]]]:
    """Returns (BootCurrent, [(number, label)]) from ``efibootmgr`` output."""
    current = None
    entries: List[Tuple[str, str]] = []
    for line in output.splitlines():
        m = re.match(r"^BootCurrent:\s*([0-9A-Fa-f]{4})", line)
        if m:
            current = m.group(1).upper()
            continue
        m = re.match(r"^Boot([0-9A-Fa-f]{4})(\*?)\s+(.*?)(?:\t|\s{2,}|\s+(?:HD|PciRoot|BBS|VenHw|FvVol|VenMedia)\().*$", line)
        if not m:
            m = re.match(r"^Boot([0-9A-Fa-f]{4})(\*?)\s+(.+)$", line)
        if m:
            entries.append((m.group(1).upper(), m.group(3).strip()))
    return current, entries


def find_entry(entries: List[Tuple[str, str]], pattern: str) -> Optional[str]:
    rx = re.compile(pattern, re.I)
    for num, label in entries:
        if rx.search(label):
            return num
    return None


GRUB_CFGS = ("/boot/grub/grub.cfg", "/boot/grub2/grub.cfg", "/boot/efi/EFI/ubuntu/grub.cfg", "/boot/efi/EFI/debian/grub.cfg")


def grub_windows_entry(paths=GRUB_CFGS) -> Optional[str]:
    """Title of the first top-level GRUB menu entry for Windows (added by os-prober)."""
    for p in paths:
        try:
            text = Path(p).read_text(errors="replace")
        except OSError:
            continue
        depth = 0
        for line in text.splitlines():
            stripped = line.strip()
            m = re.match(r"""^menuentry\s+(['"])(.+?)\1""", stripped)
            if m and depth == 0 and re.search(r"windows", m.group(2), re.I):
                return m.group(2)
            depth += stripped.count("{") - stripped.count("}")
            depth = max(depth, 0)
    return None


def grub_default_is_saved(path: Path = Path("/etc/default/grub")) -> Optional[bool]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    values = re.findall(r"""^\s*GRUB_DEFAULT=["']?([^"'\s]*)""", text, re.M)
    return bool(values) and values[-1] == "saved"


def detect_method(run: Callable[..., proc.ProcResult] = proc.run, efi_dir: Path = Path("/sys/firmware/efi"),
                  grub_cfgs=GRUB_CFGS, default_grub: Path = Path("/etc/default/grub")) -> Tuple[str, str, List[str]]:
    """How to boot Windows once on this PC: (method, target, notes).

    With GRUB on UEFI, efibootmgr is preferred: BootNext makes the firmware start Windows Boot
    Manager directly for one boot, whatever GRUB's settings are. On a legacy-BIOS install (or
    without a Windows UEFI entry), GRUB's own one-time default is used instead.
    """
    notes: List[str] = []
    if efi_dir.exists():
        r = run(["efibootmgr"], timeout=30)
        if r.ok:
            _, entries = parse_efibootmgr(r.stdout)
            num = find_entry(entries, r"Windows Boot Manager")
            if num is not None:
                return "efibootmgr", "Windows Boot Manager", notes + ["UEFI 항목 Boot%s 'Windows Boot Manager' 사용" % num]
            notes.append("UEFI 부팅이지만 Windows Boot Manager 항목이 없습니다 (%s)" % ", ".join(l for _, l in entries))
        else:
            notes.append("efibootmgr 실행 실패 (설치: apt install efibootmgr)")
    else:
        notes.append("레거시 BIOS 부팅입니다 (UEFI 아님)")
    title = grub_windows_entry(grub_cfgs)
    if title:
        tool = "grub2-reboot" if shutil.which("grub2-reboot") and not shutil.which("grub-reboot") else "grub-reboot"
        if grub_default_is_saved(default_grub) is False:
            notes.append("주의: /etc/default/grub이 GRUB_DEFAULT=saved가 아니라 grub-reboot가 동작하지 않을 수 있습니다. "
                         "GRUB_DEFAULT=saved로 바꾸고 sudo update-grub 하세요 (평소 기본값은 grub-set-default 0으로 리눅스 유지)")
        return tool, title, notes + ["GRUB 메뉴 항목 '%s' 사용" % title]
    raise RuntimeError("Windows로 부팅할 방법을 찾지 못했습니다: %s. GRUB 메뉴에 Windows가 보이도록 "
                       "/etc/default/grub에 GRUB_DISABLE_OS_PROBER=false를 넣고 sudo update-grub 하세요." % "; ".join(notes))


def set_next_boot(method: str, target: str, run: Callable[..., proc.ProcResult] = proc.run) -> str:
    if method == "auto":
        method, target, _ = detect_method(run)
    if method == "efibootmgr":
        r = run(["efibootmgr"], timeout=30)
        if not r.ok:
            raise RuntimeError("efibootmgr failed: %s" % r.tail(5))
        _, entries = parse_efibootmgr(r.stdout)
        num = find_entry(entries, target or r"Windows Boot Manager")
        if num is None:
            raise RuntimeError("no UEFI entry matches %r; entries: %s" % (target, ", ".join("%s %s" % e for e in entries)))
        r = run(["efibootmgr", "--bootnext", num], timeout=30)
        if not r.ok:
            raise RuntimeError("efibootmgr --bootnext %s failed: %s" % (num, r.tail(5)))
        return "BootNext=%s" % num
    if method in ("grub-reboot", "grub2-reboot"):
        r = run([method, target], timeout=30)
        if not r.ok:
            raise RuntimeError("%s failed: %s" % (method, r.tail(5)))
        return "%s %s" % (method, target)
    if method == "systemd-boot":
        r = run(["bootctl", "set-oneshot", target], timeout=30)
        if not r.ok:
            raise RuntimeError("bootctl failed: %s" % r.tail(5))
        return "oneshot %s" % target
    raise ValueError("unknown boot switch method %r" % method)


def boot_time() -> float:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    try:
        up = float(Path("/proc/uptime").read_text().split()[0])
        return time.time() - up
    except OSError:
        return time.time()


def user_sessions_active(run: Callable[..., proc.ProcResult] = proc.run) -> bool:
    """True when someone is logged in and not idle (greeters are ignored)."""
    r = run(["loginctl", "list-sessions", "--no-legend"], timeout=15)
    if not r.ok:
        return True  # cannot tell: assume someone is there
    for line in r.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        sid = parts[0]
        info = run(["loginctl", "show-session", sid, "-p", "Class", "-p", "State", "-p", "IdleHint"], timeout=15)
        props = dict(l.split("=", 1) for l in info.stdout.splitlines() if "=" in l)
        if props.get("Class") == "user" and props.get("State") in ("active", "online") and props.get("IdleHint", "no") == "no":
            return True
    return False


def _wall(message: str, run) -> None:
    try:
        run(["wall", message], timeout=10)
    except Exception:
        pass


def run_boot_switch(cfg: Config, watch: bool = False, run: Callable[..., proc.ProcResult] = proc.run, sleep=time.sleep) -> str:
    refs = [ControllerRef(c["id"], c["url"]) for c in cfg.get_list("controllers")]
    clients = Controllers(refs, str(cfg.require("secrets.api_token")))
    method = str(cfg.get("linux.method", "auto"))
    target = str(cfg.get("linux.target", r"Windows Boot Manager"))
    grace = int(cfg.get("linux.grace_seconds", 60))
    wait_network = int(cfg.get("linux.network_wait_seconds", 300))
    idle_shutdown = int(cfg.get("linux.idle_shutdown_minutes", 0))
    bt = boot_time()
    deadline = time.time() + (wait_network if not watch else 0)
    decision = {"action": "none"}
    while True:
        decision = clients.boot_decision(bt, force=watch)
        if decision.get("action") == "windows" or time.time() >= deadline:
            break
        sleep(20)
    if decision.get("action") == "windows":
        if watch and user_sessions_active(run):
            log.info("job %s waiting, but someone is using this PC; not switching", decision.get("job_id"))
            return "busy"
        if CANCEL_FILE.exists():
            return "cancelled"
        _wall("CS2 Mod Manager: rebooting into Windows in %ds for job %s. Cancel: sudo touch %s"
              % (grace, decision.get("job_id"), CANCEL_FILE), run)
        waited = 0
        while waited < grace:
            if CANCEL_FILE.exists():
                log.info("cancelled by %s", CANCEL_FILE)
                return "cancelled"
            sleep(5)
            waited += 5
        how = set_next_boot(method, target, run)
        log.info("switching to Windows (%s) for job %s", how, decision.get("job_id"))
        run(["systemctl", "reboot"], timeout=30)
        return "rebooting"
    if not watch and idle_shutdown > 0 and decision.get("unattended"):
        # A boot nobody asked for (power restored, BIOS "power on after power loss"): power off
        # again once it is clear nobody is using the PC, so Wake-on-LAN is armed for next time.
        sleep(max(0, idle_shutdown * 60 - (time.time() - bt)))
        if not user_sessions_active(run) and not CANCEL_FILE.exists():
            log.info("unattended boot without a job and nobody logged in: powering off")
            run(["systemctl", "poweroff"], timeout=30)
            return "poweroff"
    return "none"


def describe(cfg: Config, run: Callable[..., proc.ProcResult] = proc.run) -> str:
    """What the boot switch would do on this PC, without rebooting (``--check``)."""
    lines = []
    method = str(cfg.get("linux.method", "auto"))
    lines.append("설정된 방식: %s" % method)
    try:
        m, t, notes = detect_method(run)
        lines.append("감지 결과: %s → '%s'" % (m, t))
        lines.extend("  - " + n for n in notes)
        if method not in ("auto", m):
            lines.append("  (설정은 %s입니다. 감지 결과와 다르면 auto로 두는 것을 권장합니다)" % method)
    except RuntimeError as e:
        lines.append("감지 실패: %s" % e)
    refs = [ControllerRef(c["id"], c["url"]) for c in cfg.get_list("controllers")]
    try:
        clients = Controllers(refs, str(cfg.require("secrets.api_token")))
        reach = clients.reachable()
        lines.append("컨트롤러 응답: %s" % (", ".join(r.id for r in reach) or "없음"))
        lines.append("지금 부팅했다면: %s" % clients.boot_decision(boot_time()).get("action"))
    except Exception as e:
        lines.append("컨트롤러 확인 실패: %s" % e)
    return "\n".join(lines)
