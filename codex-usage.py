#!/usr/bin/env python3
"""Observed Codex Desktop token usage, grouped from local rollout journals.

Reads only ~/.codex/sessions and ~/.codex/archived_sessions.  It deliberately
reports tokens and observed quota windows, never an inferred Codex subscription
cost.

Tool calls are reported by the tool the harness wrapper really invoked, with a
40-character description of what it pointed at -- see ``codex_log`` for the
bound and for what is never read.  A generated report therefore carries
fragments of local command lines and paths.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

from codex_log import ObservedQuota, ParsedCodexLog, TokenInterval, TokenUsage, read_codex_log
from codex_grade import attribute_tools, codex_steps, grade_codex_session
from session_grade import GRADE_WEIGHTS
from dashboard_template import render_dashboard_document, source_tabs
from dashboard_model import (clock as _clock, codex_dashboard_model, compact,
                             elapsed_label, footer, metadata_list,
                             render_dashboard_overview, render_session_view,
                             shade, span_label)


SESSION_ROOTS = (
    ("active", Path.home() / ".codex" / "sessions"),
    ("archived", Path.home() / ".codex" / "archived_sessions"),
)
ROLL_OUT_DATE_LENGTH = len("rollout-YYYY-MM-DD")
GIT_LEAD_SECONDS = 120
GIT_GRACE_SECONDS = 30 * 60
GIT_MAX_PROJECTS = 8
GIT_OUTCOMES = ("landed", "reverted", "unmerged", "no-commit")
SESSION_SORTS = {
    "tokens-desc": "Costliest first",
    "tokens-asc": "Cheapest first",
    "recent-desc": "Most recent",
    "recent-asc": "Oldest",
    "project-asc": "Project (A→Z)",
}
STEP_SORTS = {
    "tokens-desc": ("Largest first", lambda step: (-step["tokens"]["total_tokens"],
                                                   step["index"])),
    "tokens-asc": ("Smallest first", lambda step: (step["tokens"]["total_tokens"],
                                                   step["index"])),
    "output-desc": ("Tokens produced", lambda step: (-step["tokens"]["output_tokens"],
                                                     step["index"])),
    "input-desc": ("Largest input", lambda step: (-step["tokens"]["input_tokens"],
                                                  step["index"])),
    "step-asc": ("Chronological", lambda step: step["index"]),
    "step-desc": ("Reverse order", lambda step: -step["index"]),
}
DEFAULT_STEP_SORT = "output-desc"
DEFAULT_SESSION_LIMIT = 20
DEFAULT_DASHBOARD_SESSION_LIMIT = 5
_REVERT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")


@dataclass(frozen=True)
class RolloutFile:
    path: Path
    source: str
    started_on: date


@dataclass(frozen=True)
class CodexSession:
    rollout_id: str
    thread_id: str | None
    source: str
    project: str
    cwd: str | None
    started_at: str | None
    ended_at: str | None
    model: str | None
    model_provider: str | None
    status: str
    tokens: TokenUsage | None
    intervals: tuple[TokenInterval, ...]
    tools: tuple
    last_quota: ObservedQuota | None
    warnings: tuple[str, ...]


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _worktree_parent(dot_git: Path) -> Path | None:
    """Return a worktree's primary repository, like the Claude reader does."""
    try:
        first = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    target = Path(first.split(":", 1)[1].strip())
    parts = target.parts
    if "worktrees" not in parts:
        return None
    index = len(parts) - 1 - parts[::-1].index("worktrees")
    common = Path(*parts[:index])
    return common.parent if common.name == ".git" else None


