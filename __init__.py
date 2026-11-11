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


def _tool(fn):
    """Every tool answers with JSON and never raises into Hermes."""
    def handler(args=None, **kwargs):
        try:
            return json.dumps(fn(args or {}, **kwargs))
        except PlanError as exc:
            return _err(exc)
        except Exception as exc:
            logger.exception("conductor %s failed", fn.__name__)
            return _err(exc)
    return handler


def _text(args: dict, key: str) -> str | None:
    return str(args.get(key) or "").strip() or None


def _session(kwargs: dict) -> str:
    """The gateway's route key for the chat that asked. A session id is not one: a ping sent to it is dropped."""
    try:
        from gateway.session_context import bound_session_key
        key = bound_session_key()
    except Exception:
        key = ""
    return key or str(kwargs.get("session_key") or "")


@_tool
def _doctor(args, **kwargs):
    return {"ok": True, **_captain().doctor(_text(args, "host"))}


@_tool
def _run(args, **kwargs):
    prompt, parts = str(args.get("prompt") or ""), ["free-form"]
    if not prompt.strip() or any(args.get(k) for k in ("goal", "files", "done_when", "verify", "context", "avoid", "skills", "lead")):
        prompt, parts = compose_prompt(
            str(args.get("goal") or ""), args.get("files"), str(args.get("done_when") or ""),
            verify=str(args.get("verify") or ""), context=str(args.get("context") or ""),
            avoid=args.get("avoid"), skills=args.get("skills"),
            agent=str(args.get("agent") or ""), delta=bool(args.get("continue")), lead=bool(args.get("lead")),
        )
    record = _captain().run(
        str(args.get("agent") or ""),
        prompt,
        **{key: _text(args, key) for key in ("cwd", "host", "worker", "orca_host", "provider", "worktree", "prompt_flag", "model", "mode", "card")},
        continue_session=bool(args.get("continue")),
        keep=bool(args.get("keep")),
        session_key=_session(kwargs),
    )
    sent = {"lines": len(prompt.splitlines()), "parts": parts}
    if record.get("status") == "error":  # the launcher itself failed: nothing is running and nothing will ping
        return {"ok": False, "error": record.get("output") or "the launch failed", "run": record, "prompt": sent}
    return {"ok": True, "run": record, "prompt": sent}


@_tool
def _mode(args, **kwargs):
    captain = _captain()
    mode = str(args.get("mode") or "")
    return {"ok": True, "mode": captain.set_mode(mode) if mode else captain.get_mode()}


@_tool
def _read(args, **kwargs):
    return {"ok": True, "run": _captain().read(str(args.get("run_id") or ""))}


@_tool
def _peek(args, **kwargs):
    return {"ok": True, "card": _captain().peek(str(args.get("run_id") or ""))}


@_tool
def _wait(args, **kwargs):
    return _captain().arm_wait(str(args.get("run_id") or ""))


@_tool
def _steer(args, **kwargs):
    return _captain().steer(str(args.get("run_id") or ""), str(args.get("prompt") or ""))


@_tool
def _stop(args, **kwargs):
    return _captain().stop(str(args.get("run_id") or ""))


@_tool
def _watch(args, **kwargs):
    return _captain().watch_running(host=_text(args, "host"), session_key=_session(kwargs))


