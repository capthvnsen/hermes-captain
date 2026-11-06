"""Start, read, and stop captain runs. The process table stays in plugin-data."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import uuid
from pathlib import Path

from .plan import HOST_RE, PlanError, plan_read, plan_run, plan_steer, plan_stop

MAX_READ = 8000


def default_data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir("hermes-captain")


class Captain:
    def __init__(self, data_dir: Path | None = None, spawn=None, capture=None):
        self.data_dir = data_dir or default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.spawn = spawn or _spawn
        self.capture = capture or _capture

    def doctor(self, host: str | None = None) -> dict:
        names = ("claude", "codex", "orca", "t3code")
        if host:
            if not HOST_RE.fullmatch(host):
                raise PlanError("host must be a plain SSH destination")
            script = "for c in claude codex orca t3code; do if command -v \"$c\" >/dev/null 2>&1; then echo \"$c yes\"; else echo \"$c no\"; fi; done"
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, "--", "bash", "-c", script]
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
        plan = plan_run(agent, prompt, **kwargs)
        run_id = uuid.uuid4().hex[:12]
        record = {
            "id": run_id,
            "agent": agent,
            "surface": plan.surface,
            "mode": plan.mode,
            "summary": plan.summary,
            "argv0": plan.argv[0],
            "host": kwargs.get("host") or "",
        }
        if plan.mode == "capture":
            result = self.capture(plan.argv, plan.cwd, 90)
            record["status"] = "launched" if result.get("code") == 0 else "error"
            record["output"] = _clip(result.get("stdout") or result.get("error") or "")
            record["handle"] = _guess_handle(record["output"])
        else:
            log_path = self.data_dir / f"{run_id}.log"
            pid = self.spawn(plan.argv, plan.cwd, log_path)
            record["status"] = "running"
            record["pid"] = pid
            record["log"] = str(log_path)
        self._save(run_id, record)
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
            record["status"] = "running" if pid and _alive(pid) else "exited"
        return record

    def steer(self, run_id: str, prompt: str) -> dict:
        record = self._load(run_id)
        plan = plan_steer(record.get("agent") or "", record.get("handle") or "", prompt, host=record.get("host") or None)
        result = self.capture(plan.argv, None, 60)
        return {"ok": result.get("code") == 0, "output": _clip(result.get("stdout") or result.get("error") or "")}

    def stop(self, run_id: str) -> dict:
        record = self._load(run_id)
        remote = plan_stop(record.get("agent") or "", record.get("handle") or "", host=record.get("host") or None)
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
    if isinstance(data, dict):
        for key in ("threadId", "thread_id", "id"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        thread = data.get("thread") or data.get("data") or {}
        if isinstance(thread, dict):
            for key in ("id", "threadId"):
                value = thread.get(key)
                if isinstance(value, str):
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
    stdout = (done.stdout or "") + (("\n" + done.stderr) if done.stderr else "")
    return {"code": done.returncode, "stdout": stdout, "error": ""}
