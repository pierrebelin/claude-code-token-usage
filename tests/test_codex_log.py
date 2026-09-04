from __future__ import annotations

import unittest
from pathlib import Path

import codex_log
from codex_log import TokenUsage, describe_call, read_codex_log


FIXTURES = Path(__file__).parent / "fixtures" / "codex"


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

    def test_keeps_intervals_and_calls_as_two_series_summing_to_the_last_counter(self):
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
        self.assertEqual(sum(interval.usage.total_tokens for interval in log.intervals), 270)
        self.assertFalse(hasattr(log.intervals[0], "tool"))

    def test_a_single_snapshot_reads_as_one_interval_from_the_session_start(self):
        short = read_codex_log(FIXTURES / "short.jsonl")

        self.assertEqual(short.metadata.model, "gpt-5.6-luna")
        self.assertEqual(len(short.intervals), 1)
        self.assertEqual(short.intervals[0].started_at, "2026-08-31T09:00:00Z")
        self.assertEqual(short.intervals[0].usage, TokenUsage(120, 20, 0, 30, 10, 150))

    def test_an_archived_journal_reads_like_an_active_one(self):
        archived = read_codex_log(FIXTURES / "archived.jsonl")

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


class CodexCallDescriptionTests(unittest.TestCase):
    LONG_COMMAND = "rg " + "x" * 400

    def test_unwraps_the_harness_call_and_names_the_tool_that_ran(self):
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
        tool, detail = describe_call(
            "exec",
            'const r = await tools.exec_command({"cmd": ["%s"]});' % self.LONG_COMMAND,
        )

        self.assertEqual(tool, "exec_command")
        self.assertEqual(len(detail), codex_log.TOOL_DETAIL_WIDTH)
        self.assertTrue(detail.endswith("…"))
        self.assertNotIn("x" * 60, detail)

    def test_an_unreadable_argument_object_yields_no_description_rather_than_a_guess(self):
        self.assertEqual(describe_call("exec", "await tools.thing({not json);"),
                         ("thing", ""))
        self.assertEqual(describe_call("web.run", None), ("web.run", ""))

    def test_a_brace_inside_an_escaped_quote_does_not_end_the_argument_object(self):
        self.assertEqual(
            describe_call("exec", r'await tools.exec_command({"cmd": ["rg \"a}b\""]});'),
            ("exec_command", 'rg "a}b"'))


if __name__ == "__main__":
    unittest.main()
