"""Quiet check-ins. A skill can ask the model to stay quiet. This decides."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

STUCK_MARKERS = (
    "do you want to proceed",
    "do you want to make this edit",
    "allow this command",
    "waiting for your approval",
    "needs approval",
)

STATUS_PHRASES = (
    "still running",
    "no change",
    "nothing new",
    "nothing changed",
    "no update",
)


def git_status(cwd: str) -> str:
    """Short status, including new and staged files. Arguments only."""
    return _git_status_text(_run_git(["git", "-C", str(Path(cwd)), "status", "--short"]))


def git_stat(cwd: str) -> str:
    """Back-compat name. Shows new files, not only unstaged diffs."""
    return git_status(cwd)


def _run_git(argv: list[str]) -> str:
    if len(argv) < 4 or argv[1] != "-C":
        return ""
    if not Path(argv[2]).is_dir():
        return ""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (done.stdout or "").strip()[:500]


def _git_status_text(text: str) -> str:
    return (text or "").strip()[:500]


def t3_pending(payload: str) -> bool:
    """True when a T3 inspect reply still has an approval waiting."""
    try:
        data = json.loads(payload or "")
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    thread = inner.get("thread") if isinstance(inner, dict) and isinstance(inner.get("thread"), dict) else inner
    if not isinstance(thread, dict):
        return False
    requests = thread.get("pendingRequests") or []
    if isinstance(requests, list) and requests:
        return True
    pending = thread.get("pendingRuntimeRequest")
    return isinstance(pending, dict) and bool(pending.get("id"))


def approval_stuck(output: str, alive: bool) -> bool:
    if not alive:
        return False
    low = (output or "").lower()
    return any(marker in low for marker in STUCK_MARKERS)


def fingerprint(output: str, alive: bool, stuck: bool, diff: str) -> str:
    tail = (output or "")[-500:]
    raw = f"{int(alive)}|{int(stuck)}|{diff}|{len(output or '')}|{tail}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def build_card(
    record: dict,
    *,
    output: str,
    alive: bool,
    diff: str,
    previous: str | None,
    stuck: bool | None = None,
) -> dict:
    if stuck is None:
        stuck = approval_stuck(output, alive)
    fp = fingerprint(output, alive, stuck, diff)
    speak = fp != (previous or "")
    card = {
        "run_id": record.get("id") or "",
        "agent": record.get("agent") or "",
        "status": record.get("status") or "",
        "speak": speak,
        "changed": speak,
        "stuck_on_approval": stuck,
        "fingerprint": fp,
    }
    if speak:
        card["diff"] = diff
        card["note"] = _note(record.get("status") or "", stuck, diff)
    else:
        card["note"] = "No change."
    return card


def _note(status: str, stuck: bool, diff: str) -> str:
    if stuck:
        return "Stuck on approval."
    if diff:
        first = diff.splitlines()[0][:180]
        return f"Changed: {first}"
    return status or "running"


def hold_reply(last_peek: dict | None, reply: str) -> str | None:
    """Replace a status narration after a quiet peek. Leave real answers alone."""
    if not last_peek or last_peek.get("speak") is not False:
        return None
    text = (reply or "").strip()
    if not text or len(text) > 400:
        return None
    low = text.lower()
    if "?" in text:
        return None
    if any(phrase in low for phrase in STATUS_PHRASES):
        return "No change."
    return None
