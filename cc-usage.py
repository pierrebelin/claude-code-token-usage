#!/usr/bin/env python3
"""Claude Code token usage, broken down by project.

Reads ~/.claude/projects/**/*.jsonl locally. No external service beyond the
LiteLLM price refresh (24h cache, --no-fetch to skip it).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path

from dashboard_template import render_dashboard_document, source_tabs
from dashboard_model import (claude_dashboard_model, clock as _clock,
                             elapsed_label as _elapsed, metadata_list,
                             render_dashboard_overview, render_session_view,
                             shade as _shade, span_label as _span)
from session_grade import (GRADE_WEIGHTS, grade_for, grade_session, grade_weight,
                           is_junk)

PROJECTS_DIR = Path.home() / ".claude" / "projects"
CACHE_PATH = Path.home() / ".cache" / "cc-usage" / "litellm-prices.json"
PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
CACHE_TTL = 24 * 3600
LONG_CACHE_MULTIPLIER = 2.0
LONG_CONTEXT_THRESHOLD = 200_000
FREE_MODELS = {"<synthetic>"}


class Tier:
    """The five rates the API bills on, for one pricing tier.

    Anthropic prices a request in two tiers: the standard one, and a premium one
    that applies to the whole request once its prompt goes past 200 k tokens.
    """

    __slots__ = ("input", "output", "cache_write", "cache_write_1h", "cache_read")

    def __init__(self, input_, output, cache_write, cache_write_1h, cache_read):
        self.input = input_
        self.output = output
        self.cache_write = cache_write if cache_write is not None else input_ * 1.25
        self.cache_write_1h = (cache_write_1h if cache_write_1h is not None
                               else input_ * LONG_CACHE_MULTIPLIER)
        self.cache_read = cache_read if cache_read is not None else input_ * 0.1


class Rates:
    """A model's price list: the standard tier, plus the >200 k tier if it has one."""

    __slots__ = ("base", "long", "source")

    def __init__(self, base: Tier, long_: Tier | None, source: str):
        self.base = base
        self.long = long_
        self.source = source

    def tier(self, input_tokens: int) -> Tier:
        if self.long is not None and input_tokens > LONG_CONTEXT_THRESHOLD:
            return self.long
        return self.base

    # The standard tier stays reachable as an attribute: plenty of call sites only
    # ever need a headline rate, and every model has a base tier.
    @property
    def input(self):
        return self.base.input

    @property
    def output(self):
        return self.base.output


def load_prices(allow_fetch: bool) -> dict:
    fresh = CACHE_PATH.exists() and time.time() - CACHE_PATH.stat().st_mtime < CACHE_TTL
    if fresh:
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            pass
    if allow_fetch:
        try:
            with urllib.request.urlopen(PRICES_URL, timeout=20) as r:
                raw = json.loads(r.read().decode())
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(raw))
            return raw
        except Exception as exc:
            print(f"[warning] remote prices unavailable ({exc})", file=sys.stderr)
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def build_rate_table(prices: dict) -> dict[str, Rates]:
    """Turns the LiteLLM price file into one Rates per model.

    LiteLLM carries the long-context tier under `*_above_200k_tokens` keys and the
    1 h cache-write rate under `cache_creation_input_token_cost_above_1hr`. Both
    are optional: a model without them simply has no premium tier, which is what
    Anthropic bills.
    """
    table = {}
    for name, entry in prices.items():
        if not isinstance(entry, dict):
            continue
        cost_in = entry.get("input_cost_per_token")
        cost_out = entry.get("output_cost_per_token")
        if cost_in is None or cost_out is None:
            continue
        base = Tier(
            cost_in,
            cost_out,
            entry.get("cache_creation_input_token_cost"),
            entry.get("cache_creation_input_token_cost_above_1hr"),
            entry.get("cache_read_input_token_cost"),
        )
        long_in = entry.get("input_cost_per_token_above_200k_tokens")
        long_out = entry.get("output_cost_per_token_above_200k_tokens")
        long_tier = None
        if long_in is not None and long_out is not None:
            long_tier = Tier(
                long_in,
                long_out,
                entry.get("cache_creation_input_token_cost_above_200k_tokens"),
                entry.get("cache_creation_input_token_cost_above_1hr_above_200k_tokens"),
                entry.get("cache_read_input_token_cost_above_200k_tokens"),
            )
        table[name] = Rates(base, long_tier, name)
    return table


def resolve_rates(model: str, table: dict[str, Rates], memo: dict) -> Rates | None:
    if model in memo:
        return memo[model]
    found = table.get(model)
    if found is None:
        base = model.split("[")[0]
        found = table.get(base)
    if found is None:
        candidates = [k for k in table if base in k and "/" not in k and "." not in k]
        if candidates:
            found = table[min(candidates, key=len)]
    if found is None:
        for family in ("opus", "sonnet", "haiku", "fable"):
            if family in model:
                candidates = [
                    k for k in table
                    if k.startswith(f"claude-{family}") and "/" not in k
                ]
                if candidates:
                    found = table[max(candidates)]
                break
    memo[model] = found
    return found


_REPO_CACHE: dict[str, str] = {}


def _worktree_parent(dot_git: Path) -> Path | None:
    try:
        first = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    target = Path(first.split(":", 1)[1].strip())
    parts = target.parts
    if "worktrees" in parts:
        idx = len(parts) - 1 - parts[::-1].index("worktrees")
        common = Path(*parts[:idx])
        if common.name == ".git":
            return common.parent
    return None


def repo_root(cwd: str, transcript_dir: str, split_worktrees: bool) -> str:
    """Repo root for a cwd. A worktree resolves back to its main repo."""
    key = f"{cwd}|{split_worktrees}"
    if key in _REPO_CACHE:
        return _REPO_CACHE[key]
    resolved = ""
    if cwd:
        current = Path(cwd)
        for candidate in (current, *current.parents):
            dot_git = candidate / ".git"
            if dot_git.is_dir():
                resolved = str(candidate)
                break
            if dot_git.is_file():
                parent = None if split_worktrees else _worktree_parent(dot_git)
                resolved = str(parent or candidate)
                break
    if not resolved:
        slug = transcript_dir.split("--claude-worktrees-")[0] if not split_worktrees else transcript_dir
        resolved = slug.replace("-", "/")
    _REPO_CACHE[key] = resolved
    return resolved


def usage_parts(usage: dict):
    """(uncached input, output, cache read, 5 min cache write, 1 h cache write).

    The 1 h split only exists in `cache_creation`; older transcripts carry the
    lump sum in `cache_creation_input_tokens`, which is always a 5 min write.
    """
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    created = int(usage.get("cache_creation_input_tokens") or 0)
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        w1h = int(split.get("ephemeral_1h_input_tokens") or 0)
        w5m = int(split.get("ephemeral_5m_input_tokens") or 0)
        if w1h + w5m == 0:
            w5m = created
    else:
        w1h, w5m = 0, created
    return inp, out, read, w5m, w1h


def usage_tier(usage: dict, rates: Rates) -> Tier | None:
    """The tier the API billed this request at, chosen on its prompt size."""
    if rates is None:
        return None
    inp, _out, read, w5m, w1h = usage_parts(usage)
    return rates.tier(inp + read + w5m + w1h)


def input_cost(usage: dict, rates: Rates) -> float:
    tier = usage_tier(usage, rates)
    if tier is None:
        return 0.0
    inp, _out, read, w5m, w1h = usage_parts(usage)
    return (inp * tier.input + w5m * tier.cache_write
            + w1h * tier.cache_write_1h + read * tier.cache_read)


def output_cost(usage: dict, rates: Rates) -> float:
    """Output is billed at the tier the request's *input* size selected."""
    tier = usage_tier(usage, rates)
    if tier is None:
        return 0.0
    return int(usage.get("output_tokens") or 0) * tier.output


class Bucket:
    __slots__ = ("input", "output", "cache_write_5m", "cache_write_1h",
                 "cache_read", "cost", "messages", "sessions", "unpriced",
                 "long_context", "long_unpriced", "first", "last")

    def __init__(self):
        self.input = self.output = 0
        self.cache_write_5m = self.cache_write_1h = self.cache_read = 0
        self.cost = 0.0
        self.messages = 0
        self.sessions = set()
        self.unpriced = 0
        self.long_context = 0
        self.long_unpriced = 0
        self.first = self.last = None

    @property
    def total_input(self):
        return self.input + self.cache_write_5m + self.cache_write_1h + self.cache_read

    def add(self, usage, rates, session, ts):
        self.messages += 1
        self.sessions.add(session)
        if ts:
            self.first = ts if self.first is None or ts < self.first else self.first
            self.last = ts if self.last is None or ts > self.last else self.last
        inp, out, read, w5m, w1h = usage_parts(usage)
        self.input += inp
        self.output += out
        self.cache_read += read
        self.cache_write_5m += w5m
        self.cache_write_1h += w1h
        tier = usage_tier(usage, rates)
        if tier is None:
            self.unpriced += 1
            return
        if inp + read + w5m + w1h > LONG_CONTEXT_THRESHOLD:
            self.long_context += 1
            if rates.long is None:
                # Beyond 200 k the API charges a premium this model has no published
                # rate for: the cost below is a floor for those requests.
                self.long_unpriced += 1
        self.cost += (
            inp * tier.input
            + out * tier.output
            + w5m * tier.cache_write
            + w1h * tier.cache_write_1h
            + read * tier.cache_read
        )


def stale(path: Path, since: datetime | None) -> bool:
    """True when a transcript cannot hold anything inside the window.

    Transcripts are append-only, so a file last written before the window starts
    has nothing in it. Skipping those on mtime alone turns a short window into a
    short scan, without an index to keep in sync.
    """
    if since is None:
        return False
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < since
    except OSError:
        return True


def read_cost_states(since: datetime | None = None):
    """Counters written by Claude Code itself, one per transcript.

    Authoritative: they include branches abandoned after a rewind and the side
    calls (Haiku titling) the transcript never records. Missing from sessions
    older than their introduction.
    """
    states = {}
    for path in sorted(PROJECTS_DIR.glob("*/*.jsonl")):
        if stale(path, since):
            continue
        latest = None
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"cost-state"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "cost-state" and entry.get("totalCostUSD"):
                    latest = entry
        if latest:
            states[str(path)] = latest
    return states


def all_transcripts():
    """Every transcript, session files and subagent files alike.

    Yields (path, project_dir, is_subagent). A subagent file sits two levels
    deeper, so its project folder is not `path.parent`.
    """
    for path in sorted(PROJECTS_DIR.glob("*/*.jsonl")):
        yield path, path.parent.name, False
    for path in sorted(PROJECTS_DIR.glob("*/*/subagents/*.jsonl")):
        yield path, path.parents[2].name, True


def iter_messages(since: datetime | None, skip_paths: set | None = None):
    seen = set()
    for path, project_dir, is_subagent in all_transcripts():
        if skip_paths and str(path) in skip_paths:
            continue
        if stale(path, since):
            continue
        # A cost-state counter already covers its session's subagents, so their
        # files have to be skipped along with the parent to avoid counting twice.
        if is_subagent and skip_paths and str(parent_transcript(path)) in skip_paths:
            continue
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                message = entry.get("message") or {}
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                mid = message.get("id")
                if mid:
                    if mid in seen:
                        continue
                    seen.add(mid)
                ts = entry.get("timestamp") or ""
                when = None
                if ts:
                    try:
                        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        when = None
                if since and when and when < since:
                    continue
                yield {
                    "cwd": entry.get("cwd") or "",
                    "dir": project_dir,
                    "session": entry.get("sessionId") or path.stem,
                    "model": message.get("model") or "unknown",
                    "branch": entry.get("gitBranch") or "",
                    "sidechain": bool(entry.get("isSidechain")),
                    "usage": usage,
                    "when": when,
                }


def day_key(moment: datetime) -> str:
    """The calendar day a timestamp belongs to, in the machine's own timezone.

    Transcripts store UTC. Grouping on it puts an 11 p.m. session in Paris on the
    previous day, which makes "what did today cost" wrong at the edges.
    """
    return moment.astimezone().date().isoformat()


def human(n: int) -> str:
    for unit, size in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(n)


def render_table(headers, rows, aligns):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    def fmt(cells):
        return "  ".join(
            c.ljust(widths[i]) if aligns[i] == "l" else c.rjust(widths[i])
            for i, c in enumerate(cells)
        )
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))


def _short(text, width=52):
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def describe_tool(name, tool_input):
    if not isinstance(tool_input, dict):
        return name
    for field in ("file_path", "path", "command", "pattern", "query", "url",
                  "prompt", "description", "skill", "subagent_type"):
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            if field in ("file_path", "path"):
                value = os.path.basename(value)
            return f"{name}({_short(value, 40)})"
    return name


READ_TOOLS = {"Read", "NotebookRead", "Glob", "Grep", "Edit", "Write", "MultiEdit",
              "NotebookEdit"}


def tool_target(name, tool_input) -> str:
    """The file a call points at, kept whole: the audit needs the path, not a label."""
    if name not in READ_TOOLS or not isinstance(tool_input, dict):
        return ""
    for field in ("file_path", "path", "notebook_path"):
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


_COMMAND_RE = re.compile(r"<command-name>/?([\w:-]+)</command-name>")


def slash_commands(content) -> set:
    """Slash commands named in one user message, as Claude Code writes them down."""
    if isinstance(content, list):
        content = " ".join(block.get("text") or "" for block in content
                           if isinstance(block, dict) and block.get("type") == "text")
    if not isinstance(content, str) or "<command-name>" not in content:
        return set()
    return {match.group(1) for match in _COMMAND_RE.finditer(content)}


def subagent_dir(transcript: Path) -> Path:
    """Claude Code writes each subagent to its own transcript, one level down.

    `<project>/<session>.jsonl` has its subagents in
    `<project>/<session>/subagents/agent-<agentId>.jsonl`. They never show up in
    the parent file, so a scan that only globs `*/*.jsonl` misses them entirely
    \u2014 and on a fan-out session they can outweigh the main chain.
    """
    return transcript.parent / transcript.stem / "subagents"


def parent_transcript(sub_path: Path) -> Path:
    """The session transcript a subagent file belongs to."""
    return sub_path.parents[1].with_suffix(".jsonl")


def find_transcript(needle: str) -> Path | None:
    direct = Path(needle)
    if direct.is_file():
        return direct
    matches = [p for p in PROJECTS_DIR.glob("*/*.jsonl") if p.stem.startswith(needle)]
    if not matches:
        matches = [p for p in PROJECTS_DIR.glob("*/*.jsonl") if needle in p.stem]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


class Turn:
    __slots__ = ("mid", "ts", "model", "usage", "sidechain", "agent", "skill",
                 "tools", "text_len", "think_len", "ctx", "output")

    def __init__(self, mid, ts, model, sidechain, agent, skill):
        self.mid = mid
        self.ts = ts
        self.model = model
        self.sidechain = sidechain
        self.agent = agent
        self.skill = skill
        self.usage = None
        self.tools = {}
        self.text_len = 0
        self.think_len = 0
        self.ctx = 0
        self.output = 0


