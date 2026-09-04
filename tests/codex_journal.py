"""One payload-shaped Codex session, built from its steps.

The grading and session-page tests observe an attribution over counter
intervals: what they need is a session whose steps hold known calls and known
token figures, not a journal on disk.  ``codex_session`` builds exactly that
shape -- the one ``payload_for`` produces -- so those tests state their figures
and read them back.
"""
from __future__ import annotations


TOKEN_ZERO = {"input_tokens": 0, "cached_input_tokens": 0,
              "cache_write_input_tokens": 0, "output_tokens": 0,
              "reasoning_output_tokens": 0, "total_tokens": 0}


def codex_session(steps, cwd: str = "/tmp/nowhere") -> dict:
    """One session dict shaped like the payload, from ``(calls, tokens)`` steps."""
    intervals, tools = [], []
    for index, (calls, tokens) in enumerate(steps):
        started = f"2026-08-30T10:{index * 2:02d}:00Z"
        ended = f"2026-08-30T10:{index * 2 + 1:02d}:00Z"
        intervals.append({"started_at": started, "ended_at": ended,
                          "tokens": dict(TOKEN_ZERO, **tokens)})
        for position, (tool, detail) in enumerate(calls):
            tools.append({"timestamp": f"2026-08-30T10:{index * 2:02d}:{position + 1:02d}Z",
                          "name": "exec", "call_id": f"c{index}-{position}",
                          "kind": "custom_tool_call", "tool": tool, "detail": detail,
                          "label": f"{tool}({detail})" if detail else tool})
    totals = {field: sum(interval["tokens"][field] for interval in intervals)
              for field in TOKEN_ZERO}
    return {"id": "2026-08-30T10-00-00-session", "thread_id": "t", "source": "active",
            "project": cwd, "group": cwd, "cwd": cwd,
            "started_at": intervals[0]["started_at"],
            "ended_at": intervals[-1]["ended_at"], "model": "gpt-5.6-terra",
            "model_provider": "openai", "status": "ok", "tokens": totals,
            "intervals": intervals, "tools": tools, "last_quota": None,
            "warnings": [], "is_git_project": True}
