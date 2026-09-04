from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tests.collectors import CLAUDE_FIXTURES, cc_usage


PROJECTS = CLAUDE_FIXTURES / "projects"
ALPHA = PROJECTS / "-workspace-alpha" / "with-subagent-and-cost-state.jsonl"
BETA = PROJECTS / "-workspace-beta" / "without-cost-state.jsonl"


class ClaudePriceTableTests(unittest.TestCase):
    PRICES = json.loads((CLAUDE_FIXTURES / "litellm-prices.json").read_text())
    SONNET_INPUT = 4e-06
    # One request under the premium threshold, and one above it.
    SHORT_USAGE = {"input_tokens": 100, "output_tokens": 50,
                   "cache_read_input_tokens": 1000,
                   "cache_creation_input_tokens": 200}
    LONG_USAGE = {"input_tokens": 250_000, "output_tokens": 10}
    RESOLVED = (("claude-opus-5", "claude-opus-5"),          # named outright
                ("claude-opus-5[1m]", "claude-opus-5"),      # a context variant
                ("claude-opus-5-20990101", "claude-opus-5"))  # an unknown release

    def setUp(self):
        self.table = cc_usage.build_rate_table(self.PRICES)
        self.opus = self.table["claude-opus-5"]

    def test_only_a_model_priced_on_both_sides_gets_a_rate(self):
        self.assertEqual(set(self.table), {"claude-opus-5", "claude-sonnet-5"})

    def test_a_model_without_published_cache_rates_derives_them_from_its_input(self):
        sonnet = self.table["claude-sonnet-5"].base

        self.assertEqual(sonnet.cache_write, self.SONNET_INPUT * 1.25)
        self.assertEqual(sonnet.cache_write_1h,
                         self.SONNET_INPUT * cc_usage.LONG_CACHE_MULTIPLIER)
        self.assertEqual(sonnet.cache_read, self.SONNET_INPUT * 0.1)

    def test_a_model_without_a_premium_tier_keeps_its_standard_rate_at_any_size(self):
        sonnet = self.table["claude-sonnet-5"]

        self.assertIsNone(sonnet.long)
        self.assertIs(cc_usage.usage_tier(self.LONG_USAGE, sonnet), sonnet.base)

    def test_a_prompt_past_the_threshold_is_billed_on_the_premium_tier(self):
        self.assertIs(cc_usage.usage_tier(self.SHORT_USAGE, self.opus), self.opus.base)
        self.assertIs(cc_usage.usage_tier(self.LONG_USAGE, self.opus), self.opus.long)

    def test_the_input_cost_sums_each_part_at_the_rate_its_tier_states(self):
        self.assertAlmostEqual(cc_usage.input_cost(self.SHORT_USAGE, self.opus),
                               100 * 1e-05 + 200 * 1.25e-05 + 1000 * 1e-06)

    def test_output_is_billed_at_the_tier_the_prompt_size_selected(self):
        self.assertAlmostEqual(cc_usage.output_cost(self.LONG_USAGE, self.opus),
                               10 * 7.5e-05)

    def test_an_unpriced_model_costs_nothing_rather_than_a_guessed_amount(self):
        self.assertEqual(cc_usage.input_cost(self.SHORT_USAGE, None), 0.0)
        self.assertEqual(cc_usage.output_cost(self.SHORT_USAGE, None), 0.0)

    def test_a_model_name_resolves_to_the_price_list_of_its_family(self):
        for model, expected in self.RESOLVED:
            with self.subTest(model=model):
                self.assertIs(cc_usage.resolve_rates(model, self.table, {}),
                              self.table[expected])

    def test_a_model_of_no_known_family_resolves_to_no_rate_at_all(self):
        self.assertIsNone(cc_usage.resolve_rates("some-other-model", self.table, {}))


