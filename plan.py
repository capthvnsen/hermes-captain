"""Build argv for each coding surface. No shell, no approval-bypass flags."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

PROMPT_FLAGS = {"-p", "--print", "--prompt"}
BIN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,40}$")
HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:@-]{0,200}$")
ORCA_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


@dataclass(frozen=True)
class Plan:
    surface: str
    argv: list[str]
    mode: str  # "background" waits in a log; "capture" returns the launch reply
    cwd: str | None
    summary: str
    name: str = ""


class PlanError(ValueError):
    pass


def ssh_argv(host: str, remote_argv: list[str], cwd: str | None = None) -> list[str]:
    """One remote command string. ssh joins argv, and zsh then parses the join."""
    if not HOST_RE.fullmatch(host):
        raise PlanError("host must be a plain SSH destination, like alex@100.72.158.127")
    script = 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; '
    if cwd:
        script += 'cd "$1" && shift && exec "$@"'
        words = ["bash", "-c", script, "bash", cwd, *remote_argv]
    else:
        script += 'exec "$@"'
        words = ["bash", "-c", script, "bash", *remote_argv]
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        host,
        " ".join(shlex.quote(part) for part in words),
    ]


def _host_prefix(host: str | None, cwd: str | None, argv: list[str]) -> list[str]:
    if not host:
        return argv
    return ssh_argv(host, argv, cwd)


def plan_run(
    agent: str,
    prompt: str,
    *,
    cwd: str | None = None,
    host: str | None = None,
    worker: str | None = None,
    orca_host: str | None = None,
    provider: str | None = None,
    worktree: str | None = None,
    prompt_flag: str | None = None,
    continue_session: bool = False,
    model: str | None = None,
) -> Plan:
    agent = (agent or "").strip().lower()
    prompt = (prompt or "").strip()
    if not prompt:
        raise PlanError("prompt is empty")
    if len(prompt) > 100_000:
        raise PlanError("prompt is too long")
    host = (host or "").strip() or None
    cwd = (cwd or "").strip() or None
    agent = {
        "claudecode": "claude",
        "claude-code": "claude",
        "codecs": "codex",
        "grock": "grok",
        "grok-build": "grok",
        "grokbuild": "grok",
        "open-code": "opencode",
        "opencode-ai": "opencode",
    }.get(agent, agent)
    if continue_session and agent not in {"claude", "grok"}:
        raise PlanError("continue is wired for claude and grok only")
    model = (model or "").strip() or None
    if model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,80}", model):
        raise PlanError("model must be a plain model id")
    if model and agent not in {"hermes", "claude", "grok"}:
        raise PlanError("model is wired for hermes, claude, and grok only")

    if agent == "hermes":
        argv = ["hermes", "chat", "-q", prompt, "-Q"]
        if model:
            argv = ["hermes", "chat", "-m", model, "-q", prompt, "-Q"]
        plan = Plan("cli", argv, "background", cwd, "hermes chat -q, no yolo flag")
    elif agent == "claude":
        argv = ["claude", "--continue", "-p", prompt] if continue_session else ["claude", "-p", prompt]
        if model:
            argv = ["claude", "--model", model, *argv[1:]]
        plan = Plan("cli", argv, "background", cwd, "claude print mode, permissions left on")
    elif agent == "codex":
        argv = ["codex", "exec", prompt]
        plan = Plan("cli", argv, "background", cwd, "codex exec, no bypass flag")
    elif agent == "grok":
        argv = ["grok", "--no-auto-update", "-c", "-p", prompt] if continue_session else ["grok", "--no-auto-update", "-p", prompt]
        if model:
            argv = ["grok", "--no-auto-update", "-m", model, *argv[2:]]
        plan = Plan("cli", argv, "background", cwd, "grok headless, approvals left on")
    elif agent == "opencode":
        plan = Plan("cli", ["opencode", "run", prompt], "background", cwd, "opencode run, no permission bypass flag")
    elif agent == "pycode":
        plan = Plan("cli", ["pycode", "-p", prompt], "background", cwd, "pycode prompt mode, confirm left on")
    elif agent == "orca":
        which = (worker or "claude").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", which):
            raise PlanError("orca worker name is not a plain agent id")
        argv = ["orca", "agent", "launch", "--agent", which, "--prompt", prompt, "--json"]
        if worktree:
            selector = worktree.strip()
            if not re.fullmatch(r"[A-Za-z0-9_.:/@-]{1,240}", selector):
                raise PlanError("orca worktree selector is not plain")
            argv.extend(["--worktree", selector])
        if orca_host:
            if not ORCA_HOST_RE.fullmatch(orca_host):
                raise PlanError("orca_host is not a plain selector")
            argv.extend(["--environment", orca_host])
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
        if provider:
            which = provider.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", which):
                raise PlanError("provider is not a plain id")
            argv.extend(["--provider", which])
        plan = Plan("t3", argv, "capture", cwd, "t3code handover with approval-required, not full-access")
    elif agent == "kimi":
        plan = Plan("cli", ["kimi", "-p", prompt], "background", cwd, "kimi prompt mode, no yolo flag")
    elif agent == "pi":
        plan = Plan("cli", ["pi", "--print", prompt], "background", cwd, "pi print mode")
    elif agent == "commandcode":
        plan = Plan("cli", ["commandcode", "-p", prompt], "background", cwd, "commandcode print mode, no trust flag")
    else:
        flag = (prompt_flag or "").strip()
        if not BIN_RE.fullmatch(agent):
            raise PlanError("agent must be a plain command name, or hermes, claude, codex, grok, opencode, pycode, orca, or t3")
        if flag not in PROMPT_FLAGS:
            raise PlanError("an unknown terminal agent needs prompt_flag set to -p, --print, or --prompt")
        plan = Plan("cli", [agent, flag, prompt], "background", cwd, f"{agent} print mode, no extra flags")

    prefixed = _host_prefix(host, cwd if host else None, plan.argv)
    local_cwd = None if host else cwd
    return Plan(plan.surface, prefixed, plan.mode, local_cwd, plan.summary, agent)


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


def plan_inspect(handle: str, *, host: str | None = None) -> Plan:
    handle = (handle or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", handle):
        raise PlanError("thread id has unexpected characters")
    argv = ["t3code", "--json", "threads", "inspect", "--thread", handle]
    return Plan("t3", _host_prefix(host, None, argv), "capture", None, "t3code threads inspect")


def plan_wait(handle: str, *, host: str | None = None, timeout: int = 600) -> Plan:
    handle = (handle or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", handle):
        raise PlanError("thread id has unexpected characters")
    seconds = max(1, min(int(timeout), 3600))
    argv = [
        "t3code", "--json", "threads", "wait",
        "--thread", handle,
        "--timeout", str(seconds),
        "--detail", "answers",
        "--max-chars", "1500",
    ]
    return Plan("t3", _host_prefix(host, None, argv), "capture", None, "t3code threads wait, no model")
