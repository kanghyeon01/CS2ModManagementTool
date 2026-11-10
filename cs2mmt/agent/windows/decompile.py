"""Keeping copies of the game's assemblies per build and decompiling them for the API diff.

Steam replaces the game files in place, so the old build's DLLs are copied away *before* Steam
is started for the update (``backup_managed``). ``ilspycmd`` (``dotnet tool install -g
ilspycmd``) turns each into a tree of C# files. Everything stays in the local cache folder.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from ...common import proc

__all__ = ["managed_dir_of", "backup_managed", "decompile", "prune"]

log = logging.getLogger("cs2mmt.decompile")


def managed_dir_of(game_dir: Path) -> Path:
    return game_dir / "Cities2_Data" / "Managed"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_managed(managed: Path, build: str, cache_dir: Path, assemblies: List[str]) -> Optional[Path]:
    """Copies the listed assemblies of build ``build``; no-op when the copy exists."""
    if not build:
        return None
    dest = cache_dir / "managed" / str(build)
    manifest = dest / "MANIFEST.json"
    if manifest.is_file():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    files: Dict[str, str] = {}
    for name in assemblies:
        src = managed / (name + ".dll")
        if src.is_file():
            shutil.copy2(src, dest / src.name)
            files[src.name] = _sha256(src)
    manifest.write_text(json.dumps({"build": build, "files": files}, indent=2), encoding="utf-8")
    log.info("backed up %d assemblies of build %s", len(files), build)
    return dest


def decompile(
    dll_dir: Path,
    build: str,
    cache_dir: Path,
    assemblies: List[str],
    ilspycmd: str = "ilspycmd",
    reference_dir: Optional[Path] = None,
    timeout_s: float = 1800,
    log_file: Optional[Path] = None,
    run=proc.run,
) -> Optional[Path]:
    """Decompiles each assembly into ``cache/decompiled/<build>/<assembly>/``."""
    out_root = cache_dir / "decompiled" / str(build)
    done_marker = out_root / ".complete"
    if done_marker.is_file():
        return out_root
    out_root.mkdir(parents=True, exist_ok=True)
    ok_any = False
    for name in assemblies:
        dll = dll_dir / (name + ".dll")
        if not dll.is_file():
            continue
        out = out_root / name
        if out.is_dir() and any(out.rglob("*.cs")):
            ok_any = True
            continue
        argv = [ilspycmd, "-p", "-o", str(out), "--disable-updatecheck", str(dll)]
        if reference_dir is not None:
            argv[1:1] = ["-r", str(reference_dir)]
        r = run(argv, timeout=timeout_s, log_file=log_file)
        if not r.ok and reference_dir is not None:
            r = run([ilspycmd, "-p", "-o", str(out), "--disable-updatecheck", str(dll)], timeout=timeout_s, log_file=log_file)
        if r.ok:
            ok_any = True
        else:
            log.warning("ilspycmd failed for %s: %s", name, r.tail(5))
    if ok_any:
        done_marker.write_text(build, encoding="utf-8")
        return out_root
    return None


def prune(cache_dir: Path, keep: int = 3) -> None:
    """Keeps the newest ``keep`` builds of each cache kind."""
    for kind in ("managed", "decompiled"):
        root = cache_dir / kind
        if not root.is_dir():
            continue
        builds = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in builds[keep:]:
            shutil.rmtree(old, ignore_errors=True)
