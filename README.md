# Conductor

![Conductor: make Hermes the CEO of your coding agents](assets/banner.png)

Conductor makes Hermes the CEO of your coding agents. You describe what you want once. Hermes fills the gaps, writes the brief, starts the agents, waits without spending a token, and comes back to you when the work is checked.

## How it works

![How a run goes: brain-dump, research and questions, the brief, 10+ agents at once, a zero-token wait, one ping per agent, the check and the follow-up](assets/workflow.jpg)

What each step is made of:

- **Research and interview.** The `brief` skill has Hermes look up what it can, send a cheap read-only scout into the repo, then ask you one round of at most five questions, each with a recommended answer.
- **The brief.** Conductor builds the kickoff from six fields and adds the habits that keep agents on track: the smallest change that works, stop and ask when blocked, a short final answer with the real verify output.
- **Many agents.** Each run is its own agent with its own wait. Hermes starts one run per piece in the same turn; twelve at once is tested. With `lead: true` a single agent acts as CTO and hands pieces to its own subagents.
- **The wait.** No model sits in the wait. Conductor watches the agent and puts one message into the chat that asked.
- **The check.** The `lead` skill has Hermes compare every report with done-when before it tells you the work is done.
- **The follow-up.** T3 Code and Orca runs are steered in place; Claude and Grok continue their last session. Hermes sends only the change, never the rules or the file list again.

It works with any terminal coding agent (Claude Code, Codex, Hermes, Grok, Cursor, OpenCode, Gemini, Qwen, Copilot, Amp, Droid, Kimi, Pi, or any command with a prompt flag), with T3 Code threads, and with Orca terminals. It never adds an approval-bypass flag.

A real run. One message asked Hermes to finish a half-built habit tracker. Hermes read the repo, started twelve agents in T3 Code, one per feature, and reported when all twelve had pinged back and the 24 tests passed:

![A real run: the Hermes chat on the left, twelve agent threads in T3 Code on the right](assets/demo-run.jpg)

The app those twelve agents finished:

![The finished habit tracker](assets/demo-app.png)

## Install

```bash
hermes plugins install https://github.com/capthvnsen/hermes-conductor
hermes plugins enable conductor
```

On a messaging gateway, Hermes drops the finish message unless you allow it. In the Hermes config:

```yaml
plugins:
  entries:
    conductor:
      allow_gateway_injection: true
```

Agents on other machines, and commands that are not built-in agents, work only when you list them. Both lists are empty by default:

```yaml
plugins:
  entries:
    conductor:
      settings:
        hosts: "alex@mac, build-box"   # SSH destinations Conductor may use
        commands: ""                   # extra agent commands, beyond the built-in ones
```

Then restart Hermes: the gateway, and the dashboard that serves the desktop app. A process that was already running does not load the plugin.

## Try it

1. `/conductor` shows which agents Hermes can reach.
2. Tell Hermes what you want built and where. It asks what is missing, then starts the agents.
3. Wait. Hermes messages you when an agent needs an approval and when the work is done.

## What it does on your machines

- Every start and every steer asks you first. `conductor_run` and `conductor_steer` go through Hermes's approval prompt, which shows the agent, the folder, the host and the goal. If you answer "session" or "always", Hermes stops asking for that agent in that folder on that host, or for that run. Unattended runs such as cron have nobody to approve, so they start nothing, unless you turned Hermes's own approvals off (`approvals.cron_mode: approve`, `approvals.mode: off`, or yolo).
- Hermes can only launch the built-in agents, on this machine. Other SSH hosts and other commands need your `hosts` and `commands` settings above. The list is checked again on every read, wait, steer and stop of a saved run. Conductor gives the model no tool that changes it.
- Only you can turn on drive mode, which lets Hermes keep steering a run without asking: type `/conductor mode drive`. The model can read the mode, not set it.
- Runs commands: the coding agent, `git status` in the run's folder, `ps` for a run's own process, `hermes kanban` when you pass a card, and `ssh` and `tmux` for a listed host. Arguments only, never a shell string built from your text. It never adds an approval-bypass flag. Orca starts an agent with the permission mode you set in Orca.
- Children get a minimal environment: `PATH`, `HOME`, `USER`, locale, `TERM`, Conductor's own variables, and only the variables that agent needs (for example `ANTHROPIC_API_KEY` for Claude Code, `SSH_AUTH_SOCK` for `ssh`). They do not inherit the other keys Hermes has loaded.
- An agent that needs an approval of its own pings once and pings again when it finishes.
- Network: only `ssh` to the hosts you listed. No telemetry, no update checks.
- Background work: one wait thread per run inside Hermes, and a detached agent process or tmux window for a CLI run.
- Files: run records and logs in the plugin's Hermes data folder, and event files, exit codes and logs under `~/.conductor` on the machine that runs the agent.
- Credentials: none read or stored. Agents use their own logins.

## Tests

```bash
uv run --no-project --with pytest python -m pytest tests
python3 tests/test_captain.py   # the same tests, without pytest
```