def project_root(cwd: str | None, split_worktrees: bool = False) -> str:
    """Find a Git root from cwd, falling back to cwd without shelling out."""
    if not cwd:
        return "(unknown cwd)"
    current = Path(cwd)
    for candidate in (current, *current.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return str(candidate)
        if dot_git.is_file():
            parent = None if split_worktrees else _worktree_parent(dot_git)
            return str(parent or candidate)
    return cwd


def rollout_date(path: Path) -> date | None:
    """Date encoded in Codex's rollout filename, without reading the journal."""
    name = path.name
    if not name.startswith("rollout-") or len(name) < ROLL_OUT_DATE_LENGTH:
        return None
    try:
        return date.fromisoformat(name[len("rollout-"):ROLL_OUT_DATE_LENGTH])
    except ValueError:
        return None


def rollout_id(path: Path) -> str:
    """Use the journal filename as the unique session-run identifier.

    `session_meta.session_id` identifies the surrounding Codex thread and is
    shared by several rollout journals (for example, parent and subagent runs).
    It is retained as `thread_id`, but cannot identify one usage record.
    """
    return path.stem.removeprefix("rollout-")


def discover_rollouts(
    roots: Iterable[tuple[str, Path]] = SESSION_ROOTS,
    since: date | None = None,
) -> list[RolloutFile]:
    """Find journal files and apply the date window before opening any JSONL."""
    found = []
    for source, root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            started_on = rollout_date(path)
            if started_on is None or (since is not None and started_on < since):
                continue
            found.append(RolloutFile(path, source, started_on))
    return sorted(found, key=lambda item: (item.started_on, item.path.name))


def _sum_usages(usages: Iterable[TokenUsage]) -> TokenUsage:
    values = {field: 0 for field in TokenUsage.__dataclass_fields__}
    for usage in usages:
        for field in values:
            values[field] += getattr(usage, field)
    return TokenUsage(**values)


def _session_status(parsed: ParsedCodexLog) -> str:
    if parsed.intervals:
        return "partial" if parsed.warnings else "ok"
    if parsed.complete_counters:
        return "no_exploitable_intervals"
    return "no_complete_counters"


def group_key(session: CodexSession, by: str) -> str:
    """Return the selected local grouping without changing the session record."""
    if by == "repo":
        return session.project
    if by == "cwd":
        return session.cwd or "(unknown cwd)"
    if by == "dir":
        return Path(session.cwd).name if session.cwd else "(unknown cwd)"
    raise ValueError(f"unknown grouping: {by}")


def analyze_rollouts(rollouts: Iterable[RolloutFile]) -> list[CodexSession]:
    """Read selected journals into sessions, keeping no transcript content."""
    sessions = []
    for rollout in rollouts:
        parsed = read_codex_log(rollout.path)
        metadata = parsed.metadata
        intervals = tuple(parsed.intervals)
        tokens = _sum_usages(interval.usage for interval in intervals) if intervals else None
        sessions.append(CodexSession(
            rollout_id=rollout_id(rollout.path),
            thread_id=metadata.session_id if metadata else None,
            source=rollout.source,
            project=project_root(metadata.cwd if metadata else None),
            cwd=metadata.cwd if metadata else None,
            started_at=metadata.started_at if metadata else None,
            ended_at=intervals[-1].ended_at if intervals else (metadata.started_at if metadata else None),
            model=metadata.model if metadata else None,
            model_provider=metadata.model_provider if metadata else None,
            status=_session_status(parsed),
            tokens=tokens,
            intervals=intervals,
            tools=tuple(parsed.tools),
            last_quota=parsed.quotas[-1] if parsed.quotas else None,
            warnings=tuple(parsed.warnings),
        ))
    return sorted(sessions, key=lambda item: (item.started_at or "", item.rollout_id))


def _usage_json(usage: TokenUsage | None) -> dict | None:
    if usage is None:
        return None
    return {field: getattr(usage, field) for field in TokenUsage.__dataclass_fields__}


def _quota_json(quota: ObservedQuota | None) -> dict | None:
    if quota is None:
        return None

    def window(value):
        if value is None:
            return None
        return {
            "window_minutes": value.window_minutes,
            "used_percent": value.used_percent,
            "resets_at": value.resets_at,
        }

    return {
        "observed_at": quota.timestamp,
        "plan_type": quota.plan_type,
        "primary": window(quota.primary),
        "secondary": window(quota.secondary),
    }


def _interval_json(interval: TokenInterval) -> dict:
    return {
        "started_at": interval.started_at,
        "ended_at": interval.ended_at,
        "tokens": _usage_json(interval.usage),
    }


def _session_json(session: CodexSession, by: str, git: dict | None) -> dict:
    data = {
        "id": session.rollout_id,
        "thread_id": session.thread_id,
        "source": session.source,
        "project": session.project,
        "group": group_key(session, by),
        "cwd": session.cwd,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "model": session.model,
        "model_provider": session.model_provider,
        "status": session.status,
        "tokens": _usage_json(session.tokens),
        "intervals": [_interval_json(interval) for interval in session.intervals],
        "tools": [
            {"timestamp": tool.timestamp, "name": tool.name, "call_id": tool.call_id,
             "kind": tool.kind, "tool": tool.tool, "detail": tool.detail,
             "label": tool.label}
            for tool in session.tools
        ],
        "last_quota": _quota_json(session.last_quota),
        "warnings": list(session.warnings),
    }
    if git is not None:
        data["git"] = git["session_outcomes"].get(session.rollout_id)
        data["is_git_project"] = data["git"] is not None and data["git"]["status"] not in {
            "outside_git", "unavailable_time",
        }
    else:
        data["is_git_project"] = True
    # Graded here so the terminal, the JSON payload and the dashboard all read
    # the same letter, from the same attribution.
    data.update(grade_codex_session(data, codex_steps(data)))
    return data


def payload_for(
    sessions: Iterable[CodexSession],
    since: date,
    until: date,
    by: str = "repo",
    git: dict | None = None,
) -> dict:
    """One stable, content-free payload shared by future CLI and HTML renderers."""
    sessions = list(sessions)
    token_sessions = [session for session in sessions if session.tokens is not None]
    project_sessions: dict[str, list[CodexSession]] = defaultdict(list)
    daily_usages: dict[str, list[TokenUsage]] = defaultdict(list)
    for session in sessions:
        project_sessions[group_key(session, by)].append(session)
        for interval in session.intervals:
            daily_usages[interval.ended_at[:10]].append(interval.usage)

    projects = []
    for project, grouped in project_sessions.items():
        usable = [session.tokens for session in grouped if session.tokens is not None]
        statuses = [
            (git or {}).get("session_outcomes", {}).get(session.rollout_id, {}).get("status")
            for session in grouped
        ]
        projects.append({
            "project": project,
            # Codex also records standalone conversations. Keep them in the
            # session list, but do not present their folders as projects.
            "is_git_project": git is None or any(
                status not in {None, "outside_git", "unavailable_time"} for status in statuses
            ),
            "sessions": len(grouped),
            "sessions_with_tokens": len(usable),
            "sessions_without_exploitable_counters": len(grouped) - len(usable),
            "tokens": _usage_json(_sum_usages(usable)) if usable else None,
        })
    projects.sort(
        key=lambda row: row["tokens"]["total_tokens"] if row["tokens"] is not None else -1,
        reverse=True,
    )
    daily = [
        {"date": day, "tokens": _usage_json(_sum_usages(usages))}
        for day, usages in sorted(daily_usages.items())
    ]
    return {
        "schema_version": 1,
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "group_by": by,
        "totals": {
            "sessions": len(sessions),
            "sessions_with_tokens": len(token_sessions),
            "sessions_without_exploitable_counters": len(sessions) - len(token_sessions),
            "tokens": _usage_json(_sum_usages(session.tokens for session in token_sessions))
            if token_sessions else None,
        },
        "projects": projects,
        "daily": daily,
        "sessions": [_session_json(session, by, git) for session in sessions],
        "git": git,
    }


def _git(root: str, *arguments: str, timeout: int = 10) -> str:
    """One read-only Git command; unavailable repositories simply do not report."""
    try:
        completed = subprocess.run(
            ("git", "-C", root, *arguments),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


_TOPLEVEL_CACHE: dict[str, str] = {}


def git_toplevel(path: str) -> str:
    """Return the repository root, or an empty string outside a Git repository."""
    if path in _TOPLEVEL_CACHE:
        return _TOPLEVEL_CACHE[path]
    root = _git(path, "rev-parse", "--show-toplevel").strip() if Path(path).is_dir() else ""
    _TOPLEVEL_CACHE[path] = root
    return root


def _mainline_ref(root: str) -> str:
    head = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    if head:
        return head
    for ref in ("refs/remotes/origin/main", "refs/remotes/origin/master",
                "refs/heads/main", "refs/heads/master"):
        if _git(root, "rev-parse", "--verify", "--quiet", ref).strip():
            return ref
    return "HEAD"


def _repo_history(root: str, since: datetime) -> dict:
    """Read the bounded session window and whether each commit reached mainline."""
    commits = []
    for line in _git(
        root, "log", "--all", "--no-merges", "--since", since.isoformat(),
        "--pretty=%H%x1f%ct%x1f%s",
    ).splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        try:
            commits.append({"sha": parts[0], "ts": int(parts[1]), "subject": parts[2]})
        except ValueError:
            continue
    ref = _mainline_ref(root)
    landed = set(_git(root, "rev-list", ref, "--since", since.isoformat()).split())
    reverted: dict[int, set[str]] = defaultdict(set)
    for match in _REVERT_RE.finditer(_git(
        root, "log", "--all", "--grep", "This reverts commit", "-n", "300", "--pretty=%B%x1e",
    )):
        prefix = match.group(1)
        reverted[len(prefix)].add(prefix)
    for commit in commits:
        commit["landed"] = commit["sha"] in landed
        commit["reverted"] = any(
            commit["sha"][:length] in prefixes for length, prefixes in reverted.items()
        )
    return {"mainline": ref.rsplit("/", 1)[-1], "commits": commits}


def _session_tokens(session: CodexSession) -> int:
    return session.tokens.total_tokens if session.tokens is not None else 0


def git_outcomes(
    sessions: Iterable[CodexSession], limit: int = GIT_MAX_PROJECTS
) -> dict:
    """Correlate local sessions to Git commits without inferring productivity.

    At most ``limit`` repositories are queried, ranked by observed tokens.  A
    ``no-commit`` result means only that no commit falls in the time window.
    """
    session_outcomes: dict[str, dict] = {}
    groups: dict[str, list[tuple[CodexSession, datetime, datetime]]] = defaultdict(list)
    outside_git = without_time = 0
    for session in sorted(sessions, key=_session_tokens, reverse=True):
        first = _timestamp(session.started_at)
        last = _timestamp(session.ended_at) or first
        if first is None or last is None:
            session_outcomes[session.rollout_id] = {"status": "unavailable_time", "commits": []}
            without_time += 1
            continue
        root = git_toplevel(session.project)
        if not root:
            session_outcomes[session.rollout_id] = {"status": "outside_git", "commits": []}
            outside_git += 1
            continue
        groups[root].append((session, first, last))

    ranked = sorted(
        groups.items(), key=lambda item: -sum(_session_tokens(session) for session, _first, _last in item[1])
    )
    skipped_sessions = 0
    for _root, items in ranked[limit:]:
        skipped_sessions += len(items)
        for session, _first, _last in items:
            session_outcomes[session.rollout_id] = {
                "status": "skipped_repository_limit", "commits": []
            }
    repos = []
    outcomes = {name: {"sessions": 0, "tokens": 0} for name in GIT_OUTCOMES}
    unmatched_commits = 0
    for root, items in ranked[:limit]:
        floor = min(first for _session, first, _last in items) - timedelta(days=1)
        history = _repo_history(root, floor)
        claimed: dict[str, list[dict]] = {session.rollout_id: [] for session, _first, _last in items}
        for commit in history["commits"]:
            owner = None
            for session, first, last in items:
                if first.timestamp() - GIT_LEAD_SECONDS <= commit["ts"] <= last.timestamp() + GIT_GRACE_SECONDS:
                    if owner is None or first > owner[1]:
                        owner = (session, first, last)
            if owner is None:
                unmatched_commits += 1
            else:
                claimed[owner[0].rollout_id].append(commit)

        repository_outcomes = {name: {"sessions": 0, "tokens": 0} for name in GIT_OUTCOMES}
        for session, _first, _last in items:
            commits = claimed[session.rollout_id]
            if not commits:
                status = "no-commit"
            elif any(commit["landed"] and not commit["reverted"] for commit in commits):
                status = "landed"
            elif any(commit["reverted"] for commit in commits):
                status = "reverted"
            else:
                status = "unmerged"
            short_commits = [
                {"sha": commit["sha"][:8], "landed": commit["landed"], "reverted": commit["reverted"]}
                for commit in commits
            ]
            session_outcomes[session.rollout_id] = {"status": status, "commits": short_commits}
            repository_outcomes[status]["sessions"] += 1
            repository_outcomes[status]["tokens"] += _session_tokens(session)
            outcomes[status]["sessions"] += 1
            outcomes[status]["tokens"] += _session_tokens(session)
        repos.append({
            "path": root,
            "name": Path(root).name or root,
            "mainline": history["mainline"],
            "sessions": len(items),
            "tokens": _usage_json(_sum_usages(
                session.tokens for session, _first, _last in items if session.tokens is not None
            )) if any(session.tokens is not None for session, _first, _last in items) else None,
            "outcomes": repository_outcomes,
        })
    return {
        "repository_limit": limit,
        "repos": repos,
        "outcomes": outcomes,
        "session_outcomes": session_outcomes,
        "outside_git": outside_git,
        "without_time": without_time,
        "skipped_sessions": skipped_sessions,
        "unmatched_commits": unmatched_commits,
    }


def _tokens(value: int) -> str:
    """Full digits, for a terminal column that has the width for them."""
    return f"{value:,}".replace(",", " ")


# The page is read at a glance and its rows are narrow, so every figure on it is
# abbreviated the way the Claude Code dashboard abbreviates its own.
_figure = compact


def _print_table(title: str, headers: tuple[str, ...], rows: Iterable[tuple[str, ...]]) -> None:
    rows = list(rows)
    print(f"\n{title}")
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _quota_label(quota: dict | None) -> str:
    if quota is None:
        return "—"
    observed = quota["observed_at"].replace("T", " ").replace("+00:00", "Z")
    windows = []
    if quota["primary"] is not None:
        windows.append(f"short {quota['primary']['used_percent']:g}%")
    if quota["secondary"] is not None:
        windows.append(f"weekly {quota['secondary']['used_percent']:g}%")
    return f"{observed} ({', '.join(windows) or 'observed'})"


def _git_label(git: dict | None) -> str:
    if git is None:
        return "disabled"
    return git["status"].replace("_", " ")


def _tool_rows(payload: dict) -> list[tuple[str, str, str]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    calls: dict[str, int] = defaultdict(int)
    for session in payload["sessions"]:
        for tool in session["tools"]:
            name = tool.get("tool") or tool["name"]
            grouped[name].add(session["id"])
            calls[name] += 1
    return [
        (name, str(len(grouped[name])), str(calls[name]))
        for name in sorted(calls, key=lambda value: (-calls[value], value))
    ]


def print_summary(
    payload: dict,
    top: int | None = None,
    sessions: int | None = None,
    daily: bool = False,
    tools: bool = False,
) -> None:
    totals = payload["totals"]
    print(
        f"Codex observed tokens from {payload['window']['since']} to {payload['window']['until']} — "
        f"{totals['sessions']} sessions, {totals['sessions_with_tokens']} with usable intervals"
    )
    if totals["tokens"] is not None:
        tokens = totals["tokens"]
        print(
            "Input uncached {input}  Cache read {cached}  Cache write {cache_write}  "
            "Output {output}  Reasoning {reasoning}  Total {total}".format(
                input=_tokens(tokens["input_tokens"]),
                cached=_tokens(tokens["cached_input_tokens"]),
                cache_write=_tokens(tokens["cache_write_input_tokens"]),
                output=_tokens(tokens["output_tokens"]),
                reasoning=_tokens(tokens["reasoning_output_tokens"]),
                total=_tokens(tokens["total_tokens"]),
            )
        )
    if totals["sessions_without_exploitable_counters"]:
        # Two different silences, and only one of them is a gap in the reading.
        statuses = Counter(row["status"] for row in payload["sessions"])
        parts = []
        if statuses["no_exploitable_intervals"]:
            parts.append(f"{statuses['no_exploitable_intervals']} recorded a counter "
                         "that reports zero")
        if statuses["no_complete_counters"]:
            parts.append(f"{statuses['no_complete_counters']} wrote no complete "
                         "counter at all, so nothing can be totalled from them")
        print(f"{totals['sessions_without_exploitable_counters']} session(s) carry no "
              f"usable interval: {'; '.join(parts)}.")

    _print_table(
        f"By {payload['group_by']}",
        ("Project", "Sessions", "Usable", "Input uncached", "Cache read", "Cache write", "Output", "Reasoning", "Total"),
        (
            (
                row["project"], str(row["sessions"]), str(row["sessions_with_tokens"]),
                _tokens(row["tokens"]["input_tokens"]) if row["tokens"] else "—",
                _tokens(row["tokens"]["cached_input_tokens"]) if row["tokens"] else "—",
                _tokens(row["tokens"]["cache_write_input_tokens"]) if row["tokens"] else "—",
                _tokens(row["tokens"]["output_tokens"]) if row["tokens"] else "—",
                _tokens(row["tokens"]["reasoning_output_tokens"]) if row["tokens"] else "—",
                _tokens(row["tokens"]["total_tokens"]) if row["tokens"] else "—",
            )
            for row in payload["projects"][:top]
        ),
    )
    if daily:
        _print_table(
            "By day",
            ("Date", "Input uncached", "Cache read", "Cache write", "Output", "Reasoning", "Total"),
            (
                (
                    row["date"], _tokens(row["tokens"]["input_tokens"]),
                    _tokens(row["tokens"]["cached_input_tokens"]),
                    _tokens(row["tokens"]["cache_write_input_tokens"]),
                    _tokens(row["tokens"]["output_tokens"]),
                    _tokens(row["tokens"]["reasoning_output_tokens"]),
                    _tokens(row["tokens"]["total_tokens"]),
                )
                for row in payload["daily"]
            ),
        )
    if sessions:
        ranked = sorted(
            payload["sessions"],
            key=lambda row: row["tokens"]["total_tokens"] if row["tokens"] else -1,
            reverse=True,
        )[:sessions]
        _print_table(
            "Sessions",
            ("Session", "Project", "Status", "Input uncached", "Cache read", "Cache write", "Output", "Reasoning", "Total", "Last observed quota", "Git"),
            (
                (
                    row["id"][-12:], Path(row["group"]).name or row["group"], row["status"],
                    _tokens(row["tokens"]["input_tokens"]) if row["tokens"] else "—",
                    _tokens(row["tokens"]["cached_input_tokens"]) if row["tokens"] else "—",
                    _tokens(row["tokens"]["cache_write_input_tokens"]) if row["tokens"] else "—",
                    _tokens(row["tokens"]["output_tokens"]) if row["tokens"] else "—",
                    _tokens(row["tokens"]["reasoning_output_tokens"]) if row["tokens"] else "—",
                    _tokens(row["tokens"]["total_tokens"]) if row["tokens"] else "—",
                    _quota_label(row["last_quota"]), _git_label(row.get("git")),
                )
                for row in ranked
            ),
        )
        print("  Quotas are last observed values, not spending or guaranteed limits.")
    if tools:
        _print_table("Tool calls", ("Tool", "Sessions", "Calls"), _tool_rows(payload)[:top])
    if payload["git"] is not None:
        git = payload["git"]
        print(
            "Git correlation: {repos} repository/repositories checked (limit {limit}); "
            "{outside} session(s) outside Git, {skipped} skipped by the repository limit.".format(
                repos=len(git["repos"]), limit=git["repository_limit"],
                outside=git["outside_git"], skipped=git["skipped_sessions"],
            )
        )


def _dashboard_query(state: dict, **overrides: object) -> str:
    values = {**state, **overrides}
    encoded = {key: str(value) for key, value in values.items() if value not in (None, "")}
    return "?" + urlencode(encoded) if encoded else "?"


def _dashboard_state(payload: dict) -> dict:
    listing = payload.get("listing") or {}
    return {
        # `dashboard_source` is present but null on a standalone export, so the
        # default of `.get` never fires: coerce rather than trust the key.
        "source": payload.get("dashboard_source") or "",
        "days": payload["window"].get("days") or "",
        "q": listing.get("filter_sessions", ""),
        "by": payload["group_by"],
        "sort": listing.get("sort", "tokens-desc"),
        "tsort": listing.get("sort_steps", ""),
        "tq": listing.get("filter_steps", ""),
        "max": listing.get("limit", DEFAULT_DASHBOARD_SESSION_LIMIT),
    }


def _dashboard_totals(sessions: Iterable[dict]) -> dict:
    """Aggregate the dashboard's Git-backed session observations only."""
    sessions = list(sessions)
    token_sessions = [session["tokens"] for session in sessions if session["tokens"] is not None]
    keys = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "output_tokens", "reasoning_output_tokens", "total_tokens")
    return {
        "sessions": len(sessions),
        "sessions_with_tokens": len(token_sessions),
        "sessions_without_exploitable_counters": len(sessions) - len(token_sessions),
        "tokens": ({key: sum(tokens[key] for tokens in token_sessions) for key in keys}
                   if token_sessions else None),
    }


def _dashboard_daily(sessions: Iterable[dict]) -> list[dict]:
    """Rebuild the daily series from the same Git-backed sessions as the totals."""
    keys = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "output_tokens", "reasoning_output_tokens", "total_tokens")
    daily: dict[str, dict[str, int]] = {}
    for session in sessions:
        for interval in session["intervals"]:
            date_key = interval["ended_at"][:10]
            totals = daily.setdefault(date_key, {key: 0 for key in keys})
            for key in keys:
                totals[key] += interval["tokens"][key]
    return [{"date": date_key, "tokens": tokens} for date_key, tokens in sorted(daily.items())]


def dashboard_view(
    payload: dict,
    project: str | None,
    sort: str = "tokens-desc",
    limit: int = DEFAULT_DASHBOARD_SESSION_LIMIT,
    focus: str | None = None,
    served: bool = False,
    step_sort: str = DEFAULT_STEP_SORT,
    step_filter: str = "",
) -> dict:
    """Select a bounded, ordered dashboard view without changing its totals."""
    if sort not in SESSION_SORTS:
        raise ValueError(f"unknown session sort: {sort}")
    project_sessions = [
        session for session in payload["sessions"] if session.get("is_git_project", True)
    ]
    candidates = list(project_sessions)
    if project:
        needle = project.lower()
        candidates = [session for session in candidates if needle in " ".join(
            str(session.get(field) or "")
            for field in ("id", "thread_id", "project", "cwd", "group")
        ).lower()]
    if sort == "tokens-desc":
        candidates.sort(key=lambda row: row["tokens"]["total_tokens"] if row["tokens"] else -1,
                        reverse=True)
    elif sort == "tokens-asc":
        candidates.sort(key=lambda row: row["tokens"]["total_tokens"] if row["tokens"] else float("inf"))
    elif sort == "recent-desc":
        candidates.sort(key=lambda row: row["started_at"] or "", reverse=True)
    elif sort == "recent-asc":
        candidates.sort(key=lambda row: row["started_at"] or "")
    else:
        candidates.sort(key=lambda row: (row["group"].lower(), row["started_at"] or ""))
    view = dict(payload)
    view["totals"] = _dashboard_totals(project_sessions)
    view["daily"] = _dashboard_daily(project_sessions)
    view["projects"] = [
        project for project in payload["projects"] if project.get("is_git_project", True)
    ]
    view["git_sessions"] = project_sessions
    view["listing"] = {
        "filter_sessions": project or "",
        "sort": sort,
        "sort_steps": step_sort,
        "filter_steps": step_filter,
        "limit": limit,
        "matched": len(candidates),
        "pool": len(project_sessions),
    }
    view["served"] = served
    if focus is None:
        view["sessions"] = candidates[:limit]
        view["focus"] = None
        return view
    view["focus"] = next((row for row in candidates if row["id"] == focus), None)
    view["sessions"] = []
    return view


def _dashboard_tokens(usage: dict | None) -> str:
    return _figure(usage["total_tokens"]) if usage else "—"


def _session_title(session: dict) -> str:
    return f'{Path(session["group"]).name or session["group"]} · {session["id"][-12:]}'


CODEX_FACT_TIPS = {
    "Uncached input": "Input tokens the journal counted outside the cache, summed "
                      "over every observed interval.",
    "Served from cache": "The share of this session's input the journal counted as "
                         "read back from the prompt cache rather than sent again.",
    "Cache read": "Input tokens served from the prompt cache. The journal keeps this "
                  "counter separate from the others, so it is reported as read, never "
                  "recomputed as a share of them.",
    "Written to cache": "Input tokens written into the prompt cache over the session.",
    "Tokens produced": "Output tokens the session generated, reasoning included.",
    "Reasoning output": "The part of the output the journal attributes to reasoning. "
                        "Reported on its own, without assuming it is inside the "
                        "output counter above.",
    "Observed total": "The journal's own cumulative total for this session, read as "
                      "its own counter rather than rebuilt from the categories.",
    "Peak step": "The largest input volume observed between two counter snapshots — "
                 "the closest thing this journal carries to a peak context.",
    "Counter status": "Whether the local counters were complete for the whole "
                      "session. A partial record is an observation with holes, never "
                      "a zero. The figures sum to the journal's own last cumulative "
                      "counter, first request included.",
    "Steps": "How many usable counter intervals the journal produced — the closest "
             "thing it has to a turn count. Every token figure on this page rests on "
             "them, and a skipped snapshot means one step fewer than the session "
             "really had.",
}

CODEX_COLUMN_TIPS = {
    "Step": "Position of the counter interval in the session, oldest first. Hover for "
            "the moment it covers.",
    "What the step ran": "The tool calls timestamped inside this interval, named by "
                         "the one target the journal keeps. Hover for every call of "
                         "the step and the moment it covers. No call means the model "
                         "answered without one — not that nothing happened.",
    "Tokens": "The journal's own total for this step, read as its own counter rather "
              "than rebuilt from the categories beside it.",
    "Input": "Input tokens charged on this step: the context Codex replayed to get "
             "the answer. This is the column the curve above draws.",
    "Output": "Output tokens produced on this step.",
    "Reasoning": "The part of the output the journal attributes to reasoning, "
                 "reported without assuming it sits inside the output counter.",
    "Share": "This row's part of the session's observed total.",
    "Tool": "The tool the call actually invoked, unwrapped from the harness "
            "program around it.",
    "Calls": "How many times this tool was called in the session.",
    "Tokens attributed": "The step tokens this tool is credited with: each step's "
                         "observed total split equally across the calls inside it. "
                         "An attribution, not a per-call measurement — the journal "
                         "records no token figure for a call.",
    "Avg call": "The attributed tokens divided by the number of calls. It ranks "
                "tools by weight; it is not the size of any one result.",
}


def _codex_column(label: str, numeric: bool = False) -> dict:
    """A column of the session tables, carrying its own explanation."""
    return {"label": label, "n": numeric, "tip": CODEX_COLUMN_TIPS.get(label)}

CODEX_STATUS_TIPS = {
    "ok": "Every cumulative counter in this journal was complete and increasing, so "
          "the totals below are the journal's own.",
    "partial": "The journal carries usable counters, but some snapshots were "
               "incomplete, duplicated or decreasing and were skipped. The totals are "
               "a floor.",
    "no_exploitable_intervals": "The journal carries counters, but every usable "
                                "snapshot reports zero. Nothing was consumed that it "
                                "recorded — this is a reading, not a missing figure.",
    "no_complete_counters": "No complete cumulative counter was written to this "
                            "journal; nothing can be totalled from it.",
}


def _codex_chart(session: dict, steps: list) -> dict | None:
    """The observed input volume, step by step, with the tools each step ran."""
    if len(steps) < 2:
        return None
    points = []
    for step in steps:
        usage = step["tokens"]
        points.append({
            "x": f"step {step['index']}",
            "value": usage["input_tokens"],
            "label": step["label"],
            "added": usage["total_tokens"],
            "dull": not step["tools"],
            "reset": False,
            "readout": (f"step {step['index']} · {step['label']} "
                        f"· +{_figure(usage['total_tokens'])} "
                        f"· input {_figure(usage['input_tokens'])} · cache "
                        f"{_figure(usage['cached_input_tokens'])} · out "
                        f"{_figure(usage['output_tokens'])}"),
        })
    return {
        "id": session["id"][-12:],
        "title": "How the context grew",
        "label": "Input tokens observed step by step",
        "hint": ("One point per counter snapshot: the input tokens Codex charged "
                 "between two of them, which is the context it replayed. The journal "
                 "records no per-turn context measurement, so a step covering several "
                 "requests sums them. Hover any point for the tools that ran on it."),
        "idle": "hover the curve for the step behind any point",
        "points": points,
        "legend": [("", "input tokens observed on the step"),
                   ("peak", "biggest steps")],
    }


def _step_when(step: dict) -> str:
    """The moment one counter interval covers."""
    return span_label(step["interval"]["started_at"], step["interval"]["ended_at"])


def _step_calls(step: dict) -> str:
    """Every call the step ran, named by the one target the journal kept."""
    if not step["calls_detail"]:
        return f'{_step_when(step)} · no tool call'
    listed = " · ".join(call.get("label") or call["name"]
                        for call in step["calls_detail"][:8])
    more = (f' · +{len(step["calls_detail"]) - 8} more'
            if len(step["calls_detail"]) > 8 else "")
    return f'{_step_when(step)} · {listed}{more}'




def _step_form(state: dict, session_id: str) -> str:
    """Step sorting and filtering, the counterpart of the Claude turn form."""
    esc = html_escape
    current = state.get("tsort") or DEFAULT_STEP_SORT
    reset = ""
    if state.get("tq") or current != DEFAULT_STEP_SORT:
        reset = (f'<a class="reset" href="{esc(_dashboard_query(state, tsort="", tq="", id=session_id))}">'
                 "Clear</a>")
    options = "".join(
        f'<option value="{key}"{" selected" if key == current else ""}>{label}</option>'
        for key, (label, _order) in STEP_SORTS.items())
    return ('<form class="filter" method="get" action="/session">'
            + f'<input type="hidden" name="id" value="{esc(session_id)}">'
            + f'<input type="hidden" name="source" value="{esc(state["source"])}">'
            + f'<input type="hidden" name="days" value="{esc(str(state["days"]))}">'
            + f'<input type="hidden" name="by" value="{esc(state["by"])}">'
            + f'<input type="hidden" name="sort" value="{esc(state["sort"])}">'
            + f'<input type="hidden" name="q" value="{esc(state["q"])}">'
            + f'<input type="hidden" name="max" value="{esc(str(state["max"]))}">'
            + '<span class="eyebrow">Steps</span><label><span>Sort</span>'
            + f'<select name="tsort">{options}</select></label>'
            + '<label class="grow"><span>Filter</span><input type="search" name="tq" '
            + f'placeholder="tool or target" value="{esc(state.get("tq") or "")}"></label>'
            + '<button type="submit">Apply</button>' + reset + "</form>")


def _select_steps(steps: list, state: dict) -> list:
    """Order and filter the step table the way the turn table is ordered."""
    needle = (state.get("tq") or "").lower().strip()
    kept = [step for step in steps
            if not needle or needle in step["label"].lower()
            or any(needle in name.lower() for name in step["tools"])]
    _label, order = STEP_SORTS.get(state.get("tsort") or DEFAULT_STEP_SORT,
                                   STEP_SORTS[DEFAULT_STEP_SORT])
    return sorted(kept, key=order)


def _quota_rows(quota: dict | None) -> list:
    """The last rate-limit observation, as record rows rather than its own section."""
    if quota is None:
        return [("Last observed quota", "none in this journal")]
    rows = [("Quota observed", _clock(quota["observed_at"]))]
    if quota.get("plan_type"):
        rows.append(("Observed plan", quota["plan_type"]))
    for window in (quota["primary"], quota["secondary"]):
        if window is None:
            continue
        # The contract calls these two windows primary and secondary, not short
        # and long: this journal puts a 7-day window first. Name each by the
        # duration it actually states.
        minutes = window["window_minutes"]
        label = ("Short window" if minutes < 24 * 60
                 else "Weekly window" if minutes <= 7 * 24 * 60
                 else "Long window")
        reset = datetime.fromtimestamp(window["resets_at"]).astimezone()
        rows.append((label, f'{window["used_percent"]:g}% used · '
                            f'{minutes} min · resets '
                            f'{reset.isoformat(timespec="minutes")}'))
    return rows


def _codex_sections(session: dict, steps: list, state: dict, served: bool,
                    standalone: bool) -> list:
    """What the session called, then every step, then the record behind them."""
    attributed = session.get("attributed") or attribute_tools(steps)
    total = sum(row["tokens"] for row in attributed) or 1
    called = {
        "title": "What filled the context",
        "hint": ("The journal states no token figure for a call, so a step's observed "
                 "tokens are split equally across the calls timestamped inside it — "
                 "an attribution, where the session total above is measured. A step "
                 "with no call lands on its own row. Hover any column header for what "
                 "it means."),
        "stack": [{"share": row["tokens"] / total * 100,
                   "color": shade(index, len(attributed)),
                   "title": f'{row["tool"]} — {_figure(row["tokens"])} tokens'}
                  for index, row in enumerate(attributed)],
        "columns": [_codex_column("Tool"), _codex_column("Calls", True),
                    _codex_column("Tokens attributed", True),
                    _codex_column("Avg call", True), _codex_column("Share", True)],
        "rows": [[
            {"text": row["tool"], "swatch": shade(index, len(attributed))},
            {"text": row["count"], "n": True},
            {"text": _figure(row["tokens"]), "n": True},
            {"text": _figure(round(row["tokens"] / max(1, row["count"]))), "n": True},
            {"text": f'{round(100 * row["tokens"] / total)} %', "n": True},
        ] for index, row in enumerate(attributed)],
        "empty": "No exploitable interval, so nothing can be attributed here.",
    }

    observed = sum(step["tokens"]["total_tokens"] for step in steps) or 1
    kept = _select_steps(steps, state) if standalone else steps
    timeline = {
        "title": "Step by step",
        "hint": ("One row per counter interval, in order — the closest thing this "
                 "journal has to a turn. What the step ran is read from the tool "
                 "calls timestamped inside it, each shown with the one argument field "
                 "the journal keeps, cut at 40 characters; a step with no call is one "
                 "where only the model spoke. Incomplete, duplicated or decreasing "
                 "snapshots are skipped rather than turned into consumption, so a "
                 "session can have fewer steps than it had turns."),
        "controls": (_step_form(state, session["id"])
                     if standalone and served else ""),
        "columns": [_codex_column("Step", True), _codex_column("What the step ran"),
                    _codex_column("Tokens", True), _codex_column("Input", True),
                    _codex_column("Output", True), _codex_column("Reasoning", True),
                    _codex_column("Share", True)],
        "rows": [[
            {"text": step["index"], "n": True, "title": _step_when(step)},
            {"text": step["label"], "label": True, "title": _step_calls(step)},
            {"text": _figure(step["tokens"]["total_tokens"]), "n": True},
            {"text": _figure(step["tokens"]["input_tokens"]), "n": True},
            {"text": _figure(step["tokens"]["output_tokens"]), "n": True},
            {"text": _figure(step["tokens"]["reasoning_output_tokens"]), "n": True},
            {"text": f'{round(100 * step["tokens"]["total_tokens"] / observed)} %',
             "n": True},
        ] for step in kept],
        "empty": ("No step matches the filter." if steps else
                  "No exploitable interval, so this session has no readable step."),
        "notes": ([f"{len(kept)} of {len(steps)} matching steps shown."]
                  if steps and len(kept) != len(steps) else []),
    }

    sections = [called, timeline]
    if standalone:
        record = [
            ("Session", session["id"]),
            ("Thread", session["thread_id"] or "—"),
            ("Source", session["source"]),
            ("Project", session["project"]),
            ("Working directory", session["cwd"] or "—"),
            ("Started", _clock(session["started_at"])),
            ("Ended", _clock(session["ended_at"])),
            ("Model", " / ".join(value for value in (
                session["model_provider"], session["model"]) if value) or "—"),
            ("Token figure", "journal counters"
             if session["status"] == "ok" else "partial journal counters"),
            ("Git", _git_label(session.get("git"))),
            *_quota_rows(session["last_quota"]),
            ("Skipped records", str(len(session["warnings"]))),
        ]
        warnings = ""
        if session["warnings"]:
            warnings = ("<details><summary>Every line the reader refused to turn into "
                        f'consumption ({len(session["warnings"])})</summary><ul>'
                        + "".join(f"<li>{html_escape(warning)}</li>"
                                  for warning in session["warnings"]) + "</ul></details>")
        sections.append({
            "title": "Session record",
            "hint": "What the journal states about the run itself, unaggregated — "
                    "including the last rate-limit window it observed, which is an "
                    "observation and never spending or a guaranteed limit.",
            "html": metadata_list(record) + warnings,
        })
    return sections


GRADE_TIP = ("Grade {grade}, score {score:.0f} — what this session could have "
             "avoided, as a share of the tokens it actually observed.")


def _codex_triage(session: dict) -> dict:
    """The session's grade, and the leads it is made of."""
    findings = session.get("triage") or []
    avoidable = sum(finding["tokens"] for finding in findings
                    if finding["weight"] and finding["kind"] in GRADE_WEIGHTS)
    grade = session.get("grade")
    stats = [
        {"label": "Grade", "value": grade or "—", "tone": f"g{(grade or '').lower()}",
         "tip": "A to F, from what this session could have avoided as a share of the "
                "tokens it observed — never from its size. The bands are the ones the "
                "Claude Code reader uses: A under 3, B under 8, C under 15, D under "
                "24, F beyond."},
        {"label": "Score", "value": f'{session.get("score") or 0:.0f}',
         "tip": "The weighted sum of the leads below. Each one weighs its own share of "
                "the session, capped per kind, and fades in under a couple of hundred "
                "thousand tokens: a bad rate on a small session is a rate, not "
                "something to act on."},
        {"label": "Avoidable", "value": compact(avoidable),
         "tip": "The tokens the weighing leads account for together: calls into "
                "generated or vendored content, a target requested twice, a context "
                "re-sent uncached, replies replayed past a fifth of the session. "
                "Leads that score nothing are left out."},
        {"label": "Leads", "value": str(len(findings)),
         "tip": "How many patterns were found here, weighing or not. A heavy tool is "
                "listed but scores zero: worth opening, not a fault."},
    ]
    return {
        "title": "What this session could have avoided",
        "hint": ("The letter grades this session and nothing wider: what the run "
                 "could have avoided, as a share of the tokens it observed. Being "
                 "long or heavy is not a fault — a big tool result is a lead worth "
                 "opening and weighs nothing. Every figure here is attributed over "
                 "counter intervals, because the journal prices nothing and records "
                 "no per-call token count."),
        "stats": stats,
        "findings": [{
            "value": compact(finding["tokens"]) + " tokens",
            "share": (f'{finding["share"]:.0f}% of the session · '
                      + (f'{finding["weight"]:.0f} of the score' if finding["weight"]
                         else "no penalty")),
            "reason": finding["reason"],
            "items": finding.get("items") or [],
            "advice": finding["advice"],
            "tips": finding["tips"],
        } for finding in findings],
        "note": ("Nothing stood out: no call into generated or vendored content, no "
                 "target requested twice, no context re-sent uncached."),
    }


def codex_session_view(session: dict, state: dict, served: bool,
                       standalone: bool = True) -> dict:
    """Normalize one Codex session for the shared session front."""
    usage = session["tokens"]
    steps = codex_steps(session)

    def figure(key: str) -> str:
        return _figure(usage[key]) if usage else "—"

    peak = max((interval["tokens"]["input_tokens"]
                for interval in session["intervals"]), default=0)
    facts = [
        ("Uncached input", figure("input_tokens")),
        ("Served from cache",
         f'{round(100 * usage["cached_input_tokens"] / usage["input_tokens"])} %'
         if usage and usage["input_tokens"] else "—"),
        ("Cache read", figure("cached_input_tokens")),
        ("Written to cache", figure("cache_write_input_tokens")),
        ("Tokens produced", figure("output_tokens")),
        ("Reasoning output", figure("reasoning_output_tokens")),
        ("Observed total", figure("total_tokens")),
        ("Peak step", _figure(peak) if peak else "—"),
        ("Steps", str(len(steps))),
        ("Counter status", session["status"].replace("_", " ")),
    ]

    run = [{"label": span_label(session["started_at"], session["ended_at"])}]
    if elapsed := elapsed_label(session["started_at"], session["ended_at"]):
        run.append({"value": elapsed, "label": "elapsed"})
    run += [
        {"value": str(len(steps)),
         "label": "step" + ("s" if len(steps) != 1 else "")},
        {"value": str(len(session["tools"])),
         "label": "tool call" + ("s" if len(session["tools"]) != 1 else "")},
        {"value": str(len(session["warnings"])),
         "label": "skipped record" + ("s" if len(session["warnings"]) != 1 else "")},
    ]

    exact = session["status"] == "ok"
    badges = [{
        "kind": "exact" if exact else "partial",
        "label": "exact" if exact else "partial",
        "tip": CODEX_STATUS_TIPS.get(session["status"], session["status"]),
    }]
    if grade := session.get("grade"):
        badges.append({"kind": f"grade g{grade.lower()}", "label": grade,
                       "tip": GRADE_TIP.format(grade=grade,
                                               score=session.get("score") or 0)})

    view = {
        "eyebrow": "Session",
        "id": session["id"][-12:],
        "tags": [tag for tag in (session["model"],) if tag],
        "title": Path(session["group"]).name or session["group"],
        "metric": {"value": compact(usage["total_tokens"]) if usage else "—",
                   "unit": "tokens" if usage else ""},
        "badges": badges,
        "run": run,
        "facts": [{"label": label, "value": value,
                   "tip": CODEX_FACT_TIPS.get(label)} for label, value in facts],
        "triage": _codex_triage(session),
        "chart": _codex_chart(session, steps),
        "sections": _codex_sections(session, steps, state, served, standalone),
    }
    if usage is None:
        view["notes"] = ["No exploitable cumulative token interval; this is not a "
                         "zero."]
    if served:
        view["back"] = {"href": "/" + _dashboard_query(state),
                        "label": "All sessions"}
    return view


def _session_detail(session: dict, state: dict, served: bool,
                    standalone: bool = True) -> str:
    """Render one session through the front shared with the other sources."""
    return render_session_view(
        codex_session_view(session, state, served, standalone), standalone)


def render_dashboard_body(payload: dict) -> str:
    """Render the common dashboard overview plus Codex-specific observations."""
    esc = html_escape
    state = _dashboard_state(payload)
    served = payload.get("served", False)
    focus = payload.get("focus")
    if focus is not None:
        return ('<main class="wrap" id="top">' + _session_detail(focus, state, served)
                + footer(codex_dashboard_model(payload)["footer"]) + '</main>')

    period_controls = ""
    session_controls = ""
    if served:
        reset = ""
        if state["q"] or state["sort"] != "tokens-desc":
            reset = f'<a class="reset" href="{esc(_dashboard_query(state, sort="", q="", max=""))}">Clear</a>'
        session_controls = ('<div class="filters"><form class="filter" method="get">'
                    f'<input type="hidden" name="source" value="{esc(state["source"])}">'
                    f'<input type="hidden" name="days" value="{esc(str(state["days"]))}">'
                    f'<input type="hidden" name="by" value="{esc(state["by"])}">'
                    '<span class="eyebrow">Sessions</span><label><span>Sort</span><select name="sort">' + "".join(
                        f'<option value="{choice}"{" selected" if choice == state["sort"] else ""}>{label}</option>'
                        for choice, label in SESSION_SORTS.items())
                    + '</select></label><label class="grow"><span>Filter</span><input type="search" name="q" '
                    + f'placeholder="project or id" value="{esc(state["q"])}"></label>'
                    + f'<label><span>How many</span><input type="number" name="max" min="1" max="60" value="{state["max"]}"></label>'
                    + '<button type="submit">Apply</button>' + reset + '</form></div>')
        period_controls = ('<div class="controls"><span class="eyebrow">Window</span><div class="segment">'
                    + "".join(f'<a href="{esc(_dashboard_query(state, days=days))}"'
                              f'{" aria-current=\"page\"" if str(days) == str(state["days"]) else ""}'
                              f'>{"1 year" if days >= 365 else f"{days} d"}</a>'
                              for days in (7, 30, 90, 365)) + '</div><span class="live">recomputed on every load</span></div>')
    view = codex_dashboard_model(payload)
    links = {
        session["id"]: (f'/session{_dashboard_query(state, id=session["id"])}' if served
                        else f'#s-{session["id"]}')
        for session in payload["sessions"]
    }
    if served and view.get("git"):
        links.update({
            session["id"]: f'/session{_dashboard_query(state, id=session["id"])}'
            for session in view["git"].get("top_sessions", [])
        })
    details = "" if served else "\n".join(
        f'<section id="s-{esc(session["id"])}"><h2>{esc(_session_title(session))} '
        f'\u2014 {esc(_dashboard_tokens(session["tokens"]))}</h2>'
        + _session_detail(session, state, served=False, standalone=False) + '</section>'
        for session in payload["sessions"]
    )
    listing = payload["listing"]
    note = f'<p class="note">{len(payload["sessions"])} detailed session(s).</p>'
    return render_dashboard_overview(view, source_controls=source_tabs(
                                     payload.get("dashboard_source"), payload["window"].get("days")),
                                     period_controls=period_controls,
                                     session_controls=session_controls,
                                     sessions_note=note, session_links=links, details=details)


def dashboard_document(payload: dict) -> str:
    """Assemble a standalone Codex dashboard from its fragment and shared base."""
    return render_dashboard_document(
        Path(__file__), "Codex Token Usage", render_dashboard_body(payload))


def write_dashboard(payload: dict, out_path: Path) -> Path:
    """Write a standalone HTML dashboard; the output needs no runtime files."""
    out_path.write_text(dashboard_document(payload), encoding="utf-8")
    return out_path


def collect_payload(
    since: date,
    until: date,
    project: str | None,
    by: str,
    no_git: bool,
) -> dict:
    """Collect the stable content-free payload shared by terminal and dashboard."""
    sessions = analyze_rollouts(discover_rollouts(since=since))
    if project:
        needle = project.lower()
        sessions = [
            session for session in sessions
            if needle in session.project.lower() or needle in (session.cwd or "").lower()
        ]
    git = None if no_git else git_outcomes(sessions)
    return payload_for(sessions, since, until, by=by, git=git)


def serve_dashboard(
    port: int,
    default_since: date | None,
    default_days: int | None,
    project: str | None,
    by: str,
    no_git: bool,
) -> None:
    """Serve dashboard views only on loopback, recomputing local observations."""
    import http.server
    import urllib.parse

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *values: object) -> None:
            sys.stderr.write(f"  {self.address_string()} {fmt % values}\n")

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in ("/", "/index.html", "/session"):
                self.send_error(404, "Nothing here")
                return
            params = urllib.parse.parse_qs(parsed.query)

            def value(name: str, limit: int = 160) -> str:
                return ((params.get(name) or [""])[0] or "").strip()[:limit]

            days = default_days
            raw_days = value("days", 8)
            if raw_days.isdigit():
                days = max(1, min(3650, int(raw_days)))
            until = date.today()
            since = until - timedelta(days=days - 1) if days else default_since or until
            selected_by = value("by", 8) or by
            if selected_by not in ("repo", "cwd", "dir"):
                self.send_error(400, "Invalid grouping")
                return
            sort = value("sort", 16) or "tokens-desc"
            if sort not in SESSION_SORTS:
                self.send_error(400, "Invalid session sort")
                return
            raw_limit = value("max", 4)
            limit = max(1, min(100, int(raw_limit))) if raw_limit.isdigit() else DEFAULT_DASHBOARD_SESSION_LIMIT
            selected_filter = value("q") or value("project")
            focus = value("id", 160) if parsed.path == "/session" else None
            if focus and not focus.replace("-", "").isalnum():
                self.send_error(400, "Invalid session id")
                return
            payload = collect_payload(since, until, project, selected_by, no_git)
            payload["window"]["days"] = days
            view = dashboard_view(payload, selected_filter or project, sort, limit, focus, served=True)
            if focus and view["focus"] is None:
                self.send_error(404, "Session not found")
                return
            encoded = dashboard_document(view).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Dashboard served at http://127.0.0.1:{server.server_port}/")
    print("Every load re-runs the local analysis. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read observed Codex Desktop tokens from local rollout journals."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=30,
                       help="include rollout files from the last N days (default: 30)")
    group.add_argument("--since", type=date.fromisoformat, help="include rollout files from YYYY-MM-DD")
    parser.add_argument("--project", help="keep sessions whose project or cwd contains this text")
    parser.add_argument("--by", choices=("repo", "cwd", "dir"), default="repo",
                        help="group by Git root, exact cwd, or working-directory name")
    parser.add_argument("--top", type=int, metavar="N", help="show at most N project and tool rows")
    parser.add_argument("--sessions", type=int, metavar="N", help="show the N largest sessions with quota and Git outcome")
    parser.add_argument("--daily", action="store_true", help="show the daily token table")
    parser.add_argument("--tools", action="store_true", help="show the tool-call count table")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit the stable content-free payload as JSON")
    output.add_argument("--dashboard", metavar="FILE", help="write a standalone Codex HTML dashboard")
    output.add_argument("--serve", nargs="?", const=8787, type=int, metavar="PORT",
                        help="serve the dashboard on loopback (default: 8787)")
    parser.add_argument("--sort-sessions", choices=SESSION_SORTS, default="tokens-desc",
                        help="dashboard session ordering (default: tokens-desc)")
    parser.add_argument("--sessions-max", type=int, default=DEFAULT_DASHBOARD_SESSION_LIMIT, metavar="N",
                        help=f"dashboard session/detail limit (default: {DEFAULT_DASHBOARD_SESSION_LIMIT})")
    parser.add_argument("--sort-steps", choices=tuple(STEP_SORTS), default=DEFAULT_STEP_SORT,
                        help=f"session-page step ordering (default: {DEFAULT_STEP_SORT})")
    parser.add_argument("--filter-steps", metavar="TEXT",
                        help="keep only the steps whose tool or target contains this text")
    parser.add_argument("--focus", metavar="ID", help="with --dashboard, write one session detail page")
    parser.add_argument("--served", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dashboard-source", choices=("claude", "codex"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--no-git", action="store_true", help="do not invoke Git or correlate session outcomes")
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error("--days must be at least 1")
    for name in ("top", "sessions", "sessions_max"):
        if getattr(args, name) is not None and getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    if args.focus and not args.dashboard:
        parser.error("--focus requires --dashboard")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.serve is not None:
        serve_dashboard(
            args.serve, args.since, None if args.since else args.days,
            args.project, args.by, args.no_git,
        )
        return 0
    until = date.today()
    since = args.since or until - timedelta(days=args.days - 1)
    payload = collect_payload(since, until, args.project, args.by, args.no_git)
    if args.dashboard:
        payload["window"]["days"] = None if args.since else args.days
        view = dashboard_view(payload, args.project, args.sort_sessions, args.sessions_max, args.focus,
                              served=args.served, step_sort=args.sort_steps,
                              step_filter=args.filter_steps or "")
        view["dashboard_source"] = args.dashboard_source
        if args.focus and view["focus"] is None:
            print(f"Session not found: {args.focus}", file=sys.stderr)
            return 2
        out = write_dashboard(view, Path(args.dashboard))
        print(f"Dashboard written: {out}")
        return 0
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_summary(payload, top=args.top, sessions=args.sessions,
                      daily=args.daily, tools=args.tools)
    return 0


if __name__ == "__main__":
    sys.exit(main())
