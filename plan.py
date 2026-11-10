"""Build argv for each coding surface. No shell, no approval-bypass flags."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

PROMPT_FLAGS = {"-p", "--print", "--prompt"}
BIN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,40}$")
HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:@-]{0,200}$")
ORCA_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
EVENT_KINDS = ("done", "needs-input")
RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
LONG_GOAL = 300  # characters; a goal this long is not a small, fully specified task
ALIASES = {
    "claudecode": "claude",
    "claude-code": "claude",
    "codecs": "codex",
    "grock": "grok",
    "grok-build": "grok",
    "grokbuild": "grok",
    "open-code": "opencode",
    "opencode-ai": "opencode",
    "cursor-agent": "cursor",
    "cursoragent": "cursor",
}


@dataclass(frozen=True)
class Plan:
    surface: str
    argv: list[str]
    mode: str  # "background" waits in a log; "capture" returns the launch reply
    cwd: str | None
    summary: str
    name: str = ""
    push: str = "polling"  # how the agent tells us it finished: polling, push, or push (done only)


class PlanError(ValueError):
    pass


def ssh_argv(host: str, remote_argv: list[str], cwd: str | None = None) -> list[str]:
    """One remote command string. ssh joins argv, and zsh then parses the join."""
    if not HOST_RE.fullmatch(host):
        raise PlanError("host must be a plain SSH destination, like dev@203.0.113.7")
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


TMUX_SCRIPT = r'''cwd="$1"; id="$2"; shift 2
d="$HOME/.conductor/runs"; mkdir -p "$d"; rm -f "$d/$id.exit"; : > "$d/$id.log"
command -v tmux >/dev/null || { echo "tmux is not installed on this host" >&2; exit 127; }
if ! tmux has-session -t conductor 2>/dev/null; then
  if command -v open >/dev/null; then open -na Ghostty --args -e bash -c "$(command -v tmux) new-session -d -s conductor; sleep 2"
  else tmux new-session -d -s conductor; fi
  for i in $(seq 1 30); do tmux has-session -t conductor 2>/dev/null && break; sleep 0.5; done
fi
tmux has-session -t conductor 2>/dev/null || { echo "could not start a tmux server on this host" >&2; exit 126; }
{ echo '#!/bin/bash'; [ -n "$cwd" ] && printf 'cd %q || exit 127\n' "$cwd"; printf '%q ' "$@"; echo "> $d/$id.log 2>&1"; echo "echo \$? > $d/$id.exit"; } > "$d/$id.sh"
tmux new-window -d -t conductor -n "cd-$id" "bash $d/$id.sh"
tail -n +1 -f "$d/$id.log" & t=$!
while [ ! -e "$d/$id.exit" ]; do sleep 1; done
sleep 1; kill $t 2>/dev/null
exit "$(cat "$d/$id.exit")"'''


def tmux_argv(host: str, cwd: str | None, argv: list[str], run_id: str) -> list[str]:
    """Run the agent in a tmux window on the host. The ssh call lasts as long as the window, so the existing
    wait logic works. The window uses the host's own login session, so a CLI that cannot log in over a bare ssh still can."""
    if not RUN_ID_RE.fullmatch(run_id or ""):
        raise PlanError("run id is not a plain id")
    return ssh_argv(host, ["bash", "-c", TMUX_SCRIPT, "bash", cwd or "", run_id, *argv])


def _hook_command(agent: str, kind: str) -> list[str]:
    """The hook as argv. The script travels as `python3 -c`, so it also works on the other machine."""
    return ["python3", "-c", Path(__file__).with_name("notify_hook.py").read_text(encoding="utf-8"), agent, kind]


def claude_settings() -> str:
    """Per-run hooks for `claude --settings`. Merged on top of the user's settings, which we never read or write."""
    def hook(kind: str) -> list[dict]:
        command = " ".join(shlex.quote(part) for part in _hook_command("claude", kind))
        return [{"hooks": [{"type": "command", "command": command}]}]
    return json.dumps({"hooks": {"Stop": hook("done"), "PermissionRequest": hook("needs-input"), "Notification": hook("needs-input")}})


def codex_notify() -> str:
    """Per-run `-c` override. Codex only emits turn-complete, so only done. JSON strings are valid TOML strings."""
    return "notify=[" + ",".join(json.dumps(part) for part in _hook_command("codex", "done")) + "]"


