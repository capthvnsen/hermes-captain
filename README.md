# Conductor

Conductor starts a coding agent and waits until it finishes. Hermes gets one ping. No model sits in the wait.

It starts Claude in T3, waits, and peeks. A second peek stays quiet. It can also start Hermes, Claude Code, Codex, Grok Build, OpenCode, and Pycode. Claude and Grok can continue their last session. An unknown terminal command needs `-p`, `--print`, or `--prompt`. No other flags.

## Install

```bash
hermes plugins install https://github.com/capthvnsen/hermes-captain
hermes plugins enable conductor
```

The catalog requires a public repository. This repo is private until you flip it.

## Demo

1. `/conductor` shows which agents are reachable.
2. Start a T3 run on a machine where T3 is open. Conductor waits.
3. When it finishes, peek the run. The card shows the diff, or "No change."

Keep the video under 90 seconds. Show the run, not a slide.

## Limits

- It does not add approval-bypass flags. Orca may still use its own default when it opens a tab.
- Stopping a remote terminal agent is not wired. Stopping it here does not kill the agent on the other machine.
- Orca has no process to wait on. Name an existing workspace or the launch fails closed.
- A missing login comes back as a missing login.
