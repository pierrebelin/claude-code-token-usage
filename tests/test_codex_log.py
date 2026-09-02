from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import codex_log
from codex_log import TokenUsage, describe_call, read_codex_log
from dashboard_template import render_dashboard_document, source_tabs
from codex_grade import (attribute_tools, codex_steps, grade_codex_session,
                         junk_target)
from dashboard_model import (claude_dashboard_model, codex_dashboard_model,
                             render_dashboard_overview)
from session_grade import grade_for, grade_weight


FIXTURES = Path(__file__).parent / "fixtures" / "codex"
ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location("codex_usage", ROOT / "codex-usage.py")
codex_usage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = codex_usage
SPEC.loader.exec_module(codex_usage)
UNIFIED_SPEC = importlib.util.spec_from_file_location("usage_dashboard", ROOT / "usage-dashboard.py")
usage_dashboard = importlib.util.module_from_spec(UNIFIED_SPEC)
sys.modules[UNIFIED_SPEC.name] = usage_dashboard
UNIFIED_SPEC.loader.exec_module(usage_dashboard)


class CodexLogReaderTests(unittest.TestCase):
    def test_extracts_metadata_intervals_tools_and_observed_quotas(self):
        log = read_codex_log(FIXTURES / "long-with-tools.jsonl")

        self.assertEqual(log.metadata.session_id, "session-long")
        self.assertEqual(log.metadata.cwd, "/workspace/long-project")
        self.assertEqual(log.metadata.model, "gpt-5.6-terra")
        self.assertEqual(log.metadata.model_provider, "openai")
        self.assertEqual([tool.name for tool in log.tools], ["exec", "web.run", "exec"])
        self.assertEqual(len(log.quotas), 3)
        self.assertEqual(log.quotas[-1].primary.window_minutes, 300)
        self.assertEqual(log.quotas[-1].secondary.window_minutes, 10080)
        # The first snapshot is the first request's own consumption, then two
        # deltas. Their sum is the journal's last cumulative value, 270.
        self.assertEqual(log.intervals[0].usage, TokenUsage(100, 20, 10, 30, 5, 130))
        self.assertEqual(log.intervals[1].usage, TokenUsage(60, 30, 0, 20, 3, 80))
        self.assertEqual(log.intervals[2].usage, TokenUsage(40, 20, 10, 20, 3, 60))
        self.assertEqual(log.warnings, [])

    def test_unwraps_the_harness_call_and_bounds_what_it_describes(self):
        log = read_codex_log(FIXTURES / "long-with-tools.jsonl")

        # The journal names all three `exec`/`web.run`; the tool that ran is inside.
        self.assertEqual([tool.tool for tool in log.tools],
                         ["exec_command", "web.run", "exec"])
        self.assertEqual([tool.detail for tool in log.tools], [
            "rg -n pattern src",       # unwrapped from the harness program
            "an invented search",      # a plain JSON argument object
            "const total = 1 + 1;",    # no nested call: the program's first line
        ])
        self.assertEqual([tool.label for tool in log.tools], [
            "exec_command(rg -n pattern src)",
            "web.run(an invented search)",
            "exec(const total = 1 + 1;)",
        ])

    def test_a_description_never_exceeds_its_documented_bound(self):
        long_command = "rg " + "x" * 400
        tool, detail = describe_call(
            "exec",
            'const r = await tools.exec_command({"cmd": ["%s"]});' % long_command,
        )

        self.assertEqual(tool, "exec_command")
        self.assertEqual(len(detail), codex_log.TOOL_DETAIL_WIDTH)
        self.assertTrue(detail.endswith("\u2026"))
        self.assertNotIn("x" * 60, detail)
        # An unparsable argument object yields no description rather than a guess.
        self.assertEqual(describe_call("exec", "await tools.thing({not json);"),
                         ("thing", ""))
        self.assertEqual(describe_call("web.run", None), ("web.run", ""))
        # A brace inside an escaped quote must not end the argument object early.
        self.assertEqual(
            describe_call("exec", r'await tools.exec_command({"cmd": ["rg \"a}b\""]});'),
            ("exec_command", 'rg "a}b"'))

    def test_the_reader_keeps_intervals_and_calls_as_two_separate_series(self):
        # The reader states no link between a delta and a call. Attribution is a
        # layer above it (``codex_grade``), where the rule can be named and its
        # totals checked -- it is never baked into the evidence.
        log = read_codex_log(FIXTURES / "long-with-tools.jsonl")

        self.assertEqual([tool.timestamp for tool in log.tools], [
            "2026-08-30T10:01:30Z", "2026-08-30T10:02:10Z", "2026-08-30T10:02:20Z",
        ])
        self.assertEqual([(interval.started_at, interval.ended_at) for interval in log.intervals], [
            ("2026-08-30T10:00:00Z", "2026-08-30T10:01:00Z"),
            ("2026-08-30T10:01:00Z", "2026-08-30T10:02:00Z"),
            ("2026-08-30T10:02:00Z", "2026-08-30T10:03:00Z"),
        ])
        # Nothing of the journal is dropped: the intervals sum to its own last
        # cumulative total, so the session figure is measured end to end.
        self.assertEqual(sum(interval.usage.total_tokens for interval in log.intervals), 270)
        self.assertFalse(hasattr(log.intervals[0], "tool"))

    def test_handles_short_and_archived_fixtures(self):
        short = read_codex_log(FIXTURES / "short.jsonl")
        archived = read_codex_log(FIXTURES / "archived.jsonl")

        self.assertEqual(short.metadata.model, "gpt-5.6-luna")
        # A session with a single snapshot consumed what that snapshot states; it
        # is one interval running from the session start, never an empty reading.
        self.assertEqual(len(short.intervals), 1)
        self.assertEqual(short.intervals[0].started_at, "2026-08-31T09:00:00Z")
        self.assertEqual(short.intervals[0].usage, TokenUsage(120, 20, 0, 30, 10, 150))
        self.assertEqual(archived.metadata.session_id, "session-archived")
        self.assertEqual(archived.intervals[0].usage, TokenUsage(80, 10, 0, 20, 4, 100))
        self.assertEqual(archived.intervals[1].usage, TokenUsage(15, 5, 0, 6, 2, 21))

    def test_an_all_zero_counter_reports_no_consumption_rather_than_an_interval(self):
        log = read_codex_log(FIXTURES / "zero-counters.jsonl")

        self.assertEqual(log.complete_counters, 1)
        self.assertEqual(log.intervals, [])
        self.assertEqual(log.warnings, [])

    def test_ignores_and_reports_incomplete_duplicate_and_decreasing_counters(self):
        log = read_codex_log(FIXTURES / "invalid-counters.jsonl")

        # The first valid snapshot is one interval; the final one is compared
        # against it. Invalid snapshots never become an interval reference.
        self.assertEqual(len(log.intervals), 2)
        self.assertEqual(log.intervals[0].usage, TokenUsage(100, 10, 5, 30, 4, 130))
        self.assertEqual(log.intervals[1].usage, TokenUsage(60, 10, 5, 15, 2, 75))
        self.assertEqual(len(log.warnings), 3)
        self.assertIn("incomplete cumulative token counter", log.warnings[0])
        self.assertIn("duplicate cumulative token counter", log.warnings[1])
        self.assertIn("decreasing cumulative token counter", log.warnings[2])

    def test_tolerates_a_partially_written_final_record(self):
        log = read_codex_log(FIXTURES / "partially-written.jsonl")

        self.assertEqual(log.metadata.session_id, "session-partial")
        self.assertEqual(log.intervals[0].usage, TokenUsage(50, 5, 0, 10, 1, 60))
        self.assertEqual(log.intervals[1].usage, TokenUsage(20, 3, 0, 6, 1, 26))
        self.assertIn("incomplete JSONL record", log.warnings[-1])


