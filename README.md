# Conductor

Hermes starts a coding agent in T3 Code, Orca, or a terminal. It waits without spending model tokens and delivers the agent's answer back into the chat.

## Install

```bash
hermes plugins install https://github.com/capthvnsen/hermes-captain
hermes plugins enable conductor
```

## Tools

- conductor_doctor
- conductor_run
- conductor_mode
- conductor_read
- conductor_peek
- conductor_wait
- conductor_steer
- conductor_stop
- conductor_watch

## Example

```text
conductor_run(agent="t3", provider="cursor", model="grok-4.7", goal="Fix the failing test")
```

## Tested live

On 2026-11-10, Hermes on a Linux VM led agents on a Mac over SSH.

- T3 Code, Cursor provider, model grok-4.7: started, waited with no model, answer came back.
- Orca terminal running Claude Code: started, waited with no model until the screen settled, answer came back.
- Claude Code in a plain terminal over SSH (tmux on the host): answer came back.
- Hermes CLI in a plain terminal over SSH: answer came back.
- Kimi, Pi, and Command Code without a working login: each came back within seconds as an error carrying the agent's own message.
- Orca chat launches are recorded as launched and not watched (Orca has no chat wait).
- Codex and Cursor CLI were not tested live (not logged in on the test machine).

## What it runs

- Shell commands: the agent CLI you name (`claude`, `codex`, `hermes`, `t3code`, `orca`, and others), plus `ssh` and `tmux` when a host is set. Commands are built as argument lists, never through a shell string on this machine.
- Background work: one waiting thread per run, in the Hermes process. It runs no model. It stops when the agent finishes. T3 and Orca waits also give up after four hours.
- Files: run records under the plugin's own data directory. On the machine the agent runs on, Conductor writes run logs and finish events under `~/.conductor/`, and a remote terminal run opens a window in a tmux session named `conductor`. For a Claude or Codex run, a per-run hook setting is passed on the command line; your global agent config is not edited.
- Network: only the `ssh` connection to the host you name. No third-party calls, no telemetry, no stored credentials.

## Limits

- Conductor adds no approval-bypass flags. An agent's own saved settings still apply.
- Stop works for a local process and for a T3 thread. Stopping a remote terminal or Orca run is not wired; that agent keeps running on the other machine.
- An Orca chat launch is not watched.
- A missing login comes back as a missing login.
