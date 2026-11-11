"""Conductor — start a coding agent, wait, then peek."""

from __future__ import annotations

import json
import logging

from pathlib import Path

from .plan import PlanError, compose_prompt
from .runner import Captain

logger = logging.getLogger(__name__)

_CAPTAIN: Captain | None = None
_CTX = None


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
    args = args or {}
    try:
        prompt, parts = str(args.get("prompt") or ""), ["free-form"]
        if not prompt.strip() or any(args.get(k) for k in ("goal", "files", "done_when", "verify", "context", "avoid", "skills")):
            prompt, parts = compose_prompt(
                str(args.get("goal") or ""), args.get("files"), str(args.get("done_when") or ""),
                verify=str(args.get("verify") or ""), context=str(args.get("context") or ""),
                avoid=args.get("avoid"), skills=args.get("skills"),
                agent=str(args.get("agent") or ""), delta=bool(args.get("continue")),
            )
        record = _captain().run(
            str(args.get("agent") or ""),
            prompt,
            cwd=str(args.get("cwd") or "") or None,
            host=str(args.get("host") or "") or None,
            worker=str(args.get("worker") or "") or None,
            orca_host=str(args.get("orca_host") or "") or None,
            provider=str(args.get("provider") or "") or None,
            worktree=str(args.get("worktree") or "") or None,
            prompt_flag=str(args.get("prompt_flag") or "") or None,
            continue_session=bool(args.get("continue")),
            keep=bool(args.get("keep")),
            model=str(args.get("model") or "") or None,
            mode=str(args.get("mode") or "") or None,
            card=str(args.get("card") or "") or None,
            session_key=str(kwargs.get("session_id") or kwargs.get("session_key") or ""),
        )
        return _ok({"run": record, "prompt": {"lines": len(prompt.splitlines()), "parts": parts}})
    except PlanError as exc:
        return _err(exc)
    except Exception as exc:
        logger.exception("captain run failed")
        return _err(exc)


def _mode(args, **kwargs):
    del kwargs
    try:
        captain = _captain()
        mode = str((args or {}).get("mode") or "")
        return _ok({"mode": captain.set_mode(mode) if mode else captain.get_mode()})
    except PlanError as exc:
        return _err(exc)


def _read(args, **kwargs):
    del kwargs
    try:
        return _ok({"run": _captain().read(str((args or {}).get("run_id") or ""))})
    except PlanError as exc:
        return _err(exc)


def _peek(args, **kwargs):
    del kwargs
    try:
        return _ok({"card": _captain().peek(str((args or {}).get("run_id") or ""))})
    except PlanError as exc:
        return _err(exc)


def _wait(args, **kwargs):
    del kwargs
    try:
        return _ok(_captain().arm_wait(str((args or {}).get("run_id") or "")))
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
    raw = (raw_args or "").strip()
    if raw.lower().startswith("peek"):
        run_id = raw[4:].strip()
        if not run_id:
            return "Usage: /conductor peek <run_id>"
        try:
            card = _captain().peek(run_id)
        except Exception as exc:
            return str(exc)
        if not card.get("speak"):
            return "No change."
        lines = [card.get("note") or card.get("status") or "changed"]
        if card.get("stuck_on_approval"):
            lines.append("stuck on approval")
        if card.get("diff"):
            lines.append(card["diff"])
        return "\n".join(lines)
    if raw.split()[:1] == ["mode"]:
        arg = raw[4:].strip()
        try:
            return f"Mode: {_captain().set_mode(arg) if arg else _captain().get_mode()}"
        except PlanError as exc:
            return str(exc)
    host = raw or None
    report = _captain().doctor(host)
    lines = [f"Conductor on {report.get('host')}"]
    for name, present in (report.get("agents") or {}).items():
        lines.append(f"{name}: {'ready' if present else 'missing'}")
    if report.get("error"):
        lines.append(str(report["error"]))
    return "\n".join(lines)


