---
name: token-usage
description: "Analyses Claude Code token usage and cost, per project and inside a single session. Answers questions like how much have I spent, which project costs the most, what filled the context, why was this session expensive."
---

# Claude Code and Codex usage

Local tool that rebuilds usage from `~/.claude/projects/**/*.jsonl`.
Nothing leaves the machine. No dependency beyond the Python 3.10+ stdlib.

The script sits next to this file: `cc-usage.py`. So does the dashboard template:
`usage-dashboard-template.html`. The script looks for the template in its own directory — moving
one without the other breaks `--dashboard`.

## When to use it

- "what did this project / this week cost me"
- "which session was the most expensive"
- "what filled the context of this session"
- "why was this session so expensive"
- before optimising a CLAUDE.md, a skill, or an expensive working habit
- "did that week produce anything" — what landed in git against what it cost
- "what did this session waste" — a grade per session, A to F, and what weighs on it

## Commands

```bash
SKILL=~/.claude/skills/token-usage

# per-project view
python3 $SKILL/cc-usage.py --days 30 --top 12

# one session in detail (an id prefix is enough)
python3 $SKILL/cc-usage.py --session 46e2620d --top 15

# per-tool cost summed over every session in the window
python3 $SKILL/cc-usage.py --days 30 --tools

# three costly patterns worth opening next
python3 $SKILL/cc-usage.py --days 7 --triage

# what those sessions left in git: landed, reverted, never merged, no commit
python3 $SKILL/cc-usage.py --days 30 --yield

# every session in the window graded A to F on what it could have avoided
python3 $SKILL/cc-usage.py --days 30 --audit

# combined live dashboard, recomputed on every load (Claude Code selected by default)
python3 $SKILL/usage-dashboard.py --serve

# frozen page, to archive or send
python3 $SKILL/cc-usage.py --days 30 --dashboard ~/.claude/usage/report.html
```

Options: `--days` / `--since`, `--project <substring>`, `--by repo|cwd|dir`,
`--split-worktrees`, `--models`, `--sessions N`, `--daily`, `--tools` / `--tools-max N`, `--triage`,
`--yield` / `--yield-max N`, `--audit` / `--audit-max N`, `--no-git`,
`--top N`, `--json`, `--no-cost-state`, `--no-fetch`, `--statusline`.

`--tools` is the one to reach for when the question is about a habit rather than a
session ("is Read expensive for me", "what do subagents cost me"): it runs the
per-session attribution over the whole window and sums it. It parses every transcript,
so it is the slow one — a couple of seconds for a few hundred sessions.

Dashboard sorting and filtering: `--sort-sessions date-desc|date-asc|cost-desc|cost-asc|project-asc`
(default `date-desc`, most recent first),
`--filter-sessions <text>` (project or id), `--sort-turns cost-desc|cost-asc|added-desc|context-desc|turn-asc|turn-desc`,
`--filter-turns <text>` (tool or label), `--sessions-max N`, `--focus <prefix>`
(with `--dashboard`, writes the page for a single session).

In `--serve` mode the home page carries the overview and the session list; each row shows
that session's grade and opens `/session?id=...`, which reads a single transcript
(~0.1 s), starts with the grade and the leads behind it, then unrolls every turn. A lead
appears from 10% of that session and $0.25, or from $0.25 alone when it is unambiguous
waste, so small categories do not turn into noise.
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
never written to it. Across sessions where both exist, weighted by cost, the transcript
accounts for 83 % of the counter. Always state the coverage ("N of M sessions are exact")
rather than presenting a total as a measurement.

**Cost is not in the tool call, it is in what the call leaves in context.** Every turn
resends the whole accumulation. A 40 k-token `Read` at turn 5 of a 100-turn session is
re-read 95 times. `--session` attribution measures the real context growth between two turns
— `ctx(i) - ctx(i-1) - output(i-1)`, read from the API `usage`, with no tokenizer estimate —
then weights it by the number of turns that carried it. The "carried" column counts those
repetitions. On a turn with several parallel calls, the delta is split in proportion to
result size.

