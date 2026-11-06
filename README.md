# Hermes Captain

A remote Hermes should not have to invent tmux, guess CLI flags, or add a bypass flag to get a coding agent moving. Captain is the one surface for that.

It commands four places a coding agent actually lives:

- **Claude Code** and **Codex**, as CLIs, in print or exec mode
- **Orca**, through `orca agent launch` against a running Orca runtime
- **T3 Code**, through `t3code handover` and `t3code threads send`

Pass an SSH host when those apps are on another machine. The host and the project path are arguments, not a shell string.

## What it will not do

It does not add `--dangerously-skip-permissions`, Codex bypass, or `--yolo`. T3's own CLI defaults a new thread to full access. Captain overrides that and launches with `--permission approval-required`.

Orca's own app may still pre-fill a coding agent's bypass flag when it opens a tab. Captain does not add one. That default is Orca's, and it is disclosed here so a catalog review can see it.

## Install

```bash
hermes plugins install https://github.com/capthvnsen/hermes-captain
hermes plugins enable hermes-captain
```

The catalog requires a public repository. This repo is private until you flip it.

## Tools

- `captain_doctor` — which of claude, codex, orca, and t3code are on this machine, or on an SSH host
- `captain_run` — start the work and return a run id. CLI runs stay in the background. Orca and T3 return the launch reply.
- `captain_read` — tail the log, or read the latest T3 turn
- `captain_steer` — T3 only, `threads send --if-busy steer`
- `captain_stop` — signal a local process, or `t3code threads interrupt`

`/captain` prints the doctor result.

## Demo

1. `/captain` on the machine where Claude is installed. It should say claude is ready.
2. Ask Hermes to run Captain against a tiny repo: agent `claude`, a one-line prompt, that directory.
3. `captain_read` shows the CLI output. No bypass flag in the command.
4. If Orca or T3 is on the other machine, `captain_doctor` with that SSH host shows them without launching anything.

Keep the video under 90 seconds. Show the command running, not a slide about it.

## Disclosure

- Runs `claude`, `codex`, `orca`, `t3code`, and `ssh` as subprocesses. No network of its own beyond the SSH host you name.
- Stores run records and CLI logs under the profile's `plugin-data/hermes-captain/`. Prompts and agent output land there. No telemetry.
- A missing binary returns an error. Internal errors return JSON instead of crashing Hermes.
- Remote doctor and remote runs use `ssh -o BatchMode=yes`. They fail closed if the key is not already accepted. They do not prompt for a password.
- Orca launch uses the documented `orca agent launch --agent --prompt --json` command. T3 uses `t3code --json handover` with `--permission approval-required` and `--open none`.

Commands were taken from Claude's `--help` on this install, the Orca CLI reference, and the t3code CLI readme. OpenCode is detected by doctor and not launched, because its flags were not verified.