def read_session(path: Path, with_subagents: bool = True):
    turns: dict[str, Turn] = {}
    order: list[str] = []
    results: dict[str, int] = {}
    prompts = 0
    compactions = 0
    compact_mids: set[str] = set()
    pending_compaction = False
    launched: dict[str, dict] = {}
    cwds: Counter = Counter()
    branches: Counter = Counter()
    commands: set = set()
    meta = {"cwd": "", "branch": "", "session": path.stem, "reported": None,
            "compact_mids": compact_mids, "agents": [], "commands": commands}
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = entry.get("type")
        if kind == "cost-state" and entry.get("totalCostUSD"):
            meta["reported"] = entry
            continue
        if entry.get("isCompactSummary"):
            compactions += 1
            pending_compaction = True
        if kind == "user":
            content = (entry.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            has_result = False
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    has_result = True
                    body = block.get("content")
                    size = len(body) if isinstance(body, str) else len(json.dumps(body or ""))
                    raw = entry.get("toolUseResult")
                    if isinstance(raw, str):
                        size = max(size, len(raw))
                    elif isinstance(raw, dict):
                        size = max(size, len(json.dumps(raw)))
                        if raw.get("agentId"):
                            launched[raw["agentId"]] = {
                                "tool_use_id": block.get("tool_use_id") or "",
                                "description": raw.get("description") or "",
                                "model": raw.get("resolvedModel") or "",
                            }
                    results[block.get("tool_use_id") or ""] = size
            commands |= slash_commands(content)
            if not has_result and not entry.get("isMeta"):
                prompts += 1
            continue
        if kind != "assistant":
            continue
        message = entry.get("message") or {}
        mid = message.get("id")
        if not mid:
            continue
        if mid not in turns:
            turns[mid] = Turn(
                mid,
                entry.get("timestamp") or "",
                message.get("model") or "unknown",
                bool(entry.get("isSidechain")),
                entry.get("agentName") or "",
                entry.get("attributionSkill") or "",
            )
            order.append(mid)
            if pending_compaction:
                compact_mids.add(mid)
                pending_compaction = False
            if entry.get("cwd"):
                cwds[entry["cwd"]] += 1
            if entry.get("gitBranch"):
                branches[entry["gitBranch"]] += 1
        turn = turns[mid]
        if turn.usage is None and isinstance(message.get("usage"), dict):
            usage = message["usage"]
            turn.usage = usage
            turn.output = int(usage.get("output_tokens") or 0)
            turn.ctx = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
            )
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                turn.tools[block.get("id") or ""] = (
                    block.get("name") or "?", block.get("input"))
            elif block.get("type") == "text":
                turn.text_len = max(turn.text_len, len(block.get("text") or ""))
            elif block.get("type") == "thinking":
                turn.think_len = max(turn.think_len, len(block.get("thinking") or ""))
    if cwds:
        meta["cwd"] = cwds.most_common(1)[0][0]
    if branches:
        meta["branch"] = branches.most_common(1)[0][0]
    ordered = [turns[m] for m in order if turns[m].usage is not None]
    ordered.sort(key=lambda t: (t.ts, order.index(t.mid)))

    if with_subagents:
        # A subagent runs its own context, outside the main chain: its turns are
        # kept apart from the attribution but belong to the session's total.
        for sub_path in sorted(subagent_dir(path).glob("*.jsonl")):
            agent_id = sub_path.stem.removeprefix("agent-")
            call = launched.get(agent_id, {})
            kind = ""
            for turn in ordered:
                spec = turn.tools.get(call.get("tool_use_id"))
                if spec and isinstance(spec[1], dict):
                    kind = spec[1].get("subagent_type") or ""
                    break
            label = call.get("description") or kind or agent_id[:8]
            sub_turns = read_agent_turns(sub_path, label)
            if not sub_turns:
                continue
            ordered.extend(sub_turns)
            meta["agents"].append({
                "id": agent_id,
                "label": label,
                "type": kind,
                "model": call.get("model") or sub_turns[0].model,
                "tool_use_id": call.get("tool_use_id", ""),
                "turns": sub_turns,
            })
    return ordered, results, prompts, compactions, meta


