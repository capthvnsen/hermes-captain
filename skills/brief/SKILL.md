---
name: brief
description: Use when the user brings a new idea or project for coding agents. Turns a brain-dump into a brief.
---

# The brief

Coding agents waste the most time and tokens on a vague start. Your job here is to turn what the user said into a brief an agent can finish without guessing. Spend a little now so no worker spends a lot later.

## 1. Find the gaps

A brief needs six facts. Mark each as known, findable, or only the user knows:

- goal: the one outcome, in a sentence a stranger could check.
- context: why it matters and what already exists, in 1-3 lines.
- files: where the work lives (machine, folder, the files or area).
- verify: the exact command that proves it.
- avoid: what must not change.
- done_when: what the user will accept as finished.

## 2. Research before you ask

Never ask the user what you can find. Look up libraries and APIs yourself. For facts inside the repo, send a scout: conductor_run on the cheapest installed agent with the goal "Report the layout, the test command, and the files that deal with <topic>. Change nothing.", done_when "a report under 150 words". End your turn; the report comes back as one ping.

## 3. Interview once

Ask only what the user alone knows. One message, at most five questions, each with your recommended answer, so "yes to all" is a full reply. Ask about choices that change the work: scope edges, what proves it is done, what to leave alone. Do not ask about style or details an agent can settle.

## 4. Write it and show it

Show the six fields in ten lines or fewer and start when the user agrees. If they gave the go-ahead already, start and show the brief in the same message.

If the work has several pieces, list them under the brief: one outcome each, with its own files. Pieces sharing files go in order; the rest go at once.

## 5. Hand it off

Use the lead skill. Count the pieces first. Pieces that touch different files each get their own conductor_run, all started in the same turn: twelve pieces is twelve runs at once, and each pings on its own. One agent doing twelve pieces in a row is slower and one stall blocks them all. Use a single run only for one piece, or with lead true when the pieces share files and need one agent to coordinate them. Pass the six fields as fields, not as prose in prompt: Conductor builds the kickoff and keeps it cache-friendly.