_SCHEMAS = {
    "conductor_doctor": {
        "name": "conductor_doctor",
        "description": "See which coding agents Conductor can reach on this machine or over SSH. Does not launch anything.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Optional SSH destination, such as dev@203.0.113.7. Empty means this machine."},
            },
        },
    },
    "conductor_run": {
        "name": "conductor_run",
        "description": "Start a coding agent. hermes, claude, codex, grok, opencode, and pycode are named recipes. Never adds approval-bypass flags. Pass keep only when the user wants the next task started after this one finishes.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "hermes, claude, codex, grok, cursor, opencode, pycode, orca, t3, or another terminal command name. Unknown commands need prompt_flag. The run record's events field says how it reports back: push (claude), push (done only) (codex), or polling (everything else, including T3, which waits on t3code)."},
                "prompt_flag": {"type": "string", "description": "For an unknown terminal agent only: -p, --print, or --prompt. No other flags are accepted."},
                "goal": {"type": "string", "description": "One outcome for this run. Conductor builds the kickoff from goal, context, files, avoid, verify, and done_when, so pass them every time and do not write the structure yourself. Wins over prompt. With continue, only the task part is sent, never the rules again."},
                "context": {"type": "string", "description": "1-3 lines: why, and what already exists. Not a repo dump."},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Files or folders in scope. Empty makes the agent read first and state a plan."},
                "avoid": {"type": "array", "items": {"type": "string"}, "description": "Paths or areas the agent must not touch."},
                "verify": {"type": "string", "description": "The exact command that proves the work (tests, build, lint). The agent runs it and fixes until it passes. Omit and it finds the project's test command."},
                "skills": {"type": "array", "items": {"type": "string"}, "description": "Skill names the agent should use. Only claude and codex are told. Never invent a name."},
                "done_when": {"type": "string", "description": "What done looks like, checkable."},
                "prompt": {"type": "string", "description": "Free-form task, only when goal does not fit."},
                "cwd": {"type": "string", "description": "Absolute project directory. On a remote host it is passed as an argument, not a shell string."},
                "host": {"type": "string", "description": "SSH destination when the agent is on another machine. Empty means this machine."},
                "worker": {"type": "string", "description": "Orca only: which agent tab to launch. Defaults to claude."},
                "orca_host": {"type": "string", "description": "Orca only: --environment selector, such as local. Empty means the machine the command runs on."},
                "provider": {"type": "string", "description": "T3 only: provider instance id, such as claudeAgent. Empty uses T3's saved default."},
                "worktree": {"type": "string", "description": "Orca only: worktree selector, such as path:/Users/dev/Projects/app. Required when more than one workspace matches."},
                "continue": {"type": "boolean", "description": "Continue the last claude or grok session in this directory. Other agents refuse this."},
                "keep": {"type": "boolean", "description": "When this run finishes, ping Hermes to give the next task. Only if the user still wants the work to continue."},
                "model": {"type": "string", "description": "Plain model id for hermes, claude, grok, or a T3 handover (--model). Other agents refuse this. Not a price and not a bypass flag."},
                "card": {"type": "string", "description": "Hermes kanban task id. Conductor comments progress on it and blocks it on an error or approval. It never completes it."},
                "mode": {"type": "string", "enum": ["drive", "advise"], "description": "For this run only: overrides the saved mode. drive keeps steering until done; advise only recommends."},
            },
            "required": ["agent"],
        },
    },
    "conductor_mode": {
        "name": "conductor_mode",
        "description": "Get or set what Conductor does when a run finishes and the goal is not met. drive: keep steering, at most 5 follow-ups per run. advise (default): recommend one next step and wait for the user. Only set it when the user asks.",
        "parameters": {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["drive", "advise"], "description": "Omit to read the current mode."}},
        },
    },
    "conductor_read": {
        "name": "conductor_read",
        "description": "Read the latest output of a Conductor run. For T3 this asks the thread. For CLI runs it tails the local log.",
        "parameters": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    "conductor_peek": {
        "name": "conductor_peek",
        "description": "Check a run without dumping the log. Returns speak false when nothing changed, so the lead model stays quiet. Includes a health word (working, quiet, stalled, errored, needs-approval, done), a stuck-on-approval flag, and a short diff stat when something did change.",
        "parameters": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    "conductor_wait": {
        "name": "conductor_wait",
        "description": "Wait for a run to finish and ping when it does. No model call. A 15-minute check-in is slower.",
        "parameters": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    "conductor_steer": {
        "name": "conductor_steer",
        "description": "Send a follow-up into a T3 thread with --if-busy steer. Claude, Codex, and Orca runs are one-shot; start another run instead. Returns idle_seconds and cache_warm (true under 5 minutes); when the cache is cold and the thread is long it adds advice for the user.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "prompt": {"type": "string", "description": "Only what changed since the last turn. Never re-send the rules, the file list, or the repo."},
            },
            "required": ["run_id", "prompt"],
        },
    },
    "conductor_stop": {
        "name": "conductor_stop",
        "description": "Stop a local CLI run, or interrupt a T3 thread. Orca launches have no verified stop command yet.",
        "parameters": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    "conductor_watch": {
        "name": "conductor_watch",
        "description": "Watch running T3 threads and ping when one finishes. No model in the wait. The user should not have to ask for an update.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "SSH destination when T3 is on another machine. Empty means this machine."},
            },
        },
    },
}


def _watch(args, **kwargs):
    args = args or {}
    try:
        return _ok(_captain().watch_running(
            host=str(args.get("host") or "") or None,
            session_key=str(kwargs.get("session_id") or kwargs.get("session_key") or ""),
        ))
    except PlanError as exc:
        return _err(exc)
    except Exception as exc:
        logger.exception("conductor watch failed")
        return _err(exc)


def _hold_status(response_text: str = "", **kwargs):
    del kwargs
    return _captain().hold_status_reply(response_text)


def _follow_up(run_id: str, record: dict) -> bool:
    ctx = _CTX
    if ctx is None:
        return False
    text = record.get("message") or f"Conductor: {record.get('title') or run_id} finished. Read it and tell the user."
    try:
        return bool(ctx.inject_message(text, session_key=record.get("session_key") or None))
    except Exception:
        logger.exception("conductor follow-up failed")
        return False


def register(ctx):
    global _CTX
    _CTX = ctx
    _captain().notify = _follow_up
    _captain().supervise()
    _captain().resume()
    ctx.register_skill(
        "lead",
        Path(__file__).resolve().parent / "skills" / "lead" / "SKILL.md",
        description="Use when leading coding agents. Prompts them and follows up.",
    )
    ctx.register_command(
        "conductor",
        handle_command,
        description="Show which coding agents Conductor can reach. /conductor peek <run_id> checks a run without a model. /conductor mode [drive|advise] shows or sets what happens when a run finishes unmet.",
        args_hint="[ssh-host | peek <run_id> | mode [drive|advise]]",
    )
    ctx.register_hook("transform_llm_output", _hold_status, hold_stream=True)
    handlers = {
        "conductor_doctor": _doctor,
        "conductor_run": _run,
        "conductor_mode": _mode,
        "conductor_read": _read,
        "conductor_peek": _peek,
        "conductor_wait": _wait,
        "conductor_steer": _steer,
        "conductor_stop": _stop,
        "conductor_watch": _watch,
    }
    for name, handler in handlers.items():
        ctx.register_tool(name=name, toolset="conductor", schema=_SCHEMAS[name], handler=handler)