def plan_event_wait(run_id: str, host: str, seconds: int) -> Plan:
    """One ssh command: look for the run's event file once a second, print it, delete it. Exit 6 on timeout."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id or ""):
        raise PlanError("run id is not a plain id")
    script = (
        'd="${CONDUCTOR_EVENTS:-$HOME/.conductor/events}"; i=0; while :; do '
        f'for k in {" ".join(EVENT_KINDS)}; do f="$d/{run_id}.$k.json"; '
        f'if test -e "$f"; then cat "$f"; rm -f "$d"/{run_id}.*.json; exit 0; fi; done; '
        f"[ $i -ge {int(seconds)} ] && exit 6; i=$((i+1)); sleep 1; done"
    )
    return Plan("cli", ssh_argv(host, ["bash", "-c", script]), "capture", None, "ssh event wait, no model")


def _items(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value or [] if str(v).strip()]


def compose_prompt(
    goal: str,
    files=None,
    done_when: str = "",
    *,
    verify: str = "",
    context: str = "",
    avoid=None,
    skills=None,
    agent: str = "",
    delta: bool = False,
) -> tuple[str, list[str]]:
    """The model supplies the facts. This supplies the structure and the proven habits.
    Stable text first, the changing task last, so a continued session reuses its prompt cache.
    delta=True is for a continue: the rules are already in the session, so send the task only.
    Returns the prompt and the names of the parts in it."""
    goal = (goal or "").strip()
    if not goal:
        raise PlanError("goal is missing")
    agent = (agent or "").strip().lower()
    agent = ALIASES.get(agent, agent)
    files, avoid, skills = _items(files), _items(avoid), _items(skills)
    verify = (verify or "").strip()
    parts: list[str] = []
    lines: list[str] = []

    def add(name: str, text: str) -> None:
        parts.append(name)
        lines.append(text)

    if not delta:
        add("rules", "One rule: the smallest change that works. No extra features. No new dependencies.")
        add("stop", "If you are blocked or the goal is unclear, stop and say exactly what you need. Do not guess and do not widen the scope.")
        add("final_answer", "End with a final answer under 100 words: what changed, the verify output, what is left.")
        if skills and agent == "claude":
            add("skills", f"Use these skills if they are installed: {', '.join(skills)}. If pieces are independent, use parallel subagents.")
        elif skills and agent == "codex":
            add("skills", f"Follow AGENTS.md. Apply these skills if it lists them: {', '.join(skills)}.")
        lines.append("")
    context = "\n".join((context or "").strip().splitlines()[:3])
    if context:
        add("context", f"Context: {context}")
    add("goal", f"Goal: {goal}")
    if files:
        add("files", "Files in scope:\n" + "\n".join(f"- {f}" for f in files))
    if avoid:
        add("avoid", f"Never touch: {', '.join(avoid)}.")
    if verify:
        add("verify", f"Verify: run `{verify}`. Fix the work until it passes, then report its real output.")
    else:
        add("verify_find", "Verify: find the project's test command first, run it, fix the work until it passes, then report its real output.")
    if (done_when or "").strip():
        add("done_when", f"Done when: {done_when.strip()}")
    if not delta and (not files or len(goal) > LONG_GOAL):
        add("explore", "Read the relevant code first and state your plan in 3 lines before editing.")
    return "\n".join(lines), parts


def build_prompt(*args, **kwargs) -> str:
    return compose_prompt(*args, **kwargs)[0]


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
    run_id: str | None = None,
) -> Plan:
    agent = (agent or "").strip().lower()
    prompt = (prompt or "").strip()
    if not prompt:
        raise PlanError("prompt is empty")
    if len(prompt) > 100_000:
        raise PlanError("prompt is too long")
    host = (host or "").strip() or None
    cwd = (cwd or "").strip() or None
    agent = ALIASES.get(agent, agent)
    if continue_session and agent not in {"claude", "grok"}:
        raise PlanError("continue is wired for claude and grok only")
    model = (model or "").strip() or None
    if model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,80}", model):
        raise PlanError("model must be a plain model id")
    if model and agent not in {"hermes", "claude", "grok", "t3", "t3code"}:
        raise PlanError("model is wired for hermes, claude, grok, and t3 only")

    if agent == "hermes":
        argv = ["hermes", "chat", "-q", prompt, "-Q"]
        if model:
            argv = ["hermes", "chat", "-m", model, "-q", prompt, "-Q"]
        plan = Plan("cli", argv, "background", cwd, "hermes chat -q, no yolo flag")
    elif agent == "claude":
        argv = ["claude", "--continue", "-p", prompt] if continue_session else ["claude", "-p", prompt]
        if model:
            argv = ["claude", "--model", model, *argv[1:]]
        push = "polling"
        if run_id:
            argv = ["env", f"CONDUCTOR_RUN={run_id}", "claude", "--settings", claude_settings(), *argv[1:]]
            push = "push"
        plan = Plan("cli", argv, "background", cwd, "claude print mode, permissions left on", push=push)
    elif agent == "codex":
        argv = ["codex", "exec", prompt]
        push = "polling"
        if run_id:  # shortcut: this replaces the user's notify for this run only
            argv = ["env", f"CONDUCTOR_RUN={run_id}", "codex", "exec", "-c", codex_notify(), prompt]
            push = "push (done only)"
        plan = Plan("cli", argv, "background", cwd, "codex exec, no bypass flag", push=push)
    elif agent == "grok":
        argv = ["grok", "--no-auto-update", "-c", "-p", prompt] if continue_session else ["grok", "--no-auto-update", "-p", prompt]
        if model:
            argv = ["grok", "--no-auto-update", "-m", model, *argv[2:]]
        plan = Plan("cli", argv, "background", cwd, "grok headless, approvals left on")
    elif agent == "cursor":
        if prompt.startswith("-"):
            raise PlanError("a cursor prompt cannot start with a dash")
        plan = Plan("cli", ["cursor-agent", "--print", prompt], "background", cwd, "cursor-agent print mode, no force or trust flag")
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
        if model:
            argv.extend(["--model", model])
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
            raise PlanError("agent must be a plain command name, or hermes, claude, codex, grok, cursor, opencode, pycode, orca, or t3")
        if flag not in PROMPT_FLAGS:
            raise PlanError("an unknown terminal agent needs prompt_flag set to -p, --print, or --prompt")
        plan = Plan("cli", [agent, flag, prompt], "background", cwd, f"{agent} print mode, no extra flags")

    if host and run_id and plan.surface == "cli" and plan.mode == "background":
        prefixed = tmux_argv(host, cwd, plan.argv, run_id)
        return Plan(plan.surface, prefixed, plan.mode, None, plan.summary + ", in tmux on the host", agent, plan.push)
    prefixed = _host_prefix(host, cwd if host else None, plan.argv)
    local_cwd = None if host else cwd
    return Plan(plan.surface, prefixed, plan.mode, local_cwd, plan.summary, agent, plan.push)


def plan_steer(agent: str, handle: str, prompt: str, *, host: str | None = None) -> Plan:
    agent = (agent or "").strip().lower()
    handle = (handle or "").strip()
    prompt = (prompt or "").strip()
    if not handle or not prompt:
        raise PlanError("steer needs a handle and a prompt")
    if agent not in {"t3", "t3code"}:
        raise PlanError("steer is wired for T3 threads; start another run for claude, codex, or orca")
    if not re.fullmatch(r"[A-Za-z0-9_.:%-]{1,400}", handle):
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
        if not re.fullmatch(r"[A-Za-z0-9_.:%-]{1,400}", handle):
            raise PlanError("thread id has unexpected characters")
        argv = ["t3code", "threads", "interrupt", "--thread", handle]
        return Plan("t3", _host_prefix(host, None, argv), "capture", None, "t3code threads interrupt")
    return None


def plan_read(agent: str, handle: str, *, host: str | None = None) -> Plan | None:
    agent = (agent or "").strip().lower()
    handle = (handle or "").strip()
    if agent not in {"t3", "t3code"} or not handle:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.:%-]{1,400}", handle):
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
    if not re.fullmatch(r"[A-Za-z0-9_.:%-]{1,400}", handle):
        raise PlanError("thread id has unexpected characters")
    argv = ["t3code", "--json", "threads", "inspect", "--thread", handle]
    return Plan("t3", _host_prefix(host, None, argv), "capture", None, "t3code threads inspect")


def _orca_handle(handle: str) -> str:
    handle = (handle or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:%-]{1,400}", handle):
        raise PlanError("terminal handle has unexpected characters")
    return handle


def _orca_environment(argv: list[str], environment: str | None) -> list[str]:
    environment = (environment or "").strip()
    if not environment:
        return argv
    if not ORCA_HOST_RE.fullmatch(environment):
        raise PlanError("orca_host is not a plain selector")
    return [*argv, "--environment", environment]


def plan_orca_wait(
    handle: str,
    *,
    condition: str = "tui-idle",
    timeout_ms: int = 60_000,
    host: str | None = None,
    environment: str | None = None,
) -> Plan:
    """No model. `tui-idle` is the agent CLI settling; `exit` is the process ending."""
    if condition not in {"exit", "tui-idle"}:
        raise PlanError("orca wait is exit or tui-idle")
    ms = max(1, min(int(timeout_ms), 3_600_000))
    argv = [
        "orca", "terminal", "wait",
        "--terminal", _orca_handle(handle),
        "--for", condition,
        "--timeout-ms", str(ms),
        "--json",
    ]
    argv = _orca_environment(argv, environment)
    return Plan("orca", _host_prefix(host, None, argv), "capture", None, "orca terminal wait, no model")


def plan_orca_read(handle: str, *, host: str | None = None, environment: str | None = None) -> Plan:
    argv = ["orca", "terminal", "read", "--terminal", _orca_handle(handle), "--json"]
    argv = _orca_environment(argv, environment)
    return Plan("orca", _host_prefix(host, None, argv), "capture", None, "orca terminal read")


def plan_wait(handle: str, *, host: str | None = None, timeout: int = 600) -> Plan:
    handle = (handle or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:%-]{1,400}", handle):
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


def plan_list(host: str | None = None) -> Plan:
    argv = ["t3code", "--json", "threads", "list"]
    return Plan("t3", _host_prefix(host, None, argv), "capture", None, "t3code threads list")
