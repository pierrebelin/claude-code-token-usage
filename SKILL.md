---
name: token-usage
description: "Analyses Claude Code token usage and cost, per project and inside a single session. Answers questions like how much have I spent, which project costs the most, what filled the context, why was this session expensive."
---

# Claude Code token usage

Local tool that rebuilds usage from `~/.claude/projects/**/*.jsonl`.
Nothing leaves the machine. No dependency beyond the Python 3.10+ stdlib.

The script sits next to this file: `cc-usage.py`. So does the dashboard template:
`cc-usage-template.html`. The script looks for the template in its own directory — moving
one without the other breaks `--dashboard`.

## When to use it

- "what did this project / this week cost me"
- "which session was the most expensive"
- "what filled the context of this session"
- "why was this session so expensive"
- before optimising a CLAUDE.md, a skill, or an expensive working habit

## Commands

```bash
SKILL=~/.claude/skills/token-usage

# per-project view
python3 $SKILL/cc-usage.py --days 30 --top 12

# one session in detail (an id prefix is enough)
python3 $SKILL/cc-usage.py --session 46e2620d --top 15

# live dashboard, recomputed on every load
python3 $SKILL/cc-usage.py --serve

# frozen page, to archive or send
python3 $SKILL/cc-usage.py --days 30 --dashboard ~/.claude/usage/report.html
```

Options: `--days` / `--since`, `--project <substring>`, `--by repo|cwd|dir`,
`--split-worktrees`, `--models`, `--sessions N`, `--daily`, `--top N`, `--json`,
`--no-cost-state`, `--no-fetch`.

Dashboard sorting and filtering: `--sort-sessions date-desc|date-asc|cost-desc|cost-asc|project-asc`
(default `date-desc`, most recent first),
`--filter-sessions <text>` (project or id), `--sort-turns cost-desc|cost-asc|added-desc|carried-desc|turn-asc|turn-desc`,
`--filter-turns <text>` (tool or label), `--sessions-max N`, `--focus <prefix>`
(with `--dashboard`, writes the page for a single session).

In `--serve` mode the home page carries the overview and the session list; each row opens
`/session?id=...`, which reads a single transcript (~0.1 s) and unrolls every turn.
In a frozen page everything fits in one file: the list points at internal anchors. In `--serve`
mode, two GET forms expose the same settings and the state lives in the URL
(`?days=30&sort=date-desc&q=backend&tsort=turn-asc&tq=Read`): shareable, reloadable,
JavaScript-free.

## How to read the results

**Two sources of truth, not one.** Claude Code writes a `cost-state` counter into recent
transcripts: it is authoritative and the script takes it first. For earlier sessions, cost is
rebuilt from the transcript's `usage` blocks — and that is a **floor**, not a measurement:
the transcript keeps only the final branch of the conversation, so whatever was abandoned
after a rewind was still billed but no longer appears there, and the Haiku titling calls are
never written to it. On sessions where both exist, the measured gap is -51 %. Always state
the coverage ("N of M sessions are exact") rather than presenting a total as a measurement.

**Cost is not in the tool call, it is in what the call leaves in context.** Every turn
resends the whole accumulation. A 40 k-token `Read` at turn 5 of a 100-turn session is
re-read 95 times. `--session` attribution measures the real context growth between two turns
— `ctx(i) - ctx(i-1) - output(i-1)`, read from the API `usage`, with no tokenizer estimate —
then weights it by the number of turns that carried it. The "carried" column counts those
repetitions. On a turn with several parallel calls, the delta is split in proportion to
result size.

**Do not confuse re-reading with waste.** A re-read after a compaction is legitimate, the
context was emptied. A `Read` with `offset`/`limit` is a partial read, not a duplicate.
Separate the two before concluding there is redundancy.

## Facts verified on this machine

- `/clear` opens a **new session**: new `sessionId`, new transcript, and the command is
  written at the head of the new file. Context restarts at ~55 k of startup material, part
  of it served from cache.
- `/compact` stays in the **same session** and injects a summary carried by every later
  turn. Measured on a real session: $5.41 for a single compaction.
- The cache is written with a 1 h TTL, billed at 2x the input rate. The script reads the
  `cache_creation.ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens` split.
- Claude Code rewrites each assistant message several times (streaming deltas).
  Deduplication happens on `message.id`; without it, +87 % overcount.
- Subagents are not flagged `isSidechain` in these transcripts: their usage lands in the
  global counter but not in the per-tool breakdown.

## Limits to state

- Cost is at the **public API list price**, not what a subscription bills. It is a
  comparative measure.
- The long-context rate (beyond 200 k) is not modelled in the fallback computation.
- Prices come from LiteLLM, cached 24 h in `~/.cache/cc-usage/`. Offline, the last cache is
  used; with no cache, tokens are counted and cost is 0, and that is reported.
