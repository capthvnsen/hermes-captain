"""Build argv for each coding surface. No shell, no approval-bypass flags."""

from __future__ import annotations

import re
from dataclasses import dataclass

HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:@-]{0,200}$")
ORCA_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


@dataclass(frozen=True)
class Plan:
    surface: str
    argv: list[str]
    mode: str  # "background" waits in a log; "capture" returns the launch reply
    cwd: str | None
    summary: str


class PlanError(ValueError):
    pass


def _host_prefix(host: str | None, cwd: str | None, argv: list[str]) -> list[str]:
    if not host:
        return argv
    if not HOST_RE.fullmatch(host):
        raise PlanError("host must be a plain SSH destination, like alex@100.72.158.127")
    remote = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        host,
        "--",
    ]
    if cwd:
        # cwd is an argument to a fixed script, never interpolated.
        return remote + ["bash", "-c", 'cd "$1" && shift && exec "$@"', "bash", cwd, *argv]
    return remote + argv


def plan_run(
    agent: str,
    prompt: str,
    *,
    cwd: str | None = None,
    host: str | None = None,
    worker: str | None = None,
    orca_host: str | None = None,
) -> Plan:
    agent = (agent or "").strip().lower()
    prompt = (prompt or "").strip()
    if not prompt:
        raise PlanError("prompt is empty")
    if len(prompt) > 100_000:
        raise PlanError("prompt is too long")
    host = (host or "").strip() or None
    cwd = (cwd or "").strip() or None

    if agent == "claude":
        argv = ["claude", "-p", prompt]
        plan = Plan("cli", argv, "background", cwd, "claude print mode, permissions left on")
    elif agent == "codex":
        argv = ["codex", "exec", prompt]
        plan = Plan("cli", argv, "background", cwd, "codex exec, no bypass flag")
    elif agent == "orca":
        which = (worker or "claude").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", which):
            raise PlanError("orca worker name is not a plain agent id")
        argv = ["orca", "agent", "launch", "--agent", which, "--prompt", prompt, "--json"]
        if orca_host:
            if not ORCA_HOST_RE.fullmatch(orca_host):
                raise PlanError("orca_host is not a plain selector")
            argv.extend(["--host", orca_host])
        plan = Plan("orca", argv, "capture", cwd, "orca agent launch, json, no extra bypass flag")
    elif agent in {"t3", "t3code"}:
        argv = [
            "t3code",
            "--json",
            "handover",
            "--prompt", prompt,
            "--permission", "approval-required",
            "--open", "none",
            "--checkout", "current",
        ]
        plan = Plan("t3", argv, "capture", cwd, "t3code handover with approval-required, not full-access")
    else:
        raise PlanError("agent must be claude, codex, orca, or t3")

    prefixed = _host_prefix(host, cwd if host else None, plan.argv)
    local_cwd = None if host else cwd
    return Plan(plan.surface, prefixed, plan.mode, local_cwd, plan.summary)


def plan_steer(agent: str, handle: str, prompt: str, *, host: str | None = None) -> Plan:
    agent = (agent or "").strip().lower()
    handle = (handle or "").strip()
    prompt = (prompt or "").strip()
    if not handle or not prompt:
        raise PlanError("steer needs a handle and a prompt")
    if agent not in {"t3", "t3code"}:
        raise PlanError("steer is wired for T3 threads; start another run for claude, codex, or orca")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", handle):
        raise PlanError("thread id has unexpected characters")
    argv = [
        "t3code", "--json", "threads", "send",
        "--thread", handle,
        "--prompt", prompt,
        "--if-busy", "steer",
    ]
    return Plan("t3", _host_prefix(host, None, argv), "capture", None, "t3code threads send --if-busy steer")


def plan_stop(agent: str, handle: str, *, host: str | None = None) -> Plan | None:
    """Return a capture plan, or None when the caller should signal a local process."""
    agent = (agent or "").strip().lower()
    handle = (handle or "").strip()
    if agent in {"t3", "t3code"} and handle:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", handle):
            raise PlanError("thread id has unexpected characters")
        argv = ["t3code", "threads", "interrupt", "--thread", handle]
        return Plan("t3", _host_prefix(host, None, argv), "capture", None, "t3code threads interrupt")
    return None


def plan_read(agent: str, handle: str, *, host: str | None = None) -> Plan | None:
    agent = (agent or "").strip().lower()
    handle = (handle or "").strip()
    if agent not in {"t3", "t3code"} or not handle:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", handle):
        raise PlanError("thread id has unexpected characters")
    argv = [
        "t3code", "--json", "threads", "read",
        "--thread", handle,
        "--detail", "answers",
        "--last-turn",
        "--max-chars", "8000",
    ]
    return Plan("t3", _host_prefix(host, None, argv), "capture", None, "t3code threads read")