class CodexUsageTests(unittest.TestCase):
    def test_combined_dashboard_tabs_default_to_claude_and_keep_window(self):
        tabs = source_tabs("claude", 30)
        document = render_dashboard_document(
            ROOT / "cc-usage.py", "Usage", f"<main>overview{tabs}</main>",
        )

        self.assertIn('class="source-tabs"', document)
        self.assertIn('/?source=claude&amp;days=30" aria-current="page"', document)
        self.assertIn('/?source=codex&amp;days=30"', document)
        self.assertIn('class="source-tabs__loader"', document)
        self.assertIn('classList.add("is-loading")', document)
        self.assertNotIn('<nav class="source-tabs"', render_dashboard_document(
            ROOT / "cc-usage.py", "Usage", "<main>overview</main>"))

    def test_combined_gateway_selects_and_forwards_one_source(self):
        output = Path("/tmp/dashboard.html")
        claude = usage_dashboard._dashboard_command(
            "claude", output, 30, "/", {"q": ["project"], "sort": ["cost-desc"]},
        )
        codex = usage_dashboard._dashboard_command(
            "codex", output, 30, "/session", {"id": ["session-1"], "sort": ["tokens-desc"]},
        )

        self.assertIn("--dashboard-source", claude)
        self.assertEqual(claude[claude.index("--dashboard-source") + 1], "claude")
        self.assertIn("--filter-sessions", claude)
        self.assertEqual(codex[codex.index("--dashboard-source") + 1], "codex")
        self.assertIn("--focus", codex)

    def test_dashboard_defaults_to_five_sessions(self):
        args = codex_usage.parse_args([])
        self.assertEqual(args.days, 30)
        self.assertEqual(args.sessions_max, 5)

    def test_common_dashboard_model_normalizes_claude_and_codex_overviews(self):
        claude = claude_dashboard_model({
            "totals": {"cost_usd": 1.25, "sessions": 1, "cache_read": 1200, "output": 80},
            "projects": [{"name": "alpha", "cost_usd": 1.25, "sessions": 1}],
            "daily": [{"date": "2026-08-30", "cost_usd": 1.25, "sessions": 1}],
            "sessions": [{"id": "claude-session", "short": "claude", "project": "alpha",
                          "start": "2026-08-30T10:00:00Z", "turns": 2, "prompts": 1,
                          "compactions": 0, "cost": 1.25, "grade": "A", "exact": True}],
            "generated": "2026-08-30T10:05:00Z",
        })
        codex = codex_dashboard_model({
            "totals": {"sessions": 1, "tokens": {"total_tokens": 161,
                       "cached_input_tokens": 55, "output_tokens": 46}},
            "projects": [{"project": "alpha", "sessions": 1,
                          "tokens": {"total_tokens": 161}}],
            "daily": [{"date": "2026-08-30", "tokens": {"total_tokens": 161}}],
            "sessions": [{"id": "codex-session", "group": "alpha",
                          "started_at": "2026-08-30T10:00:00Z", "status": "partial",
                          "tokens": {"total_tokens": 161},
                          "git": {"status": "landed", "commits": []}}],
            "git_sessions": [{"id": "codex-session", "group": "alpha",
                              "started_at": "2026-08-30T10:00:00Z", "status": "ok",
                              "tokens": {"total_tokens": 161},
                              "git": {"status": "landed", "commits": []}}],
            "git": {"repos": [{"name": "alpha", "mainline": "main", "sessions": 1,
                                  "outcomes": {"landed": {"sessions": 1, "tokens": 161},
                                               "reverted": {"sessions": 0, "tokens": 0},
                                               "unmerged": {"sessions": 0, "tokens": 0},
                                               "no-commit": {"sessions": 0, "tokens": 0}}}],
                    "outcomes": {"landed": {"sessions": 1, "tokens": 161},
                                 "reverted": {"sessions": 0, "tokens": 0},
                                 "unmerged": {"sessions": 0, "tokens": 0},
                                 "no-commit": {"sessions": 0, "tokens": 0}},
                    "session_outcomes": {"codex-session": {"commits": []}},
                    "outside_git": 0, "skipped_sessions": 0, "unmatched_commits": 0},
        })

        for view in (claude, codex):
            self.assertEqual(set(view), set(claude))
            self.assertEqual(len(view["stats"]), 5)
            self.assertEqual(len(view["daily"]), 1)
            self.assertEqual(len(view["projects"]), 1)
            self.assertEqual(len(view["sessions"]), 1)
            self.assertIn(view["title"], render_dashboard_overview(view))
        self.assertEqual([label for label, _value, _lead in codex["stats"]], [
            "Total tokens", "Sessions", "Projects", "Cache-read tokens", "Tokens produced",
        ])
        self.assertIn("What it left in git", render_dashboard_overview(codex))
        self.assertIn("Tokens per landed commit", render_dashboard_overview(codex))
        rendered_claude = render_dashboard_overview(claude)
        rendered_codex = render_dashboard_overview(codex)
        self.assertIn('chip grade', rendered_claude)
        self.assertIn('>Exact</span>', rendered_claude)
        self.assertIn('>—</span>', rendered_codex)
        self.assertIn('>Partiel</span>', rendered_codex)

    def test_groups_fixture_deltas_by_project_day_and_session_without_false_zero(self):
        rollouts = [
            codex_usage.RolloutFile(FIXTURES / "long-with-tools.jsonl", "active", date(2026, 8, 30)),
            codex_usage.RolloutFile(FIXTURES / "archived.jsonl", "archived", date(2026, 8, 1)),
            codex_usage.RolloutFile(FIXTURES / "short.jsonl", "active", date(2026, 8, 31)),
            codex_usage.RolloutFile(FIXTURES / "zero-counters.jsonl", "active", date(2026, 8, 27)),
            codex_usage.RolloutFile(FIXTURES / "without-counters.jsonl", "active", date(2026, 8, 31)),
        ]
        sessions = codex_usage.analyze_rollouts(rollouts)
        payload = codex_usage.payload_for(sessions, date(2026, 8, 1), date(2026, 8, 31))

        self.assertEqual(payload["totals"]["sessions"], 5)
        self.assertEqual(payload["totals"]["sessions_with_tokens"], 3)
        self.assertEqual(payload["totals"]["sessions_without_exploitable_counters"], 2)
        # Each session totals its journal's own last cumulative snapshot: 270,
        # 121 and 150. Nothing is left behind a baseline.
        self.assertEqual(payload["totals"]["tokens"], {
            "input_tokens": 415,
            "cached_input_tokens": 105,
            "cache_write_input_tokens": 20,
            "output_tokens": 126,
            "reasoning_output_tokens": 27,
            "total_tokens": 541,
        })
        self.assertEqual([row["date"] for row in payload["daily"]],
                         ["2026-08-01", "2026-08-30", "2026-08-31"])
        short = next(row for row in payload["sessions"] if row["thread_id"] == "session-short")
        self.assertEqual(short["status"], "ok")
        self.assertEqual(short["tokens"]["total_tokens"], 150)
        zero = next(row for row in payload["sessions"] if row["thread_id"] == "session-zero")
        self.assertEqual(zero["status"], "no_exploitable_intervals")
        self.assertIsNone(zero["tokens"])
        no_counters = next(row for row in payload["sessions"] if row["thread_id"] == "session-no-counters")
        self.assertEqual(no_counters["status"], "no_complete_counters")
        self.assertIsNone(no_counters["tokens"])
        long = next(row for row in payload["sessions"] if row["thread_id"] == "session-long")
        self.assertEqual([tool["tool"] for tool in long["tools"]],
                         ["exec_command", "web.run", "exec"])
        self.assertEqual([tool["label"] for tool in long["tools"]], [
            "exec_command(rg -n pattern src)", "web.run(an invented search)",
            "exec(const total = 1 + 1;)",
        ])
        self.assertEqual(long["last_quota"]["primary"]["window_minutes"], 300)
        json.dumps(payload)
        with redirect_stdout(io.StringIO()) as output:
            codex_usage.print_summary(payload, sessions=4, daily=True, tools=True)
        terminal = output.getvalue()
        self.assertIn("Input uncached 415", terminal)
        self.assertIn("Total 541", terminal)
        self.assertIn("Last observed quota", terminal)
        self.assertIn("Tool calls", terminal)

    def test_dashboard_is_standalone_transcript_free_and_links_to_session_details(self):
        rollouts = [
            codex_usage.RolloutFile(FIXTURES / "long-with-tools.jsonl", "active", date(2026, 8, 30)),
            codex_usage.RolloutFile(FIXTURES / "archived.jsonl", "archived", date(2026, 8, 1)),
        ]
        payload = codex_usage.payload_for(
            codex_usage.analyze_rollouts(rollouts), date(2026, 8, 1), date(2026, 8, 31)
        )
        view = codex_usage.dashboard_view(payload, project=None, limit=2)
        document = codex_usage.dashboard_document(view)

        self.assertIn("<!doctype html>", document)
        self.assertIn("Day by day", document)
        self.assertIn("What filled the context", document)
        self.assertIn("Tokens attributed", document)
        self.assertIn("Step by step", document)
        self.assertIn("What the step ran", document)
        self.assertIn('href="#s-', document)
        # The tool a harness wrapper invoked, and the bounded description of it.
        self.assertIn("exec_command", document)
        self.assertIn("rg -n pattern src", document)
        self.assertNotIn("<script", document)
        self.assertNotIn("<link", document)
        self.assertNotIn("response_item", document)
        # A description is bounded; a tool result is never read at all.
        self.assertNotIn("invented-tool-output-never-rendered", document)

        served = codex_usage.dashboard_view(
            payload, project="long-project", sort="recent-desc", limit=1, served=True
        )
        served["dashboard_source"] = "codex"
        served_document = codex_usage.dashboard_document(served)
        self.assertIn('name="q"', served_document)
        self.assertIn('placeholder="project or id"', served_document)
        self.assertIn('<span>How many</span>', served_document)
        self.assertIn('<span class="eyebrow">Window</span>', served_document)
        self.assertIn('name="by" value="repo"', served_document)
        self.assertIn('name="source" value="codex"', served_document)

        filtered = codex_usage.dashboard_view(
            payload, project="long-with-tools", sort="tokens-desc", limit=20
        )
        self.assertEqual([session["id"] for session in filtered["sessions"]], ["long-with-tools"])

        focus = codex_usage.dashboard_view(payload, project=None, focus=payload["sessions"][0]["id"])
        self.assertIsNotNone(focus["focus"])
        self.assertNotIn('<div class="rows">', codex_usage.dashboard_document(focus))

    def test_filters_rollouts_before_reading_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / "sessions"
            archived = root / "archived"
            active.mkdir()
            archived.mkdir()
            shutil.copyfile(FIXTURES / "short.jsonl", active / "rollout-2026-08-20T09-00-00-old.jsonl")
            shutil.copyfile(FIXTURES / "long-with-tools.jsonl", archived / "rollout-2026-08-30T09-00-00-new.jsonl")

            rollouts = codex_usage.discover_rollouts(
                (("active", active), ("archived", archived)), since=date(2026, 8, 30)
            )

        self.assertEqual([(item.source, item.started_on) for item in rollouts], [
            ("archived", date(2026, 8, 30)),
        ])

    def test_collapses_a_worktree_to_its_primary_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            worktree = root / "worker"
            repository.mkdir()
            (repository / ".git").mkdir()
            worktree.mkdir()
            (worktree / ".git").write_text(
                f"gitdir: {repository / '.git' / 'worktrees' / 'worker'}\n", encoding="utf-8"
            )

            self.assertEqual(codex_usage.project_root(str(worktree)), str(repository))

    def test_git_correlation_keeps_token_totals_and_marks_landed_commits(self):
        session = codex_usage.CodexSession(
            rollout_id="rollout-a", thread_id="thread-a", source="active", project="/repo",
            cwd="/repo", started_at="2026-08-30T09:00:00+00:00",
            ended_at="2026-08-30T09:10:00+00:00", model=None, model_provider=None,
            status="ok", tokens=TokenUsage(10, 3, 0, 4, 1, 14), intervals=(),
            tools=(), last_quota=None, warnings=(),
        )
        commit_at = int(datetime(2026, 8, 30, 9, 5, tzinfo=timezone.utc).timestamp())
        history = {
            "mainline": "main",
            "commits": [{"sha": "1234567890abcdef", "ts": commit_at,
                         "landed": True, "reverted": False}],
        }
        with patch.object(codex_usage, "git_toplevel", return_value="/repo"), \
             patch.object(codex_usage, "_repo_history", return_value=history):
            git = codex_usage.git_outcomes([session])

        self.assertEqual(git["outcomes"]["landed"], {"sessions": 1, "tokens": 14})
        self.assertEqual(git["session_outcomes"]["rollout-a"], {
            "status": "landed",
            "commits": [{"sha": "12345678", "landed": True, "reverted": False}],
        })
        payload = codex_usage.payload_for([session], date(2026, 8, 30), date(2026, 8, 30), git=git)
        self.assertEqual(payload["totals"]["tokens"]["total_tokens"], 14)
        self.assertEqual(payload["sessions"][0]["git"]["status"], "landed")
        self.assertTrue(payload["projects"][0]["is_git_project"])

        conversation = codex_usage.CodexSession(
            rollout_id="rollout-chat", thread_id="thread-chat", source="active", project="/chat",
            cwd="/chat", started_at="2026-08-30T09:00:00+00:00",
            ended_at="2026-08-30T09:10:00+00:00", model=None, model_provider=None,
            status="ok", tokens=TokenUsage(1, 0, 0, 0, 0, 1), intervals=(),
            tools=(), last_quota=None, warnings=(),
        )
        filtered = codex_usage.payload_for(
            [session, conversation], date(2026, 8, 30), date(2026, 8, 30),
            git={**git, "session_outcomes": {**git["session_outcomes"],
                                               "rollout-chat": {"status": "outside_git", "commits": []}}},
        )
        self.assertEqual(
            {row["project"] for row in filtered["projects"] if row["is_git_project"]}, {"/repo"},
        )
        view = codex_usage.dashboard_view(filtered, project=None)
        self.assertEqual(view["totals"]["sessions"], 1)
        self.assertEqual(view["totals"]["tokens"]["total_tokens"], 14)
        self.assertEqual([row["project"] for row in view["projects"]], ["/repo"])
        self.assertEqual([row["id"] for row in view["sessions"]], ["rollout-a"])

    def test_no_git_does_not_invoke_git_correlation(self):
        with patch.object(codex_usage, "discover_rollouts", return_value=[]), \
             patch.object(codex_usage, "analyze_rollouts", return_value=[]), \
             patch.object(codex_usage, "git_outcomes") as correlate, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(codex_usage.main(["--since", "2026-08-30", "--no-git", "--json"]), 0)

        correlate.assert_not_called()
        self.assertIsNone(json.loads(output.getvalue())["git"])

    def test_payload_keeps_api_equivalent_absent_without_unambiguous_model_mapping(self):
        sessions = codex_usage.analyze_rollouts([
            codex_usage.RolloutFile(FIXTURES / "long-with-tools.jsonl", "active", date(2026, 8, 30)),
            codex_usage.RolloutFile(FIXTURES / "without-counters.jsonl", "active", date(2026, 8, 31)),
        ])
        payload = codex_usage.payload_for(sessions, date(2026, 8, 30), date(2026, 8, 31))

        self.assertNotIn("api_equivalent", payload)
        self.assertTrue(all("api_equivalent" not in session for session in payload["sessions"]))
        self.assertNotIn("api_equivalent", vars(codex_usage.parse_args(["--days", "1"])))

    def test_dashboard_server_is_loopback_only(self):
        with patch("http.server.ThreadingHTTPServer") as server, redirect_stdout(io.StringIO()):
            codex_usage.serve_dashboard(0, None, 7, None, "repo", True)

        self.assertEqual(server.call_args.args[0], ("127.0.0.1", 0))
        server.return_value.serve_forever.assert_called_once()


