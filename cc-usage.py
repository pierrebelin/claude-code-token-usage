#!/usr/bin/env python3
"""Claude Code token usage, broken down by project.

Reads ~/.claude/projects/**/*.jsonl locally. No external service beyond the
LiteLLM price refresh (24h cache, --no-fetch to skip it).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
CACHE_PATH = Path.home() / ".cache" / "cc-usage" / "litellm-prices.json"
PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
CACHE_TTL = 24 * 3600
LONG_CACHE_MULTIPLIER = 2.0
FREE_MODELS = {"<synthetic>"}


class Rates:
    __slots__ = ("input", "output", "cache_write", "cache_read", "source")

    def __init__(self, input_, output, cache_write, cache_read, source):
        self.input = input_
        self.output = output
        self.cache_write = cache_write if cache_write is not None else input_ * 1.25
        self.cache_read = cache_read if cache_read is not None else input_ * 0.1
        self.source = source


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
    table = {}
    for name, entry in prices.items():
        if not isinstance(entry, dict):
            continue
        cost_in = entry.get("input_cost_per_token")
        cost_out = entry.get("output_cost_per_token")
        if cost_in is None or cost_out is None:
            continue
        table[name] = Rates(
            cost_in,
            cost_out,
            entry.get("cache_creation_input_token_cost"),
            entry.get("cache_read_input_token_cost"),
            name,
        )
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


class Bucket:
    __slots__ = ("input", "output", "cache_write_5m", "cache_write_1h",
                 "cache_read", "cost", "messages", "sessions", "unpriced", "first", "last")

    def __init__(self):
        self.input = self.output = 0
        self.cache_write_5m = self.cache_write_1h = self.cache_read = 0
        self.cost = 0.0
        self.messages = 0
        self.sessions = set()
        self.unpriced = 0
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
        self.input += inp
        self.output += out
        self.cache_read += read
        self.cache_write_5m += w5m
        self.cache_write_1h += w1h
        if rates is None:
            self.unpriced += 1
            return
        self.cost += (
            inp * rates.input
            + out * rates.output
            + w5m * rates.cache_write
            + w1h * rates.input * LONG_CACHE_MULTIPLIER
            + read * rates.cache_read
        )


def read_cost_states():
    """Counters written by Claude Code itself, one per transcript.

    Authoritative: they include branches abandoned after a rewind and the side
    calls (Haiku titling) the transcript never records. Missing from sessions
    older than their introduction.
    """
    states = {}
    for path in sorted(PROJECTS_DIR.glob("*/*.jsonl")):
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


def iter_messages(since: datetime | None, skip_paths: set | None = None):
    seen = set()
    for path in sorted(PROJECTS_DIR.glob("*/*.jsonl")):
        if skip_paths and str(path) in skip_paths:
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
                    "dir": path.parent.name,
                    "session": entry.get("sessionId") or path.stem,
                    "model": message.get("model") or "unknown",
                    "branch": entry.get("gitBranch") or "",
                    "sidechain": bool(entry.get("isSidechain")),
                    "usage": usage,
                    "when": when,
                }


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


def read_session(path: Path):
    turns: dict[str, Turn] = {}
    order: list[str] = []
    results: dict[str, int] = {}
    prompts = 0
    compactions = 0
    cwds: Counter = Counter()
    branches: Counter = Counter()
    meta = {"cwd": "", "branch": "", "session": path.stem, "reported": None}
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
                    results[block.get("tool_use_id") or ""] = size
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
    return ordered, results, prompts, compactions, meta


def input_cost(usage: dict, rates: Rates) -> float:
    if rates is None:
        return 0.0
    inp = int(usage.get("input_tokens") or 0)
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
    return (inp * rates.input + w5m * rates.cache_write
            + w1h * rates.input * LONG_CACHE_MULTIPLIER + read * rates.cache_read)


def attribute(chain, results):
    """Spreads context growth across whatever caused it.

    added_i = ctx_i - (ctx_{i-1} + output_{i-1}): tokens injected between two
    turns, as measured by the API. Each addition is then weighted by the number
    of turns that carried it to the end of the segment (a compaction or a
    /clear opens a new segment).
    """
    n = len(chain)
    segment_end = [n - 1] * n
    starts = [0]
    for i in range(1, n):
        if chain[i].ctx < chain[i - 1].ctx * 0.7:
            starts.append(i)
    for pos, start in enumerate(starts):
        end = starts[pos + 1] - 1 if pos + 1 < len(starts) else n - 1
        for i in range(start, end + 1):
            segment_end[i] = end

    entries = []
    for i, turn in enumerate(chain):
        if i in starts:
            added = turn.ctx
            source = ("Startup (system prompt, CLAUDE.md, tools)"
                      if i == 0 else "Resume after compaction")
            carried = segment_end[i] - i + 1
            entries.append({"turn": i, "label": source, "tool": "(startup)",
                            "added": added, "carried": carried})
            continue
        previous = chain[i - 1]
        added = turn.ctx - previous.ctx - previous.output
        if added <= 0:
            continue
        carried = segment_end[i] - i + 1
        sizes = {tid: max(results.get(tid, 0), 1) for tid in previous.tools}
        total = sum(sizes.values())
        if total and previous.tools:
            for tid, (name, tool_input) in previous.tools.items():
                share = added * sizes[tid] / total
                entries.append({"turn": i, "label": describe_tool(name, tool_input),
                                "tool": name, "added": share, "carried": carried})
        else:
            entries.append({"turn": i, "label": "User prompt / system context",
                            "tool": "(no tool)", "added": added, "carried": carried})
    return entries


TURN_SORTS = {
    "cost-desc": ("Costliest first", lambda e: (-e["cost"], e["turn"])),
    "cost-asc": ("Cheapest first", lambda e: (e["cost"], e["turn"])),
    "added-desc": ("Tokens added", lambda e: (-e["added"], e["turn"])),
    "carried-desc": ("Most carried", lambda e: (-e["carried"], e["turn"])),
    "turn-asc": ("Chronological", lambda e: e["turn"]),
    "turn-desc": ("Reverse order", lambda e: -e["turn"]),
}

DEFAULT_SESSION_SORT = "date-desc"

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
    total_in = total_out = 0.0
    for turn in turns:
        rates = resolve_rates(turn.model, table, memo)
        total_in += input_cost(turn.usage, rates)
        total_out += turn.output * (rates.output if rates else 0.0)
    ctx_sum = sum(t.ctx for t in main) or 1
    main_in = sum(input_cost(t.usage, resolve_rates(t.model, table, memo)) for t in main)
    per_ctx_token = main_in / ctx_sum
    entries = attribute(main, results)
    for e in entries:
        e["cost"] = e["added"] * e["carried"] * per_ctx_token
    reported = meta.get("reported")
    scale = 1.0
    ref = None
    if reported:
        ref = float(reported.get("totalCostUSD") or 0.0)
        mine = total_in + total_out
        if ref > 0 and mine > 0:
            scale = ref / mine
    if scale != 1.0:
        for e in entries:
            e["cost"] *= scale
        total_in *= scale
        total_out *= scale
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
    sources.append({"tool": "(response generation)", "count": len(turns),
                    "added": sum(t.output for t in turns), "cost": round(total_out, 4)})
    skills = defaultdict(float)
    for turn in turns:
        if turn.skill:
            rates = resolve_rates(turn.model, table, memo)
            skills[turn.skill] += (input_cost(turn.usage, rates)
                                   + turn.output * (rates.output if rates else 0.0)) * scale
    rows = [{"turn": e["turn"], "label": _short(e["label"], 64), "tool": e["tool"],
             "added": int(e["added"]), "carried": e["carried"], "cost": round(e["cost"], 4)}
            for e in entries]
    needle = turn_filter.lower().strip()
    if needle:
        rows = [r for r in rows
                if needle in r["label"].lower() or needle in r["tool"].lower()]
    _label, key = TURN_SORTS.get(turn_sort, TURN_SORTS["cost-desc"])
    kept = sorted(rows, key=key)
    return {
        "id": path.stem,
        "short": path.stem[:8],
        "project": os.path.basename((meta["cwd"] or "").rstrip("/")) or "?",
        "branch": meta["branch"],
        "start": main[0].ts,
        "end": main[-1].ts,
        "turns": len(main),
        "prompts": prompts,
        "compactions": compactions,
        "context_final": main[-1].ctx,
        "output": sum(t.output for t in turns),
        "cost": round(total_in + total_out, 4),
        "cost_context": round(total_in, 4),
        "cost_output": round(total_out, 4),
        "exact": ref is not None,
        "reported": round(ref, 4) if ref is not None else None,
        "restitution": round(100 / scale, 1) if scale and ref is not None else None,
        "sources": sources,
        "skills": [{"name": k, "cost": round(v, 4)} for k, v in
                   sorted(skills.items(), key=lambda kv: -kv[1])],
        "entries": kept[:top],
        "entries_total": len(kept),
    }


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _tokens(n: float) -> str:
    a = abs(n)
    for unit, size in ((" G", 1e9), (" M", 1e6), (" k", 1e3)):
        if a >= size:
            return f"{n / size:.1f}" + unit
    return str(int(round(n)))


MONTHS_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTHS_LONG = ("January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")


def _clock(iso: str | None) -> str:
    if not iso:
        return "\u2014"
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return "\u2014"
    return f"{MONTHS_SHORT[moment.month - 1]} {moment.day} {moment:%H:%M}"


def _day(iso: str) -> str:
    moment = datetime.fromisoformat(iso)
    return f"{MONTHS_LONG[moment.month - 1]} {moment.day}"


def _moment(iso: str | None):
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None


def _span(start: str, end: str) -> str:
    """Start and end of a session, with the date written once when it is the same."""
    a, b = _moment(start), _moment(end)
    if a and b and a.date() == b.date():
        return f"{_clock(start)} \u2192 {b:%H:%M}"
    return f"{_clock(start)} \u2192 {_clock(end)}"


def _elapsed(start: str, end: str) -> str:
    a, b = _moment(start), _moment(end)
    if not a or not b:
        return ""
    minutes = int((b - a).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d}"


SOURCE_TIPS = {
    "(startup)": "The system prompt, CLAUDE.md and the tool definitions, plus any "
                 "summary a /compact injected. Loaded once, then carried by every "
                 "turn that follows it.",
    "(no tool)": "Your prompts and the system reminders around them \u2014 context growth "
                 "on turns where no tool result came back.",
    "(response generation)": "The output tokens Claude produced. Billed once at the "
                             "output rate, never carried into later turns.",
}


def source_tip(tool: str, count: int) -> str:
    if tool in SOURCE_TIPS:
        return SOURCE_TIPS[tool]
    call = "call" if count == 1 else "calls"
    return (f"What {tool} returned, over {count} {call}. Cost = tokens added \u00d7 the "
            "number of later turns that carried them.")


def _shade(index: int, count: int) -> str:
    mix = 18 + (index / max(1, count - 1)) * 52
    return f"color-mix(in oklab, var(--accent) {100 - mix:.0f}%, var(--sunk))"


def _query(params: dict, **overrides) -> str:
    """Builds a relative URL that carries the dashboard's current state."""
    import urllib.parse
    merged = {k: str(v) for k, v in {**params, **overrides}.items() if v not in (None, "")}
    return "?" + urllib.parse.urlencode(merged) if merged else "?"


