# Claude Code Token Usage

What your Claude Code sessions cost, project by project, then what filled the context
inside each one.

![Usage dashboard: counters, daily curve and session list](docs/dashboard-overview.png)

Reads `~/.claude/projects/**/*.jsonl` locally. No transcript, no prompt, no filename ever
leaves your machine. Python 3.10+, stdlib only.

## Install as a skill

The repository *is* a Claude Code skill. Clone it into `~/.claude/skills/`:

```bash
git clone https://github.com/pierrebelin/claude-code-token-usage.git ~/.claude/skills/token-usage
```

Copy **everything** — `SKILL.md`, `cc-usage.py`, `cc-usage-template.html`, `README.md`.
The script looks for the template in its own directory, and the folder name is what
`/token-usage` resolves to.

Then ask in plain language:

```
> how much have I spent this month?
> which project costs me the most?
> what filled the context of session 46e2620d?
> open the usage dashboard
```

`SKILL.md` also tells Claude how to *read* the result: which sessions are exact and which
are a floor, why a re-read after a compaction is not waste, what the carry factor means.
That reading grid is why the skill install beats a bare script.

## Dashboard

```bash
python3 ~/.claude/skills/token-usage/cc-usage.py --serve
```

`http://127.0.0.1:8787/`, loopback only, re-runs the analysis on every load (~1 s,
plus ~0.5 s for the git correlation below — `--no-git` drops it).
Home page: counters, cost per project, daily curve, session list. Each row carries the
session's grade and opens its own page (~0.1 s), showing what that session could have
avoided, the per-tool breakdown and every turn. A lead appears when it accounts for at
least 10% of that session and $0.25 — or from $0.25 alone when it is unambiguous waste —
so small categories do not turn into noise.

![Session page: what filled the context, then turn by turn](docs/dashboard-session.png)

Sort and filter are GET forms: state lives in the URL, so
`?days=30&sort=date-desc&q=backend` is shareable, and nothing depends on JavaScript.

A frozen, self-contained HTML file instead of a server:

```bash
cc-usage.py --days 30 --dashboard report.html
cc-usage.py --dashboard session.html --focus 420f8978   # one session
```

## Terminal

The script is standalone:

```bash
echo "alias ccusage='python3 ~/.claude/skills/token-usage/cc-usage.py'" >> ~/.zshrc
```

```bash
ccusage --days 30 --top 12             # where the money goes, grouped by git root
ccusage --days 30 --sessions 10        # spot the costly session
ccusage --session 46e2620d --top 15    # take it apart
ccusage --days 30 --tools              # the same breakdown, summed over every session
ccusage --days 7 --triage               # the few costly patterns worth opening
ccusage --days 30 --yield              # what those sessions left in git
ccusage --days 30 --audit              # every session graded A to F
```

`--session` takes one session apart:

```
Source                 Calls  Tokens added  Cost $  Share
(replayed output)        221        248.7k   10.91    11%
Bash                     198        162.1k    8.72     9%
(compaction)               2         73.4k    5.81     6%
Read                       6         76.8k    2.94     3%
(startup)                  1         21.4k    2.56     3%
(response generation)    224        251.9k    8.56     9%
(subagents)                4         60.9k   57.85    58%
```

`--tools` runs that attribution across the whole window and sums it, which turns an
anecdote into a measure. It parses every transcript in the window — 2.4 s for 200
sessions here — so it sits behind its own flag.

## What it left in git

```bash
cc-usage.py --days 30 --yield
```

Cost answers *how much*. This answers *for what*. Each session is correlated with the
commits its repository received while it was running — from two minutes before its first
turn to half an hour after its last — and the outcome is read off git: the commit reached
the mainline, it was reverted afterwards, it was committed and never merged, or the
session ended without one.

```
Outcome                  Sess.    Cost $  Share
Landed on the mainline     113  1,860.71    71%
Reverted afterwards          0      0.00     0%
Committed, never merged      2     15.30     1%
No commit                   89    756.19    29%

$9.12 per commit that landed, across 204 commits.
```

A commit belongs to the last session that was still running when it was authored, so two
overlapping sessions never bank the same work. `No commit` is a category, not a verdict:
reading, debugging and planning sessions legitimately end without one. What is worth
watching is how much of the bill sits there, week after week.

