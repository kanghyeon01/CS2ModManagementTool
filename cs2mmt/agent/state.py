"""The agent's own small memory (a JSON file): finished work keys, handled reports, sessions."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["AgentState"]


class AgentState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: Dict[str, Any] = {"done": {}, "seen_items": {}, "sessions": {}, "builds_tested": {}}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except (OSError, ValueError):
                backup = self.path.with_suffix(".broken-%d" % int(time.time()))
                try:
                    os.replace(self.path, backup)
                except OSError:
                    pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- finished work ----
    def mark_done(self, work_key: str, state: str, summary: str = "") -> None:
        self.data["done"][work_key] = {"state": state, "at": time.time(), "summary": summary[:2000]}
        # Keep the file small: the last 500 keys.
        if len(self.data["done"]) > 500:
            for k in sorted(self.data["done"], key=lambda k: self.data["done"][k]["at"])[:-500]:
                del self.data["done"][k]
        self.save()

    def done(self, work_key: str) -> Optional[Dict[str, Any]]:
        entry = self.data["done"].get(work_key)
        if entry and entry.get("state") == "succeeded":
            return entry
        return None

    def game_update_covered(self, build: Optional[str]) -> Optional[str]:
        """A successful game_update for this build or a later one, if any."""
        if not build or not str(build).isdigit():
            return None
        for key, entry in self.data["done"].items():
            if entry.get("state") != "succeeded" or not key.startswith("game_update:"):
                continue
            other = key.split(":", 1)[1]
            if other.isdigit() and int(other) >= int(build):
                return key
        return None

    # ---- handled reports ----
    def unseen(self, item_ids: Iterable[str]) -> List[str]:
        return [i for i in item_ids if i not in self.data["seen_items"]]

    def mark_seen(self, item_ids: Iterable[str]) -> None:
        now = time.time()
        for i in item_ids:
            self.data["seen_items"][i] = now
        if len(self.data["seen_items"]) > 3000:
            for k in sorted(self.data["seen_items"], key=self.data["seen_items"].get)[:-3000]:
                del self.data["seen_items"][k]
        self.save()

    # ---- Claude sessions (session_mode = "resume") ----
    def session_for(self, name: str) -> Optional[str]:
        return self.data["sessions"].get(name)

    def set_session(self, name: str, session_id: Optional[str]) -> None:
        if session_id:
            self.data["sessions"][name] = session_id
            self.save()

    # ---- builds the mods were last tested on ----
    def set_tested(self, mod_id: str, build: str, version: str) -> None:
        self.data["builds_tested"][mod_id] = {"build": build, "version": version, "at": time.time()}
        self.save()

    def tested(self, mod_id: str) -> Optional[Dict[str, Any]]:
        return self.data["builds_tested"].get(mod_id)