def _codex_session(steps, cwd="/tmp/nowhere") -> dict:
    """One session dict shaped like the payload, from (calls, tokens) steps."""
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


TOKEN_ZERO = {"input_tokens": 0, "cached_input_tokens": 0,
              "cache_write_input_tokens": 0, "output_tokens": 0,
              "reasoning_output_tokens": 0, "total_tokens": 0}


class CodexGradeTests(unittest.TestCase):
    def test_attribution_conserves_every_step_total(self):
        session = _codex_session([
            ([("read_file", "a.py")], {"total_tokens": 300, "input_tokens": 280}),
            ([("read_file", "b.py"), ("web__run", "a query")],
             {"total_tokens": 500, "input_tokens": 460}),
            ([], {"total_tokens": 200, "input_tokens": 190}),
        ])
        attributed = attribute_tools(codex_steps(session))

        self.assertEqual(sum(row["tokens"] for row in attributed), 1000)
        by_tool = {row["tool"]: row for row in attributed}
        # The two-call step splits equally; the callless step lands on its own row.
        self.assertEqual(by_tool["read_file"]["tokens"], 550)
        self.assertEqual(by_tool["read_file"]["count"], 2)
        self.assertEqual(by_tool["web__run"]["tokens"], 250)
        self.assertEqual(by_tool["(no tool call)"]["tokens"], 200)

    def test_a_truncated_call_target_is_not_counted_as_a_repeat(self):
        # Two different URLs share the first forty characters of one curl line;
        # the journal cut both at the same place, so they are not evidence.
        cut = "curl --fail --silent --show-error --loc\u2026"
        steps = [([("exec_command", cut)], {"total_tokens": 400_000,
                                            "input_tokens": 380_000})] * 6
        truncated = grade_codex_session(_codex_session(steps),
                                        codex_steps(_codex_session(steps)))
        whole = _codex_session([([("exec_command", "cargo test")],
                                 {"total_tokens": 400_000, "input_tokens": 380_000})] * 6)
        repeated = grade_codex_session(whole, codex_steps(whole))

        self.assertNotIn("duplicate-reads",
                         [finding["kind"] for finding in truncated["triage"]])
        self.assertIn("duplicate-reads",
                      [finding["kind"] for finding in repeated["triage"]])

    def test_a_junk_target_is_read_out_of_a_command_fragment(self):
        self.assertTrue(junk_target("rg -n pattern node_modules/left-pad"))
        self.assertTrue(junk_target("cat ./dist/bundle.js"))
        self.assertTrue(junk_target("open package-lock.json"))
        self.assertFalse(junk_target("rg -n pattern src"))
        self.assertFalse(junk_target(""))

    def test_a_clean_session_scores_nothing_and_grades_a(self):
        session = _codex_session([
            ([("read_file", f"file{index}.py")],
             {"total_tokens": 5_000, "input_tokens": 4_800,
              "cached_input_tokens": 4_700, "output_tokens": 100})
            for index in range(6)
        ])
        graded = grade_codex_session(session, codex_steps(session))

        self.assertEqual(graded["score"], 0.0)
        self.assertEqual(graded["grade"], "A")
        self.assertEqual([finding["weight"] for finding in graded["triage"]], [])

    def test_both_readers_weigh_a_share_on_the_same_bands(self):
        # The unit differs -- dollars there, observed tokens here -- but an equal
        # share weighs the same and lands in the same band.
        self.assertEqual(grade_weight("cache", 12.0, 4.0, 2.0),
                         grade_weight("cache", 12.0, 400_000, 200_000))
        self.assertEqual([grade_for(score) for score in (0, 2.9, 3, 7.9, 8, 14.9, 23.9, 24)],
                         ["A", "A", "B", "B", "C", "C", "D", "F"])

    def test_a_graded_session_carries_the_letter_into_payload_and_page(self):
        session = _codex_session([
            ([("exec_command", "cargo test")],
             {"total_tokens": 900_000, "input_tokens": 880_000,
              "output_tokens": 120_000})
            for _ in range(8)
        ])
        session.update(grade_codex_session(session, codex_steps(session)))
        view = codex_usage.codex_session_view(session, {"source": "codex", "days": 30,
                                                        "q": "", "by": "repo",
                                                        "sort": "tokens-desc",
                                                        "tsort": "", "tq": "", "max": 5},
                                              served=False)

        self.assertIn(session["grade"], "ABCDF")
        # The status badge first, then the letter: the Claude session page order.
        self.assertEqual([badge["label"] for badge in view["badges"]],
                         ["exact", session["grade"]])
        self.assertEqual([stat["label"] for stat in view["triage"]["stats"]],
                         ["Grade", "Score", "Avoidable", "Leads"])
        self.assertEqual([section["title"] for section in view["sections"]],
                         ["What filled the context", "Step by step", "Session record"])

    def test_the_session_record_folds_in_the_quota_and_the_skipped_records(self):
        session = _codex_session([([("exec_command", "cargo test")],
                                   {"total_tokens": 900, "input_tokens": 880})])
        session["warnings"] = ["line 12: duplicate cumulative token counter"]
        session["last_quota"] = {
            "observed_at": "2026-08-30T10:05:00Z", "plan_type": "plus",
            "primary": {"window_minutes": 10080, "used_percent": 49.0,
                        "resets_at": 1787000000},
            "secondary": None,
        }
        session.update(grade_codex_session(session, codex_steps(session)))
        record = next(section for section in codex_usage._codex_sections(
            session, codex_steps(session), {}, False, True)
            if section["title"] == "Session record")

        # A 7-day window is named for the duration it states, not for its slot.
        self.assertIn("Weekly window", record["html"])
        self.assertIn("49% used", record["html"])
        self.assertIn("Skipped records", record["html"])
        self.assertIn("line 12: duplicate cumulative token counter", record["html"])


if __name__ == "__main__":
    unittest.main()