The mainline is `origin/HEAD` when the repo publishes one, then `main` or `master`. Only
the repos that carry the most cost are queried — `--yield-max N`, eight by default — and
the run says how many sessions that left out. `--no-git` never shells out to git at all,
and the dashboard then drops the section.

## A grade per session

```bash
cc-usage.py --days 30 --audit
```

Every session gets a letter, A to F. It grades **what that run could have avoided**, as a
share of what the run cost — never its size. A twelve-hour session that wasted nothing
scores A; a two-dollar one that spent a third of itself rebuilding its cache does not.

```
Session grades since 2026-08-02 — 204 of 204 sessions read
  Median grade A (score 1.7)   A 145 · B 22 · C 19 · D 17 · F 1

Grade  Session   Project            Cost $  Score  What weighs most
F      74b68bee  HemicycleData       25.84     26  cache rebuilds 20% ($5.07)
D      0121a476  Configurator.Back   57.38     21  cache rebuilds 12% ($6.79)
D      716e04ac  Stid.Platform.SES   39.37     19  compaction 12% ($4.93)

Avoidable across those sessions: replayed output $346.98, cache rebuilds $102.20,
compaction $42.50, duplicate reads $1.00.
```

What weighs: files read out of `node_modules`, build output or lock files; the same file
read again inside one context, compaction-reset and offset reads excluded; a prompt cache
rebuilt after an idle gap; a compaction carried to the end of the session; replies still
replayed past a fifth of the bill; and CLAUDE.md files past 8 kB, priced against the
`(startup)` cost measured on that very session.

What does not: subagents, and a heavy tool result. Both are leads worth opening — they
appear in the list — but neither is a fault, so both score zero. A finding worth less than
a couple of dollars fades in proportionally: a rate is not a problem when there is nothing
to act on.

The instructions are read from `~/.claude/CLAUDE.md` and the ones above the session's own
directory, `@`-imports included. `--audit-max N` caps how many transcripts are parsed, 300
by default, and the header states how many that was. The dashboard needs no flag: the
sessions it lists are already parsed, so each row carries its letter and each session page
opens on its grade.

## Status line

```bash
cc-usage.py --statusline
```

Reads Claude Code's status JSON on stdin and prints one line — `$3.10 today · session
$0.42 · ctx 84k` — in about 0.1 s. Wire it into `~/.claude/settings.json`:

```json
{ "statusLine": { "type": "command",
                  "command": "python3 ~/.claude/skills/token-usage/cc-usage.py --statusline" } }
