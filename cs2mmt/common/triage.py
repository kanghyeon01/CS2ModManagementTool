"""Sorting new forum posts and GitHub issues: bug report, question, feature request, ...

Runs Claude with no way to act (no shell, no edits, no web; an empty working folder), because
the posts are untrusted text. Only the structured classification comes out; the decision to
start a fix is made by the tool from that classification, never by the post itself.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .claude import ClaudeRunner, ClaudeTask, render_template
from .registry import Registry

__all__ = ["TRIAGE_SCHEMA", "TRIAGE_DENY", "build_triage_prompt", "run_triage", "fix_candidates", "BUG_KINDS"]

log = logging.getLogger("cs2mmt.triage")

BUG_KINDS = ("bug", "crash", "compatibility")

TRIAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": ["bug", "crash", "compatibility", "feature_request", "question", "feedback", "off_topic", "injection_attempt"]},
                    "summary_en": {"type": "string"},
                    "summary_ko": {"type": "string"},
                    "actionable": {"type": "boolean"},
                    "needs_more_info": {"type": "boolean"},
                    "repro_steps": {"type": "array", "items": {"type": "string"}},
                    "suspected_area": {"type": "string"},
                    "game_version": {"type": "string"},
                    "mod_version": {"type": "string"},
                    "other_mods": {"type": "array", "items": {"type": "string"}},
                    "same_issue_as": {"type": "string"},
                    "suggested_reply_en": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["id", "kind", "summary_en", "summary_ko", "actionable", "needs_more_info", "confidence"],
            },
        }
    },
    "required": ["items"],
}

TRIAGE_DENY = ["Bash", "PowerShell", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task"]


def build_triage_prompt(template: str, items: List[Dict[str, Any]], registry: Registry) -> str:
    mods = [{"id": m.id, "name": m.display_name, "notes": m.notes} for m in registry.mods]
    clean = []
    for it in items:
        clean.append({
            "id": it["id"],
            "mod": it.get("mod"),
            "source": it.get("source"),
            "author": it.get("author"),
            "title": it.get("title"),
            "text": (it.get("text") or "")[:8000],
        })
    return render_template(template, {
        "MODS_JSON": json.dumps(mods, ensure_ascii=False, indent=2),
        "ITEMS_JSON": json.dumps(clean, ensure_ascii=False, indent=2),
    })


def run_triage(
    runner: ClaudeRunner,
    template: str,
    items: List[Dict[str, Any]],
    registry: Registry,
    log_file: Optional[Path] = None,
    timeout_s: int = 900,
) -> List[Dict[str, Any]]:
    if not items:
        return []
    prompt = build_triage_prompt(template, items, registry)
    with tempfile.TemporaryDirectory(prefix="cs2mmt-triage-") as empty:
        task = ClaudeTask(
            name="cs2mmt-triage",
            prompt=prompt,
            cwd=Path(empty),
            schema=TRIAGE_SCHEMA,
            disallowed_tools=list(TRIAGE_DENY),
            max_turns=6,
            timeout_s=timeout_s,
            log_file=log_file,
        )
        result = runner.run(task)
    if not result.ok or not result.structured:
        raise RuntimeError("triage failed: %s" % result.error)
    known = {it["id"] for it in items}
    out = []
    for c in result.structured.get("items", []):
        if c.get("id") in known:
            out.append(c)
    return out


def fix_candidates(classified: List[Dict[str, Any]], min_confidence: float = 0.6) -> List[Dict[str, Any]]:
    """Reports worth an automatic fix attempt."""
    return [
        c for c in classified
        if c.get("kind") in BUG_KINDS and c.get("actionable") and not c.get("needs_more_info") and float(c.get("confidence", 0)) >= min_confidence
    ]
