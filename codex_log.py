"""Reader for one Codex rollout JSONL file, retaining evidence and no more.

This module deliberately stops at the log contract.  It does not scan the local
Codex directory, calculate a price, call Git, or render a report; those belong
to later delivery steps.

It retains metadata, counters, quotas, parse warnings, and -- for each tool
call -- its name and one derived, truncated description of what it pointed at.
Prompts, model responses and tool *outputs* are never read.  The description is
bounded at ``TOOL_DETAIL_WIDTH`` characters and the source text it comes from is
discarded on the spot, so a rollout is never rebuilt from what is kept.  That
description does carry a fragment of a command, a path or a prompt: it is the
same treatment the Claude Code reader gives its own transcripts, and it is what
makes a session legible rather than a wall of identical rows.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int

    @classmethod
    def from_mapping(cls, value: object) -> "TokenUsage | None":
        if not isinstance(value, dict):
            return None
        values = []
        for name in TOKEN_FIELDS:
            token_count = value.get(name)
            if not isinstance(token_count, int) or isinstance(token_count, bool):
                return None
            values.append(token_count)
        return cls(*values)

    def delta_from(self, earlier: "TokenUsage") -> "TokenUsage":
        return TokenUsage(*(getattr(self, name) - getattr(earlier, name)
                            for name in TOKEN_FIELDS))

    def decreasing_fields(self, earlier: "TokenUsage") -> tuple[str, ...]:
        return tuple(name for name in TOKEN_FIELDS
                     if getattr(self, name) < getattr(earlier, name))


@dataclass(frozen=True)
class SessionMetadata:
    session_id: str | None
    cwd: str | None
    started_at: str | None
    model: str | None
    model_provider: str | None


@dataclass(frozen=True)
class TokenInterval:
    started_at: str
    ended_at: str
    usage: TokenUsage


@dataclass(frozen=True)
class ToolCall:
    """One recorded call: what the journal named it, and what it pointed at.

    ``name`` is the journal's own label.  On this harness it is often a wrapper
    -- every call arrives as ``exec`` -- so ``tool`` carries the nested tool the
    wrapper actually invoked, and falls back to ``name`` when there is none.
    ``detail`` is the truncated target, empty when the call carries none.
    """

    timestamp: str | None
    name: str
    call_id: str | None
    kind: str
    tool: str = ""
    detail: str = ""

    @property
    def label(self) -> str:
        return f"{self.tool}({self.detail})" if self.detail else self.tool


@dataclass(frozen=True)
class QuotaWindow:
    window_minutes: int
    used_percent: float
    resets_at: int


@dataclass(frozen=True)
class ObservedQuota:
    timestamp: str
    plan_type: str | None
    primary: QuotaWindow | None
    secondary: QuotaWindow | None


@dataclass
class ParsedCodexLog:
    metadata: SessionMetadata | None = None
    intervals: list[TokenInterval] = field(default_factory=list)
    tools: list[ToolCall] = field(default_factory=list)
    quotas: list[ObservedQuota] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    complete_counters: int = 0


TOOL_DETAIL_WIDTH = 40
# The harness wraps every call in a small program: `await tools.web__run({...})`.
# The name that matters is the nested one, not the `exec` wrapper around it.
_NESTED_CALL = re.compile(r"\btools\.([A-Za-z_][\w.]*)\s*\(")
# Read in this order, first match wins: the field a human would name the call by.
_TARGET_FIELDS = ("cmd", "command", "search_query", "query", "path", "file_path",
                  "url", "pattern", "patch", "prompt", "code", "input", "chars")


def _short(text: str, width: int = TOOL_DETAIL_WIDTH) -> str:
    """Collapse to one line and cut to `width`, marking the cut."""
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:width - 1] + "\u2026"


def _object_at(source: str, start: int) -> dict | None:
    """The first brace-balanced literal at or after `start`, when it is JSON.

    A hand-rolled scan rather than a regex: the argument object contains braces
    and quotes of its own, and anything that fails to parse is simply given up
    on -- a partial guess would be worse than no description at all.
    """
    opening = source.find("{", start)
    if opening == -1:
        return None
    depth, in_string, escaped = 0, False, False
    for index in range(opening, len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(source[opening:index + 1])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def _flatten(part: object) -> str:
    """One recognisable string out of a command word or a query object."""
    if isinstance(part, dict):
        return next((value for value in part.values() if isinstance(value, str)), "")
    return str(part)


def _target(arguments: dict | None) -> str:
    """The one field that names a call, shortened; nothing when none fits."""
    if not isinstance(arguments, dict):
        return ""
    for name in _TARGET_FIELDS:
        value = arguments.get(name)
        if isinstance(value, list):
            # A list of words is a command; a list of objects is a batch of
            # queries. Both flatten to the strings a reader would recognise.
            value = " ".join(_flatten(part) for part in value)
        if isinstance(value, str) and value.strip():
            if name in ("path", "file_path"):
                value = value.rsplit("/", 1)[-1]
            return _short(value)
    return ""


def describe_call(name: str, source: object) -> tuple[str, str]:
    """Return (tool, detail) for one call, reading its arguments once.

    `source` is the raw argument text the journal recorded.  It is read here and
    never returned: only the nested tool name and one bounded description leave
    this function.
    """
    if not isinstance(source, str) or not source.strip():
        return name, ""
    nested = list(_NESTED_CALL.finditer(source))
    if not nested:
        # A plain JSON argument object, or a program the harness ran as-is.
        arguments = _object_at(source, 0)
        if arguments is not None:
            return name, _target(arguments)
        first = next((line for line in source.splitlines() if line.strip()), "")
        return name, _short(first)
    tools = list(dict.fromkeys(match.group(1) for match in nested))
    detail = _target(_object_at(source, nested[0].end() - 1))
    if len(tools) > 1:
        rest = " + ".join(tools[1:])
        detail = f"{detail} + {rest}" if detail else f"+ {rest}"
    return tools[0], detail


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _quota_window(value: object) -> QuotaWindow | None:
    if not isinstance(value, dict):
        return None
    minutes = value.get("window_minutes")
    used_percent = value.get("used_percent")
    resets_at = value.get("resets_at")
    if (not isinstance(minutes, int) or isinstance(minutes, bool)
            or not _is_number(used_percent)
            or not isinstance(resets_at, int) or isinstance(resets_at, bool)):
        return None
    return QuotaWindow(minutes, float(used_percent), resets_at)


def _event_model(event: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return (model, provider, cwd) from model-bearing, content-free events."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None, None, None
    if event.get("type") == "turn_context":
        return _string(payload.get("model")), None, _string(payload.get("cwd"))
    if (event.get("type") == "event_msg"
            and payload.get("type") == "thread_settings_applied"):
        settings = payload.get("thread_settings")
        if isinstance(settings, dict):
            return (
                _string(settings.get("model")),
                _string(settings.get("model_provider_id")),
                _string(settings.get("cwd")),
            )
    return None, None, None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _token_snapshot(event: dict[str, Any]) -> object | None:
    payload = event.get("payload")
    if (event.get("type") != "event_msg" or not isinstance(payload, dict)
            or payload.get("type") != "token_count"):
        return None
    info = payload.get("info")
    return info.get("total_token_usage") if isinstance(info, dict) else None


def _append_quota(result: ParsedCodexLog, event: dict[str, Any], line_number: int) -> None:
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return
    rate_limits = payload.get("rate_limits")
    if rate_limits is None:
        return
    timestamp = _string(event.get("timestamp"))
    if not isinstance(rate_limits, dict) or timestamp is None:
        result.warnings.append(f"line {line_number}: incomplete quota observation")
        return
    primary = _quota_window(rate_limits.get("primary"))
    secondary = _quota_window(rate_limits.get("secondary"))
    if rate_limits.get("primary") is not None and primary is None:
        result.warnings.append(f"line {line_number}: incomplete primary quota")
    if rate_limits.get("secondary") is not None and secondary is None:
        result.warnings.append(f"line {line_number}: incomplete secondary quota")
    if primary is not None or secondary is not None:
        result.quotas.append(ObservedQuota(
            timestamp, _string(rate_limits.get("plan_type")), primary, secondary
        ))


def read_codex_log(path: Path) -> ParsedCodexLog:
    """Read one JSONL rollout without retaining transcript content.

    The counters are cumulative from zero for the journal, so the first complete
    snapshot is itself one interval, running from the session's own start.  Every
    later complete, strictly non-decreasing snapshot produces one more, as the
    difference from the last valid one.  Invalid, duplicate, and decreasing
    snapshots are reported and do not replace that reference, avoiding fabricated
    consumption.
    """
    result = ParsedCodexLog()
    session_id = cwd = started_at = model = model_provider = None
    previous_usage: TokenUsage | None = None
    previous_timestamp: str | None = None

    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                result.warnings.append(f"line {line_number}: incomplete JSONL record")
                continue
            if not isinstance(event, dict):
                result.warnings.append(f"line {line_number}: invalid event envelope")
                continue

            timestamp = _string(event.get("timestamp"))
            payload = event.get("payload")
            if event.get("type") == "session_meta" and isinstance(payload, dict):
                session_id = session_id or _string(payload.get("session_id")) or _string(payload.get("id"))
                cwd = cwd or _string(payload.get("cwd"))
                started_at = started_at or timestamp or _string(payload.get("timestamp"))
                model_provider = model_provider or _string(payload.get("model_provider"))

            event_model, event_provider, event_cwd = _event_model(event)
            model = event_model or model
            model_provider = event_provider or model_provider
            cwd = event_cwd or cwd

            if (event.get("type") == "response_item" and isinstance(payload, dict)
                    and payload.get("type") in {"function_call", "custom_tool_call"}):
                name = _string(payload.get("name"))
                if name is None:
                    result.warnings.append(f"line {line_number}: tool call without a name")
                else:
                    source = payload.get("input")
                    if not isinstance(source, str):
                        source = payload.get("arguments")
                    tool, detail = describe_call(name, source)
                    result.tools.append(ToolCall(
                        timestamp, name, _string(payload.get("call_id")) or _string(payload.get("id")),
                        payload["type"], tool, detail,
                    ))

            _append_quota(result, event, line_number)
            snapshot = _token_snapshot(event)
            if snapshot is None:
                continue
            usage = TokenUsage.from_mapping(snapshot)
            if usage is None or timestamp is None:
                result.warnings.append(f"line {line_number}: incomplete cumulative token counter")
                continue
            result.complete_counters += 1
            if previous_usage is None:
                # The counter is cumulative from zero for this journal, so the
                # first snapshot is the first request's own consumption -- not a
                # baseline to subtract away. A resumed or forked thread opens its
                # own count rather than inheriting its parent's, so nothing is
                # double-counted by reading it. An all-zero first snapshot is a
                # counter written before anything was consumed and reports none.
                previous_usage, previous_timestamp = usage, timestamp
                if any(getattr(usage, name) for name in TOKEN_FIELDS):
                    result.intervals.append(
                        TokenInterval(started_at or timestamp, timestamp, usage))
                continue
            if usage == previous_usage:
                result.warnings.append(f"line {line_number}: duplicate cumulative token counter")
                continue
            decreasing = usage.decreasing_fields(previous_usage)
            if decreasing:
                result.warnings.append(
                    f"line {line_number}: decreasing cumulative token counter ({', '.join(decreasing)})"
                )
                continue
            result.intervals.append(TokenInterval(previous_timestamp, timestamp, usage.delta_from(previous_usage)))
            previous_usage, previous_timestamp = usage, timestamp

    if session_id is not None or cwd is not None or started_at is not None:
        result.metadata = SessionMetadata(session_id, cwd, started_at, model, model_provider)
    return result