```

It appends `cache rebuilds $X` once that figure passes 50 cents, which is the one
number you can still act on while the session is running.

## Options

| Option | Effect |
|---|---|
| `--days N` / `--since YYYY-MM-DD` | analysis window |
| `--project <substring>` | filter on the project key |
| `--by repo\|cwd\|dir` | grouping key (default: git root) |
| `--split-worktrees` | count each worktree separately |
| `--session <prefix>` | break down a single session |
| `--sessions N` | the N costliest sessions |
| `--models` / `--daily` | per-model / per-day breakdown |
| `--tools` / `--tools-max N` | per-tool cost summed over the window (default cap: 500 sessions) |
| `--triage` | the three costly patterns most worth inspecting |
| `--yield` / `--yield-max N` | git outcome per session (default cap: 8 repos) |
| `--audit` / `--audit-max N` | grade every session A to F (default cap: 300 sessions) |
| `--no-git` | never shell out to git |
| `--statusline` | one line for Claude Code's `statusLine` hook |
| `--top N` | limit the display |
| `--serve [PORT]` | live dashboard (default 8787) |
| `--dashboard FILE` | write a self-contained HTML page |
| `--focus <prefix>` | with `--dashboard`, a single session |
| `--json` | machine-readable output |
| `--no-cost-state` | ignore the internal counters, recompute everything |
| `--no-fetch` | do not query LiteLLM for prices |
| `--sort-sessions <key>` | `date-desc` (default), `date-asc`, `cost-desc`, `cost-asc`, `project-asc` |
| `--sort-turns <key>` | `cost-desc` (default), `cost-asc`, `added-desc`, `context-desc`, `turn-asc`, `turn-desc` |
| `--filter-sessions <text>` / `--filter-turns <text>` | text filters |
| `--sessions-max N` | detailed sessions (default 5, max 60) |

## How reliable the figures are

**Internal counter (`cost-state`)**, written by Claude Code into recent transcripts, is
authoritative and taken first — those sessions carry the `exact` chip.

**Rebuilt from the transcript** for older ones. This is a **floor**: the transcript keeps
only the final branch, so whatever was abandoned after a rewind was still billed but no
longer appears, and Haiku titling calls are never written. Across the sessions where both
exist, weighted by cost, the transcript accounts for **83 %** of the counter. Those
sessions carry the `floor` chip.

In session view, when the counter exists, amounts are rescaled onto it: the breakdown
comes from the transcript, the level from the counter.

## How attribution works

What a tool call costs is not what it returns, it is what it leaves in context. Every turn
resends the whole accumulation, so a heavy result inserted early is paid again on every
later turn.

Attribution measures real context growth between two turns —
`ctx(i) - ctx(i-1) - output(i-1)`, read from the API's `usage`, no tokenizer estimate —
then weights it by the number of turns that carried it. On a turn with parallel calls the
delta is split in proportion to result size.

Two things re-base that accumulation, and the breakdown names them apart. A `/compact` is
read off the transcript's own `isCompactSummary` flag rather than guessed from a drop in
context size. A rewind leaves no flag at all: it only shows up as a context that shrank,
which is what the `(context reset)` line is.

The `output(i-1)` term above is Claude's own reply, which joins the conversation and is
resent as input on every later turn — billed once as generation, then again at the input
rate for as long as it is carried. That is the `(replayed output)` line, and on a long
session it is routinely the single largest one. With it in place the sum reconstitutes the
session's measured context cost exactly, to the token, on every session tested.

## What it reveals

Figures below come from `--days 30 --tools` on one machine — 194 sessions, so a measure
rather than an anecdote. Run it on yours; the shape holds, the numbers will not.

- **Producing code costs nothing.** 3 226 `Edit` calls: **$22**. 2 450 `Read` calls:
  **$325**. Fewer calls, fifteen times the cost. You pay for loading context, not for
  changing it.
- **Claude's own replies are the largest line.** `(replayed output)` — every reply resent
  as context on every later turn — came to **$597, 20 %** of everything. It grows with
  session length, not with reply size, and nothing but ending the session shortens it.
- **Startup is a recurring bill, not a one-off.** System prompt, CLAUDE.md and tool
  definitions: **$460 over 194 sessions**, ~$2.40 each, paid again on every turn of every
  one of them. Every kilo-token of CLAUDE.md has a running cost.
- **Subagents dominate when you use them.** 46 sessions out of 194 launched any, and those
  runs alone came to **$359**. On the costliest session on record they were 58 % of the
  bill, against 33 % for everything the main chain loaded.
- **`/compact` is expensive.** The injected summary is carried by every later turn: 31
  compactions, **$84**, roughly $2.70 each. `/clear` costs a fraction — prefer it when the
  subject changes outright.
- **Idle time has a price.** The prompt cache expires after five minutes; the next turn
  then rewrites the whole prefix at the write rate instead of reading it back at a tenth.
  222 turns did exactly that, **$264** of avoidable cost, after a pause of 11 minutes on
  median.
- **Re-reads are not the problem.** Post-compaction reads and `offset` reads set aside,
  only 2 % genuine redundancy was left.

## Limits

- Public API list price, not what a subscription bills. A comparative measure, not an
  invoice.
- Beyond 200 k tokens of prompt the API charges a premium rate. It is applied where
  LiteLLM publishes one; where it does not, the request is priced at the standard rate and
  the run says how many were affected, so the total is a floor for those.
- Day boundaries follow the machine's timezone, not the UTC the transcripts store.
- LiteLLM prices, cached 24 h in `~/.cache/cc-usage/`. Offline with no cache: tokens
  counted, cost 0, reported explicitly.
- `--yield` reads the local git history through read-only commands, and the grade reads
  the CLAUDE.md files a session loaded. Nothing is written, nothing is sent; a repository
  or a file that cannot be read is skipped rather than reported as a fault.
- Two optional outbound requests, neither carrying your data: the LiteLLM price file
  (`--no-fetch`) and the Google Fonts the page loads. For zero external request, delete
  the three `<link>` tags in `cc-usage-template.html` — system fallbacks are declared.

## Licence

[MIT](LICENSE).
