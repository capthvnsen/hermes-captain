"""Start, read, and stop captain runs. The process table stays in plugin-data."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .peek import approval_stuck, build_card, git_stat, hold_reply, t3_pending
from .plan import HOST_RE, PlanError, plan_inspect, plan_read, plan_run, plan_steer, plan_stop, plan_wait, ssh_argv

MAX_READ = 8000
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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
        }
        if session_key:
            record["session_key"] = session_key
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
        card = build_card(
            record,
            output=output,
            alive=alive,
            diff=diff,
            previous=record.get("fingerprint"),
            stuck=stuck,
        )
        record["fingerprint"] = card["fingerprint"]
        record["stuck_on_approval"] = card["stuck_on_approval"]
        self._save(run_id, record)
        self.last_peek = {"speak": card["speak"], "run_id": run_id}
        return card

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
        if record.get("surface") == "t3" and record.get("handle"):
            plan = plan_wait(record["handle"], host=record.get("host") or None, timeout=600)
            result = self.capture(plan.argv, None, 620)
            ping = "done" if result.get("code") == 0 else "wait failed"
        else:
            pid = record.get("pid")
            proc = self._procs.get(int(pid)) if pid else None
            if proc is not None:
                proc.wait()
            elif pid and self._running(pid):
                while self._running(pid):
                    time.sleep(0.2)
            elif not pid:
                ping = "no process to wait on"
        try:
            fresh = self._load(run_id)
        except PlanError:
            return
        fresh["status"] = "exited" if ping == "done" else fresh.get("status")
        fresh["ping"] = ping
        fresh["pinged_at"] = time.time()
        self._save(run_id, fresh)
        if fresh.get("keep") and ping == "done" and self.notify:
            try:
                self.notify(run_id, fresh)
            except Exception:
                fresh["follow_up"] = "ping failed"
                self._save(run_id, fresh)

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
        result = self.capture(plan.argv, None, 60)
        return {"ok": result.get("code") == 0, "output": _clip(result.get("stdout") or result.get("error") or "")}

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


def _alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
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
