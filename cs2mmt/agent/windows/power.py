"""Keeping Windows awake during a job, user idle time, and what to do when the work is done."""

from __future__ import annotations

import ctypes
import logging
import os
import re
import time
from typing import Optional

from ...common import proc

__all__ = ["prevent_sleep", "allow_sleep", "idle_seconds", "uptime_seconds", "boot_time", "shutdown", "reboot_to_firmware_entry", "cancel_shutdown"]

log = logging.getLogger("cs2mmt.power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def _kernel32():
    if os.name != "nt":
        return None
    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def prevent_sleep(display: bool = True) -> None:
    k = _kernel32()
    if k is not None:
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | (ES_DISPLAY_REQUIRED if display else 0)
        k.SetThreadExecutionState(ctypes.c_uint(flags))


def allow_sleep() -> None:
    k = _kernel32()
    if k is not None:
        k.SetThreadExecutionState(ctypes.c_uint(ES_CONTINUOUS))


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input (0 when unknown)."""
    if os.name != "nt":
        return 0.0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
        return 0.0
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF  # type: ignore[attr-defined]
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


def uptime_seconds() -> float:
    k = _kernel32()
    if k is not None:
        k.GetTickCount64.restype = ctypes.c_ulonglong
        return k.GetTickCount64() / 1000.0
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except OSError:
        return 0.0


def boot_time() -> float:
    return time.time() - uptime_seconds()


def shutdown(delay_s: int = 120, reason: str = "CS2 Mod Manager: job finished", run=proc.run) -> None:
    if os.name == "nt":
        run(["shutdown", "/s", "/t", str(int(delay_s)), "/c", reason[:500]], timeout=30)
    else:
        run(["shutdown", "-h", "+%d" % max(1, int(delay_s) // 60)], timeout=30)


def cancel_shutdown(run=proc.run) -> None:
    if os.name == "nt":
        run(["shutdown", "/a"], timeout=30)


def reboot_to_firmware_entry(pattern: str, delay_s: int = 60, run=proc.run) -> Optional[str]:
    """One-time boot into another OS via the UEFI BootNext variable (needs administrator rights).

    ``bcdedit /enum firmware`` lists the firmware entries; the one whose description matches
    ``pattern`` (e.g. 'ubuntu|debian') is set as the boot sequence for the next start only.
    """
    r = run(["bcdedit", "/enum", "firmware"], timeout=60)
    if not r.ok:
        raise RuntimeError("bcdedit /enum firmware failed (run the agent with highest privileges): %s" % r.tail(5))
    guid = None
    current = None
    for line in r.stdout.splitlines():
        m = re.match(r"^identifier\s+(\{[0-9a-fA-F-]+\})", line.strip())
        if m:
            current = m.group(1)
            continue
        m = re.match(r"^description\s+(.*)$", line.strip())
        if m and current and re.search(pattern, m.group(1), re.I):
            guid = current
            break
    if guid is None:
        raise RuntimeError("no firmware entry matches %r" % pattern)
    r = run(["bcdedit", "/set", "{fwbootmgr}", "bootsequence", guid], timeout=60)
    if not r.ok:
        raise RuntimeError("bcdedit bootsequence failed: %s" % r.tail(5))
    run(["shutdown", "/r", "/t", str(int(delay_s)), "/c", "CS2 Mod Manager: back to Linux"], timeout=30)
    return guid