class ClaudeTranscriptTests(unittest.TestCase):
    WINDOW_START = datetime(2026, 8, 31, tzinfo=timezone.utc)
    REPORTED_COST = 1.2345
    # msg-1 is written twice, and one line is cut mid-record.
    # Session transcripts first, then the subagent files one level down.
    BILLED_MODELS = ["claude-opus-5", "claude-opus-5", "claude-opus-5",
                     "claude-sonnet-5", "claude-haiku-4-5-20251001"]

    def setUp(self):
        patcher = patch.object(cc_usage, "PROJECTS_DIR", PROJECTS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_counter_claude_code_writes_is_read_once_per_transcript(self):
        states = cc_usage.read_cost_states()

        self.assertEqual([path.rsplit("/", 1)[-1] for path in states],
                         ["with-subagent-and-cost-state.jsonl"])
        self.assertEqual(next(iter(states.values()))["totalCostUSD"], self.REPORTED_COST)

    def test_a_billed_message_is_read_once_however_often_the_journal_repeats_it(self):
        messages = list(cc_usage.iter_messages(None))

        self.assertEqual([message["model"] for message in messages], self.BILLED_MODELS)

    def test_the_window_drops_the_messages_written_before_it_starts(self):
        messages = list(cc_usage.iter_messages(self.WINDOW_START))

        self.assertEqual([message["session"] for message in messages], ["session-beta"])

    def test_a_skipped_transcript_takes_its_subagents_with_it(self):
        # A cost-state counter already covers the subagents of its session, so
        # counting their files again would bill the same tokens twice.
        messages = list(cc_usage.iter_messages(None, skip_paths={str(ALPHA)}))

        self.assertEqual([message["session"] for message in messages], ["session-beta"])

    def test_a_session_reads_as_its_billed_turns_its_prompts_and_its_compactions(self):
        turns, _results, prompts, compactions, meta = cc_usage.read_session(ALPHA)

        self.assertEqual([turn.mid for turn in turns],
                         ["msg-1", "msg-2", "msg-3", "msg-agent-1"])
        self.assertEqual(prompts, 1)
        self.assertEqual(compactions, 1)
        self.assertEqual(meta["compact_mids"], {"msg-3"})
        self.assertEqual(meta["cwd"], "/workspace/alpha")
        self.assertEqual(meta["branch"], "main")
        self.assertEqual(meta["commands"], {"review"})
        self.assertEqual(meta["reported"]["totalCostUSD"], self.REPORTED_COST)

    def test_a_turns_context_is_everything_the_request_carried_in(self):
        turns, _results, _prompts, _compactions, _meta = cc_usage.read_session(ALPHA)

        self.assertEqual(turns[0].ctx, 100 + 1000 + 200)
        self.assertEqual(turns[0].output, 50)

    def test_a_subagent_is_named_by_the_call_that_launched_it(self):
        _turns, _results, _prompts, _compactions, meta = cc_usage.read_session(ALPHA)
        agent = meta["agents"][0]

        self.assertEqual(agent["label"], "map the repo")
        self.assertEqual(agent["type"], "explorer")
        self.assertEqual(agent["model"], "claude-haiku-4-5-20251001")

    def test_reading_a_session_alone_leaves_its_subagents_out(self):
        turns, _results, _prompts, _compactions, meta = cc_usage.read_session(
            ALPHA, with_subagents=False)

        self.assertEqual([turn.mid for turn in turns], ["msg-1", "msg-2", "msg-3"])
        self.assertEqual(meta["agents"], [])

    def test_a_tool_result_is_measured_by_the_largest_body_the_journal_carries(self):
        _turns, results, _prompts, _compactions, _meta = cc_usage.read_session(ALPHA)

        self.assertEqual(results["tu-1"],
                         len(json.dumps({"filePath": "/workspace/alpha/main.py",
                                         "numLines": 1})))

    def test_a_transcript_without_a_counter_reports_no_cost_of_its_own(self):
        _turns, _results, _prompts, _compactions, meta = cc_usage.read_session(BETA)

        self.assertIsNone(meta["reported"])


if __name__ == "__main__":
    unittest.main()
