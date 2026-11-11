"""Quiet check-ins. A skill can ask the model to stay quiet. This decides."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

QUIET_AFTER = 180
STALLED_AFTER = 600
CACHE_WARM_FOR = 300
LONG_THREAD = 20  # turns. A thread with only messageCount counts two messages as a turn.
FAILED_STATES = ("failed", "error", "errored", "interrupted")
LIMIT_MARKERS = ("usage limit reached", "hit your limit", "rate limit exceeded", "quota exceeded")
LOGIN_MARKERS = ("please run /login", "not logged in", "invalid api key", "authentication failed")

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


def t3_thread(payload: str) -> dict:
    """The thread object inside a T3 inspect reply, or {}."""
    try:
        data = json.loads(payload or "")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    thread = inner.get("thread") if isinstance(inner.get("thread"), dict) else inner
    return thread if isinstance(thread, dict) else {}


def t3_pending(payload: str) -> bool:
    """True when a T3 inspect reply still has an approval waiting."""
    thread = t3_thread(payload)
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


def error_reason(code: int | None, text: str) -> str:
    """Why a finished run failed, or "". Looks at the tail only, where the agent's last words are."""
    low = (text or "")[-500:].lower()
    if any(marker in low for marker in LIMIT_MARKERS):
        return "usage limit reached"
    if any(marker in low for marker in LOGIN_MARKERS):
        return "login failed"
    return f"exited with code {code}" if code else ""


RESET_IN = re.compile(r"(?:try again|resets?)\s+in\s+(\d+)\s*([hm])", re.I)
RESET_AT = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b(?:\s*\(([A-Za-z_+/-]+)\))?", re.I)


def reset_time(text: str, now: float | None = None) -> float | None:
    """When a usage limit lifts, from "resets 3:00 PM (America/Denver)" or "try again in 2 hours". None if unclear."""
    now = now or time.time()
    text = (text or "")[-500:]
    m = RESET_IN.search(text)
    if m:
        return now + int(m[1]) * (3600 if m[2].lower() == "h" else 60)
    m = RESET_AT.search(text)
    if not m or not 1 <= int(m[1]) <= 12 or int(m[2] or 0) > 59:
        return None
    try:
        zone = ZoneInfo(m[4]) if m[4] else None  # shortcut: no zone means this machine's, when the agent is elsewhere say so
    except (KeyError, ValueError, OSError):
        zone = None
    at = datetime.fromtimestamp(now, zone).replace(hour=int(m[1]) % 12 + (12 if m[3].lower() == "p" else 0), minute=int(m[2] or 0), second=0, microsecond=0)
    if at.timestamp() <= now:
        at += timedelta(days=1)
    return at.timestamp()


def thread_failure(thread: dict) -> str:
    """Why a T3 thread stopped badly, or "" when it did not."""
    state = thread.get("runStatus")
    if state == "interrupted":
        return "the run was interrupted"
    if state in FAILED_STATES:
        return str(thread.get("lastError") or "").strip()[:160] or "the thread run failed"
    return ""


def activity_at(record: dict, thread: dict) -> float | None:
    """Last sign of life: the log's mtime for a CLI run, the thread's updatedAt for T3."""
    if record.get("log"):
        try:
            return Path(record["log"]).stat().st_mtime
        except OSError:
            return None
    try:
        return datetime.fromisoformat(str(thread.get("updatedAt")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def idle_seconds(record: dict, thread: dict, *, use_last_active: bool = False) -> float | None:
    seen = [activity_at(record, thread)]
    if use_last_active:
        seen.append(record.get("last_active"))
    seen = [s for s in seen if s]
    return max(0.0, time.time() - max(seen)) if seen else None


def run_health(
    *, alive: bool, idle: float | None, stuck: bool = False, reason: str = "", stalled_after: float = STALLED_AFTER,
) -> tuple[str, str]:
    """One word and one line. working, quiet, stalled, errored, needs-approval, done."""
    if reason:
        return "errored", reason
    if not alive:
        return "done", "finished"
    if stuck:
        return "needs-approval", "waiting on an approval; approve it on that machine"
    if idle is not None and idle >= stalled_after:
        return "stalled", f"no new output for {int(idle // 60)} minutes"
    if idle is not None and idle >= QUIET_AFTER:
        return "quiet", f"no new output for {int(idle // 60)} minutes"
    return "working", ""


def cache_policy(idle: float | None, thread: dict) -> dict:
    """Is the prompt cache still warm, and is the thread long enough that a cold start hurts."""
    if idle is None:
        return {"idle_seconds": None, "cache_warm": None}
    out = {"idle_seconds": int(idle), "cache_warm": idle < CACHE_WARM_FOR}
    turns = thread.get("turnCount") or (thread.get("messageCount") or 0) // 2
    if not out["cache_warm"] and turns >= LONG_THREAD:
        out["advice"] = (
            "Continuing this thread re-reads the whole history. "
            "Consider starting fresh with a two-line summary. Tell the user and let them choose."
        )
    return out


def fingerprint(output: str, alive: bool, stuck: bool, diff: str, health: str = "") -> str:
    tail = (output or "")[-500:]
    raw = f"{int(alive)}|{int(stuck)}|{diff}|{len(output or '')}|{tail}|{health}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def build_card(
    record: dict,
    *,
    output: str,
    alive: bool,
    diff: str,
    previous: str | None,
    stuck: bool | None = None,
    health: str = "working",
    why: str = "",
) -> dict:
    if stuck is None:
        stuck = approval_stuck(output, alive)
    fp = fingerprint(output, alive, stuck, diff, health)
    speak = fp != (previous or "")
    card = {
        "run_id": record.get("id") or "",
        "agent": record.get("agent") or "",
        "status": record.get("status") or "",
        "health": health,
        "speak": speak,
        "changed": speak,
        "stuck_on_approval": stuck,
        "fingerprint": fp,
    }
    if speak:
        card["diff"] = diff
        card["note"] = why if health in ("stalled", "errored") else _note(record.get("status") or "", stuck, diff)
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
