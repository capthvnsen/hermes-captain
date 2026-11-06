"""Hermes Captain — one surface for the coding agents a remote Hermes has to drive."""

from __future__ import annotations

import json
import logging

from .plan import PlanError
from .runner import Captain

logger = logging.getLogger(__name__)

_CAPTAIN: Captain | None = None


def _captain() -> Captain:
    global _CAPTAIN
    if _CAPTAIN is None:
        _CAPTAIN = Captain()
    return _CAPTAIN


def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload})


def _err(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": str(exc)})


def _doctor(args, **kwargs):
    del kwargs
    host = str((args or {}).get("host") or "").strip() or None
    try:
        return _ok(_captain().doctor(host))
    except Exception as exc:
        logger.exception("captain doctor failed")
        return _err(exc)


def _run(args, **kwargs):
    del kwargs
    args = args or {}
    try:
        record = _captain().run(
            str(args.get("agent") or ""),
            str(args.get("prompt") or ""),
            cwd=str(args.get("cwd") or "") or None,
            host=str(args.get("host") or "") or None,
            worker=str(args.get("worker") or "") or None,
            orca_host=str(args.get("orca_host") or "") or None,
        )
        return _ok({"run": record})
    except PlanError as exc:
        return _err(exc)
    except Exception as exc:
        logger.exception("captain run failed")
        return _err(exc)


def _read(args, **kwargs):
    del kwargs
    try:
        return _ok({"run": _captain().read(str((args or {}).get("run_id") or ""))})
    except PlanError as exc:
        return _err(exc)


def _steer(args, **kwargs):
    del kwargs
    args = args or {}
    try:
        return json.dumps(_captain().steer(str(args.get("run_id") or ""), str(args.get("prompt") or "")))
    except PlanError as exc:
        return _err(exc)


def _stop(args, **kwargs):
    del kwargs
    try:
        return json.dumps(_captain().stop(str((args or {}).get("run_id") or "")))
    except PlanError as exc:
        return _err(exc)


def handle_command(raw_args: str) -> str:
    host = (raw_args or "").strip() or None
    report = _captain().doctor(host)
    lines = [f"Captain on {report.get('host')}"]
    for name, present in (report.get("agents") or {}).items():
        lines.append(f"{name}: {'ready' if present else 'missing'}")
    if report.get("error"):
        lines.append(str(report["error"]))
    return "\n".join(lines)


_SCHEMAS = {
    "captain_doctor": {
        "name": "captain_doctor",
        "description": "See which coding agents Captain can reach on this machine or over SSH. Does not launch anything.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Optional SSH destination, such as alex@mac. Empty means this machine."},
            },
        },
    },
    "captain_run": {
        "name": "captain_run",
        "description": "Start a coding agent. claude and codex run in the background. orca and t3 hand the prompt to the running app and return. Never adds approval-bypass flags. T3 is launched approval-required.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "claude, codex, orca, or t3"},
                "prompt": {"type": "string", "description": "The task to hand off."},
                "cwd": {"type": "string", "description": "Absolute project directory. On a remote host it is passed as an argument, not a shell string."},
                "host": {"type": "string", "description": "SSH destination when the agent is on another machine. Empty means this machine."},
                "worker": {"type": "string", "description": "Orca only: which agent tab to launch. Defaults to claude."},
                "orca_host": {"type": "string", "description": "Orca only: --host selector from orca host list, such as local or ssh:name."},
            },
            "required": ["agent", "prompt"],
        },
    },
    "captain_read": {
        "name": "captain_read",
        "description": "Read the latest output of a Captain run. For T3 this asks the thread. For CLI runs it tails the local log.",
        "parameters": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    "captain_steer": {
        "name": "captain_steer",
        "description": "Send a follow-up into a T3 thread with --if-busy steer. Claude, Codex, and Orca runs are one-shot; start another run instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["run_id", "prompt"],
        },
    },
    "captain_stop": {
        "name": "captain_stop",
        "description": "Stop a local CLI run, or interrupt a T3 thread. Orca launches have no verified stop command yet.",
        "parameters": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
}


def register(ctx):
    ctx.register_command(
        "captain",
        handle_command,
        description="Show which coding agents Captain can reach.",
        args_hint="[ssh-host]",
    )
    handlers = {
        "captain_doctor": _doctor,
        "captain_run": _run,
        "captain_read": _read,
        "captain_steer": _steer,
        "captain_stop": _stop,
    }
    for name, handler in handlers.items():
        ctx.register_tool(name=name, toolset="hermes-captain", schema=_SCHEMAS[name], handler=handler)
