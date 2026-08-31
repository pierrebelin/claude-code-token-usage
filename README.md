# token-usage

What your Claude Code sessions cost, project by project, then what filled the context
inside each one.

The script reads `~/.claude/projects/**/*.jsonl` on your machine. No transcript, no
prompt, no filename ever leaves it. Python 3.10+, stdlib only, no package to install.

## Install

Clone anywhere, then symlink it as a Claude Code skill:

```bash
git clone https://github.com/pierrebelin/claude-code-token-usage.git ~/src/claude-code-token-usage
ln -s ~/src/claude-code-token-usage ~/.claude/skills/token-usage
```

The symlink name is what `/token-usage` resolves to — keep it in sync with the `name:` in
`SKILL.md`. A plain `cp -R` into `~/.claude/skills/token-usage/` works just as well; for a
single repository, `<repo>/.claude/skills/token-usage/` instead. Wherever it lands, the
usage data always comes from `~/.claude/projects/`.

```
claude-code-token-usage/
├── SKILL.md                  instructions for Claude Code
├── README.md                 this file
├── cc-usage.py               the tool
└── cc-usage-template.html    dashboard template (edit it freely)
```

The two files must stay side by side: `cc-usage.py` looks for the template in its own
directory. The template is **not** a viewable page — it holds no data until the script
fills it. Open the output (`--dashboard`) or the server (`--serve`), never the template.

Using it without Claude Code is fine, the script is standalone. Handy alias:

```bash
echo "alias ccusage='python3 ~/src/claude-code-token-usage/cc-usage.py'" >> ~/.zshrc
```

The examples below assume that alias.

## The four uses

### 1. Where the money goes

```bash
ccusage --days 30 --top 12
```

```
Project              Sess.   Input  Cache W  Cache R  Output    Cost $
backend-api            96  721.6k    25.5M     1.4G    5.9M  1,197.25
web-client            103   35.3k    22.9M     1.1G    4.5M    965.56
```

Grouped by git root; a worktree is folded into its parent repository.

### 2. Why this session was expensive

```bash
ccusage --days 30 --sessions 10        # spot the session
ccusage --session 46e2620d --top 15    # take it apart
```

```
Source                 Calls  Tokens added  Cost $  Share
Bash                     198        162.1k   20.88    28%
(startup)                  3         94.9k   20.04    27%
(response generation)    224        251.9k   20.50    28%
Read                       6         76.8k    7.03    10%
```

Then the individual additions, turn by turn, with their carry factor.

### 3. Live dashboard

```bash
ccusage --serve
```

Serves `http://127.0.0.1:8787/` on the loopback interface and **re-runs the analysis on
every load** (~0.8 s). A 7 d / 30 d / 90 d / 1 year window bar sits in the header.
`Ctrl+C` to stop, or `pkill -f "cc-usage.py --serve"`.

The home page carries only the overview: counters, cost per project, daily curve, then the
session list. **Each row opens its own page** (`/session?id=...`), which reads a single
transcript: ~0.1 s against ~1 s for the home page. The session page shows the per-tool
breakdown and every turn, with its own sort and its own filter. The back link keeps the
window and the filters.

Two control blocks sit above the session list: **Sessions** (sort by cost or date, text
filter on project or id, number of detailed sessions) and **Turns**, which lives on the
session page (sort by cost, tokens added, carry factor or chronological order, text filter
on tool or label). These are GET forms: the state lives in the URL,
`?days=30&sort=date-desc&q=backend&tsort=turn-asc&tq=Read` is shareable and
reloadable, and since nothing depends on JavaScript the page renders in any viewer.
Sorting happens server-side, on the numbers actually displayed.

The window links only work while the server is listening: a page served then reopened after
`Ctrl+C` gives a browser connection error, not a tool error.

Port and filters:

```bash
ccusage --serve 9000 --project backend-api
```

### 4. Frozen page

```bash
mkdir -p ~/.claude/usage
ccusage --days 30 --dashboard ~/.claude/usage/report.html && open ~/.claude/usage/report.html
```

Self-contained HTML file, data embedded, one single file: the list points at internal
anchors and each session's detail is included below. To archive or to send. To extract a
single session:

```bash
ccusage --dashboard session.html --focus 420f8978
```

No forms: with no server behind it, a control would have nothing to submit. Sorting and
filtering then go through the command-line options, which produce the same page:

```bash
ccusage --days 30 --sort-sessions date-desc --filter-turns Read --dashboard report.html
```