def read_agent_turns(path: Path, label: str) -> list[Turn]:
    """The billed turns of one subagent transcript, deduplicated like the main one."""
    turns: dict[str, Turn] = {}
    order: list[str] = []
    for line in path.open(encoding="utf-8", errors="replace"):
        if '"usage"' not in line or '"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message") or {}
        mid = message.get("id")
        usage = message.get("usage")
        if not mid or not isinstance(usage, dict) or mid in turns:
            continue
        turn = Turn(mid, entry.get("timestamp") or "",
                    message.get("model") or "unknown", True, label,
                    entry.get("attributionSkill") or "")
        turn.usage = usage
        turn.output = int(usage.get("output_tokens") or 0)
        turn.ctx = (int(usage.get("input_tokens") or 0)
                    + int(usage.get("cache_read_input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0))
        turns[mid] = turn
        order.append(mid)
    return [turns[m] for m in order]


CACHE_REBUILD_SHARE = 0.5
CACHE_REBUILD_FLOOR = 20_000


def segment_starts(chain, compact_mids=()):
    """Turn indices where the context re-bases, and which of them are compactions."""
    flagged = {i for i, turn in enumerate(chain) if turn.mid in compact_mids}
    starts = [0]
    for i in range(1, len(chain)):
        if i in flagged or chain[i].ctx < chain[i - 1].ctx * 0.7:
            starts.append(i)
    return starts, flagged


def cache_rebuilds(chain, table, memo, starts):
    """Turns that paid to write the whole prefix into the cache again.

    A steady session writes only the delta and reads the rest back at a tenth of
    the input rate. When the cache entry has expired \u2014 the TTL is 5 min unless
    Claude Code asks for an hour \u2014 the next turn re-writes everything at 1.25x
    instead. The difference between those two rates, over the tokens rewritten, is
    what an idle gap costs. Segment starts are excluded: they legitimately
    rewrite everything.
    """
    known = set(starts)
    count = tokens = 0
    extra = 0.0
    gaps = []
    for i, turn in enumerate(chain):
        if i in known:
            continue
        _inp, _out, _read, w5m, w1h = usage_parts(turn.usage or {})
        written = w5m + w1h
        if not turn.ctx or written <= CACHE_REBUILD_FLOOR:
            continue
        if written < CACHE_REBUILD_SHARE * turn.ctx:
            continue
        tier = usage_tier(turn.usage, resolve_rates(turn.model, table, memo))
        if tier is None:
            continue
        count += 1
        tokens += written
        extra += written * (tier.cache_write - tier.cache_read)
        gap = _gap_seconds(chain[i - 1].ts, turn.ts)
        if gap is not None:
            gaps.append(gap)
    gaps.sort()
    return {"count": count, "tokens": tokens, "extra_cost": extra,
            "median_gap": gaps[len(gaps) // 2] if gaps else None}


def _gap_seconds(before: str, after: str):
    try:
        start = datetime.fromisoformat(before.replace("Z", "+00:00"))
        end = datetime.fromisoformat(after.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (end - start).total_seconds()


SEGMENT_LABELS = {
    "(startup)": "Startup (system prompt, CLAUDE.md, tools)",
    "(compaction)": "Resume after compaction",
    "(context reset)": "Context reset (rewind)",
}


def attribute(chain, results, compact_mids=()):
    """Spreads context growth across whatever caused it.

    added_i = ctx_i - (ctx_{i-1} + output_{i-1}): tokens injected between two
    turns, as measured by the API. Each addition is then weighted by the number
    of turns that carried it to the end of the segment.

    A segment boundary re-bases the accumulation, and there are two kinds. A
    /compact is read off the transcript's own `isCompactSummary` flag, so it is
    named for what it is. A rewind leaves no flag: it only shows up as a context
    that shrank, which the ratio below catches. Both must open a segment for the
    attribution to sum back to the measured cost; only their labels differ.
    """
    n = len(chain)
    segment_end = [n - 1] * n
    segment_of = [0] * n
    starts, flagged = segment_starts(chain, compact_mids)
    for pos, start in enumerate(starts):
        end = starts[pos + 1] - 1 if pos + 1 < len(starts) else n - 1
        for i in range(start, end + 1):
            segment_end[i] = end
            segment_of[i] = pos

    entries = []
    for i, turn in enumerate(chain):
        if i in starts:
            added = turn.ctx
            tool = ("(startup)" if i == 0
                    else "(compaction)" if i in flagged else "(context reset)")
            carried = segment_end[i] - i + 1
            entries.append({"turn": i, "label": SEGMENT_LABELS[tool], "tool": tool,
                            "added": added, "carried": carried, "ctx": turn.ctx,
                            "segment": segment_of[i], "target": ""})
            continue
        previous = chain[i - 1]
        carried = segment_end[i] - i + 1
        if previous.output:
            # Claude's own reply joins the conversation and is resent as input on
            # every later turn of the segment. Billed once at the output rate as
            # generation, then again at the input rate for as long as it is carried.
            entries.append({"turn": i, "label": "Claude's reply, resent as context",
                            "tool": "(replayed output)", "added": previous.output,
                            "carried": carried, "ctx": turn.ctx,
                            "segment": segment_of[i], "target": ""})
        added = turn.ctx - previous.ctx - previous.output
        if added == 0:
            continue
        sizes = {tid: max(results.get(tid, 0), 1) for tid in previous.tools}
        total = sum(sizes.values())
        if total and previous.tools:
            for tid, (name, tool_input) in previous.tools.items():
                share = added * sizes[tid] / total
                entries.append({"turn": i, "label": describe_tool(name, tool_input),
                                "tool": name, "added": share, "carried": carried,
                                "ctx": turn.ctx, "segment": segment_of[i],
                                "target": tool_target(name, tool_input),
                                "partial": isinstance(tool_input, dict) and bool(
                                    tool_input.get("offset") or tool_input.get("limit"))})
        else:
            entries.append({"turn": i, "label": "User prompt / system context",
                            "tool": "(no tool)", "added": added, "carried": carried,
                            "ctx": turn.ctx, "segment": segment_of[i], "target": ""})
    return entries


TURN_SORTS = {
    "cost-desc": ("Costliest first", lambda e: (-e["cost"], e["turn"])),
    "cost-asc": ("Cheapest first", lambda e: (e["cost"], e["turn"])),
    "added-desc": ("Tokens added", lambda e: (-e["added"], e["turn"])),
    "context-desc": ("Largest context", lambda e: (-e["ctx"], e["turn"])),
    "turn-asc": ("Chronological", lambda e: e["turn"]),
    "turn-desc": ("Reverse order", lambda e: -e["turn"]),
}

DEFAULT_SESSION_SORT = "date-desc"
TRIAGE_SESSION_LIMIT = 12
TRIAGE_RESULT_LIMIT = 3
SESSION_FINDING_MIN_SHARE = 0.10
SESSION_FINDING_MIN_COST = 0.25

# The bands, the per-kind weights and the fade-in live in session_grade.py: the
# Codex reader grades on the same scale, in its own unit.
CLAUDE_MD_LIMIT = 8_000        # bytes, resent on every turn of every session


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def claude_md_chain(path: Path, depth: int = 0, seen: set | None = None) -> list:
    """A CLAUDE.md and the files it pulls in, since an @-import is paid like the rest."""
    seen = set() if seen is None else seen
    key = str(path)
    if depth > 3 or key in seen or not path.is_file():
        return []
    seen.add(key)
    text = _read_text(path)
    files = [{"path": key, "bytes": len(text.encode("utf-8"))}]
    for line in text.splitlines():
        token = line.strip().split(" ", 1)[0]
        if not token.startswith("@") or len(token) < 2:
            continue
        target = Path(token[1:]).expanduser()
        if not target.is_absolute():
            target = path.parent / target
        files += claude_md_chain(target, depth + 1, seen)
    return files


_INSTRUCTIONS: dict[str, list] = {}


def instruction_chain(cwd: str) -> list:
    """The CLAUDE.md files a session run from `cwd` carried on every one of its turns.

    The user's file and the project's are both loaded, and an @-import is paid
    like the rest of the file, so what counts is the chain rather than one path.
    """
    if cwd in _INSTRUCTIONS:
        return _INSTRUCTIONS[cwd]
    seen: set = set()
    chain = claude_md_chain(Path.home() / ".claude" / "CLAUDE.md", seen=seen)
    if cwd:
        base = Path(cwd)
        for parent in (base, *list(base.parents)[:5]):
            chain += claude_md_chain(parent / "CLAUDE.md", seen=seen)
    _INSTRUCTIONS[cwd] = chain
    return chain


SESSION_SORTS = {
    "cost-desc": "Costliest first",
    "cost-asc": "Cheapest first",
    "date-desc": "Most recent",
    "date-asc": "Oldest",
    "project-asc": "Project (A\u2192Z)",
}


def breakdown_sort_key(name: str):
    """Re-sorts sessions on the numbers actually displayed.

    Upstream selection works off the aggregate; the detail is rescaled onto
    Claude Code's internal counter. Without this second sort, a cost column
    renders out of order.
    """
    def moment(detail):
        raw = detail.get("start") or ""
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return 0.0

    if name == "cost-asc":
        return lambda d: (d["cost"], d["id"])
    if name == "date-desc":
        return lambda d: (-moment(d), d["id"])
    if name == "date-asc":
        return lambda d: (moment(d), d["id"])
    if name == "project-asc":
        return lambda d: (d["project"].lower(), -d["cost"])
    return lambda d: (-d["cost"], d["id"])


def session_sort_key(name: str):
    """Sort key applied to (project, session) pairs before reading transcripts.

    Sorting precedes truncation: asking for the most recent must return the most
    recent, not the priciest among the most recent.
    """
    floor = datetime.min.replace(tzinfo=timezone.utc)
    if name == "cost-asc":
        return lambda item: (item[1].cost, item[0][1])
    if name == "date-desc":
        return lambda item: (-(item[1].last or floor).timestamp(), item[0][1])
    if name == "date-asc":
        return lambda item: ((item[1].last or floor).timestamp(), item[0][1])
    if name == "project-asc":
        return lambda item: (os.path.basename(item[0][0].rstrip("/")).lower(), -item[1].cost)
    return lambda item: (-item[1].cost, item[0][1])


def session_payload(path: Path, table, memo, top: int = 15,
                    turn_sort: str = "cost-desc", turn_filter: str = ""):
    turns, results, prompts, compactions, meta = read_session(path)
    if not turns:
        return None
    main = [t for t in turns if not t.sidechain]
    if not main:
        return None
    total_in = total_out = agents_cost = 0.0
    for turn in turns:
        rates = resolve_rates(turn.model, table, memo)
        if turn.sidechain:
            # A subagent burns its own context, off the main chain: one line, whole
            # cost, rather than a share of an attribution that never saw it.
            agents_cost += input_cost(turn.usage, rates) + output_cost(turn.usage, rates)
            continue
        total_in += input_cost(turn.usage, rates)
        total_out += output_cost(turn.usage, rates)
    ctx_sum = sum(t.ctx for t in main) or 1
    main_in = sum(input_cost(t.usage, resolve_rates(t.model, table, memo)) for t in main)
    per_ctx_token = main_in / ctx_sum
    entries = attribute(main, results, meta["compact_mids"])
    for e in entries:
        e["cost"] = e["added"] * e["carried"] * per_ctx_token
    starts, _flagged = segment_starts(main, meta["compact_mids"])
    cache = cache_rebuilds(main, table, memo, starts)
    reported = meta.get("reported")
    scale = 1.0
    ref = None
    if reported:
        ref = float(reported.get("totalCostUSD") or 0.0)
        mine = total_in + total_out + agents_cost
        if ref > 0 and mine > 0:
            scale = ref / mine
    if scale != 1.0:
        for e in entries:
            e["cost"] *= scale
        total_in *= scale
        total_out *= scale
        agents_cost *= scale
        cache["extra_cost"] *= scale
    cache["extra_cost"] = round(cache["extra_cost"], 4)
    # The three input counters the API itself bills on, summed over the session.
    billed = {"fresh": 0, "cache_read": 0, "cache_write": 0}
    for turn in turns:
        usage = turn.usage or {}
        billed["fresh"] += int(usage.get("input_tokens") or 0)
        billed["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        billed["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
    billed["total"] = sum(billed.values())
    grouped = defaultdict(lambda: {"added": 0.0, "cost": 0.0, "count": 0})
    for e in entries:
        g = grouped[e["tool"]]
        g["added"] += e["added"]
        g["cost"] += e["cost"]
        g["count"] += 1
    sources = [
        {"tool": tool, "count": g["count"], "added": int(g["added"]),
         "cost": round(g["cost"], 4)}
        for tool, g in sorted(grouped.items(), key=lambda kv: -kv[1]["cost"])
    ]
    sources.append({"tool": "(response generation)", "count": len(main),
                    "added": sum(t.output for t in main), "cost": round(total_out, 4)})
    agents = []
    for spec in meta["agents"]:
        cost = 0.0
        for turn in spec["turns"]:
            rates = resolve_rates(turn.model, table, memo)
            cost += (input_cost(turn.usage, rates) + output_cost(turn.usage, rates)) * scale
        agents.append({"label": spec["label"], "type": spec["type"],
                       "model": spec["model"], "turns": len(spec["turns"]),
                       "output": sum(t.output for t in spec["turns"]),
                       "cost": round(cost, 4)})
    agents.sort(key=lambda a: -a["cost"])
    if agents:
        sources.append({"tool": "(subagents)", "count": len(agents),
                        "added": sum(a["output"] for a in agents),
                        "cost": round(agents_cost, 4)})
    sources.sort(key=lambda src: -src["cost"])

    # Per-file view of the same attribution, for the setup audit: which paths were
    # loaded, at what cost, and how often the same one came back inside a single
    # segment. A read after a compaction or a rewind opens a new segment, so it is
    # not counted as a repeat, and neither is a partial read with an offset.
    files: dict[str, dict] = {}
    repeats: Counter = Counter()
    for e in entries:
        target = e.get("target")
        if not target:
            continue
        row = files.setdefault(target, {"path": target, "reads": 0, "edits": 0,
                                        "cost": 0.0, "added": 0.0, "repeats": 0})
        if e["tool"] in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            row["edits"] += 1
        else:
            row["reads"] += 1
            if not e.get("partial"):
                repeats[(target, e["segment"])] += 1
        row["cost"] += e["cost"]
        row["added"] += e["added"]
    for (target, _segment), seen in repeats.items():
        if seen > 1:
            files[target]["repeats"] += seen - 1
    ranked_files = [{**row, "cost": round(row["cost"], 4), "added": int(row["added"])}
                    for row in sorted(files.values(), key=lambda r: -r["cost"])[:60]]
    junk_rows = sorted((row for row in files.values()
                        if row["reads"] and is_junk(row["path"])),
                       key=lambda row: -row["cost"])
    dupe_rows = sorted((row for row in files.values()
                        if row["repeats"] and row["reads"]),
                       key=lambda row: -row["cost"] * row["repeats"] / row["reads"])
    waste = {
        "junk": {"cost": round(sum(row["cost"] for row in junk_rows), 4),
                 "reads": sum(row["reads"] for row in junk_rows),
                 "items": [f"{os.path.basename(row['path'])} — ${_money(row['cost'])}"
                           for row in junk_rows[:4]]},
        "dupes": {"cost": round(sum(row["cost"] * row["repeats"] / row["reads"]
                                    for row in dupe_rows), 4),
                  "repeats": sum(row["repeats"] for row in dupe_rows),
                  "items": [f"{os.path.basename(row['path'])} — {row['repeats']}×"
                            for row in dupe_rows[:4]]},
    }

    skills = defaultdict(float)
    for turn in turns:
        if turn.skill:
            rates = resolve_rates(turn.model, table, memo)
            skills[turn.skill] += (input_cost(turn.usage, rates)
                                   + output_cost(turn.usage, rates)) * scale
    # One point per turn, in order, labelled by whatever grew the context most on
    # it: the shape of the session, which a table sorted by cost cannot show.
    by_turn: dict[int, dict] = {}
    for e in entries:
        point = by_turn.get(e["turn"])
        if point is None or e["added"] > point["added"]:
            by_turn[e["turn"]] = {"turn": e["turn"], "ctx": e["ctx"],
                                  "label": _short(e["label"], 48), "tool": e["tool"],
                                  "added": e["added"], "cost": 0.0}
    for e in entries:
        by_turn[e["turn"]]["cost"] += e["cost"]
    curve = []
    for point in sorted(by_turn.values(), key=lambda d: d["turn"]):
        curve.append({**point, "added": int(point["added"]),
                      "cost": round(point["cost"], 4),
                      "reset": point["tool"] in SEGMENT_LABELS})

    rows = [{"turn": e["turn"], "label": _short(e["label"], 64), "tool": e["tool"],
             "added": int(e["added"]), "ctx": e["ctx"], "cost": round(e["cost"], 4)}
            for e in entries]
    needle = turn_filter.lower().strip()
    if needle:
        rows = [r for r in rows
                if needle in r["label"].lower() or needle in r["tool"].lower()]
    _label, key = TURN_SORTS.get(turn_sort, TURN_SORTS["cost-desc"])
    kept = sorted(rows, key=key)
    payload = {
        "id": path.stem,
        "short": path.stem[:8],
        "project": os.path.basename((meta["cwd"] or "").rstrip("/")) or "?",
        "cwd": meta["cwd"] or "",
        "branch": meta["branch"],
        "start": main[0].ts,
        "end": main[-1].ts,
        "turns": len(main),
        "prompts": prompts,
        "compactions": compactions,
        "context_final": main[-1].ctx,
        "context_peak": max(t.ctx for t in main),
        "billed": billed,
        "output": sum(t.output for t in turns),
        "cost": round(total_in + total_out + agents_cost, 4),
        "cost_context": round(total_in, 4),
        "cost_output": round(total_out, 4),
        "cost_agents": round(agents_cost, 4),
        "agents": agents,
        "cache": cache,
        "curve": curve,
        "exact": ref is not None,
        "reported": round(ref, 4) if ref is not None else None,
        "restitution": round(100 / scale, 1) if scale and ref is not None else None,
        "sources": sources,
        "skills": [{"name": k, "cost": round(v, 4)} for k, v in
                   sorted(skills.items(), key=lambda kv: -kv[1])],
        "files": ranked_files,
        "waste": waste,
        "commands": sorted(meta["commands"]),
        "entries": kept[:top],
        "entries_total": len(kept),
    }
    payload["triage"] = session_findings(payload)
    payload["score"], payload["grade"] = grade_session(payload)
    return payload


def aggregate_sources(session_ids, table, memo, limit: int | None = None):
    """Sums the per-tool breakdown over many sessions.

    One session tells you that a Read was expensive that day. Summed over a
    window, it tells you what a habit costs. Every transcript in the window has
    to be parsed for this, so it stays behind its own flag.
    """
    grouped = defaultdict(lambda: {"added": 0.0, "cost": 0.0, "count": 0,
                                   "sessions": set()})
    agents = defaultdict(lambda: {"cost": 0.0, "turns": 0, "runs": 0})
    cache = {"count": 0, "tokens": 0, "extra_cost": 0.0}
    scanned = skipped = 0
    total = 0.0
    done: set[str] = set()
    for sid in session_ids:
        # A session whose subagent worked in another repo lands under two project
        # keys. It is still one transcript, and must be parsed once.
        if sid in done:
            continue
        done.add(sid)
        if limit is not None and scanned >= limit:
            skipped += 1
            continue
        found = find_transcript(sid)
        if found is None:
            continue
        detail = session_payload(found, table, memo, top=0)
        if detail is None:
            continue
        scanned += 1
        total += detail["cost"]
        for src in detail["sources"]:
            entry = grouped[src["tool"]]
            entry["added"] += src["added"]
            entry["cost"] += src["cost"]
            entry["count"] += src["count"]
            entry["sessions"].add(sid)
        for agent in detail["agents"]:
            key = agent["type"] or "(unnamed)"
            agents[key]["cost"] += agent["cost"]
            agents[key]["turns"] += agent["turns"]
            agents[key]["runs"] += 1
        for field in ("count", "tokens", "extra_cost"):
            cache[field] += detail["cache"][field]
    sources = [{"tool": tool, "count": g["count"], "added": int(g["added"]),
                "sessions": len(g["sessions"]), "cost": round(g["cost"], 4)}
               for tool, g in sorted(grouped.items(), key=lambda kv: -kv[1]["cost"])]
    return {"sources": sources, "scanned": scanned, "skipped": skipped,
            "total": round(total, 4), "cache": cache,
            "agents": [dict(v, type=k) for k, v in
                       sorted(agents.items(), key=lambda kv: -kv[1]["cost"])]}


def session_findings(detail: dict):
    """Returns the actionable patterns found in one already-parsed session.

    Each one carries a weight, and their sum is the session's grade. Only what
    the run could have avoided weighs: a large subagent bill or a heavy tool
    result is a lead worth opening, not a fault, and scores zero.
    """
    findings = []

    def add(kind: str, cost: float, reason: str, advice: str, tips: tuple,
            items=(), weight: float | None = None, floor: float | None = None):
        threshold = floor if floor is not None else max(
            SESSION_FINDING_MIN_COST, detail["cost"] * SESSION_FINDING_MIN_SHARE)
        if cost < threshold:
            return
        share = 100 * cost / max(detail["cost"], 0.0001)
        findings.append({"kind": kind, "cost": round(cost, 4),
                         "share": round(share, 1),
                         "weight": round(grade_weight(kind, share, cost)
                                         if weight is None else weight, 2),
                         "reason": reason, "advice": advice, "tips": tips,
                         "items": list(items),
                         "id": detail["id"], "short": detail["short"],
                         "project": detail["project"]})

    cache = detail.get("cache") or {}
    if cache.get("extra_cost", 0) > 0:
        add("cache", cache["extra_cost"],
            f"The prompt cache was rebuilt on {cache['count']} turn(s).",
            "When continuing the same task, resume before the cache expires.",
            ("Do not rush work just for the cache; this matters only when you were already "
             "about to resume the same task.",
             "After a long break, treat the next turn as a deliberate restart rather than "
             "an unexpected extra cost."))

    if detail.get("cost_agents", 0) > 0:
        share = 100 * detail["cost_agents"] / max(detail["cost"], 0.0001)
        add("subagents", detail["cost_agents"],
            f"Subagents account for {share:.0f}% of this session.",
            "Check whether fewer, narrower agents would cover the work.",
            ("Give each agent one outcome, explicit files or boundaries, and a clear "
             "stop condition.",
             "Avoid overlapping exploration: have one agent locate the code before "
             "asking another to change it."))

    sources = {source["tool"]: source["cost"] for source in detail["sources"]}
    replayed = sources.get("(replayed output)", 0)
    if replayed > 0:
        add("replayed-output", replayed,
            "Earlier replies remained in context and were replayed on later turns.",
            "At the next change of phase, start a fresh session.",
            ("Split work at natural handoffs: investigation, implementation, then review.",
             "Carry only a short handoff summary into the next session, not the whole "
             "working conversation."))

    tool_sources = [source for source in detail["sources"]
                    if not source["tool"].startswith("(")]
    if tool_sources:
        largest = max(tool_sources, key=lambda source: source["cost"])
        add("tool-result", largest["cost"],
            f"{largest['tool']} results were carried into later turns.",
            "Check whether that result could be narrower.",
            ("Search first, then read the relevant slice instead of loading a whole file, "
             "directory, or log.",
             "Ask commands for the smallest useful output: targeted filters, limits, and "
             "paths beat broad listings."))

    compacted = sources.get("(compaction)", 0)
    if compacted > 0:
        add("compaction", compacted,
            "A compaction summary was carried through the rest of the session.",
            "If the subject changed, a fresh session is cheaper than compacting it.",
            ("Use compaction only to continue the same problem with less context, not to "
             "jump into a different task.",
             "When the subject changes, start fresh and state only the facts needed for "
             "the next task."))

    # The three below are unambiguous: no share threshold, only the cost floor.
    # A quarter of context spent on a lock file is worth naming in a $200 session
    # exactly as much as in a $3 one.
    waste = detail.get("waste") or {}
    junk = waste.get("junk") or {}
    if junk.get("cost", 0) > 0:
        add("junk-reads", junk["cost"],
            f"{_count(junk['reads'], 'read')} of generated, vendored or locked files "
            "were loaded into context.",
            "Point the read at the source file rather than at its build product.",
            ("Lock files, bundles and dependency trees answer almost nothing and are "
             "carried by every later turn.",
             "When a dependency really is the question, read the one file inside it "
             "rather than the directory."),
            junk["items"], floor=SESSION_FINDING_MIN_COST)

    dupes = waste.get("dupes") or {}
    if dupes.get("cost", 0) > 0:
        add("duplicate-reads", dupes["cost"],
            f"{_count(dupes['repeats'], 'repeat read')} of a file already in this "
            "context. Reads after a compaction or a rewind, and partial reads with an "
            "offset, are not counted here.",
            "The file is already in context: ask for the part that changed rather than "
            "the file again.",
            ("A re-read costs its whole size again, and every later turn carries both "
             "copies.",
             "After an edit, the diff is what changed \u2014 the file does not have to "
             "come back whole."),
            dupes["items"], floor=SESSION_FINDING_MIN_COST)

    chain = [spec for spec in instruction_chain(detail.get("cwd") or "")
             if spec["bytes"]]
    size = sum(spec["bytes"] for spec in chain)
    if size > CLAUDE_MD_LIMIT:
        add("instructions", sources.get("(startup)", 0),
            f"Instructions total {size / 1000:.1f} kB, resent on every turn of every "
            "session. Startup \u2014 system prompt, instructions and tool definitions "
            "\u2014 is what that cost here.",
            "Keep the standing rules; move the rest to files Claude opens when it "
            "needs them.",
            ("A CLAUDE.md is read on every turn, whether or not the turn needs it.",
             "Rules that apply to one directory belong in that directory, not in the "
             "file every session loads."),
            [f"{spec['path']} \u2014 {spec['bytes'] / 1000:.1f} kB" for spec in chain[:4]],
            weight=min(8.0, 4.0 * (size / CLAUDE_MD_LIMIT - 1)),
            floor=SESSION_FINDING_MIN_COST)
    return findings


def triage(session_buckets, table, memo, candidate_limit: int = TRIAGE_SESSION_LIMIT,
           result_limit: int = TRIAGE_RESULT_LIMIT):
    """Returns the few costly patterns worth opening a session for.

    This deliberately reads only the most expensive sessions in the window. It is
    a local shortcut to investigation, not a scoring system, a budget, or an
    attempt to judge every working habit.
    """
    details = []
    seen = set()
    for (project, sid), _bucket in sorted(session_buckets.items(),
                                          key=lambda item: -item[1].cost):
        if sid in seen:
            continue
        seen.add(sid)
        path = find_transcript(sid)
        if path is None:
            continue
        detail = session_payload(path, table, memo, top=0)
        if detail is None:
            continue
        detail["project"] = os.path.basename(project.rstrip("/")) or detail["project"]
        detail["triage"] = session_findings(detail)
        detail["score"], detail["grade"] = grade_session(detail)
        details.append(detail)
        if len(details) >= candidate_limit:
            break

    findings = [finding for detail in details for finding in detail["triage"]]

    findings.sort(key=lambda finding: -finding["cost"])
    chosen = []
    kinds = set()
    for unique_kind in (True, False):
        for finding in findings:
            if finding in chosen or (unique_kind and finding["kind"] in kinds):
                continue
            chosen.append(finding)
            kinds.add(finding["kind"])
            if len(chosen) == result_limit:
                return {"findings": chosen, "scanned": len(details)}
    return {"findings": chosen, "scanned": len(details)}


GIT_LEAD_SECONDS = 120        # a commit counts from two minutes before the first turn
GIT_GRACE_SECONDS = 30 * 60   # ...and up to half an hour after the last one
YIELD_MAX_PROJECTS = 8
YIELD_OUTCOMES = ("landed", "reverted", "unmerged", "no-commit")
YIELD_LABELS = {
    "landed": "Landed on the mainline",
    "reverted": "Reverted afterwards",
    "unmerged": "Committed, never merged",
    "no-commit": "No commit",
}
YIELD_SHORT = {"landed": "Landed", "reverted": "Reverted",
               "unmerged": "Unmerged", "no-commit": "No commit"}


def _git(root: str, *arguments: str, timeout: int = 10) -> str:
    """One read-only git call. Any failure returns '': yield degrades, never raises."""
    try:
        proc = subprocess.run(
            ("git", "-C", root, *arguments), capture_output=True, text=True,
            timeout=timeout, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


_TOPLEVEL_CACHE: dict[str, str] = {}


def git_toplevel(path: str) -> str:
    """The repo a project key points at, or '' when it points at no repo at all."""
    if path in _TOPLEVEL_CACHE:
        return _TOPLEVEL_CACHE[path]
    resolved = ""
    if path and os.path.isdir(path):
        resolved = _git(path, "rev-parse", "--show-toplevel").strip()
    _TOPLEVEL_CACHE[path] = resolved
    return resolved


def mainline_ref(root: str) -> str:
    """The ref a commit has to reach before it counts as landed."""
    head = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    if head:
        return head
    for ref in ("refs/remotes/origin/main", "refs/remotes/origin/master",
                "refs/heads/main", "refs/heads/master"):
        if _git(root, "rev-parse", "--verify", "--quiet", ref).strip():
            return ref
    return "HEAD"


_REVERT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")


def repo_history(root: str, since_iso: str) -> dict:
    """Commits in the window, each flagged for the mainline and for later reverts."""
    window = ["--since", since_iso] if since_iso else []
    commits = []
    for line in _git(root, "log", "--all", "--no-merges", *window,
                     "--pretty=%H%x1f%ct%x1f%s").splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        try:
            commits.append({"sha": parts[0], "ts": int(parts[1]), "subject": parts[2]})
        except ValueError:
            continue
    ref = mainline_ref(root)
    landed = set(_git(root, "rev-list", ref, *window).split())
    # A revert can land long after the window, so this one query is not bounded by it.
    reverted: dict[int, set] = defaultdict(set)
    for match in _REVERT_RE.finditer(
            _git(root, "log", "--all", "--grep", "This reverts commit", "-n", "300",
                 "--pretty=%B%x1e")):
        prefix = match.group(1)
        reverted[len(prefix)].add(prefix)
    for commit in commits:
        commit["landed"] = commit["sha"] in landed
        commit["reverted"] = any(commit["sha"][:size] in shas
                                 for size, shas in reverted.items())
    return {"ref": ref.rsplit("/", 1)[-1], "commits": commits}


def yield_report(session_buckets, limit: int | None = YIELD_MAX_PROJECTS) -> dict:
    """What each session left behind in git.

    The outcome is read from the repository, not from the transcript: a commit
    made while a session was running, plus a short grace period, is attributed to
    it, and the outcome is whether that commit reached the mainline, was reverted
    afterwards, or never merged. Sessions whose directory is not a git repository
    are left out rather than counted as unproductive.

    `no-commit` is a category, not a verdict: reading, debugging and planning
    sessions legitimately end without one.
    """
    groups: dict[str, list] = defaultdict(list)
    seen = set()
    outside = 0
    for (project, sid), bucket in sorted(session_buckets.items(),
                                         key=lambda item: -item[1].cost):
        if sid in seen or bucket.first is None:
            continue
        seen.add(sid)
        root = git_toplevel(project)
        if not root:
            outside += 1
            continue
        groups[root].append({
            "id": sid, "short": sid[:8], "root": root,
            "project": os.path.basename(root) or root, "cost": bucket.cost,
            "first": bucket.first, "last": bucket.last or bucket.first,
            "commits": [],
        })

    ranked = sorted(groups.items(), key=lambda kv: -sum(s["cost"] for s in kv[1]))
    skipped = 0
    if limit is not None and len(ranked) > limit:
        skipped = sum(len(items) for _root, items in ranked[limit:])
        ranked = ranked[:limit]

    repos, sessions, unmatched = [], [], 0
    for root, items in ranked:
        floor = min(session["first"] for session in items) - timedelta(days=1)
        history = repo_history(root, floor.astimezone().isoformat(timespec="seconds"))
        for commit in history["commits"]:
            # A commit belongs to the last session that was still running when it
            # was authored, so two overlapping sessions never bank the same work.
            owner = None
            for session in items:
                start = session["first"].timestamp() - GIT_LEAD_SECONDS
                end = session["last"].timestamp() + GIT_GRACE_SECONDS
                if start <= commit["ts"] <= end and (
                        owner is None or session["first"] > owner["first"]):
                    owner = session
            if owner is None:
                unmatched += 1
                continue
            owner["commits"].append(commit)
        tally = {name: {"sessions": 0, "cost": 0.0} for name in YIELD_OUTCOMES}
        for session in items:
            commits = session["commits"]
            if not commits:
                outcome = "no-commit"
            elif any(c["landed"] and not c["reverted"] for c in commits):
                outcome = "landed"
            elif any(c["reverted"] for c in commits):
                outcome = "reverted"
            else:
                outcome = "unmerged"
            session["outcome"] = outcome
            tally[outcome]["sessions"] += 1
            tally[outcome]["cost"] += session["cost"]
            sessions.append({
                "id": session["id"], "short": session["short"],
                "project": session["project"], "outcome": outcome,
                "cost": round(session["cost"], 4),
                "start": session["first"].isoformat(),
                "commits": [{"sha": c["sha"][:8], "subject": _short(c["subject"], 60),
                             "landed": c["landed"], "reverted": c["reverted"]}
                            for c in commits],
            })
        landed_commits = sum(1 for session in items for c in session["commits"]
                             if c["landed"] and not c["reverted"])
        repos.append({
            "name": os.path.basename(root) or root, "path": root,
            "mainline": history["ref"],
            "cost": round(sum(session["cost"] for session in items), 4),
            "sessions": len(items), "commits": len(history["commits"]),
            "landed_commits": landed_commits,
            "outcomes": {name: {"sessions": value["sessions"],
                                "cost": round(value["cost"], 4)}
                         for name, value in tally.items()},
        })

    totals = {name: {"sessions": 0, "cost": 0.0} for name in YIELD_OUTCOMES}
    for repo in repos:
        for name, value in repo["outcomes"].items():
            totals[name]["sessions"] += value["sessions"]
            totals[name]["cost"] = round(totals[name]["cost"] + value["cost"], 4)
    landed_commits = sum(repo["landed_commits"] for repo in repos)
    sessions.sort(key=lambda session: -session["cost"])
    return {
        "repos": sorted(repos, key=lambda repo: -repo["cost"]),
        "outcomes": totals,
        "sessions": sessions,
        "landed_commits": landed_commits,
        "cost_per_landed": round(totals["landed"]["cost"] / landed_commits, 4)
                           if landed_commits else None,
        "unmatched_commits": unmatched,
        "outside_git": outside,
        "skipped_sessions": skipped,
    }


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _tokens(n: float) -> str:
    a = abs(n)
    for unit, size in ((" G", 1e9), (" M", 1e6), (" k", 1e3)):
        if a >= size:
            return f"{n / size:.1f}" + unit
    return str(int(round(n)))


MONTHS_LONG = ("January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")


AUDIT_MAX_SESSIONS = 300


def audit_sessions(session_buckets, table, memo,
                   limit: int = AUDIT_MAX_SESSIONS) -> dict:
    """Grades the sessions of the window, one by one.

    There is no machine-wide grade here on purpose: what a setup costs is only
    visible in the sessions that ran under it, so every figure below belongs to
    a session. The costliest ones are read first, capped by `limit`, and the
    report says how many that was.
    """
    ranked, seen = [], set()
    for (project, sid), bucket in sorted(session_buckets.items(),
                                         key=lambda item: -item[1].cost):
        if sid in seen:
            continue
        seen.add(sid)
        ranked.append((project, sid))

    rows: list = []
    kinds: dict = defaultdict(float)
    cost_seen = 0.0
    for project, sid in ranked[:limit]:
        path = find_transcript(sid)
        if path is None:
            continue
        detail = session_payload(path, table, memo, top=0)
        if detail is None:
            continue
        cost_seen += detail["cost"]
        weighted = [finding for finding in detail["triage"] if finding["weight"] > 0]
        for finding in weighted:
            if finding["kind"] in GRADE_WEIGHTS:
                kinds[finding["kind"]] += finding["cost"]
        lead = max(weighted, key=lambda finding: finding["weight"], default=None)
        rows.append({
            "id": detail["id"], "short": detail["short"],
            "project": os.path.basename(project.rstrip("/")) or detail["project"],
            "cost": detail["cost"], "score": detail["score"],
            "grade": detail["grade"],
            "lead": None if lead is None else {"kind": lead["kind"],
                                               "cost": lead["cost"],
                                               "share": lead["share"]},
        })
    # The median, not the mean: one runaway session should not repaint a window
    # in which everything else was clean.
    scores = sorted(row["score"] for row in rows)
    middle = scores[len(scores) // 2] if scores else 0.0
    return {
        "rows": rows,
        "parsed": len(rows),
        "pool": len(ranked),
        "cost_seen": round(cost_seen, 4),
        "score": round(middle, 1),
        "grade": grade_for(middle),
        "spread": {letter: sum(1 for row in rows if row["grade"] == letter)
                   for letter in "ABCDF"},
        "kinds": {kind: round(cost, 4)
                  for kind, cost in sorted(kinds.items(), key=lambda kv: -kv[1])},
    }


def _day(iso: str) -> str:
    moment = datetime.fromisoformat(iso)
    return f"{MONTHS_LONG[moment.month - 1]} {moment.day}"


SOURCE_TIPS = {
    "(startup)": "The system prompt, CLAUDE.md and the tool definitions. Loaded "
                 "once at the head of the session, then carried by every turn that "
                 "follows it.",
    "(compaction)": "The context a /compact rebuilt: its summary, plus the startup "
                    "material re-sent behind it. Read off the transcript's own "
                    "compaction flag. Carried by every turn after it.",
    "(context reset)": "The context re-based after a rewind. No flag marks those, so "
                       "they are detected on a context that shrank. The tokens were "
                       "already loaded once \u2014 this line is the re-baselining, "
                       "not a second load.",
    "(no tool)": "Your prompts and the system reminders around them \u2014 context growth "
                 "on turns where no tool result came back.",
    "(response generation)": "The output tokens Claude produced, billed once at the "
                             "output rate. What it then costs to carry them is the "
                             "separate (replayed output) line.",
    "(subagents)": "What the subagents this session launched cost, in full. Each "
                   "runs its own context, so none of it appears in the attribution "
                   "above \u2014 only the report it handed back does.",
    "(replayed output)": "Claude's own replies, resent as input context on every later "
                         "turn. Paid a second time, at the input rate, for as long as "
                         "the segment lasts \u2014 which is why it grows with session "
                         "length rather than with reply size.",
}


def source_tip(tool: str, count: int) -> str:
    if tool in SOURCE_TIPS:
        return SOURCE_TIPS[tool]
    call = "call" if count == 1 else "calls"
    return (f"What {tool} returned, over {count} {call}. Cost = tokens added \u00d7 the "
            "number of later turns that carried them.")


COLUMN_TIPS = {
    "Source": "Where the tokens came from: one tool, or a pseudo-source for the "
              "startup context, your own prompts, and Claude's output.",
    "Calls": "How many times this source appeared in the session.",
    "Tokens added": "The new tokens this source pushed into the context, before any "
                    "multiplication by the turns that carry them.",
    "Tokens": "The new tokens this turn added to the context \u2014 its own size, not "
              "the running total.",
    "Turn": "Position of the turn in the session, oldest first.",
    "What the turn added": "The tool call, or the kind of message, that grew the "
                           "context on this turn.",
    "Context": "The context the API measured on that turn: everything replayed, plus "
               "what this turn added. This is a read number, not an estimate.",
    "Avg call": "Tokens added per call \u2014 a source can be expensive because each "
                "call is huge, or because it is called constantly.",
    "Cost": "The share of the session's measured input cost attributed to this line: "
            "tokens added, weighted by the turns that replay them. Attribution is a "
            "model; only the session total is measured.",
    "Share": "This line's part of the session's total cost.",
}


FACT_TIPS = {
    "Context": "What the input tokens cost: the whole context, replayed on every turn, "
               "at cache and full rates. Usually the larger half of a session.",
    "Generation": "What Claude's own output cost. Billed once, never replayed.",
    "Input tokens billed": "Every input token the API charged for, over the session: "
                           "uncached, cache writes and cache reads added together. It "
                           "dwarfs the context size because each turn resends the lot.",
    "Served from cache": "Share of those input tokens that came from the prompt cache, "
                         "at about a tenth of the input rate. A low figure means "
                         "something changes the prefix and invalidates the cache.",
    "Written to cache": "Tokens written into the cache, at about 1.25\u00d7 the input "
                        "rate. Paid once per new prefix, repaid by the next turn that "
                        "reads it back.",
    "Uncached input": "Input tokens charged at the full rate \u2014 never cached, or "
                      "expired from the cache before the next turn.",
    "Subagents": "What the subagents launched from this session cost, in full. They "
                 "run their own context, so none of it shows up in the attribution "
                 "below \u2014 only the report each handed back does.",
    "Cache rebuilt": "What was paid to write the whole prefix into the cache again "
                     "after it had expired, instead of reading it back at a tenth of "
                     "the rate. The entry lives 5 minutes; a longer pause pays twice.",
    "Tokens produced": "Output tokens generated over the session, thinking included.",
    "Peak context": "The largest context the API measured on a single turn \u2014 the "
                    "high-water mark, which a compaction resets.",
    "Accounted for by transcript": "How much of Claude Code's own cost counter this "
                                   "page's breakdown adds up to. Below 100 % means the "
                                   "transcript does not carry every billed turn.",
    "Internal counter": "No cost-state entry in this transcript, so the total is a "
                        "floor rebuilt from the turns rather than a figure Claude Code "
                        "itself recorded.",
}


EXACT_TIP = ("Cost read from the cost-state counter Claude Code writes into the "
             "transcript itself. Authoritative: this is what the session was billed.")

FLOOR_TIP = ("No cost-state counter in this transcript, so the cost is rebuilt from "
             "its usage blocks. The transcript keeps only the final branch, so work "
             "abandoned after a rewind was billed but no longer appears, and the "
             "Haiku titling calls are never written to it. A minimum, not a "
             "measurement \u2014 across the sessions carrying both, the transcript "
             "accounts for 83 % of the counter.")


def _chip(exact: bool, focusable: bool = True) -> str:
    """The exact/floor chip, explaining on hover what the figure rests on.

    Inside a clickable row the chip takes no tab stop of its own: the row
    already is one, and a second stop on a purely explanatory pill would only
    lengthen the keyboard path through the list.
    """
    tip = EXACT_TIP if exact else FLOOR_TIP
    label = "exact" if exact else "floor"
    focus = ' tabindex="0"' if focusable else ""
    return (f'<span class="chip {label} src"{focus} '
            f'data-tip="{html_escape(tip)}">{label}</span>')


GRADE_TIP = ("Grade {grade}, score {score:.0f} — what this session could have "
             "avoided, as a share of what it cost: junk or duplicate reads, a rebuilt "
             "cache, a compaction, replies replayed to the end. Being long or "
             "expensive is not a fault.")


def _grade_chip(session: dict, focusable: bool = True) -> str:
    """The A-to-F letter, which grades one session and nothing wider."""
    grade = session.get("grade")
    if not grade:
        return ""
    focus = ' tabindex="0"' if focusable else ""
    tip = GRADE_TIP.format(grade=grade, score=session.get("score") or 0)
    return (f'<span class="chip grade g{grade.lower()} src"{focus} '
            f'data-tip="{html_escape(tip)}">{grade}</span>')


def _th(label: str, numeric: bool = False) -> str:
    """A column header that explains itself on hover, reusing the .src tooltip."""
    tip = COLUMN_TIPS.get(label)
    cell = '<th class="n">' if numeric else "<th>"
    if not tip:
        return f"{cell}{html_escape(label)}</th>"
    return (f'{cell}<span class="src" tabindex="0" data-tip="{html_escape(tip)}">'
            f"{html_escape(label)}</span></th>")


def _query(params: dict, **overrides) -> str:
    """Builds a relative URL that carries the dashboard's current state."""
    import urllib.parse
    merged = {k: str(v) for k, v in {**params, **overrides}.items() if v not in (None, "")}
    return "?" + urllib.parse.urlencode(merged) if merged else "?"


def _state(payload: dict) -> dict:
    listing = payload.get("listing") or {}
    days = payload["window"].get("days")
    return {
        "source": payload.get("dashboard_source", ""),
        "days": days or "",
        "sort": listing.get("sort_sessions", ""),
        "q": listing.get("filter_sessions", ""),
        "tsort": listing.get("sort_turns", ""),
        "tq": listing.get("filter_turns", ""),
        "max": listing.get("limit", ""),
    }


def _select(name: str, choices, chosen: str, label: str) -> str:
    esc = html_escape
    options = "".join(
        f'<option value="{value}"{" selected" if value == chosen else ""}>{esc(text)}</option>'
        for value, text in choices)
    return (f'<label><span>{esc(label)}</span>'
            f'<select name="{name}">{options}</select></label>')


def _hidden(name: str, value) -> str:
    if value in (None, ""):
        return ""
    return f'<input type="hidden" name="{name}" value="{html_escape(str(value))}">'


def _turn_form(payload: dict, action: str = "") -> str:
    """Turn sorting and filtering form, shared by both views."""
    esc = html_escape
    state = _state(payload)
    choices = [(key, label) for key, (label, _fn) in TURN_SORTS.items()]
    reset = ""
    if state["tq"] or state["tsort"] not in ("", "cost-desc"):
        reset = f'<a class="reset" href="{esc(_query(state, tsort="", tq=""))}">Clear</a>'
    return (
        f'<form class="filter" method="get" action="{esc(action)}">'
        + _hidden("id", payload.get("focus", {}).get("id") if payload.get("focus") else "")
        + _hidden("days", state["days"]) + _hidden("sort", state["sort"])
        + _hidden("q", state["q"]) + _hidden("max", state["max"])
        + '<span class="eyebrow">Turns</span>'
        + _select("tsort", choices, state["tsort"] or "cost-desc", "Sort")
        + '<label class="grow"><span>Filter</span><input type="search" name="tq" '
        + f'placeholder="tool or label" value="{esc(state["tq"])}"></label>'
        + '<button type="submit">Apply</button>' + reset + "</form>")


def _claude_chart(session: dict) -> dict | None:
    """The context, turn by turn, with the commands that moved it.

    The turn table sorted by cost tells you which call was expensive. It cannot
    show the shape: the startup plateau, the step a big read leaves behind, the
    cliff a compaction cuts. That shape is what the curve is for.
    """
    curve = session.get("curve") or []
    if len(curve) < 2:
        return None
    dull = {"(replayed output)", "(no tool)"}
    points = [{
        "x": f'turn {point["turn"]}',
        "value": point["ctx"],
        "label": point["label"],
        "added": point["added"],
        "dull": point["tool"] in dull,
        "reset": bool(point.get("reset")),
        "reset_tag": "compaction" if point["tool"] == "(compaction)" else "reset",
        "readout": (f'turn {point["turn"]} · {point["label"]} · '
                    f'+{_tokens(point["added"])} · ${_money(point["cost"])} '
                    f'· ctx {_tokens(point["ctx"])}'),
    } for point in curve]
    legend = [("", "context carried into the turn"), ("peak", "biggest additions")]
    if any(point["reset"] for point in points[1:]):
        legend.append(("dashed", "context re-based"))
    return {
        "id": session["short"],
        "title": "How the context grew",
        "label": "Context size turn by turn",
        "hint": ("The context carried into each turn, in order. Every turn pays for "
                 "the whole height of the curve under it, which is why a step early "
                 "on costs more than the same step late. Hover any point for the "
                 "command behind it."),
        "idle": "hover the curve for the turn behind any point",
        "points": points,
        "legend": legend,
    }


def _claude_triage(session: dict) -> dict | None:
    """The session's grade, and the leads it is made of."""
    findings = sorted(session.get("triage") or [],
                      key=lambda finding: (-finding["weight"], -finding["cost"]))
    grade = session.get("grade")
    if not grade and not findings:
        return None
    avoidable = sum(finding["cost"] for finding in findings
                    if finding["weight"] and finding["kind"] in GRADE_WEIGHTS)
    stats = [
        {"label": "Grade", "value": grade or "—", "tone": f"g{(grade or '').lower()}",
         "tip": "A to F, from what this session could have avoided as a share of what "
                "it cost — never from its size. The bands are A under 3, B under "
                "8, C under 15, D under 24, F beyond."},
        {"label": "Score", "value": f'{session.get("score") or 0:.0f}',
         "tip": "The weighted sum of the leads below. Each one weighs its own share of "
                "the session, capped per kind, and fades in under a couple of dollars: "
                "a bad rate on small change is a rate, not something to act on."},
        {"label": "Avoidable", "value": "$" + _money(avoidable),
         "tip": "What the leads that weigh cost together: junk or duplicate reads, a "
                "cache rebuilt after an idle gap, a compaction carried to the end, "
                "replies replayed past a fifth of the bill. Leads that score nothing "
                "are left out."},
        {"label": "Leads", "value": str(len(findings)),
         "tip": "How many patterns were found here, weighing or not. Subagents and a "
                "heavy tool result are listed but score zero: worth opening, not "
                "faults."},
    ]
    return {
        "title": "What this session could have avoided",
        "hint": ("The letter grades this session and nothing wider: what the run "
                 "could have avoided, as a share of what it cost. Being long or "
                 "expensive is not a fault — a heavy subagent or tool result is a "
                 "lead worth opening, and weighs nothing on the grade. Leads appear "
                 "from 10% of the session and $0.25; junk reads, duplicate reads and "
                 "oversized instructions from $0.25 alone."),
        "stats": stats,
        "findings": [{
            "value": "$" + _money(finding["cost"]),
            "share": (f'{finding["share"]:.0f}% of the session · '
                      + (f'{finding["weight"]:.0f} of the score' if finding["weight"]
                         else "no penalty")),
            "reason": finding["reason"],
            "items": finding.get("items") or [],
            "advice": finding["advice"],
            "tips": finding["tips"],
        } for finding in findings],
        "note": ("Nothing stood out: no junk read, no file read twice, no cache "
                 "rebuilt after a pause."),
    }


def _column_spec(label: str, numeric: bool = False) -> dict:
    """A column of the session tables, carrying its own explanation."""
    return {"label": label, "n": numeric, "tip": COLUMN_TIPS.get(label)}


def _claude_sections(session: dict, payload: dict, standalone: bool) -> list:
    """Everything below the curve: what filled the context, then every turn."""
    sources = [source for source in session["sources"] if source["cost"] > 0]
    total_sources = sum(source["cost"] for source in sources) or 1
    breakdown = {
        "title": "What filled the context",
        "hint": ("Every turn resends the whole accumulated context, so a source costs "
                 "its own size multiplied by the number of later turns that carry it "
                 "— an attribution, where the session total above is measured. "
                 "Hover any source or column header for what it means."),
        "stack": [{"share": source["cost"] / total_sources * 100,
                   "color": _shade(index, len(sources)),
                   "title": f'{source["tool"]} — ${_money(source["cost"])}'}
                  for index, source in enumerate(sources)],
        "columns": [_column_spec("Source"), _column_spec("Calls", True),
                    _column_spec("Tokens added", True), _column_spec("Avg call", True),
                    _column_spec("Cost", True), _column_spec("Share", True)],
        "rows": [[
            {"text": source["tool"], "swatch": _shade(index, len(sources)),
             "tip": source_tip(source["tool"], source["count"])},
            {"text": source["count"], "n": True},
            {"text": _tokens(source["added"]), "n": True},
            {"text": _tokens(source["added"] / max(1, source["count"])), "n": True},
            {"text": "$" + _money(source["cost"]), "n": True},
            {"text": f'{round(source["cost"] / total_sources * 100)} %', "n": True},
        ] for index, source in enumerate(sources)],
        "empty": "No source carried a cost in this session.",
    }

    rows = session["entries"]
    turn_total = sum(entry["cost"] for entry in rows) or 1
    turns = {
        "title": "Turn by turn",
        "controls": (_turn_form(payload, action="/session")
                     if payload.get("served") else ""),
        "columns": [_column_spec("Turn", True), _column_spec("What the turn added"),
                    _column_spec("Tokens", True), _column_spec("Context", True),
                    _column_spec("Cost", True), _column_spec("Share", True)],
        "rows": [[
            {"text": entry["turn"], "n": True},
            {"text": entry["label"], "label": True, "title": entry["label"]},
            {"text": _tokens(entry["added"]), "n": True},
            {"text": _tokens(entry["ctx"]), "n": True},
            {"text": "$" + _money(entry["cost"]), "n": True},
            {"text": f'{round(100 * entry["cost"] / turn_total)} %', "n": True},
        ] for entry in rows],
        "empty": "No turn matches the filter.",
        "notes": [],
    }
    total_rows = session.get("entries_total", len(rows))
    if rows and total_rows > len(rows):
        turns["notes"].append(f"{len(rows)} of {total_rows} matching turns shown.")
    sections = [breakdown, turns]

    if session.get("agents"):
        sections.append({
            "title": "Subagents",
            "hint": ("Each runs its own context, off the main chain. What the main "
                     "session paid is only the report handed back — the Agent "
                     "line above; what the run itself cost is here."),
            "columns": ["Agent", _column_spec("Type"), _column_spec("Turns", True),
                        _column_spec("Output", True), _column_spec("Cost", True)],
            "rows": [[
                {"text": agent["label"], "label": True, "title": agent["label"]},
                agent["type"] or "—",
                {"text": agent["turns"], "n": True},
                {"text": _tokens(agent["output"]), "n": True},
                {"text": "$" + _money(agent["cost"]), "n": True},
            ] for agent in session["agents"]],
        })

    if standalone:
        sections.append({
            "title": "Session record",
            "hint": "What the transcript states about the run itself, unaggregated.",
            "html": metadata_list([
                ("Session", session["id"]),
                ("Project", session["project"]),
                ("Working directory", session["cwd"] or "—"),
                ("Branch", session.get("branch") or "—"),
                ("Started", _clock(session["start"])),
                ("Ended", _clock(session["end"])),
                ("Cost figure", "counter" if session["exact"] else "transcript floor"),
                ("Slash commands", ", ".join(session["commands"]) or "—"),
            ]),
        })
    return sections


def claude_session_view(session: dict, payload: dict, standalone: bool) -> dict:
    """Normalize one Claude session for the shared session front."""
    billed = session.get("billed") or {}
    billed_total = billed.get("total") or 0
    cache = session.get("cache") or {}
    facts = [("Context", "$" + _money(session["cost_context"])),
             ("Generation", "$" + _money(session["cost_output"]))]
    if session.get("cost_agents"):
        facts.append(("Subagents", "$" + _money(session["cost_agents"])))
    if cache.get("count"):
        facts.append(("Cache rebuilt", "$" + _money(cache["extra_cost"])))
    facts += [
        ("Input tokens billed", _tokens(billed_total)),
        ("Served from cache",
         f'{round(100 * billed.get("cache_read", 0) / billed_total)} %'
         if billed_total else "—"),
        ("Written to cache", _tokens(billed.get("cache_write", 0))),
        ("Uncached input", _tokens(billed.get("fresh", 0))),
        ("Tokens produced", _tokens(session["output"])),
        ("Peak context", _tokens(session.get("context_peak")
                                 or session["context_final"])),
        (("Accounted for by transcript", f'{session["restitution"]} %')
         if session["exact"] else ("Internal counter", "missing")),
    ]

    run = [{"label": _span(session["start"], session["end"])}]
    if elapsed := _elapsed(session["start"], session["end"]):
        run.append({"value": elapsed, "label": "elapsed"})
    run += [
        {"value": str(session["turns"]),
         "label": "turn" + ("s" if session["turns"] != 1 else "")},
        {"value": str(session["prompts"]),
         "label": "prompt" + ("s" if session["prompts"] != 1 else "")},
        {"value": str(session["compactions"]),
         "label": "compaction" + ("s" if session["compactions"] != 1 else "")},
    ]

    badges = [{"kind": "exact" if session["exact"] else "floor",
               "label": "exact" if session["exact"] else "floor",
               "tip": EXACT_TIP if session["exact"] else FLOOR_TIP}]
    if grade := session.get("grade"):
        badges.append({"kind": f"grade g{grade.lower()}", "label": grade,
                       "tip": GRADE_TIP.format(grade=grade,
                                               score=session.get("score") or 0)})

    notes = []
    if cache.get("count"):
        gap = cache.get("median_gap")
        when = f", after a {gap / 60:.0f} min pause on median" if gap else ""
        plural = "s" if cache["count"] != 1 else ""
        notes.append(f'The prompt cache had expired on {cache["count"]} turn{plural}'
                     f'{when}: {_tokens(cache["tokens"])} tokens were written again '
                     f"rather than read back, ${_money(cache['extra_cost'])} of "
                     "avoidable cost.")
    if session["skills"]:
        listing = ", ".join(f'{skill["name"]} (${_money(skill["cost"])})'
                            for skill in session["skills"])
        notes.append(f"Turns attributed to a skill: {listing}.")

    view = {
        "eyebrow": "Session",
        "id": session["short"],
        "tags": [session["branch"]] if session.get("branch") else [],
        "title": session["project"],
        "metric": {"prefix": "$", "value": _money(session["cost"])},
        "badges": badges,
        "run": run,
        "facts": [{"label": label, "value": value, "tip": FACT_TIPS.get(label)}
                  for label, value in facts],
        "triage": _claude_triage(session),
        "chart": _claude_chart(session),
        "sections": _claude_sections(session, payload, standalone),
        "notes": notes,
    }
    if payload.get("served"):
        view["back"] = {"href": "/" + _query(_state(payload)), "label": "All sessions"}
    return view


def render_session_detail(session: dict, payload: dict, standalone: bool) -> list:
    """Renders one session through the front shared with the other sources."""
    return [render_session_view(claude_session_view(session, payload, standalone),
                                standalone)]


def render_yield(payload: dict) -> list:
    """The git outcome of the window: what landed, what came back, what never shipped."""
    data = payload.get("yield") or {}
    esc = html_escape
    out = ['<section><h2>What it left in git</h2>']
    repos = data.get("repos") or []
    if not repos:
        out.append('<p class="note">No session in this window ran inside a git '
                   "repository, so there is nothing to correlate.</p></section>")
        return out

    served = bool(payload.get("served"))
    state = _state(payload)
    outcomes = data["outcomes"]
    total = sum(value["cost"] for value in outcomes.values()) or 1
    count = len(YIELD_OUTCOMES)

    out.append('<div class="stack">')
    for i, name in enumerate(YIELD_OUTCOMES):
        share = outcomes[name]["cost"] / total * 100
        if share <= 0:
            continue
        out.append(f'<span style="width:{share:.2f}%;background:{_shade(i, count)}" '
                   f'title="{esc(YIELD_LABELS[name])} — '
                   f'${_money(outcomes[name]["cost"])}"></span>')
    out.append("</div>")
    out.append('<div class="legend">')
    for i, name in enumerate(YIELD_OUTCOMES):
        value = outcomes[name]
        out.append(f'<span><span class="swatch" style="background:{_shade(i, count)}">'
                   f'</span>{esc(YIELD_LABELS[name])} · ${_money(value["cost"])} '
                   f'· {100 * value["cost"] / total:.0f}%</span>')
    out.append("</div>")

    correlated = sum(value["sessions"] for value in outcomes.values())
    facts = [("Sessions correlated", str(correlated)),
             ("Commits landed", str(data["landed_commits"])),
             ("Cost per landed commit",
              f'${_money(data["cost_per_landed"])}' if data["cost_per_landed"]
              else "—"),
             ("Nothing on the mainline",
              f'${_money(total - outcomes["landed"]["cost"])}')]
    out.append('<div class="facts">')
    for label, value in facts:
        tip = FACT_TIPS.get(label)
        eyebrow = (f'<span class="eyebrow src" tabindex="0" data-tip="{esc(tip)}">'
                   f"{esc(label)}</span>" if tip
                   else f'<span class="eyebrow">{esc(label)}</span>')
        out.append(f'<div class="fact">{eyebrow}<b>{esc(value)}</b></div>')
    out.append("</div>")

    out.append('<div class="scroll"><table><thead><tr>'
               + _th("Repo") + _th("Mainline") + _th("Sess.", numeric=True)
               + "".join(_th(YIELD_SHORT[name], numeric=True)
                         for name in YIELD_OUTCOMES)
               + _th("Cost", numeric=True) + "</tr></thead><tbody>")
    for repo in repos:
        cells = "".join(f'<td class="n">{repo["outcomes"][name]["sessions"]}</td>'
                        for name in YIELD_OUTCOMES)
        out.append(f'<tr><td>{esc(repo["name"])}</td>'
                   f'<td><span class="branch">{esc(repo["mainline"])}</span></td>'
                   f'<td class="n">{repo["sessions"]}</td>{cells}'
                   f'<td class="n">${_money(repo["cost"])}</td></tr>')
    out.append("</tbody></table></div>")

    unshipped = [s for s in data["sessions"] if s["outcome"] != "landed"][:6]
    if unshipped:
        out.append('<span class="eyebrow">Costliest sessions with nothing on '
                   "the mainline</span>")
        out.append('<div class="rows">')
        peak = max(s["cost"] for s in unshipped) or 1
        for rank, session in enumerate(unshipped, 1):
            share = max(1.5, (session["cost"] / peak) * 100)
            meta = f'{_clock(session["start"])} · {YIELD_LABELS[session["outcome"]]}'
            if session["commits"]:
                meta += " · " + _count(len(session["commits"]), "commit")
            body = (f'<span class="row-fill"></span>'
                    f'<span class="rank">{rank:02d}</span>'
                    f'<span class="row-main"><span class="row-title">'
                    f'<span class="proj">{esc(session["project"])}</span>'
                    f'<span class="id">{esc(session["short"])}</span></span>'
                    f'<span class="row-meta">{esc(meta)}</span></span>'
                    f'<span class="row-cost">${_money(session["cost"])}</span>')
            if served:
                target = "/session" + _query(state, id=session["id"])
                out.append(f'<a class="row" style="--share:{share:.1f}%" '
                           f'href="{esc(target)}">{body}</a>')
            else:
                out.append(f'<div class="row" style="--share:{share:.1f}%">{body}</div>')
        out.append("</div>")

    notes = []
    if data["unmatched_commits"]:
        notes.append(_count(data["unmatched_commits"], "commit")
                     + " were authored outside every session")
    if data["outside_git"]:
        notes.append(_count(data["outside_git"], "session")
                     + " ran outside a git repo")
    if data["skipped_sessions"]:
        notes.append(_count(data["skipped_sessions"], "session")
                     + " in smaller repos were left out")
    tail = (" " + "; ".join(notes) + ".") if notes else ""
    out.append('<p class="note">Outcomes come from git, correlated on time: a commit '
               "authored while a session ran — from two minutes before its first "
               "turn to half an hour after its last — is attributed to it, then "
               "checked against the mainline. A session without a commit is not waste "
               "on its own: reading, debugging and planning end that way. What matters "
               f"is how much of the bill sits there, week after week.{esc(tail)}</p>")
    out.append("</section>")
    return out


GRADE_KINDS = {
    "junk-reads": "junk reads",
    "duplicate-reads": "duplicate reads",
    "cache": "cache rebuilds",
    "compaction": "compaction",
    "replayed-output": "replayed output",
    "instructions": "instructions",
}
GRADE_NOTE = ("The grade counts what a session could have avoided, as a share of what "
              "it cost.\n  Size is not a fault: a long session that wasted nothing "
              "still scores A.")


def _lead_text(row: dict) -> str:
    lead = row.get("lead")
    if not lead:
        return "\u2014"
    return (f"{GRADE_KINDS.get(lead['kind'], lead['kind'])} "
            f"{lead['share']:.0f}% (${_money(lead['cost'])})")


def print_audit(data: dict, since: datetime | None, top: int = 15):
    period = f" since {since.date()}" if since else ""
    print(f"\nSession grades{period} \u2014 {data['parsed']} of "
          f"{_count(data['pool'], 'session')} read")
    if not data["rows"]:
        print("No session to grade in this window.")
        return
    spread = " \u00b7 ".join(f"{letter} {count}"
                             for letter, count in data["spread"].items() if count)
    print(f"  Median grade {data['grade']} (score {data['score']:.1f})   {spread}\n")
    worst = sorted(data["rows"], key=lambda row: (-row["score"], -row["cost"]))
    hidden = max(0, len(worst) - top)
    rows = [[row["grade"], row["short"], _short(row["project"], 22),
             f"{row['cost']:,.2f}", f"{row['score']:.0f}", _lead_text(row)]
            for row in worst[:top]]
    if hidden:
        rows.append([f"({hidden} more)", "", "", "", "", ""])
    render_table(["Grade", "Session", "Project", "Cost $", "Score",
                  "What weighs most"], rows, ["l", "l", "l", "r", "r", "l"])
    if data["kinds"]:
        parts = ", ".join(f"{GRADE_KINDS.get(kind, kind)} ${_money(cost)}"
                          for kind, cost in data["kinds"].items())
        print(f"\nAvoidable across those sessions: {parts}.")
    print(f"  {GRADE_NOTE}")


def print_session_grade(detail: dict):
    findings = sorted(detail.get("triage") or [],
                      key=lambda finding: (-finding["weight"], -finding["cost"]))
    print(f"\nGrade {detail['grade']} (score {detail['score']:.0f}) \u2014 what this "
          "session could have avoided\n")
    if not findings:
        print("  Nothing stood out.")
        return
    for rank, finding in enumerate(findings, 1):
        penalty = (f"{finding['weight']:.0f} of the score" if finding["weight"]
                   else "no penalty")
        print(f"{rank}. ${_money(finding['cost'])} \u00b7 {finding['share']:.0f}% "
              f"\u00b7 {penalty}")
        print(f"   {finding['reason']}")
        for item in finding["items"]:
            print(f"     \u00b7 {item}")
        print(f"   \u2192 {finding['advice']}")
    print(f"\n  {GRADE_NOTE}")


def render_footer(payload: dict) -> list:
    return ["<footer>",
            "<div>Cost at public API list price — not what a subscription bills.</div>",
            f'<div>Generated {_clock(payload["generated"])}.</div>',
            "</footer>"]


def render_session_page(payload: dict) -> str:
    out = ['<main class="wrap" id="top">']
    out += render_session_detail(payload["focus"], payload, standalone=True)
    out += render_footer(payload)
    out.append("</main>")
    return "\n".join(out)


def window_meta(since, days) -> dict:
    return {"since": since.isoformat() if since else None, "days": days,
            "label": f"since {since.date()}" if since else "all history"}


def _render_body_legacy(payload: dict) -> str:
    """Builds the page body as HTML, with no JavaScript.

    Everything is computed here: a browser running no scripts, a preview pane or
    an offline reader all show the same figures.
    """
    if payload.get("focus"):
        return render_session_page(payload)

    esc = html_escape
    listing = payload.get("listing") or {}
    state = _state(payload)
    served = bool(payload.get("served"))

    out = ['<div class="wrap">', "<header>"]
    out.append("<h1>Claude Code Token Usage</h1>")
    out.append('<p class="lede">What the sessions cost, project by project, then what '
               "filled the context inside each one. Rebuilt locally from the "
               "transcripts; nothing leaves the machine.</p>")

    if served:
        current = payload["window"].get("days")
        links = ['<div class="controls"><span class="eyebrow">Window</span>',
                 '<div class="segment">']
        for days in (7, 30, 90, 365):
            label = "1 year" if days >= 365 else f"{days} d"
            mark = ' aria-current="page"' if current == days else ""
            links.append(f'<a href="{esc(_query(state, days=days))}"{mark}>{label}</a>')
        links.append('</div><span class="live">recomputed on every load</span></div>')
        out.append("".join(links))

    totals = payload["totals"]
    stats = [
        ("Total cost", _money(totals["cost_usd"]), "$", True),
        ("Sessions", str(totals["sessions"]), "", False),
        ("Projects", str(len(payload["projects"])), "", False),
        ("Cache-read tokens", _tokens(totals["cache_read"]), "", False),
        ("Tokens produced", _tokens(totals["output"]), "", False),
    ]
    out.append('<div class="headline">')
    for label, value, unit, lead in stats:
        cls = "stat lead" if lead else "stat"
        prefix = f'<span class="unit">{unit}</span>' if unit else ""
        out.append(f'<div class="{cls}"><span class="eyebrow">{label}</span>'
                   f"<b>{prefix}{value}</b></div>")
    out.append("</div>")

    out.append("</header>")

    daily = payload["daily"]
    out.append("<section><h2>Day by day</h2>")
    if daily:
        W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 220, 54, 14, 16, 26
        inner_w, inner_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
        peak_value = max(d["cost_usd"] for d in daily) or 1
        span = max(1, len(daily) - 1)

        def px(i):
            return PAD_L + (inner_w / 2 if len(daily) == 1 else (i / span) * inner_w)

        def py(v):
            return PAD_T + inner_h - (v / peak_value) * inner_h

        svg = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Daily cost">',
               '<defs><linearGradient id="areaFade" x1="0" y1="0" x2="0" y2="1">'
               '<stop class="fade-top" offset="0"/>'
               '<stop class="fade-bottom" offset="1"/></linearGradient></defs>']
        for frac in (0, 0.5, 1):
            y = PAD_T + inner_h * frac
            svg.append(f'<line class="grid-line" x1="{PAD_L}" y1="{y:.1f}" '
                       f'x2="{W - PAD_R}" y2="{y:.1f}"/>')
            svg.append(f'<text class="axis" x="{PAD_L - 10}" y="{y + 3.5:.1f}" '
                       f'text-anchor="end">${_money(peak_value * (1 - frac))}</text>')
        path = " ".join(f'{"L" if i else "M"}{px(i):.1f},{py(d["cost_usd"]):.1f}'
                        for i, d in enumerate(daily))
        svg.append(f'<path class="area" d="{path} L{px(len(daily) - 1):.1f},'
                   f'{PAD_T + inner_h} L{px(0):.1f},{PAD_T + inner_h} Z"/>')
        svg.append(f'<path class="spark" d="{path}"/>')
        svg.append(f'<circle class="cap" cx="{px(len(daily) - 1):.1f}" '
                   f'cy="{py(daily[-1]["cost_usd"]):.1f}" r="4"/>')
        band = inner_w / max(1, span)
        for i, point in enumerate(daily):
            cx, cy = px(i), py(point["cost_usd"])
            plural = "s" if point["sessions"] != 1 else ""
            readout = (f'{_day(point["date"])} \u00b7 ${_money(point["cost_usd"])} '
                       f'\u00b7 {point["sessions"]} session{plural}')
            svg.append(f'<rect class="hit" x="{cx - band / 2:.1f}" y="{PAD_T}" '
                       f'width="{band:.2f}" height="{inner_h:.1f}"/>')
            svg.append(
                f'<g class="readout">'
                f'<line class="guide" x1="{cx:.1f}" y1="{PAD_T}" '
                f'x2="{cx:.1f}" y2="{PAD_T + inner_h:.1f}"/>'
                f'<circle class="guide-dot" cx="{cx:.1f}" cy="{cy:.1f}" r="4"/>'
                f'<text class="readout-tag" x="{PAD_L + 4}" y="{PAD_T + 13}">'
                f'{esc(readout)}</text></g>')
        marks = range(len(daily)) if len(daily) <= 6 else (0, span // 2, span)
        for i in marks:
            anchor = "start" if i == 0 else "end" if i == span else "middle"
            svg.append(f'<text class="axis" x="{px(i):.1f}" y="{H - 6}" '
                       f'text-anchor="{anchor}">{_day(daily[i]["date"])}</text>')
        svg.append("</svg>")
        out.append(f'<div class="chart">{"".join(svg)}</div>')
        peak = max(daily, key=lambda d: d["cost_usd"])
        plural = "s" if peak["sessions"] != 1 else ""
        out.append(f'<p class="note">Peak on {_day(peak["date"])} at '
                   f'${_money(peak["cost_usd"])}, across {peak["sessions"]} '
                   f'session{plural}.</p>')
    else:
        out.append('<p class="note">No activity in this window.</p>')
    out.append("</section>")

    out.append("<section><h2>Sessions</h2>")

    if served:
        sort_sessions = listing.get("sort_sessions", DEFAULT_SESSION_SORT)
        reset = ""
        if listing.get("filter_sessions") or sort_sessions != DEFAULT_SESSION_SORT:
            reset = f'<a class="reset" href="{esc(_query(state, sort="", q="", max=""))}">Clear</a>'
        out.append('<div class="filters">')
        out.append(
            '<form class="filter" method="get">'
            + _hidden("days", state["days"]) + _hidden("tsort", state["tsort"])
            + _hidden("tq", state["tq"])
            + '<span class="eyebrow">Sessions</span>'
            + _select("sort", list(SESSION_SORTS.items()), sort_sessions, "Sort")
            + '<label class="grow"><span>Filter</span><input type="search" name="q" '
            + f'placeholder="project or id" value="{esc(state["q"])}"></label>'
            + '<label><span>How many</span><input type="number" name="max" min="1" max="60" '
            + f'value="{int(listing.get("limit", 5))}"></label>'
            + '<button type="submit">Apply</button>' + reset + "</form>")
        out.append("</div>")

        matched = listing.get("matched", 0)
        pool = listing.get("pool", 0)
        shown = len(payload["sessions"])
        plural = "s" if shown != 1 else ""
        recap = f"{shown} session{plural} detailed"
        if listing.get("filter_sessions"):
            recap += (f' of {matched} matching “{esc(state["q"])}”, '
                      f"out of {pool} in total")
        elif matched < pool:
            recap += f" of {pool}"
        if listing.get("filter_turns"):
            recap += f'. Turns limited to “{esc(state["tq"])}”'
        out.append(f'<p class="note">{recap}.</p>')

    if not payload["sessions"]:
        out.append('<p class="empty">No session matches. Widen the window or clear '
                   "the filter.</p>")
    else:
        out.append('<div class="rows">')
        peak_session = max(s["cost"] for s in payload["sessions"]) or 1
        for rank, session in enumerate(payload["sessions"], 1):
            compact_plural = "s" if session["compactions"] != 1 else ""
            prompt_plural = "s" if session["prompts"] != 1 else ""
            turn_plural = "s" if session["turns"] != 1 else ""
            meta = (f'{_clock(session["start"])} · {session["turns"]} turn{turn_plural} · '
                    f'{session["prompts"]} prompt{prompt_plural} · '
                    f'{session["compactions"]} compaction{compact_plural}')
            target = (_query(state, id=session["id"]) if served
                      else f'#s-{session["short"]}')
            prefix = "/session" if served else ""
            share = max(1.5, (session["cost"] / peak_session) * 100)
            out.append(
                f'<a class="row" style="--share:{share:.1f}%" '
                f'href="{esc(prefix + target)}">'
                f'<span class="row-fill"></span>'
                f'<span class="rank">{rank:02d}</span>' 
                f'<span class="row-main"><span class="row-title">'
                f'<span class="proj">{esc(session["project"])}</span>'
                f'<span class="id">{esc(session["short"])}</span>'
                f'{_chip(session["exact"], focusable=False)}'
                f'{_grade_chip(session, focusable=False)}</span>'
                f'<span class="row-meta">{esc(meta)}</span></span>'
                f'<span class="row-cost">${_money(session["cost"])}</span></a>')
        out.append("</div>")
    out.append("</section>")

    top_cost = payload["projects"][0]["cost_usd"] if payload["projects"] else 1
    out.append("<section><h2>Cost by project</h2>")
    out.append('<div class="bars">')
    for project in payload["projects"]:
        width = max(0.6, (project["cost_usd"] / top_cost) * 100) if top_cost else 0.6
        plural = "s" if project["sessions"] != 1 else ""
        tip = (f'{project["path"]} — {_tokens(project["cache_read"])} cache-read '
               f'tokens, {_tokens(project["output"])} produced')
        out.append(
            f'<div class="bar-row" title="{esc(tip)}">'
            f'<div class="bar-name">{esc(project["name"])}</div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{width:.1f}%"></div></div>'
            f'<div class="bar-value">${_money(project["cost_usd"])}'
            f'<small>{project["sessions"]} session{plural}</small></div></div>')
    out.append("</div></section>")

    if payload.get("yield"):
        out += render_yield(payload)

    if not served:
        for session in payload["sessions"]:
            out.append(f'<section id="s-{esc(session["short"])}">')
            out.append(f'<h2>{esc(session["project"])} '
                       f'<span class="id">{esc(session["short"])}</span> — '
                       f'${_money(session["cost"])}</h2>')
            out += render_session_detail(session, payload, standalone=False)
            out.append("</section>")

    out += render_footer(payload)
    out.append("</div>")
    return "\n".join(out)


def render_body(payload: dict) -> str:
    """Render the common dashboard overview plus Claude-specific session evidence."""
    if payload.get("focus"):
        return render_session_page(payload)

    esc = html_escape
    view = claude_dashboard_model(payload)
    state = _state(payload)
    served = bool(payload.get("served"))
    period_controls = ""
    session_controls = ""
    if served:
        current = payload["window"].get("days")
        links = ['<div class="controls"><span class="eyebrow">Window</span><div class="segment">']
        for days in (7, 30, 90, 365):
            label = "1 year" if days >= 365 else f"{days} d"
            mark = ' aria-current="page"' if current == days else ""
            links.append(f'<a href="{esc(_query(state, days=days))}"{mark}>{label}</a>')
        links.append('</div><span class="live">recomputed on every load</span></div>')
        period_controls = "".join(links)

    listing = payload.get("listing") or {}
    sessions_note = ""
    if served:
        sort_sessions = listing.get("sort_sessions", DEFAULT_SESSION_SORT)
        reset = ""
        if listing.get("filter_sessions") or sort_sessions != DEFAULT_SESSION_SORT:
            reset = f'<a class="reset" href="{esc(_query(state, sort="", q="", max=""))}">Clear</a>'
        session_controls = ('<div class="filters"><form class="filter" method="get">'
                     + _hidden("source", state["source"]) + _hidden("days", state["days"])
                     + _hidden("tsort", state["tsort"])
                     + _hidden("tq", state["tq"]) + '<span class="eyebrow">Sessions</span>'
                     + _select("sort", list(SESSION_SORTS.items()), sort_sessions, "Sort")
                     + '<label class="grow"><span>Filter</span><input type="search" name="q" '
                     + f'placeholder="project or id" value="{esc(state["q"])}"></label>'
                     + '<label><span>How many</span><input type="number" name="max" min="1" max="60" '
                     + f'value="{int(listing.get("limit", 5))}"></label><button type="submit">Apply</button>'
                     + reset + '</form></div>')
        sessions_note = f'<p class="note">{len(payload["sessions"])} detailed session(s).</p>'

    links = {
        session["short"]: ("/session" + _query(state, id=session["id"]) if served
                           else f'#s-{session["short"]}')
        for session in payload["sessions"]
    }
    details = ""
    if not served:
        details = "\n".join(
            f'<section id="s-{esc(session["short"])}"><h2>{esc(session["project"])} '
            f'<span class="id">{esc(session["short"])}</span> — ${_money(session["cost"])}</h2>'
            + "\n".join(render_session_detail(session, payload, standalone=False)) + '</section>'
            for session in payload["sessions"]
        )
    return render_dashboard_overview(
        view, source_controls=source_tabs(payload.get("dashboard_source"),
                                          payload["window"].get("days")),
        period_controls=period_controls, session_controls=session_controls,
        sessions_note=sessions_note, session_links=links,
        after_projects="\n".join(render_yield(payload)) if payload.get("yield") else "",
        details=details)


def write_dashboard(payload: dict, out_path: Path):
    """Write a standalone Claude dashboard from its fragment and shared base."""
    out_path.write_text(render_dashboard_document(
        Path(__file__), "Claude Code Token Usage", render_body(payload)),
        encoding="utf-8")
    return out_path


def analyze_session(needle: str, table, memo, top: int):
    path = find_transcript(needle)
    if path is None:
        print(f"No transcript matches '{needle}'.")
        return
    turns, results, prompts, compactions, meta = read_session(path)
    if not turns:
        print("Transcript has no usable assistant turn.")
        return
    main = [t for t in turns if not t.sidechain]
    side = [t for t in turns if t.sidechain]

    total_in = total_out = agents_cost = 0.0
    for turn in turns:
        rates = resolve_rates(turn.model, table, memo)
        cost_in = input_cost(turn.usage, rates)
        cost_out = output_cost(turn.usage, rates)
        if turn.sidechain:
            agents_cost += cost_in + cost_out
            continue
        total_in += cost_in
        total_out += cost_out

    ctx_sum = sum(t.ctx for t in main) or 1
    main_in = 0.0
    for turn in main:
        main_in += input_cost(turn.usage, resolve_rates(turn.model, table, memo))
    per_ctx_token = main_in / ctx_sum

    entries = attribute(main, results, meta["compact_mids"])
    for e in entries:
        e["cost"] = e["added"] * e["carried"] * per_ctx_token

    first, last = main[0].ts, main[-1].ts
    def clock(ts):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except Exception:
            return None
    start, end = clock(first), clock(last)
    duration = ""
    if start and end:
        minutes = int((end - start).total_seconds() // 60)
        duration = f"{minutes // 60} h {minutes % 60:02d}"

    print(f"\nSession {path.stem[:8]} — {os.path.basename(meta['cwd'].rstrip('/')) or '?'}"
          f"{(' (' + meta['branch'] + ')') if meta['branch'] else ''}")
    if start:
        print(f"  {start:%Y-%m-%d %H:%M} → {end:%H:%M}   {duration}")
    print(f"  {len(main)} main turns · {len(side)} subagent turns · "
          f"{prompts} prompts · {compactions} compaction(s)")
    print(f"  Final context {human(main[-1].ctx)} tokens · "
          f"output {human(sum(t.output for t in turns))} tokens")
    breakdown = f"context {total_in:,.2f} · generation {total_out:,.2f}"
    if agents_cost:
        breakdown += f" · subagents {agents_cost:,.2f}"
    print(f"  Cost ${total_in + total_out + agents_cost:,.2f}  ({breakdown})")
    reported = meta.get("reported")
    scale = 1.0
    if reported:
        ref = float(reported.get("totalCostUSD") or 0.0)
        mine = total_in + total_out + agents_cost
        if ref > 0 and mine > 0:
            scale = ref / mine
        print(f"  Claude Code counter (authoritative) ${ref:,.2f} — "
              f"the transcript accounts for only {100 / scale if scale else 0:.0f}%")
        if reported.get("modelUsage"):
            for model, usage in sorted(reported["modelUsage"].items(),
                                       key=lambda kv: -(kv[1].get("costUSD") or 0)):
                print(f"    {model:28} ${float(usage.get('costUSD') or 0):7,.2f}")
        print("  Amounts below are rescaled onto that counter; the breakdown itself "
              "comes from the transcript.")

    starts, _flagged = segment_starts(main, meta["compact_mids"])
    cache = cache_rebuilds(main, table, memo, starts)
    if scale != 1.0:
        for e in entries:
            e["cost"] *= scale
        total_out *= scale
        total_in *= scale
        agents_cost *= scale
        cache["extra_cost"] *= scale
    grouped = defaultdict(lambda: {"added": 0.0, "cost": 0.0, "count": 0})
    for e in entries:
        g = grouped[e["tool"]]
        g["added"] += e["added"]
        g["cost"] += e["cost"]
        g["count"] += 1
    ranked = sorted(grouped.items(), key=lambda kv: -kv[1]["cost"])
    grand = sum(g["cost"] for _, g in ranked) + total_out + agents_cost or 1
    print("\nWhat filled the context — cost = tokens added x turns that carry them\n")
    rows = [
        [tool, str(g["count"]), human(int(g["added"])),
         human(int(g["added"] / max(1, g["count"]))), f"{g['cost']:,.2f}",
         f"{100 * g['cost'] / grand:.0f}%"]
        for tool, g in ranked
    ]
    main_out = sum(t.output for t in main)
    rows.append(["(response generation)", str(len(main)), human(main_out),
                 human(main_out // max(1, len(main))),
                 f"{total_out:,.2f}", f"{100 * total_out / grand:.0f}%"])
    if agents_cost:
        side_out = sum(t.output for t in side)
        rows.append(["(subagents)", str(len(meta["agents"])), human(side_out),
                     human(side_out // max(1, len(meta["agents"]))),
                     f"{agents_cost:,.2f}", f"{100 * agents_cost / grand:.0f}%"])
    render_table(["Source", "Calls", "Tokens added", "Avg call", "Cost $", "Share"],
                 rows, ["l", "r", "r", "r", "r", "r"])

    print(f"\n{top} costliest individual additions\n")
    render_table(
        ["Turn", "What was added", "Tokens", "Context", "Cost $"],
        [
            [str(e["turn"]), _short(e["label"]), human(int(e["added"])),
             human(e["ctx"]), f"{e['cost']:,.2f}"]
            for e in sorted(entries, key=lambda x: -x["cost"])[:top]
        ],
        ["r", "l", "r", "r", "r"],
    )

    if side:
        by_agent = defaultdict(lambda: {"turns": 0, "cost": 0.0, "out": 0})
        for turn in side:
            rates = resolve_rates(turn.model, table, memo)
            entry = by_agent[turn.agent or "(subagent)"]
            entry["turns"] += 1
            entry["out"] += turn.output
            entry["cost"] += (input_cost(turn.usage, rates)
                              + output_cost(turn.usage, rates)) * scale
        print("\nSubagents (own context, outside the main chain)\n")
        render_table(
            ["Agent", "Turns", "Output", "Cost $"],
            [[a, str(v["turns"]), human(v["out"]), f"{v['cost']:,.2f}"]
             for a, v in sorted(by_agent.items(), key=lambda kv: -kv[1]["cost"])],
            ["l", "r", "r", "r"],
        )

    skills = defaultdict(float)
    for turn in turns:
        if turn.skill:
            rates = resolve_rates(turn.model, table, memo)
            skills[turn.skill] += input_cost(turn.usage, rates) + output_cost(
                turn.usage, rates)
    if cache["count"]:
        gap = cache["median_gap"]
        when = f", after a {gap / 60:.0f} min gap on median" if gap else ""
        print(f"\nCache rebuilt from scratch on {cache['count']} turn(s){when}: "
              f"{human(cache['tokens'])} tokens rewritten at the write rate\n"
              f"instead of read back at a tenth of it — "
              f"${cache['extra_cost']:,.2f} of avoidable cost.\n"
              "The prompt cache expires after 5 minutes; a longer pause pays for the "
              "whole prefix again.")

    if skills:
        print("\nTurns attributed to a skill\n")
        render_table(["Skill", "Cost $"],
                     [[k, f"{v:,.2f}"] for k, v in sorted(skills.items(), key=lambda kv: -kv[1])],
                     ["l", "r"])
    graded = session_payload(path, table, memo, top=0)
    if graded:
        print_session_grade(graded)
    print("\nCost = public API list price. Attribution spreads the measured context "
          "growth;\nparallel calls share their delta in proportion to result size.")


def print_triage(data: dict, since: datetime | None):
    period = f" since {since.date()}" if since else ""
    findings = data.get("findings") or []
    print(f"\nWhat to inspect{period} — {data.get('scanned', 0)} costly session(s) checked\n")
    if not findings:
        print("No costly pattern stood out in the sessions checked.")
        return
    for rank, finding in enumerate(findings, 1):
        print(f"{rank}. ${finding['cost']:,.2f} — {finding['project']} {finding['short']}")
        print(f"   {finding['reason']}")
        print(f"   → {finding['advice']}")


def print_yield(data: dict, since: datetime | None, top: int = 8):
    period = f" since {since.date()}" if since else ""
    repos = data["repos"]
    if not repos:
        print(f"\nYield{period}\n")
        print("No session in this window ran inside a git repository.")
        return
    counted = sum(value["sessions"] for value in data["outcomes"].values())
    print(f"\nWhat the sessions left in git{period} — {_count(len(repos), 'repo')}, "
          f"{_count(counted, 'session')}\n")
    total_cost = sum(value["cost"] for value in data["outcomes"].values()) or 1
    rows = [[YIELD_LABELS[name],
             str(data["outcomes"][name]["sessions"]),
             f"{data['outcomes'][name]['cost']:,.2f}",
             f"{100 * data['outcomes'][name]['cost'] / total_cost:.0f}%"]
            for name in YIELD_OUTCOMES]
    rows.append(["TOTAL", str(counted), f"{total_cost:,.2f}", ""])
    render_table(["Outcome", "Sess.", "Cost $", "Share"], rows, ["l", "r", "r", "r"])

    if data["cost_per_landed"] is not None:
        print(f"\n${data['cost_per_landed']:,.2f} per commit that landed, across "
              f"{_count(data['landed_commits'], 'commit')}.")
    notes = []
    if data["unmatched_commits"]:
        notes.append(f"{_count(data['unmatched_commits'], 'commit')} fell outside "
                     "every session")
    if data["outside_git"]:
        notes.append(f"{_count(data['outside_git'], 'session')} ran outside a git repo")
    if data["skipped_sessions"]:
        notes.append(f"{_count(data['skipped_sessions'], 'session')} in smaller "
                     "repos left out")
    if notes:
        print("  " + "; ".join(notes) + ".")

    unshipped = [s for s in data["sessions"] if s["outcome"] != "landed"][:top]
    if unshipped:
        print("\nCostliest sessions with nothing on the mainline\n")
        render_table(
            ["Project", "Session", "Start", "Outcome", "Cost $"],
            [[s["project"], s["short"], _clock(s["start"]),
              YIELD_LABELS[s["outcome"]], f"{s['cost']:,.2f}"] for s in unshipped],
            ["l", "l", "l", "l", "r"],
        )
        print("\n  A session without a commit is not waste on its own: reading,"
              "\n  debugging and planning end that way. The number to watch is how"
              "\n  much of the bill sits there week after week.")

    print("\n  Outcomes come from git, correlated on time: commits authored while a"
          "\n  session ran (2 min before its first turn to 30 min after its last)"
          f"\n  and checked against the mainline. Repos: "
          + ", ".join(f"{repo['name']} ({repo['mainline']})" for repo in repos) + ".")


def statusline(table, memo) -> str:
    """One line for Claude Code's own status bar: today, and the session in front of you.

    Claude Code pipes its status JSON on stdin. The figures have to be there in a
    fraction of a second, which the mtime filter makes possible: today's window
    touches only today's transcripts.
    """
    context = {}
    if not sys.stdin.isatty():
        try:
            context = json.loads(sys.stdin.read() or "{}")
        except (json.JSONDecodeError, ValueError):
            context = {}

    midnight = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0)
    today = 0.0
    counted = set()
    for path_str, state in read_cost_states(midnight).items():
        sid = state.get("sessionId") or Path(path_str).stem
        if sid in counted:
            continue
        counted.add(sid)
        today += float(state.get("totalCostUSD") or 0.0)
    bucket = Bucket()
    for msg in iter_messages(midnight, {p for p in read_cost_states(midnight)}):
        bucket.add(msg["usage"], resolve_rates(msg["model"], table, memo),
                   msg["session"], msg["when"])
    today += bucket.cost

    parts = [f"${today:,.2f} today"]
    raw = context.get("transcript_path") or ""
    path = Path(raw) if raw else None
    if path is None or not path.is_file():
        sid = context.get("session_id") or ""
        path = find_transcript(sid) if sid else None
    if path is not None and path.is_file():
        detail = session_payload(path, table, memo, top=0)
        if detail:
            parts.append(f"session ${detail['cost']:,.2f}")
            parts.append(f"ctx {human(detail['context_final'])}")
            cache = detail.get("cache") or {}
            if cache.get("extra_cost", 0) >= 0.5:
                parts.append(f"cache rebuilds ${cache['extra_cost']:,.2f}")
    return " \u00b7 ".join(parts)


def serve(port: int, default_days: int | None, extra: list[str]):
    """Serves the dashboard, re-running the analysis on every load.

    Listens on the loopback interface only: nothing is exposed to the network.
    """
    import http.server
    import subprocess
    import tempfile
    import threading
    import urllib.parse
    import webbrowser

    script = str(Path(__file__).resolve())

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in ("/", "/index.html", "/session"):
                self.send_error(404, "Nothing here")
                return
            params = urllib.parse.parse_qs(parsed.query)

            def one(name, limit=80):
                return ((params.get(name) or [""])[0] or "").strip()[:limit]

            days = default_days
            raw = one("days", 8)
            if raw.isdigit():
                days = max(1, min(3650, int(raw)))
            with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
                out = Path(tmp.name)
            window = list(extra)
            if days and "--since" in window:
                at = window.index("--since")
                del window[at:at + 2]
            cmd = [sys.executable, script, "--dashboard", str(out), "--served"] + window
            if days:
                cmd += ["--days", str(days)]
            if one("sort") in SESSION_SORTS:
                cmd += ["--sort-sessions", one("sort")]
            if one("tsort") in TURN_SORTS:
                cmd += ["--sort-turns", one("tsort")]
            if one("q"):
                cmd += ["--filter-sessions", one("q")]
            if one("tq"):
                cmd += ["--filter-turns", one("tq")]
            limit = one("max", 4)
            if limit.isdigit():
                cmd += ["--sessions-max", str(max(1, min(60, int(limit))))]
            focus = one("id", 64)
            if parsed.path == "/session":
                if not focus.replace("-", "").isalnum():
                    self.send_error(400, "Invalid session id")
                    out.unlink(missing_ok=True)
                    return
                cmd += ["--focus", focus]
            started = time.time()
            try:
                run = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                if run.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                    body = ("<pre>Analysis failed\n\n"
                            + (run.stderr or run.stdout or "")[-4000:] + "</pre>")
                    payload = body.encode()
                    self.send_response(500)
                else:
                    payload = out.read_bytes()
                    self.send_response(200)
                    print(f"  rendered in {time.time() - started:.1f}s"
                          + (f" ({days}-day window)" if days else ""))
            finally:
                out.unlink(missing_ok=True)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Dashboard served at {url}")
    print("Every load re-runs the analysis. Ctrl+C to stop.\n")
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="rolling window, in days (default: 30)")
    parser.add_argument("--since", help="ISO start date (YYYY-MM-DD)")
    parser.add_argument("--by", choices=("repo", "cwd", "dir"), default="repo",
                        help="grouping key (default: git root)")
    parser.add_argument("--split-worktrees", action="store_true",
                        help="count each worktree on its own instead of folding it into its repo")
    parser.add_argument("--top", type=int, metavar="N", help="show only the first N rows")
    parser.add_argument("--models", action="store_true", help="per-model breakdown")
    parser.add_argument("--sessions", type=int, metavar="N",
                        help="the N costliest sessions")
    parser.add_argument("--daily", action="store_true", help="per-day series")
    parser.add_argument("--tools", action="store_true",
                        help="per-tool cost summed over every session in the window "
                             "(parses each transcript, so it is the slow one)")
    parser.add_argument("--tools-max", type=int, default=500, metavar="N",
                        help="cap on the sessions --tools parses (default: 500)")
    parser.add_argument("--triage", action="store_true",
                        help="show the few costly patterns worth inspecting")
    parser.add_argument("--audit", action="store_true",
                        help="grade each session A to F on what it could have avoided "
                             "(the dashboard always shows the letter)")
    parser.add_argument("--audit-max", type=int, default=AUDIT_MAX_SESSIONS,
                        metavar="N",
                        help=f"cap on the sessions --audit parses "
                             f"(default: {AUDIT_MAX_SESSIONS})")
    parser.add_argument("--yield", dest="git_yield", action="store_true",
                        help="what each session left in git: landed, reverted, "
                             "never merged, or no commit at all")
    parser.add_argument("--yield-max", type=int, default=YIELD_MAX_PROJECTS,
                        metavar="N",
                        help=f"cap on the repos --yield queries "
                             f"(default: {YIELD_MAX_PROJECTS})")
    parser.add_argument("--no-git", action="store_true",
                        help="never shell out to git; the dashboard drops its yield "
                             "section")
    parser.add_argument("--project", help="substring filter on the project key")
    parser.add_argument("--session", metavar="ID",
                        help="break down a single session (id prefix or path)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--statusline", action="store_true",
                        help="one compact line for Claude Code's statusLine hook: "
                             "today's cost, then the session on stdin")
    parser.add_argument("--dashboard", metavar="FILE",
                        help="write a self-contained HTML dashboard")
    parser.add_argument("--serve", nargs="?", const=8787, type=int, metavar="PORT",
                        help="serve the dashboard on the loopback interface and re-run "
                             "the analysis on every load (default: port 8787)")
    parser.add_argument("--served", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dashboard-source", choices=("claude", "codex"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--no-cost-state", action="store_true",
                        help="ignore cost-state counters, recompute everything from transcripts")
    parser.add_argument("--no-fetch", action="store_true",
                        help="do not query LiteLLM; use the cache, or no prices at all")
    parser.add_argument("--sort-sessions", choices=tuple(SESSION_SORTS),
                        default=DEFAULT_SESSION_SORT,
                        help="order of the dashboard's detailed sessions "
                             f"(default: {DEFAULT_SESSION_SORT})")
    parser.add_argument("--filter-sessions", metavar="TEXT",
                        help="only detail sessions whose project or id contains TEXT")
    parser.add_argument("--sort-turns", choices=tuple(TURN_SORTS), default="cost-desc",
                        help="order of the turns inside each session")
    parser.add_argument("--filter-turns", metavar="TEXT",
                        help="only keep turns whose label or tool contains TEXT")
    parser.add_argument("--sessions-max", type=int, default=5, metavar="N",
                        help="number of detailed sessions (default: 5)")
    parser.add_argument("--focus", metavar="ID",
                        help="with --dashboard, write the page for a single session")
    args = parser.parse_args()

    if args.serve:
        args.sessions_max = max(1, min(60, args.sessions_max))
        passthrough = []
        for flag, value in (("--by", args.by), ("--project", args.project),
                            ("--since", args.since),
                            ("--yield-max", args.yield_max
                             if args.yield_max != YIELD_MAX_PROJECTS else None)):
            if value and not (flag == "--by" and value == "repo"):
                passthrough += [flag, str(value)]
        for flag, on in (("--split-worktrees", args.split_worktrees),
                         ("--no-cost-state", args.no_cost_state),
                         ("--no-git", args.no_git),
                         ("--no-fetch", args.no_fetch)):
            if on:
                passthrough.append(flag)
        serve(args.serve, args.days, passthrough)
        return

    since = None
    if args.since:
        # Local midnight, to match the day boundaries the day-by-day view uses.
        since = datetime.fromisoformat(args.since).astimezone()
    elif args.days:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    table = build_rate_table(load_prices(not args.no_fetch))
    memo: dict = {}

    if args.statusline:
        try:
            print(statusline(table, memo))
        except Exception as exc:  # a status bar must never take the session down
            print(f"cc-usage: {exc}", file=sys.stderr)
        return

    if args.session:
        analyze_session(args.session, table, memo, args.top or 12)
        return

    if args.focus and args.dashboard:
        found = find_transcript(args.focus)
        detail = session_payload(found, table, memo, top=200,
                                 turn_sort=args.sort_turns,
                                 turn_filter=args.filter_turns or "") if found else None
        if detail is None:
            print(f"Session not found: {args.focus}", file=sys.stderr)
            sys.exit(2)
        payload = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "served": args.served,
            "dashboard_source": args.dashboard_source,
            "window": window_meta(since, args.days),
            "listing": {"sort_sessions": args.sort_sessions,
                        "filter_sessions": args.filter_sessions or "",
                        "sort_turns": args.sort_turns,
                        "filter_turns": args.filter_turns or "",
                        "limit": args.sessions_max, "matched": 0, "pool": 0},
            "focus": detail,
        }
        out = write_dashboard(payload, Path(args.dashboard))
        print(f"Session page written: {out}")
        return

    states = {} if args.no_cost_state else read_cost_states(since)
    measured = set()
    seen_sessions = set()
    projects: dict[str, Bucket] = defaultdict(Bucket)
    models: dict[str, Bucket] = defaultdict(Bucket)
    sessions: dict[tuple, Bucket] = defaultdict(Bucket)
    days: dict[str, Bucket] = defaultdict(Bucket)
    unknown_models = set()

    for path_str, state in states.items():
        path = Path(path_str)
        cwd = branch = ""
        when = None
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"cwd"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("cwd"):
                    cwd = entry["cwd"]
                    branch = entry.get("gitBranch") or ""
                    ts = entry.get("timestamp") or ""
                    if ts:
                        try:
                            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except ValueError:
                            when = None
                    break
        sid = state.get("sessionId") or path.stem
        if sid in seen_sessions:
            measured.add(path_str)
            continue
        seen_sessions.add(sid)
        if since and when and when < since:
            measured.add(path_str)
            continue
        if args.by == "repo":
            key = repo_root(cwd, path.parent.name, args.split_worktrees)
        elif args.by == "cwd":
            key = cwd
        else:
            key = path.parent.name
        key = key or "(unknown)"
        if args.project and args.project not in key:
            measured.add(path_str)
            continue
        measured.add(path_str)
        for model, usage in (state.get("modelUsage") or {}).items():
            bucket = projects[key]
            bucket.messages += 1
            bucket.sessions.add(sid)
            bucket.input += int(usage.get("inputTokens") or 0)
            bucket.output += int(usage.get("outputTokens") or 0)
            bucket.cache_read += int(usage.get("cacheReadInputTokens") or 0)
            bucket.cache_write_5m += int(usage.get("cacheCreationInputTokens") or 0)
            bucket.cost += float(usage.get("costUSD") or 0.0)
            if when:
                bucket.first = when if bucket.first is None or when < bucket.first else bucket.first
                bucket.last = when if bucket.last is None or when > bucket.last else bucket.last
            mb = models[model]
            mb.messages += 1
            mb.sessions.add(sid)
            mb.input += int(usage.get("inputTokens") or 0)
            mb.output += int(usage.get("outputTokens") or 0)
            mb.cache_read += int(usage.get("cacheReadInputTokens") or 0)
            mb.cache_write_5m += int(usage.get("cacheCreationInputTokens") or 0)
            mb.cost += float(usage.get("costUSD") or 0.0)

            for target in (sessions[(key, sid)],
                           days[day_key(when)] if when else None):
                if target is None:
                    continue
                target.messages += 1
                target.sessions.add(sid)
                target.input += int(usage.get("inputTokens") or 0)
                target.output += int(usage.get("outputTokens") or 0)
                target.cache_read += int(usage.get("cacheReadInputTokens") or 0)
                target.cache_write_5m += int(usage.get("cacheCreationInputTokens") or 0)
                target.cost += float(usage.get("costUSD") or 0.0)
                if when:
                    target.first = when if target.first is None or when < target.first else target.first
                    target.last = when if target.last is None or when > target.last else target.last

    for msg in iter_messages(since, measured):
        if args.by == "repo":
            key = repo_root(msg["cwd"], msg["dir"], args.split_worktrees)
        elif args.by == "cwd":
            key = msg["cwd"]
        else:
            key = msg["dir"]
        key = key or "(unknown)"
        if args.project and args.project not in key:
            continue
        rates = resolve_rates(msg["model"], table, memo)
        if rates is None and msg["model"] not in FREE_MODELS:
            unknown_models.add(msg["model"])
        payload = (msg["usage"], rates, msg["session"], msg["when"])
        projects[key].add(*payload)
        models[msg["model"]].add(*payload)
        sessions[(key, msg["session"])].add(*payload)
        if msg["when"]:
            days[day_key(msg["when"])].add(*payload)

    if not projects:
        print("No data in this window.")
        return

    triage_data = triage(sessions, table, memo) if args.triage else None

    if args.triage:
        print_triage(triage_data, since)
        return

    if args.audit and not args.dashboard:
        data = audit_sessions(sessions, table, memo, limit=max(1, args.audit_max))
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print_audit(data, since, top=args.top or 15)
        return

    if args.git_yield:
        data = yield_report(sessions, limit=max(1, args.yield_max))
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print_yield(data, since)
        return

    def serialize(bucket: Bucket):
        return {
            "sessions": len(bucket.sessions),
            "messages": bucket.messages,
            "input": bucket.input,
            "cache_write_5m": bucket.cache_write_5m,
            "cache_write_1h": bucket.cache_write_1h,
            "cache_read": bucket.cache_read,
            "output": bucket.output,
            "cost_usd": round(bucket.cost, 4),
            "unpriced_messages": bucket.unpriced,
            "long_context_messages": bucket.long_context,
            "long_context_unpriced": bucket.long_unpriced,
            "first": bucket.first.isoformat() if bucket.first else None,
            "last": bucket.last.isoformat() if bucket.last else None,
        }

    ordered = sorted(projects.items(), key=lambda kv: -kv[1].cost)
    total = Bucket()
    for b in projects.values():
        total.input += b.input
        total.output += b.output
        total.cache_write_5m += b.cache_write_5m
        total.cache_write_1h += b.cache_write_1h
        total.cache_read += b.cache_read
        total.cost += b.cost
        total.long_context += b.long_context
        total.long_unpriced += b.long_unpriced
        total.sessions |= b.sessions
        if b.first and (total.first is None or b.first < total.first):
            total.first = b.first
        if b.last and (total.last is None or b.last > total.last):
            total.last = b.last

    if args.dashboard:
        needle = (args.filter_sessions or "").lower().strip()
        candidates = list(sessions.items())
        if needle:
            candidates = [item for item in candidates
                          if needle in item[0][0].lower() or needle in item[0][1].lower()]
        matched = len({sid for (_proj, sid), _b in candidates})
        candidates.sort(key=session_sort_key(args.sort_sessions))
        breakdowns = []
        done = set()
        for (proj, sid), bucket in candidates:
            if sid in done or len(breakdowns) >= args.sessions_max:
                continue
            found = find_transcript(sid)
            if found is None:
                continue
            detail = session_payload(found, table, memo,
                                     turn_sort=args.sort_turns,
                                     turn_filter=args.filter_turns or "")
            if detail:
                done.add(sid)
                detail["project"] = os.path.basename(proj.rstrip("/")) or detail["project"]
                breakdowns.append(detail)
        breakdowns.sort(key=breakdown_sort_key(args.sort_sessions))
        payload = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "served": args.served,
            "dashboard_source": args.dashboard_source,
            "window": window_meta(since, args.days),
            "coverage": {"measured": len(seen_sessions), "total": len(total.sessions)},
            "yield": None if args.no_git else yield_report(
                sessions, limit=max(1, args.yield_max)),
            "totals": serialize(total),
            "projects": [dict(serialize(b), name=os.path.basename(k.rstrip("/")) or k, path=k)
                         for k, b in ordered],
            "models": [dict(serialize(b), name=k) for k, b in
                       sorted(models.items(), key=lambda kv: -kv[1].cost)],
            "daily": [dict(serialize(b), date=d) for d, b in sorted(days.items())],
            "sessions": breakdowns,
            "listing": {
                "sort_sessions": args.sort_sessions,
                "filter_sessions": args.filter_sessions or "",
                "sort_turns": args.sort_turns,
                "filter_turns": args.filter_turns or "",
                "limit": args.sessions_max,
                "matched": matched,
                "pool": len({sid for _proj, sid in sessions}),
            },
        }
        out = write_dashboard(payload, Path(args.dashboard))
        print(f"Dashboard written: {out}")
        print(f"  {len(payload['projects'])} projects · {len(breakdowns)} detailed sessions")
        return

    if args.json:
        print(json.dumps({
            "projects": {k: serialize(v) for k, v in projects.items()},
            "models": {k: serialize(v) for k, v in models.items()},
            "unknown_models": sorted(unknown_models),
        }, indent=2))
        return

    hidden = 0
    if args.top and len(ordered) > args.top:
        hidden = len(ordered) - args.top
        ordered = ordered[: args.top]
    rows = [
        [
            os.path.basename(key.rstrip("/")) or key,
            str(len(b.sessions)),
            human(b.input),
            human(b.cache_write_5m + b.cache_write_1h),
            human(b.cache_read),
            human(b.output),
            f"{b.cost:,.2f}",
        ]
        for key, b in ordered
    ]
    if hidden:
        rows.append([f"({hidden} more)", "", "", "", "", "", ""])
    rows.append([
        "TOTAL", str(len(total.sessions)), human(total.input),
        human(total.cache_write_5m + total.cache_write_1h), human(total.cache_read),
        human(total.output), f"{total.cost:,.2f}",
    ])
    period = f" since {since.date()}" if since else ""
    print(f"\nUsage by project{period} — API list price, USD\n")
    render_table(
        ["Project", "Sess.", "Input", "Cache W", "Cache R", "Output", "Cost $"],
        rows, ["l", "r", "r", "r", "r", "r", "r"],
    )

    if args.models:
        print("\nBy model\n")
        render_table(
            ["Model", "Msg", "Input", "Cache W", "Cache R", "Output", "Cost $"],
            [
                [m, str(b.messages), human(b.input),
                 human(b.cache_write_5m + b.cache_write_1h), human(b.cache_read),
                 human(b.output), f"{b.cost:,.2f}"]
                for m, b in sorted(models.items(), key=lambda kv: -kv[1].cost)
            ],
            ["l", "r", "r", "r", "r", "r", "r"],
        )

    if args.sessions:
        top = sorted(sessions.items(), key=lambda kv: -kv[1].cost)[: args.sessions]
        print(f"\n{len(top)} costliest sessions\n")
        render_table(
            ["Project", "Session", "Start", "Msg", "Total in", "Output", "Cost $"],
            [
                [os.path.basename(k[0].rstrip("/")) or k[0], k[1][:8],
                 b.first.strftime("%Y-%m-%d %H:%M") if b.first else "-",
                 str(b.messages), human(b.total_input), human(b.output), f"{b.cost:,.2f}"]
                for k, b in top
            ],
            ["l", "l", "l", "r", "r", "r", "r"],
        )

    if args.daily:
        print("\nBy day\n")
        render_table(
            ["Day", "Sess.", "Total in", "Output", "Cost $"],
            [
                [d, str(len(b.sessions)), human(b.total_input), human(b.output),
                 f"{b.cost:,.2f}"]
                for d, b in sorted(days.items())
            ],
            ["l", "r", "r", "r", "r"],
        )

    if args.tools:
        ranked = sorted(sessions.items(), key=lambda kv: -kv[1].cost)
        summary = aggregate_sources([sid for (_proj, sid), _b in ranked],
                                    table, memo, limit=args.tools_max)
        grand = sum(src["cost"] for src in summary["sources"]) or 1
        print(f"\nWhere the money goes, by source — {summary['scanned']} session(s) "
              "parsed\n")
        render_table(
            ["Source", "Sessions", "Calls", "Tokens added", "Cost $", "Share"],
            [[src["tool"], str(src["sessions"]), str(src["count"]),
              human(src["added"]), f"{src['cost']:,.2f}",
              f"{100 * src['cost'] / grand:.0f}%"]
             for src in summary["sources"][: args.top or 20]],
            ["l", "r", "r", "r", "r", "r"],
        )
        if summary["skipped"]:
            print(f"  {summary['skipped']} cheaper session(s) left out by "
                  f"--tools-max {args.tools_max}.")
        if summary["agents"]:
            print("\nSubagents by type\n")
            render_table(
                ["Type", "Runs", "Turns", "Cost $"],
                [[a["type"], str(a["runs"]), str(a["turns"]), f"{a['cost']:,.2f}"]
                 for a in summary["agents"]],
                ["l", "r", "r", "r"],
            )
        rebuilt = summary["cache"]
        if rebuilt["count"]:
            print(f"\nCache rebuilt on {rebuilt['count']} turn(s) across those "
                  f"sessions: {human(rebuilt['tokens'])} tokens rewritten, "
                  f"${rebuilt['extra_cost']:,.2f} of avoidable cost.")

    if unknown_models:
        print(f"\n[warning] no price found for: {', '.join(sorted(unknown_models))}"
              "\n          their tokens are counted, their cost is 0.")
    if total.long_context:
        share = 100 * total.long_context / max(1, sum(b.messages for b in projects.values()))
        print(f"\n{total.long_context} request(s) went past {LONG_CONTEXT_THRESHOLD // 1000} k "
              f"tokens of prompt ({share:.0f}% of them), where the API charges a premium rate.")
        if total.long_unpriced:
            print(f"  {total.long_unpriced} of those run on a model with no published "
                  "long-context rate:\n  they are billed here at the standard rate, so "
                  "their cost is a floor.")
    total_sessions = len(total.sessions)
    if states and not args.no_cost_state:
        counted = len(seen_sessions & {s for b in projects.values() for s in b.sessions})
        print(f"\n{counted} of {total_sessions} session(s) measured by Claude Code's "
              "internal counter (cost-state), exact.")
        print("The rest are rebuilt from the transcript: a floor, because branches\n"
              "abandoned after a rewind and titling calls never appear there.")
    print("\nCost = public API list price, not what a subscription bills.")


if __name__ == "__main__":
    main()
