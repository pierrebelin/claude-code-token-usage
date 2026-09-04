from __future__ import annotations

import unittest

from dashboard_model import (claude_dashboard_model, codex_dashboard_model,
                             render_dashboard_overview)


ZERO_OUTCOME = {"sessions": 0, "tokens": 0}
LANDED_OUTCOME = {"landed": {"sessions": 1, "tokens": 161}, "reverted": dict(ZERO_OUTCOME),
                  "unmerged": dict(ZERO_OUTCOME), "no-commit": dict(ZERO_OUTCOME)}


class DashboardModelTests(unittest.TestCase):
    CLAUDE_PAYLOAD = {
        "totals": {"cost_usd": 1.25, "sessions": 1, "cache_read": 1200, "output": 80},
        "projects": [{"name": "alpha", "cost_usd": 1.25, "sessions": 1}],
        "daily": [{"date": "2026-08-30", "cost_usd": 1.25, "sessions": 1}],
        "sessions": [{"id": "claude-session", "short": "claude", "project": "alpha",
                      "start": "2026-08-30T10:00:00Z", "turns": 2, "prompts": 1,
                      "compactions": 0, "cost": 1.25, "grade": "A", "exact": True}],
        "generated": "2026-08-30T10:05:00Z",
    }
    CODEX_PAYLOAD = {
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
                           "outcomes": LANDED_OUTCOME}],
                "outcomes": LANDED_OUTCOME,
                "session_outcomes": {"codex-session": {"commits": []}},
                "outside_git": 0, "skipped_sessions": 0, "unmatched_commits": 0},
    }
    CODEX_STATS = ["Total tokens", "Sessions", "Projects", "Cache-read tokens",
                   "Tokens produced"]

    def setUp(self):
        self.claude = claude_dashboard_model(self.CLAUDE_PAYLOAD)
        self.codex = codex_dashboard_model(self.CODEX_PAYLOAD)

    def test_both_sources_normalize_into_one_model_of_the_same_shape(self):
        self.assertEqual(set(self.codex), set(self.claude))
        for source, view in (("claude", self.claude), ("codex", self.codex)):
            with self.subTest(source=source):
                self.assertEqual(len(view["stats"]), 5)
                self.assertEqual(len(view["daily"]), 1)
                self.assertEqual(len(view["projects"]), 1)
                self.assertEqual(len(view["sessions"]), 1)
                self.assertIn(view["title"], render_dashboard_overview(view))

    def test_a_codex_overview_counts_tokens_where_the_claude_one_counts_dollars(self):
        self.assertEqual([label for label, _value, _lead in self.codex["stats"]],
                         self.CODEX_STATS)

    def test_a_codex_overview_states_what_the_sessions_left_in_git(self):
        rendered = render_dashboard_overview(self.codex)

        self.assertIn("What it left in git", rendered)
        self.assertIn("Tokens per landed commit", rendered)

    def test_a_claude_session_row_carries_its_grade_and_its_exact_cost(self):
        rendered = render_dashboard_overview(self.claude)

        self.assertIn("chip grade", rendered)
        self.assertIn(">Exact</span>", rendered)

    def test_a_codex_session_row_names_a_missing_grade_and_a_partial_reading(self):
        rendered = render_dashboard_overview(self.codex)

        self.assertIn(">—</span>", rendered)
        self.assertIn(">Partiel</span>", rendered)


if __name__ == "__main__":
    unittest.main()