**Four lines carry no tool name, and each means something different.** `(startup)` is the
system prompt, CLAUDE.md and the tool definitions, loaded once and carried by every turn.
`(compaction)` is what a `/compact` rebuilt, read off the transcript's own flag, not
guessed. `(context reset)` is a rewind — the tokens were already loaded once, the line is
the re-baselining. `(replayed output)` is Claude's own replies resent as input on every
later turn: billed once as generation, then again at the input rate for as long as they are
carried, which makes it grow with session length rather than with reply size. On a long
session it is often the largest single line, and the only cure is a shorter session.

**Subagents run their own context.** Their transcripts live in
`<project>/<session>/subagents/agent-*.jsonl`, not in the parent file. What the main
session paid is only the report handed back — the `Agent` line; what the run itself cost is
the `(subagents)` line and the per-agent table. On fan-out sessions they routinely outweigh
the whole main chain, so never answer "why was this session expensive" without looking at
them.

**An idle gap costs money.** The prompt cache entry lives five minutes. After a longer
pause the next turn rewrites the whole prefix at the write rate instead of reading it back
at a tenth of it. The script reports those turns, the tokens rewritten and the avoidable
cost. It is the one finding a user can act on without changing how they work — only when.

**`--yield` answers "for what", not "how much".** It correlates each session with the
commits its repo received while it ran — two minutes before the first turn to half an hour
after the last — then reads the outcome off git: landed on the mainline, reverted
afterwards, committed but never merged, or no commit at all. A commit is attributed to the
last session still running when it was authored, so overlapping sessions never bank the
same work. Never present `no commit` as waste: reading, debugging and planning sessions end
that way. The figure that means something is how much of the bill sits in that category
week after week, and whether it moves.

**`--audit` grades sessions, one by one.** There is no machine-wide score: a letter A to
F belongs to a run. It measures what that run could have avoided — junk or duplicate
reads, a cache rebuilt after an idle gap, a compaction carried to the end, replies
replayed past a fifth of the bill, instructions past 8 kB priced against the `(startup)`
cost measured on that session — as a **share of what the session cost**, never its size.
A long, expensive session that wasted nothing scores A, and saying otherwise is the one
mistake to avoid when reporting a grade. Subagents and a heavy tool result appear as
leads but weigh nothing: they are worth opening, not faults. Findings under a couple of
dollars are damped, because a bad rate on small change is not a problem to act on. The
dashboard needs no flag for any of this — the letter is on every row of the session list.

**Do not confuse re-reading with waste.** A re-read after a compaction is legitimate, the
context was emptied. A `Read` with `offset`/`limit` is a partial read, not a duplicate.
Separate the two before concluding there is redundancy.

## Facts verified on this machine

- `/clear` opens a **new session**: new `sessionId`, new transcript, and the command is
  written at the head of the new file. Context restarts at ~55 k of startup material, part
  of it served from cache.
- `/compact` stays in the **same session** and injects a summary carried by every later
  turn. Detected on the transcript's `isCompactSummary` flag, so it is named rather than
  inferred. Over 31 compactions on this machine: $2.70 each on average, $5.41 on the worst.
- The cache is written with a 1 h TTL, billed at 2x the input rate. The script reads the
  `cache_creation.ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens` split, and takes
  the 1 h rate from LiteLLM's `cache_creation_input_token_cost_above_1hr` when it has one.
- Claude Code rewrites each assistant message several times (streaming deltas).
  Deduplication happens on `message.id`; without it, +87 % overcount.
- Subagent turns carry `isSidechain: true`, but only inside their own transcript, one
  directory below the session file. The parent transcript never mentions their usage.

## Limits to state

- Cost is at the **public API list price**, not what a subscription bills. It is a
  comparative measure.
- Past 200 k tokens of prompt the API charges a premium rate. It is applied where LiteLLM
  publishes one; where it does not, the run says how many requests were affected and their
  cost is a floor.
- Day boundaries follow the machine's timezone, not the UTC stored in the transcripts.
- Prices come from LiteLLM, cached 24 h in `~/.cache/cc-usage/`. Offline, the last cache is
  used; with no cache, tokens are counted and cost is 0, and that is reported.
