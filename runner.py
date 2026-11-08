"""Start, read, and stop captain runs. The process table stays in plugin-data."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger("hermes_captain.runner")

from .peek import (
    approval_stuck,
    build_card,
    cache_policy,
    error_reason,
    git_stat,
    hold_reply,
    idle_seconds,
    run_health,
    t3_pending,
    t3_thread,
    thread_failure,
)
from .plan import (
    HOST_RE,
    PlanError,
    plan_inspect,
    plan_list,
    plan_read,
    plan_run,
    plan_steer,
    plan_stop,
    plan_wait,
    ssh_argv,
)

MAX_READ = 8000
MAX_RESULT = 1500
T3_POLL = 60  # seconds per T3 wait; the health check runs between waits
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MODES = ("drive", "advise")
MAX_ROUNDS = 5


def default_data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir("conductor")


class Captain:
    def __init__(self, data_dir: Path | None = None, spawn=None, capture=None, diff=None):
        self.data_dir = data_dir or default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.spawn = spawn or _spawn
        self.capture = capture or _capture
        self.diff = diff or git_stat
        self._procs: dict[int, subprocess.Popen] = {}
        self._waits: dict[str, threading.Thread] = {}
        self.last_peek: dict | None = None
        self.notify = None
        self.retry_delay = 5
        self.health_every = 30
        self._supervise_stop = threading.Event()

    def get_mode(self) -> str:
        """drive or advise. advise unless the user chose drive: no surprise spend."""
        try:
            mode = json.loads((self.data_dir / "mode.json").read_text(encoding="utf-8")).get("mode")
        except (OSError, json.JSONDecodeError, AttributeError):
            return "advise"
        return mode if mode in MODES else "advise"

    def set_mode(self, mode: str) -> str:
        mode = _check_mode(mode)
        (self.data_dir / "mode.json").write_text(json.dumps({"mode": mode}) + "\n", encoding="utf-8")
        return mode

    def doctor(self, host: str | None = None) -> dict:
        names = ("hermes", "claude", "codex", "grok", "opencode", "pycode", "orca", "t3code", "kimi", "pi", "commandcode")
        if host:
            if not HOST_RE.fullmatch(host):
                raise PlanError("host must be a plain SSH destination")
            script = (
                'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; '
                "for c in hermes claude codex grok opencode pycode orca t3code kimi pi commandcode; do "
                'if command -v "$c" >/dev/null 2>&1; then echo "$c yes"; else echo "$c no"; fi; '
                "done"
            )
            argv = ssh_argv(host, ["bash", "-c", script])
            result = self.capture(argv, None, 20)
            found = {}
            for line in (result.get("stdout") or "").splitlines():
                parts = line.split()
                if len(parts) == 2:
                    found[parts[0]] = parts[1] == "yes"
            return {"host": host, "ok": result.get("code") == 0, "agents": found, "error": result.get("error")}
        import shutil
        return {
            "host": "local",
            "ok": True,
            "agents": {name: shutil.which(name) is not None for name in names},
        }

    def run(self, agent: str, prompt: str, **kwargs) -> dict:
        keep = bool(kwargs.pop("keep", False))
        session_key = str(kwargs.pop("session_key", "") or "")
        mode = kwargs.pop("mode", None)
        mode = _check_mode(mode) if mode else None
        plan = plan_run(agent, prompt, **kwargs)
        run_id = uuid.uuid4().hex[:12]
        record = {
            "id": run_id,
            "agent": plan.name or agent,
            "surface": plan.surface,
            "mode": plan.mode,
            "summary": plan.summary,
            "argv0": plan.argv[0],
            "host": kwargs.get("host") or "",
            "cwd": kwargs.get("cwd") or "",
            "keep": keep,
            "last_active": time.time(),
        }
        if session_key:
            record["session_key"] = session_key
        if mode:
            record["follow_mode"] = mode  # "mode" on the record is already the launch mode
        if plan.mode == "capture":
            result = self.capture(plan.argv, plan.cwd, 90)
            stdout = result.get("stdout") or ""
            record["status"] = "launched" if result.get("code") == 0 else "error"
            record["handle"] = _guess_handle(stdout)
            record["output"] = _clip(stdout or result.get("error") or "")
        else:
            log_path = self.data_dir / f"{run_id}.log"
            pid = self.spawn(plan.argv, plan.cwd, log_path)
            if self.spawn is _spawn:
                proc = getattr(_spawn, "_last", None)
                if proc is not None:
                    self._procs[proc.pid] = proc
            record["status"] = "running"
            record["pid"] = pid
            record["pid_started"] = _proc_start(pid)
            record["log"] = str(log_path)
        self._save(run_id, record)
        if record.get("pid") or (record.get("surface") == "t3" and record.get("handle")):
            self.arm_wait(run_id)
        return record

    def read(self, run_id: str) -> dict:
        record = self._load(run_id)
        if record.get("surface") == "t3" and record.get("handle"):
            plan = plan_read(record.get("agent") or "t3", record["handle"], host=record.get("host") or None)
            if plan is not None:
                result = self.capture(plan.argv, None, 30)
                record["output"] = _clip(result.get("stdout") or result.get("error") or "")
                record["status"] = "launched" if result.get("code") == 0 else "error"
                self._save(run_id, record)
                return record
        log = record.get("log")
        if log and Path(log).exists():
            record["output"] = _clip(Path(log).read_text(encoding="utf-8", errors="replace"))
            pid = record.get("pid")
            record["status"] = "running" if pid and self._running(pid) else "exited"
        return record

    def peek(self, run_id: str) -> dict:
        record = self.read(run_id)
        output = record.get("output") or ""
        alive = record.get("status") in {"running", "launched"}
        diff = ""
        cwd = record.get("cwd") or ""
        host = record.get("host") or ""
        if cwd and host:
            diff = self._remote_status(host, cwd)
        elif cwd:
            diff = self.diff(cwd) or ""
        if record.get("surface") == "t3" and record.get("handle"):
            record["inspect"] = self._inspect(record)
        stuck = t3_pending(record.get("inspect") or "") or approval_stuck(output, alive)
        health, why = self._health(record, output, alive, stuck, t3_thread(record.get("inspect") or ""))
        card = build_card(
            record,
            output=output,
            alive=alive,
            diff=diff,
            previous=record.get("fingerprint"),
            stuck=stuck,
            health=health,
            why=why,
        )
        record["fingerprint"] = card["fingerprint"]
        record["stuck_on_approval"] = card["stuck_on_approval"]
        self._save(run_id, record)
        self.last_peek = {"speak": card["speak"], "run_id": run_id}
        return card

    def supervise(self, interval: float = 2.0) -> bool:
        """Keep waits alive after the turn that started them has exited.

        The lock is held for the life of this process. A short-lived turn worker
        loses the race to the gateway, which is the process that can message the user.
        """
        import fcntl

        if getattr(self, "_supervisor", None) and self._supervisor.is_alive():
            return True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.data_dir / ".supervisor.lock"
        lock_path.touch()
        handle = open(lock_path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._lock_handle = handle

        def loop() -> None:
            while not self._supervise_stop.is_set():
                try:
                    self.resume()
                except Exception:
                    logger.exception("conductor supervise")
                self._supervise_stop.wait(interval)

        thread = threading.Thread(target=loop, name="conductor-supervise", daemon=True)
        thread.start()
        self._supervisor = thread
        return True

    def arm_wait(self, run_id: str) -> dict:
        """Wait for the run with no model. A 15-minute check cannot beat this."""
        record = self._load(run_id)
        if record.get("pinged_at"):
            return {"ok": True, "ping": record.get("ping"), "seconds": 0, "model": False}
        record["wait_started"] = time.time()
        self._save(run_id, record)
        thread = threading.Thread(target=self._wait_loop, args=(run_id,), daemon=True)
        thread.start()
        self._waits[run_id] = thread
        return {"ok": True, "waiting": True, "model": False}

    def watch_running(self, host: str | None = None, session_key: str = "") -> dict:
        """Arm a no-model wait on every running T3 thread. Ping when one finishes."""
        plan = plan_list(host)
        result = self.capture(plan.argv, None, 30)
        if result.get("code") != 0:
            return {"ok": False, "error": result.get("error") or "could not list threads", "watched": []}
        try:
            data = json.loads(result.get("stdout") or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "error": "thread list was not json", "watched": []}
        threads = (data.get("data") or {}).get("threads") or []
        watching = self._open_handles()
        watched = []
        for thread in threads:
            if thread.get("runStatus") != "running" and thread.get("activityRunStatus") != "running":
                continue
            handle = thread.get("id") or ""
            if not handle or handle in watching:
                continue
            run_id = uuid.uuid4().hex[:12]
            record = {
                "id": run_id,
                "agent": "t3",
                "surface": "t3",
                "handle": handle,
                "host": host or "",
                "title": thread.get("title") or "",
                "status": "running",
                "keep": False,
                "last_active": time.time(),
            }
            if session_key:
                record["session_key"] = session_key
            self._save(run_id, record)
            self.arm_wait(run_id)
            watched.append({"run_id": run_id, "title": record["title"]})
        return {"ok": True, "watched": watched, "model": False}

    def _open_handles(self) -> set[str]:
        handles = set()
        for path in self.data_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("handle") and not record.get("pinged_at"):
                handles.add(record["handle"])
        return handles

    def _wait_t3(self, record: dict) -> str:
        host = record.get("host") or None
        fails = 0
        for _ in range(4 * 3600 // T3_POLL):
            plan = plan_wait(record["handle"], host=host, timeout=T3_POLL)
            result = self.capture(plan.argv, None, T3_POLL + 20)
            outcome = _wait_outcome(result.get("stdout") or "") or _wait_outcome(result.get("stderr") or "")
            if outcome == "completed":
                return "done"
            if outcome == "needs-attention":
                return "needs-attention"
            if outcome == "timeout" or result.get("code") in (6, 124):
                fails = 0
                if self._tick(record["id"]) == "errored":
                    return "errored"
                continue
            if result.get("code") == 0 and not outcome:
                return "done"
            if outcome and self._tick(record["id"]) == "errored":  # a wait answer we do not know may be a failed run
                return "errored"
            fails += 1  # ssh to the other machine can blip; three in a row is a real failure
            if fails >= 3:
                return "wait failed"
            time.sleep(self.retry_delay)
        return "wait failed"

    def wait_done(self, run_id: str, timeout: float = 30) -> dict:
        thread = self._waits.get(run_id)
        if thread is not None:
            thread.join(timeout)
        record = self._load(run_id)
        started = record.get("wait_started") or record.get("pinged_at") or time.time()
        pinged = record.get("pinged_at")
        return {
            "ok": bool(pinged),
            "ping": record.get("ping"),
            "seconds": round((pinged - started), 3) if pinged else None,
            "model": False,
            "status": record.get("status"),
        }

    def _wait_loop(self, run_id: str) -> None:
        try:
            record = self._load(run_id)
        except PlanError:
            return
        ping = "done"
        code = None
        if record.get("surface") == "t3" and record.get("handle"):
            ping = self._wait_t3(record)
        elif record.get("pid"):
            code = self._wait_pid(run_id, record["pid"])
        else:
            ping = "no process to wait on"
        try:
            fresh = self._load(run_id)
        except PlanError:
            return
        if code and fresh.get("status") != "stopped":  # our own SIGTERM is not a crash
            fresh["exit_code"] = code
        fresh["status"] = "exited" if ping in ("done", "errored") else fresh.get("status")
        fresh["ping"] = ping
        fresh["pinged_at"] = fresh["last_active"] = time.time()
        fresh["message"] = self._finish_message(fresh, ping)
        self._save(run_id, fresh)
        self._deliver(run_id, fresh)

    def _wait_pid(self, run_id: str, pid: int) -> int | None:
        """Block until the process exits, checking health on the way. The exit code, when we are its parent."""
        last = time.time()
        while self._running(pid):
            time.sleep(0.2)
            if time.time() - last >= self.health_every:
                last = time.time()
                self._tick(run_id)
        proc = self._procs.get(int(pid))
        return proc.returncode if proc is not None else None

    def _health(self, record: dict, output: str, alive: bool, stuck: bool, thread: dict) -> tuple[str, str]:
        state = thread.get("runStatus")
        reason = thread_failure(thread)
        if state:  # a T3 thread knows better than the record whether it is still going
            alive = state not in ("idle", "completed") or stuck
        if not alive and not reason:
            reason = error_reason(record.get("exit_code"), output)
        return run_health(alive=alive, idle=idle_seconds(record, thread), stuck=stuck, reason=reason)

    def _tick(self, run_id: str) -> str:
        """One no-model health check inside the wait. Pings once per stall or approval."""
        record = self._load(run_id)
        t3 = record.get("surface") == "t3" and record.get("handle")
        inspect = self._inspect(record) if t3 else ""
        output = "" if t3 else self.read(run_id).get("output") or ""
        stuck = t3_pending(inspect) or approval_stuck(output, True)
        word, why = self._health(record, output, True, stuck, t3_thread(inspect))
        alerted = record.get("alerted") or []
        if word in ("stalled", "needs-approval") and word not in alerted:
            record["alerted"] = [*alerted, word]
            self._save(run_id, record)
            name = record.get("title") or run_id
            where = f" on {record['host']}" if record.get("host") else ""
            message = (
                f"Conductor: {name}{where} is {word}: {why}. Tell the user this now, in one line. "
                f"Check it with conductor_peek {run_id}. Do not steer it and do not start another run."
            )
            try:  # shortcut: a ping that fails to send is not retried; the finish ping still is
                if self.notify:
                    self.notify(run_id, {**record, "message": message})
            except Exception:
                logger.exception("conductor alert")
        elif word == "working" and alerted:
            record["alerted"] = []
            self._save(run_id, record)
        return word

    def _deliver(self, run_id: str, record: dict) -> None:
        try:
            sent = bool(self.notify and self.notify(run_id, record))
        except Exception:
            sent = False
        record["follow_up"] = "delivered" if sent else "undelivered"
        self._save(run_id, record)

    def resume(self) -> None:
        """After a restart: re-send missed messages and re-arm T3 waits."""
        for path in sorted(self.data_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            run_id = record.get("id") or ""
            if not RUN_ID_RE.fullmatch(run_id):
                continue
            if record.get("follow_up") == "undelivered" and record.get("message"):
                self._deliver(run_id, record)
            elif record.get("pinged_at") or run_id in self._waits:
                continue
            elif record.get("handle"):
                self.arm_wait(run_id)
            elif record.get("pid"):
                started = _proc_start(record["pid"])
                if started and started == record.get("pid_started"):
                    self.arm_wait(run_id)
                else:  # a pid alone can belong to a stranger now
                    record["ping"] = "wait lost"
                    record["pinged_at"] = time.time()
                    record["message"] = self._finish_message(record, "wait lost")
                    self._save(run_id, record)
                    self._deliver(run_id, record)

    def _finish_message(self, record: dict, ping: str) -> str:
        name = record.get("title") or record.get("id")
        where = f" on {record['host']}" if record.get("host") else ""
        run_id = record.get("id")
        if ping not in ("done", "needs-attention", "errored"):
            return (
                f"Conductor: lost track of {name}{where} ({ping}). It may still be running. "
                f"Tell the user this now. Check it once with conductor_peek {run_id}. Do not start another run."
            )
        result, pending, failure = "", False, ""
        if record.get("surface") == "t3" and record.get("handle"):
            host = record.get("host") or None
            read = plan_read(record.get("agent") or "t3", record["handle"], host=host)
            if read is not None:
                got = self.capture(read.argv, None, 30)
                result = _answer_text(got.get("stdout") or "") if got.get("code") == 0 else ""
            inspect = self._inspect(record)
            pending = t3_pending(inspect)
            failure = thread_failure(t3_thread(inspect)) or ("the thread run failed" if ping == "errored" else "")
        elif record.get("log") and Path(record["log"]).exists():
            result = Path(record["log"]).read_text(encoding="utf-8", errors="replace").strip()
        if len(result) > MAX_RESULT:
            result = f"[first {len(result) - MAX_RESULT} characters cut]\n{result[-MAX_RESULT:]}"
        result = result or "(the agent returned no answer text)"
        if pending or ping == "needs-attention":
            return (
                f"Conductor: {name}{where} is waiting on you, probably an approval. "
                f"Tell the user now that it needs their approval on that machine. What it last said:\n{result}\n"
                "Do not steer it and do not start another run to get past the approval."
            )
        reason = failure or error_reason(record.get("exit_code"), result)
        if reason:
            return (
                f"Conductor: {name}{where} errored: {reason} (run {run_id}). "
                f"Tell the user now, in one line. What it last said:\n{result}\n"
                "Do not start another run to retry unless the user says so."
            )
        mode = record.get("follow_mode") or self.get_mode()
        rounds = record.get("rounds") or 0
        if mode == "advise":
            then = (
                "Then: if the user's goal is met, stop. If it is not, recommend one next step in one line and wait for the user. "
                "Do not call conductor_steer or conductor_run."
            )
        elif rounds >= MAX_ROUNDS:
            then = (
                f"Then: stop. This run has used all {MAX_ROUNDS} follow-ups. Tell the user where it stands and wait. "
                "Do not call conductor_steer or conductor_run."
            )
        else:
            then = (
                f"Then: if the user's goal is met, stop. If it is not, send one follow-up with conductor_steer on run {run_id} "
                f"(round {rounds + 1} of {MAX_ROUNDS}). Stop and tell the user at once if the run is stalled, errored, "
                "needs approval, or lost. Never start another run to get past an approval."
            )
        return (
            f"Conductor: {name}{where} finished (run {run_id}). "
            f"Message the user now with this result, in your own words:\n{result}\n{then}"
        )

    def _inspect(self, record: dict) -> str:
        plan = plan_inspect(record["handle"], host=record.get("host") or None)
        result = self.capture(plan.argv, None, 20)
        return result.get("stdout") or ""

    def _remote_status(self, host: str, cwd: str) -> str:
        argv = ssh_argv(host, ["git", "-C", cwd, "status", "--short"])
        result = self.capture(argv, None, 15)
        return (result.get("stdout") or "").strip()[:500]

    def _running(self, pid: int) -> bool:
        proc = self._procs.get(int(pid))
        if proc is not None:
            return proc.poll() is None
        return _alive(pid)

    def hold_status_reply(self, reply: str) -> str | None:
        return hold_reply(self.last_peek, reply)

    def steer(self, run_id: str, prompt: str) -> dict:
        record = self._load(run_id)
        plan = plan_steer(record.get("agent") or "", record.get("handle") or "", prompt, host=record.get("host") or None)
        probe = self.capture(plan_inspect(record["handle"], host=record.get("host") or None).argv, None, 20)
        if probe.get("code") != 0:
            return {"ok": False, "output": "could not check for a pending approval, so nothing was sent"}
        if t3_pending(probe.get("stdout") or ""):
            return {"ok": False, "output": "stuck on an approval; approve it on that machine first. Nothing was sent"}
        thread = t3_thread(probe.get("stdout") or "")
        policy = cache_policy(idle_seconds(record, thread, use_last_active=True), thread)
        result = self.capture(plan.argv, None, 60)
        if result.get("code") == 0:
            record["last_active"] = time.time()
            record["rounds"] = (record.get("rounds") or 0) + 1
            self._save(run_id, record)
        return {"ok": result.get("code") == 0, "output": _clip(result.get("stdout") or result.get("error") or ""), **policy}

    def stop(self, run_id: str) -> dict:
        record = self._load(run_id)
        remote = plan_stop(record.get("agent") or "", record.get("handle") or "", host=record.get("host") or None)
        if record.get("host") and remote is None:
            return {"ok": False, "output": "remote stop is not wired; the agent on the other machine keeps running"}
        if remote is not None:
            result = self.capture(remote.argv, None, 30)
            record["status"] = "stopped" if result.get("code") == 0 else "error"
            self._save(run_id, record)
            return {"ok": result.get("code") == 0, "output": _clip(result.get("stdout") or "")}
        pid = record.get("pid")
        if not pid:
            return {"ok": False, "output": "no local process to stop"}
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            os.kill(int(pid), signal.SIGTERM)
        record["status"] = "stopped"
        self._save(run_id, record)
        return {"ok": True, "output": f"signaled {pid}"}

    def _path(self, run_id: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id or ""):
            raise PlanError("run id is not a plain id")
        return self.data_dir / f"{run_id}.json"

    def _save(self, run_id: str, record: dict) -> None:
        self._path(run_id).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    def _load(self, run_id: str) -> dict:
        path = self._path(run_id)
        if not path.exists():
            raise PlanError(f"no run {run_id}")
        return json.loads(path.read_text(encoding="utf-8"))


def _check_mode(mode: str) -> str:
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise PlanError("mode must be drive or advise")
    return mode


def _wait_outcome(stdout: str) -> str:
    try:
        data = json.loads(stdout or "")
    except json.JSONDecodeError:
        return ""
    found = []

    def walk(node):
        if isinstance(node, dict):
            wait = node.get("wait")
            if isinstance(wait, dict) and isinstance(wait.get("outcome"), str):
                found.append(wait["outcome"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    if found:
        return found[0]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and error.get("code") == "THREAD_WAIT_TIMEOUT":
        return "timeout"
    return ""


def _answer_text(stdout: str) -> str:
    """The assistant's final answer from `t3code threads read`, never the JSON wrapper."""
    try:
        data = json.loads(stdout or "")
    except json.JSONDecodeError:
        return (stdout or "").strip()
    inner = data.get("data") if isinstance(data, dict) else None
    thread = inner.get("thread") if isinstance(inner, dict) else None
    if not isinstance(thread, dict):
        return ""
    messages = [m for m in thread.get("messages") or [] if isinstance(m, dict) and m.get("role") == "assistant" and not m.get("streaming")]
    turns = [t for t in thread.get("turns") or [] if isinstance(t, dict)]
    final = turns[-1].get("finalMessageId") if turns else None
    for message in messages:
        if message.get("id") == final and isinstance(message.get("text"), str):
            return message["text"].strip()
    texts = [m["text"].strip() for m in messages if isinstance(m.get("text"), str) and m["text"].strip()]
    return texts[-1] if texts else ""