## All options

| Option | Effect |
|---|---|
| `--days N` / `--since YYYY-MM-DD` | analysis window |
| `--project <substring>` | filter on the project key |
| `--by repo\|cwd\|dir` | grouping key (default: git root) |
| `--split-worktrees` | count each worktree separately |
| `--session <prefix>` | break down a single session |
| `--sessions N` | the N costliest sessions |
| `--models` | per-model breakdown |
| `--daily` | per-day series |
| `--top N` | limit the display |
| `--serve [PORT]` | live dashboard (default 8787) |
| `--dashboard FILE` | write a self-contained HTML page |
| `--json` | machine-readable output |
| `--no-cost-state` | ignore the internal counters, recompute everything |
| `--no-fetch` | do not query LiteLLM for prices |
| `--sort-sessions <key>` | order of the detailed sessions: `date-desc` (default, most recent first), `date-asc`, `cost-desc`, `cost-asc`, `project-asc` |
| `--filter-sessions <text>` | only detail sessions whose project or id contains the text |
| `--sort-turns <key>` | order of the turns: `cost-desc` (default), `cost-asc`, `added-desc`, `carried-desc`, `turn-asc`, `turn-desc` |
| `--filter-turns <text>` | only keep turns whose label or tool contains the text |
| `--sessions-max N` | number of detailed sessions (default 14, max 60) |
| `--focus <prefix>` | with `--dashboard`, write the page for a single session |

## How reliable the figures are

Two sources, not one.

**Internal counter (`cost-state`).** Claude Code writes it into recent transcripts. It is
authoritative and the script takes it first. Those sessions carry the `exact` chip.

**Rebuilt from the transcript.** For older sessions. This is a **floor**: the transcript
keeps only the final branch of the conversation, so whatever was abandoned after a rewind
was still billed but no longer appears there, and the Haiku titling calls are never written
to it. On sessions where both exist, the measured gap is **-51 %**. Those sessions carry
the `floor` chip.

The page and the terminal output always state the coverage.

In session view, when the counter exists, the amounts are rescaled onto it: the
**breakdown** comes from the transcript's structure, the **level** comes from the counter.

## How attribution works

What a tool call costs is not what it returns, it is what it leaves in context. Every turn
resends the whole accumulation, so a heavy result inserted early is paid again on every
later turn.

Attribution measures the real context growth between two turns —
`ctx(i) - ctx(i-1) - output(i-1)`, read from the `usage` the API returns, with no tokenizer
estimate — then weights it by the number of turns that carried it. The sum of the
attributions reconstitutes the session's context cost exactly, by construction.

On a turn with several parallel calls, the delta is split in proportion to result size. A
compaction opens a new segment.

## What it reveals

Measured findings, not theoretical ones:

- **Producing code costs nothing.** On a real session: 108 `Edit` for $0.64, 102 `Read` for
  $16.86. What you pay for is loading the context.
- **Startup is a major line item.** System prompt + CLAUDE.md + tool definitions: 42.6 k
  tokens carried over 154 turns, $4.32 for a single session. Every kilo-token of CLAUDE.md
  has a recurring cost.
- **`/compact` is expensive.** The injected summary is carried by every later turn: $5.41
  for one measured compaction. `/clear` opens a fresh session and costs a fraction of that
  — prefer it when the subject changes outright.
- **Re-reads are not the problem.** After setting aside post-compaction reads (legitimate)
  and partial reads with `offset`, only 2 % of genuine redundancy was left.

## Limits

- Cost at the **public API list price**, not what a subscription bills. A comparative
  measure, not an invoice.
- The long-context rate (beyond 200 k tokens) is not modelled in the fallback computation.
- Subagents are not flagged `isSidechain` in these transcripts: their usage lands in the
  global counter but not in the per-tool breakdown.
- LiteLLM prices, cached 24 h in `~/.cache/cc-usage/`. Offline, the last cache is used;
  with no cache, tokens are counted and cost is 0, reported explicitly.
- The server listens on `127.0.0.1` only. Nothing is reachable from the network.
- Two outbound requests exist, both optional and neither carrying your data: the LiteLLM
  price file (skip with `--no-fetch`), and the Inter / JetBrains Mono web fonts the
  dashboard page loads from Google Fonts. For zero external request, delete the three
  `<link>` tags at the top of `cc-usage-template.html` — the system fallbacks
  (`system-ui`, `ui-monospace`) are already declared.

## Licence

[MIT](LICENSE).