def _state(payload: dict) -> dict:
    listing = payload.get("listing") or {}
    days = payload["window"].get("days")
    return {
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


def render_session_detail(session: dict, payload: dict, standalone: bool) -> list:
    """Renders one session: headline figures, per-tool breakdown, turns."""
    esc = html_escape
    out = []
    chip = "exact" if session["exact"] else "floor"
    chip_label = "exact" if session["exact"] else "floor"
    compact_plural = "s" if session["compactions"] != 1 else ""
    prompt_plural = "s" if session["prompts"] != 1 else ""
    turn_plural = "s" if session["turns"] != 1 else ""

    if standalone:
        state = _state(payload)
        if payload.get("served"):
            out.append(f'<a class="back" href="/{esc(_query(state))}">All sessions</a>')
        out.append('<header class="hero">')
        out.append('<div class="hero-left">')
        out.append('<div class="hero-top"><span class="eyebrow">Session</span>'
                   f'<span class="id">{esc(session["short"])}</span>'
                   + (f'<span class="branch">{esc(session["branch"])}</span>'
                      if session.get("branch") else "")
                   + "</div>")
        out.append(f'<h1>{esc(session["project"])}</h1>')
        out.append(f'<div class="identity"><span class="hero-cost"><span>$</span>'
                   f'{_money(session["cost"])}</span>'
                   f'<span class="chip {chip}">{chip_label}</span></div>')
        run = [f'<span>{esc(_span(session["start"], session["end"]))}</span>']
        elapsed = _elapsed(session["start"], session["end"])
        if elapsed:
            run.append(f'<span><b>{esc(elapsed)}</b> elapsed</span>')
        run += [
            f'<span><b>{session["turns"]}</b> turn{turn_plural}</span>',
            f'<span><b>{session["prompts"]}</b> prompt{prompt_plural}</span>',
            f'<span><b>{session["compactions"]}</b> compaction{compact_plural}</span>',
        ]
        out.append("</div>")
        out.append('<div class="hero-meta">' + "".join(run) + "</div>")
        out.append("</header>")

    facts = [
        ("Context", "$" + _money(session["cost_context"])),
        ("Generation", "$" + _money(session["cost_output"])),
        ("Tokens produced", _tokens(session["output"])),
        ("Final context", _tokens(session["context_final"])),
        (("Accounted for by transcript", f'{session["restitution"]} %')
         if session["exact"] else ("Internal counter", "missing")),
    ]
    out.append('<div class="facts">')
    for label, value in facts:
        out.append(f'<div class="fact"><span class="eyebrow">{esc(label)}</span>'
                   f"<b>{esc(str(value))}</b></div>")
    out.append("</div>")

    sources = [src for src in session["sources"] if src["cost"] > 0]
    total_sources = sum(src["cost"] for src in sources) or 1
    out.append("<section>")
    if standalone:
        out.append('<div class="section-head">')
        out.append("<h2>What filled the context</h2>")
        out.append('<p class="note">Every turn resends the whole accumulated context, '
                   "so a source costs its own size multiplied by the number of later "
                   "turns that carry it. Hover a source to see what it covers.</p>")
        out.append("</div>")
    else:
        out.append("<h2>What filled the context</h2>")
    out.append('<div class="stack">')
    for i, src in enumerate(sources):
        share = (src["cost"] / total_sources) * 100
        out.append(f'<span style="width:{share:.2f}%;background:{_shade(i, len(sources))}" '
                   f'title="{esc(src["tool"])} — ${_money(src["cost"])}"></span>')
    out.append("</div>")
    out.append('<div class="scroll"><table><thead><tr><th>Source</th>'
               '<th class="n">Calls</th><th class="n">Tokens added</th>'
               '<th class="n">Cost</th><th class="n">Share</th>'
               "</tr></thead><tbody>")
    for i, src in enumerate(sources):
        share = round((src["cost"] / total_sources) * 100)
        out.append(
            f'<tr><td><span class="swatch" style="background:{_shade(i, len(sources))}">'
            f'</span><span class="src" tabindex="0" '
            f'data-tip="{esc(source_tip(src["tool"], src["count"]))}">'
            f'{esc(src["tool"])}</span></td>'
            f'<td class="n">{src["count"]}</td>'
            f'<td class="n">{_tokens(src["added"])}</td>'
            f'<td class="n">${_money(src["cost"])}</td>'
            f'<td class="n">{share} %</td></tr>')
    out.append("</tbody></table></div>")
    out.append("</section>")

    out.append("<section>")
    out.append("<h2>Turn by turn</h2>")
    if standalone and payload.get("served"):
        out.append(f'<div class="filters">{_turn_form(payload, action="/session")}</div>')
    rows = session["entries"]
    if rows:
        out.append('<div class="scroll"><table><thead><tr><th class="n">Turn</th>'
                   '<th>What the turn added</th><th class="n">Tokens</th>'
                   '<th class="n">Carried</th><th class="n">Cost</th>'
                   "</tr></thead><tbody>")
        for entry in rows:
            out.append(
                f'<tr><td class="n">{entry["turn"]}</td>'
                f'<td class="label" title="{esc(entry["label"])}">{esc(entry["label"])}</td>'
                f'<td class="n">{_tokens(entry["added"])}</td>'
                f'<td class="n">×{entry["carried"]}</td>'
                f'<td class="n">${_money(entry["cost"])}</td></tr>')
        out.append("</tbody></table></div>")
        total_rows = session.get("entries_total", len(rows))
        if total_rows > len(rows):
            out.append(f'<p class="note">{len(rows)} of {total_rows} matching turns '
                       "shown.</p>")
    else:
        out.append('<p class="empty">No turn matches the filter.</p>')
    out.append("</section>")

    if session["skills"]:
        listing = ", ".join(f'{esc(k["name"])} (${_money(k["cost"])})'
                            for k in session["skills"])
        out.append(f'<p class="note">Turns attributed to a skill: {listing}.</p>')
    return out


def render_footer(payload: dict) -> list:
    days_flag = payload["window"].get("days")
    command = ("cc-usage.py --serve" if payload.get("served")
               else f"cc-usage.py --days {days_flag} --dashboard report.html"
               if days_flag else "cc-usage.py --dashboard report.html")
    return ["<footer>",
            f'<div>Regenerate: <code>{html_escape(command)}</code></div>',
            "<div>Cost at public API list price — not what a subscription bills.</div>",
            f'<div>Generated {_clock(payload["generated"])}.</div>',
            "</footer>"]


def render_session_page(payload: dict) -> str:
    out = ['<div class="wrap">']
    out += render_session_detail(payload["focus"], payload, standalone=True)
    out += render_footer(payload)
    out.append("</div>")
    return "\n".join(out)


def window_meta(since, days) -> dict:
    return {"since": since.isoformat() if since else None, "days": days,
            "label": f"since {since.date()}" if since else "all history"}


def render_body(payload: dict) -> str:
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
    out.append(f'<div class="eyebrow">{esc(payload["window"]["label"])}</div>')
    out.append("<h1>Claude Code usage report</h1>")
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

    cov = payload["coverage"]
    estimated = max(0, cov["total"] - cov["measured"])
    if estimated == 0:
        note = (f'<strong>{cov["measured"]} of {cov["total"]} sessions</strong> come from '
                "Claude Code's internal counter. Figures are exact.")
    else:
        note = (f'<strong>{cov["measured"]} of {cov["total"]} sessions</strong> come from '
                "Claude Code's internal counter and are exact. The other "
                f"{estimated} are rebuilt from the transcript, which keeps only the "
                "final branch: whatever was abandoned after a rewind was still "
                "billed but no longer appears there. For those, the total is a floor.")
    out.append(f'<div class="caveat">{note}</div>')
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
        for i, point in enumerate(daily):
            cx, cy = px(i), py(point["cost_usd"])
            tip = f'{_day(point["date"])} — ${_money(point["cost_usd"])}'
            svg.append(f'<circle class="hit" cx="{cx:.1f}" cy="{cy:.1f}" r="12">'
                       f'<title>{esc(tip)}</title></circle>'
                       f'<circle class="dot" cx="{cx:.1f}" cy="{cy:.1f}" r="4"/>')
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
            + f'value="{int(listing.get("limit", 14))}"></label>'
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
            chip = "exact" if session["exact"] else "floor"
            chip_label = "exact" if session["exact"] else "floor"
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
                f'<span class="rank">{rank:02d}</span>' 
                f'<span class="row-main"><span class="row-title">'
                f'<span class="proj">{esc(session["project"])}</span>'
                f'<span class="id">{esc(session["short"])}</span>'
                f'<span class="chip {chip}">{chip_label}</span></span>'
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


SKELETON_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
"""

SKELETON_MID = """</head>
<body>
"""

SKELETON_TAIL = """</body>
</html>
"""


def write_dashboard(payload: dict, out_path: Path):
    """Assembles a full HTML document from the template.

    The template is a fragment (title, styles, body) with no skeleton, and has to
    stay that way to remain publishable as is. The standalone document, though,
    needs the doctype and the charset, without which a browser opening the file
    over file:// falls into quirks mode and mangles non-ASCII characters.
    """
    template = Path(__file__).with_name("cc-usage-template.html")
    if not template.exists():
        raise SystemExit(f"template not found: {template}")
    fragment = template.read_text(encoding="utf-8").replace(
        "<!--__BODY__-->", render_body(payload))
    marker = "</style>"
    cut = fragment.find(marker)
    if cut == -1:
        raise SystemExit("invalid template: no <style> block")
    cut += len(marker)
    head, body = fragment[:cut], fragment[cut:]
    out_path.write_text(
        SKELETON_HEAD + head + SKELETON_MID + body + SKELETON_TAIL,
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

    total_in = total_out = 0.0
    for turn in turns:
        rates = resolve_rates(turn.model, table, memo)
        total_in += input_cost(turn.usage, rates)
        total_out += turn.output * (rates.output if rates else 0.0)

    ctx_sum = sum(t.ctx for t in main) or 1
    main_in = 0.0
    for turn in main:
        main_in += input_cost(turn.usage, resolve_rates(turn.model, table, memo))
    per_ctx_token = main_in / ctx_sum

    entries = attribute(main, results)
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
    print(f"  Cost ${total_in + total_out:,.2f}  "
          f"(context {total_in:,.2f} · generation {total_out:,.2f})")
    reported = meta.get("reported")
    scale = 1.0
    if reported:
        ref = float(reported.get("totalCostUSD") or 0.0)
        mine = total_in + total_out
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

    if scale != 1.0:
        for e in entries:
            e["cost"] *= scale
        total_out *= scale
        total_in *= scale
    grouped = defaultdict(lambda: {"added": 0.0, "cost": 0.0, "count": 0})
    for e in entries:
        g = grouped[e["tool"]]
        g["added"] += e["added"]
        g["cost"] += e["cost"]
        g["count"] += 1
    ranked = sorted(grouped.items(), key=lambda kv: -kv[1]["cost"])
    grand = sum(g["cost"] for _, g in ranked) + total_out or 1
    print("\nWhat filled the context — cost = tokens added x turns that carry them\n")
    rows = [
        [tool, str(g["count"]), human(int(g["added"])), f"{g['cost']:,.2f}",
         f"{100 * g['cost'] / grand:.0f}%"]
        for tool, g in ranked
    ]
    rows.append(["(response generation)", str(len(turns)),
                 human(sum(t.output for t in turns)), f"{total_out:,.2f}",
                 f"{100 * total_out / grand:.0f}%"])
    render_table(["Source", "Calls", "Tokens added", "Cost $", "Share"],
                 rows, ["l", "r", "r", "r", "r"])

    print(f"\n{top} costliest individual additions\n")
    render_table(
        ["Turn", "What was added", "Tokens", "Carried", "Cost $"],
        [
            [str(e["turn"]), _short(e["label"]), human(int(e["added"])),
             f"x{e['carried']}", f"{e['cost']:,.2f}"]
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
            entry["cost"] += input_cost(turn.usage, rates) + turn.output * (
                rates.output if rates else 0.0)
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
            skills[turn.skill] += input_cost(turn.usage, rates) + turn.output * (
                rates.output if rates else 0.0)
    if skills:
        print("\nTurns attributed to a skill\n")
        render_table(["Skill", "Cost $"],
                     [[k, f"{v:,.2f}"] for k, v in sorted(skills.items(), key=lambda kv: -kv[1])],
                     ["l", "r"])
    print("\nCost = public API list price. Attribution spreads the measured context "
          "growth;\nparallel calls share their delta in proportion to result size.")


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
    parser.add_argument("--days", type=int, help="rolling window, in days")
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
    parser.add_argument("--project", help="substring filter on the project key")
    parser.add_argument("--session", metavar="ID",
                        help="break down a single session (id prefix or path)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--dashboard", metavar="FILE",
                        help="write a self-contained HTML dashboard")
    parser.add_argument("--serve", nargs="?", const=8787, type=int, metavar="PORT",
                        help="serve the dashboard on the loopback interface and re-run "
                             "the analysis on every load (default: port 8787)")
    parser.add_argument("--served", action="store_true", help=argparse.SUPPRESS)
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
    parser.add_argument("--sessions-max", type=int, default=14, metavar="N",
                        help="number of detailed sessions (default: 14)")
    parser.add_argument("--focus", metavar="ID",
                        help="with --dashboard, write the page for a single session")
    args = parser.parse_args()

    if args.serve:
        args.sessions_max = max(1, min(60, args.sessions_max))
        passthrough = []
        for flag, value in (("--by", args.by), ("--project", args.project),
                            ("--since", args.since)):
            if value and not (flag == "--by" and value == "repo"):
                passthrough += [flag, str(value)]
        for flag, on in (("--split-worktrees", args.split_worktrees),
                         ("--no-cost-state", args.no_cost_state),
                         ("--no-fetch", args.no_fetch)):
            if on:
                passthrough.append(flag)
        serve(args.serve, args.days, passthrough)
        return

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    elif args.days:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    table = build_rate_table(load_prices(not args.no_fetch))
    memo: dict = {}

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

    states = {} if args.no_cost_state else read_cost_states()
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
                           days[when.date().isoformat()] if when else None):
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
            days[msg["when"].date().isoformat()].add(*payload)

    if not projects:
        print("No data in this window.")
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
            "window": window_meta(since, args.days),
            "coverage": {"measured": len(seen_sessions), "total": len(total.sessions)},
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

    if unknown_models:
        print(f"\n[warning] no price found for: {', '.join(sorted(unknown_models))}"
              "\n          their tokens are counted, their cost is 0.")
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
