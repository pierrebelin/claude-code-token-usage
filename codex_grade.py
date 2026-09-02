"""What one Codex session could have avoided, from its counters alone.

The Claude Code reader grades a session on dollars it attributes turn by turn.
A Codex journal carries no price and no per-turn composition: it carries
cumulative counters, the moment each was written, and the name and bounded
target of every tool call.  So the unit here is **observed tokens**, and every
figure below is an *attribution* over intervals -- stated as one, never as a
measurement of a single call.

The rules, in full:

* a **step** is one counter interval plus the calls whose timestamp falls inside
  it;
* a step's observed tokens are split equally across the calls it holds, because
  the journal records no per-call figure; a step holding no call is attributed
  to ``(no tool call)``;
* every finding is a share of the session's own observed total, weighted on the
  A-F scale shared with the Claude Code reader in ``session_grade.py``.

Nothing here reads a prompt, a model reply or a tool result.  The only content
it touches is the bounded target ``codex_log`` already kept for each call.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from session_grade import (JUNK_NAMES, JUNK_FRAGMENTS, grade_for, grade_weight)


# Tokens, not dollars, so the fade-in needs its own materiality: below this an
# avoidable volume is a rate, not something to act on. A couple of hundred
# thousand tokens is roughly one large file re-sent through a working session.
GRADE_MATERIAL_TOKENS = 200_000
# A finding appears from a tenth of the session, or from this floor alone when
# the pattern is unambiguous (junk, duplicates, oversized instructions).
FINDING_MIN_SHARE = 0.10
FINDING_MIN_TOKENS = 50_000
# A step that read back less than this share of its input from the cache, while
# re-sending more than the floor uncached, paid for a prefix it already had.
CACHE_READ_SHARE = 0.5
CACHE_MISS_FLOOR = 20_000
# AGENTS.md is what Codex resends on every request, the way CLAUDE.md is.
AGENTS_LIMIT = 8_000           # bytes
AGENTS_DEPTH = 5
NO_TOOL = "(no tool call)"
# What ``codex_log`` writes where it truncated a call target.
_CUT = "\u2026"
_JUNK_DIRECTORIES = tuple(fragment.strip("/") for fragment in JUNK_FRAGMENTS)
_ITEM_LIMIT = 4


def codex_steps(session: dict) -> list:
    """The session's steps: one counter interval, and the calls that ran inside it.

    A step is what this journal can honestly call a turn. The counters are
    written per snapshot and the tool calls carry their own timestamp, so a call
    landing inside an interval is attributed to it. Nothing else is inferred: a
    step with no call is a step where only the model spoke.
    """
    calls = [dict(call, index=position)
             for position, call in enumerate(session.get("tools") or [])]
    steps = []
    for index, interval in enumerate(session.get("intervals") or [], start=1):
        inside = [call for call in calls if call["timestamp"]
                  and interval["started_at"] < call["timestamp"] <= interval["ended_at"]]
        names = list(dict.fromkeys(call.get("tool") or call["name"] for call in inside))
        labels = list(dict.fromkeys(call.get("label") or call["name"] for call in inside))
        steps.append({
            "index": index,
            "interval": interval,
            "tokens": interval["tokens"],
            "tools": names,
            "calls": len(inside),
            "calls_detail": inside,
            # One step often runs one call: name it by what that call pointed at.
            # Several, and the targets stop fitting -- the tool names carry more.
            "label": (labels[0] if len(labels) == 1
                      else ", ".join(names) or NO_TOOL),
        })
    return steps


def junk_target(detail: str) -> bool:
    """A bounded call target that names generated, vendored or locked content.

    ``detail`` is a command fragment as often as it is a path, so the directory
    names are matched as path segments anywhere in it rather than against an
    absolute path.
    """
    lowered = " ".join(detail.replace("\\", "/").lower().split())
    if not lowered:
        return False
    if any(name.lower() in lowered for name in JUNK_NAMES):
        return True
    return any(f"/{directory}/" in lowered or lowered.startswith(f"{directory}/")
               or f" {directory}/" in lowered for directory in _JUNK_DIRECTORIES)


def attribute_tools(steps: list[dict]) -> list[dict]:
    """Split each step's observed tokens across the calls that ran inside it.

    Equal shares, because the journal states no per-call figure. The totals are
    conserved: every step's tokens land on exactly one row.
    """
    tokens: dict[str, float] = defaultdict(float)
    calls: dict[str, int] = defaultdict(int)
    for step in steps:
        observed = step["tokens"]["total_tokens"]
        inside = step.get("calls_detail") or []
        if not inside:
            tokens[NO_TOOL] += observed
            calls[NO_TOOL] += 1
            continue
        share = observed / len(inside)
        for call in inside:
            name = call.get("tool") or call["name"]
            tokens[name] += share
            calls[name] += 1
    total = sum(tokens.values()) or 1
    return sorted(
        ({"tool": name, "count": calls[name], "tokens": int(round(value)),
          "share": round(100 * value / total, 1)} for name, value in tokens.items()),
        key=lambda row: (-row["tokens"], row["tool"]))


def _call_tokens(steps: list[dict]) -> dict[int, float]:
    """Tokens attributed to each individual call, keyed by its position."""
    attributed: dict[int, float] = {}
    for step in steps:
        inside = step.get("calls_detail") or []
        if not inside:
            continue
        share = step["tokens"]["total_tokens"] / len(inside)
        for call in inside:
            attributed[call["index"]] = share
    return attributed


def _gap_seconds(before: str | None, after: str | None) -> float | None:
    try:
        start = datetime.fromisoformat((before or "").replace("Z", "+00:00"))
        end = datetime.fromisoformat((after or "").replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (end - start).total_seconds()


def _read_bytes(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").encode("utf-8"))
    except OSError:
        return 0


_AGENTS: dict[str, list] = {}


def agents_chain(cwd: str) -> list:
    """The AGENTS.md files a session run from ``cwd`` carried on every request."""
    if cwd in _AGENTS:
        return _AGENTS[cwd]
    chain, seen = [], set()
    candidates = [Path.home() / ".codex" / "AGENTS.md"]
    if cwd:
        base = Path(cwd)
        candidates += [parent / "AGENTS.md" for parent in (base, *list(base.parents)[:AGENTS_DEPTH])]
    for candidate in candidates:
        key = str(candidate)
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        size = _read_bytes(candidate)
        if size:
            chain.append({"path": key, "bytes": size})
    _AGENTS[cwd] = chain
    return chain


def _cache_misses(steps: list[dict]) -> dict:
    """Steps that re-sent a prefix they should have read back from the cache.

    The first step is the session's own opening request -- prefix, instructions
    and tool definitions, sent once and legitimately uncached -- so it is
    excluded rather than counted as waste.
    """
    count = tokens = 0
    gaps = []
    for position, step in enumerate(steps):
        if position == 0:
            continue
        usage = step["tokens"]
        uncached = usage["input_tokens"] - usage["cached_input_tokens"]
        if uncached <= CACHE_MISS_FLOOR:
            continue
        if usage["cached_input_tokens"] >= CACHE_READ_SHARE * usage["input_tokens"]:
            continue
        count += 1
        tokens += uncached
        gap = _gap_seconds(steps[position - 1]["interval"]["ended_at"],
                           step["interval"]["started_at"])
        if gap is not None:
            gaps.append(gap)
    gaps.sort()
    return {"count": count, "tokens": tokens,
            "median_gap": gaps[len(gaps) // 2] if gaps else None}


def _replayed_output(steps: list[dict]) -> int:
    """Replies already produced, carried back into every later request.

    Each step's input contains at least everything the model said before it, so
    the running output total is a floor on the replayed share of that step --
    capped at the input actually observed, which it can never exceed.
    """
    replayed = carried = 0
    for step in steps:
        usage = step["tokens"]
        replayed += min(carried, usage["input_tokens"])
        carried += usage["output_tokens"]
    return int(replayed)


def session_findings(session: dict, steps: list[dict],
                     attributed: list[dict]) -> list[dict]:
    """The avoidable patterns this journal can actually evidence, weighted.

    Only what the run could have avoided weighs. A heavy tool is a lead worth
    opening, not a fault, and scores zero.
    """
    usage = session.get("tokens") or {}
    total = usage.get("total_tokens") or 0
    if not total or not steps:
        return []
    findings = []

    def add(kind: str, tokens: float, reason: str, advice: str, tips: tuple,
            items=(), weight: float | None = None, floor: int | None = None):
        threshold = floor if floor is not None else max(
            FINDING_MIN_TOKENS, total * FINDING_MIN_SHARE)
        if tokens < threshold:
            return
        share = 100 * tokens / total
        findings.append({
            "kind": kind, "tokens": int(round(tokens)), "share": round(share, 1),
            "weight": round(grade_weight(kind, share, tokens, GRADE_MATERIAL_TOKENS)
                            if weight is None else weight, 2),
            "reason": reason, "advice": advice, "tips": tips, "items": list(items),
            "id": session["id"], "short": session["id"][-12:],
            "project": session.get("group") or session.get("project") or "",
        })

    cache = _cache_misses(steps)
    if cache["tokens"]:
        gap = cache["median_gap"]
        when = f", after a {gap / 60:.0f} min pause on median" if gap else ""
        add("cache", cache["tokens"],
            f"On {cache['count']} step(s) the context was re-sent uncached"
            f"{when} rather than read back from the prompt cache.",
            "When continuing the same task, resume before the cache expires.",
            ("Do not rush work just for the cache; this matters only when you were "
             "already about to resume the same task.",
             "After a long break, treat the next request as a deliberate restart "
             "rather than an unexpected extra cost."))

    replayed = _replayed_output(steps)
    if replayed:
        add("replayed-output", replayed,
            "Earlier replies remained in context and were sent again on later steps.",
            "At the next change of phase, start a fresh session.",
            ("Split work at natural handoffs: investigation, implementation, then "
             "review.",
             "Carry only a short handoff summary into the next session, not the whole "
             "working conversation."))

    per_call = _call_tokens(steps)
    junk_tokens = 0.0
    junk_items: dict[str, int] = defaultdict(int)
    seen: dict[tuple[str, str], int] = defaultdict(int)
    duplicate_tokens = 0.0
    duplicate_items: dict[str, int] = defaultdict(int)
    for step in steps:
        for call in step.get("calls_detail") or []:
            detail = call.get("detail") or ""
            name = call.get("tool") or call["name"]
            tokens = per_call.get(call["index"], 0.0)
            if detail and junk_target(detail):
                junk_tokens += tokens
                junk_items[detail] += 1
            # A target the journal had to cut is not evidence of a repeat: eight
            # different URLs share the first forty characters of one curl line.
            # Only a target kept whole can be compared against another.
            if not detail or detail.endswith(_CUT):
                continue
            key = (name, detail)
            seen[key] += 1
            if seen[key] > 1:
                duplicate_tokens += tokens
                duplicate_items[f"{name}({detail})"] += 1

    if junk_tokens:
        add("junk-reads", junk_tokens,
            f"{sum(junk_items.values())} call(s) pointed at generated, vendored or "
            "locked content, whose result every later step then carried.",
            "Point the call at the source file rather than at its build product.",
            ("Lock files, bundles and dependency trees answer almost nothing and are "
             "carried by every later request.",
             "When a dependency really is the question, read the one file inside it "
             "rather than the directory."),
            [f"{detail} — {count}×" for detail, count in
             sorted(junk_items.items(), key=lambda pair: -pair[1])[:_ITEM_LIMIT]],
            floor=FINDING_MIN_TOKENS)

    if duplicate_tokens:
        add("duplicate-reads", duplicate_tokens,
            f"{sum(duplicate_items.values())} call(s) repeated a target already "
            "requested in this session. The journal records no result, so a repeat "
            "is counted on the target alone.",
            "The answer is already in context: ask for what changed rather than for "
            "the same target again.",
            ("A repeated call costs its whole result again, and every later step "
             "carries both copies.",
             "After an edit, the diff is what changed — the file does not have to "
             "come back whole."),
            [f"{label} — {count + 1}×" for label, count in
             sorted(duplicate_items.items(), key=lambda pair: -pair[1])[:_ITEM_LIMIT]],
            floor=FINDING_MIN_TOKENS)

    tools = [row for row in attributed if row["tool"] != NO_TOOL]
    if tools:
        largest = max(tools, key=lambda row: row["tokens"])
        add("tool-result", largest["tokens"],
            f"{largest['tool']} results were carried into later steps.",
            "Check whether that result could be narrower.",
            ("Search first, then read the relevant slice instead of loading a whole "
             "file, directory, or log.",
             "Ask commands for the smallest useful output: targeted filters, limits, "
             "and paths beat broad listings."))

    chain = agents_chain(session.get("cwd") or "")
    size = sum(spec["bytes"] for spec in chain)
    if size > AGENTS_LIMIT:
        # Step one is the opening request, so its input is what the standing
        # instructions actually cost on this session rather than a proxy for it.
        add("instructions", steps[0]["tokens"]["input_tokens"],
            f"Instructions total {size / 1000:.1f} kB, resent on every request of "
            "every session. The first step — system prompt, instructions and tool "
            "definitions — is what that volume was here.",
            "Keep the standing rules; move the rest to files Codex opens when it "
            "needs them.",
            ("An AGENTS.md is read on every request, whether or not the request "
             "needs it.",
             "Rules that apply to one directory belong in that directory, not in the "
             "file every session loads."),
            [f"{spec['path']} — {spec['bytes'] / 1000:.1f} kB"
             for spec in chain[:_ITEM_LIMIT]],
            weight=min(8.0, 4.0 * (size / AGENTS_LIMIT - 1)),
            floor=FINDING_MIN_TOKENS)
    return findings


def grade_codex_session(session: dict, steps: list[dict]) -> dict:
    """Attribution, findings and the letter, for one already-parsed session."""
    attributed = attribute_tools(steps)
    findings = sorted(session_findings(session, steps, attributed),
                      key=lambda finding: (-finding["weight"], -finding["tokens"]))
    score = round(sum(finding["weight"] for finding in findings), 1)
    return {"attributed": attributed, "triage": findings, "score": score,
            "grade": grade_for(score)}