def handle_command(raw_args: str) -> str:
    raw = (raw_args or "").strip()
    if raw.split()[:1] == ["peek"]:
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
    try:
        report = _captain().doctor(raw or None)
    except PlanError as exc:
        return str(exc)
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
                "host": {"type": "string", "description": "Optional SSH destination, such as alex@mac. Empty means this machine."},
            },
        },
    },
    "conductor_run": {
        "name": "conductor_run",
        "description": "Start a coding agent and get one ping when it finishes, errors, stalls, or needs an approval. Do not poll. Any terminal agent works: named recipes, or a command name plus prompt_flag. orca and t3 open it in the user's app, where it can also be steered. Never adds approval-bypass flags.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "claude, codex, hermes, grok, cursor, opencode, gemini, qwen, copilot, amp, droid, kimi, pi, pycode, commandcode, orca, t3, or any other terminal command name with prompt_flag. An agent with no headless mode runs through orca (worker). The run record's events field says how it reports back: push (claude), push (done only) (codex), or polling (the rest; T3 and Orca wait on their own CLI)."},
                "prompt_flag": {"type": "string", "description": "For an unknown terminal agent only, how it takes a prompt: -p, --print, --prompt, -q, -m, --message, -x, exec, or run. No other flags are accepted."},
                "goal": {"type": "string", "description": "One outcome for this run. Conductor builds the kickoff from goal, context, files, avoid, verify, and done_when, so pass them every time and do not write the structure yourself. Wins over prompt. With continue, only the task part is sent, never the rules again."},
                "context": {"type": "string", "description": "1-3 lines: why, and what already exists. Not a repo dump."},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Files or folders in scope. Empty makes the agent read first and state a plan."},
                "avoid": {"type": "array", "items": {"type": "string"}, "description": "Paths or areas the agent must not touch."},
                "verify": {"type": "string", "description": "The exact command that proves the work (tests, build, lint). The agent runs it and fixes until it passes. Omit and it finds the project's test command."},
                "lead": {"type": "boolean", "description": "True makes this agent the technical lead: it splits the goal, hands pieces to its own subagents on the cheapest model that fits, and reviews their work instead of coding. For work with several pieces."},
                "skills": {"type": "array", "items": {"type": "string"}, "description": "Skill names the agent should use. Only claude and codex are told. Never invent a name."},
                "done_when": {"type": "string", "description": "What done looks like, checkable."},
                "prompt": {"type": "string", "description": "Free-form task, only when goal does not fit."},
                "cwd": {"type": "string", "description": "Absolute project directory. On a remote host it is passed as an argument, not a shell string."},
                "host": {"type": "string", "description": "SSH destination when the agent is on another machine. Empty means this machine."},
                "worker": {"type": "string", "description": "Orca only: which agent to launch in a terminal tab, any agent Orca knows. Defaults to claude."},
                "orca_host": {"type": "string", "description": "Orca only: --environment selector, such as local. Empty means the machine the command runs on."},
                "provider": {"type": "string", "description": "T3 only: provider instance id, such as claudeAgent. Empty uses T3's saved default."},
                "worktree": {"type": "string", "description": "Orca only: worktree selector, such as path:/Users/alex/Projects/app. Required when more than one workspace matches."},
                "continue": {"type": "boolean", "description": "Continue the last claude or grok session in this directory. Other agents refuse this."},
                "keep": {"type": "boolean", "description": "When this run finishes, ping Hermes to give the next task. Only if the user still wants the work to continue."},
                "model": {"type": "string", "description": "Plain model id for hermes, claude, grok, an Orca launch, or a T3 handover. Other agents refuse this. Not a price and not a bypass flag."},
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
        "description": "Read the latest output of a Conductor run: the last answer of a T3 thread, or the tail of a CLI run's log. Prefer conductor_peek; the finish ping already carries the result.",
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
        "description": "Send a follow-up into a T3 thread or an Orca terminal. The session keeps its context, so this is cheaper than a new run, and Conductor pings again when the new turn finishes. CLI runs are one-shot: start another run with continue instead. For T3 it returns idle_seconds and cache_warm (true under 5 minutes); when the cache is cold and the thread is long it adds advice for the user.",
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
        "description": "Stop a run: signals a local CLI run, closes the tmux window of a remote one, interrupts a T3 thread or an Orca terminal.",
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
    ctx.register_skill(
        "brief",
        Path(__file__).resolve().parent / "skills" / "brief" / "SKILL.md",
        description="Use when the user brings a new idea or project for coding agents. Turns a brain-dump into a brief.",
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