def _clip(text: str) -> str:
    text = text or ""
    if len(text) <= MAX_READ:
        return text
    return text[-MAX_READ:]


def _guess_handle(output: str) -> str:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    nodes = [data]
    inner = data.get("data")
    if isinstance(inner, dict):
        nodes.append(inner)
        if isinstance(inner.get("thread"), dict):
            nodes.insert(0, inner["thread"])
    result = data.get("result")
    if isinstance(result, dict):
        outcome = result.get("outcome")
        if isinstance(outcome, dict) and isinstance(outcome.get("handle"), str):
            return outcome["handle"]
    if isinstance(data.get("thread"), dict):
        nodes.insert(0, data["thread"])
    for node in nodes:
        for key in ("threadId", "thread_id", "id"):
            value = node.get(key)
            if isinstance(value, str) and value and key != "id" or (key == "id" and "title" in node):
                if isinstance(value, str) and value:
                    return value
    return ""


def _proc_start(pid: int) -> str:
    """Start time of a pid. Same pid with a different start time is a different process."""
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""
    return (done.stdout or "").strip()


def _alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _spawn(argv: list[str], cwd: str | None, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    proc = subprocess.Popen(
        argv,
        cwd=cwd or None,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _spawn._last = proc  # type: ignore[attr-defined]
    return proc.pid


def _capture(argv: list[str], cwd: str | None, timeout: int) -> dict:
    try:
        done = subprocess.run(
            argv,
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {"code": 124, "stdout": exc.stdout or "", "error": "timed out"}
    except FileNotFoundError as exc:
        return {"code": 127, "stdout": "", "error": f"not found: {exc.filename}"}
    stdout = done.stdout or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    stderr = done.stderr or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return {"code": done.returncode, "stdout": stdout, "stderr": stderr, "error": stderr if done.returncode else ""}
