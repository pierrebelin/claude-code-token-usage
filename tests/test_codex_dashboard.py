from __future__ import annotations

import unittest
from datetime import date

from codex_grade import codex_steps, grade_codex_session

from tests.codex_journal import codex_session
from tests.collectors import FIXTURES, codex_usage


DASHBOARD_STATE = {"source": "codex", "days": 30, "q": "", "by": "repo",
                   "sort": "tokens-desc", "tsort": "", "tq": "", "max": 5}


class CodexDashboardTests(unittest.TestCase):
    ROLLOUTS = (("long-with-tools.jsonl", "active", date(2026, 8, 30)),
                ("archived.jsonl", "archived", date(2026, 8, 1)))
    SECTION_TITLES = ("Day by day", "What filled the context", "Tokens attributed",
                      "Step by step", "What the step ran")
    # A tool description is bounded; a tool result is never read at all.
    NEVER_RENDERED = ("<script", "<link", "response_item",
                      "invented-tool-output-never-rendered")

    def setUp(self):
        rollouts = [codex_usage.RolloutFile(FIXTURES / name, source, started_on)
                    for name, source, started_on in self.ROLLOUTS]
        self.payload = codex_usage.payload_for(
            codex_usage.analyze_rollouts(rollouts), date(2026, 8, 1), date(2026, 8, 31))

    def _document(self, **overrides) -> str:
        view = codex_usage.dashboard_view(self.payload, **overrides)
        return codex_usage.dashboard_document(view)

    def test_the_document_stands_alone_and_names_every_section_a_reader_gets(self):
        document = self._document(project=None, limit=2)

        self.assertIn("<!doctype html>", document)
        for title in self.SECTION_TITLES:
            with self.subTest(section=title):
                self.assertIn(title, document)
        self.assertIn('href="#s-', document)

    def test_the_document_carries_the_bounded_call_target_and_no_transcript(self):
        document = self._document(project=None, limit=2)

        self.assertIn("exec_command", document)
        self.assertIn("rg -n pattern src", document)
        for fragment in self.NEVER_RENDERED:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, document)

    def test_a_served_document_offers_the_controls_the_gateway_forwards(self):
        view = codex_usage.dashboard_view(self.payload, project="long-project",
                                          sort="recent-desc", limit=1, served=True)
        view["dashboard_source"] = "codex"
        document = codex_usage.dashboard_document(view)

        self.assertIn('name="q"', document)
        self.assertIn('placeholder="project or id"', document)
        self.assertIn("<span>How many</span>", document)
        self.assertIn('<span class="eyebrow">Window</span>', document)
        self.assertIn('name="by" value="repo"', document)
        self.assertIn('name="source" value="codex"', document)

    def test_a_filter_keeps_only_the_sessions_whose_project_matches(self):
        view = codex_usage.dashboard_view(self.payload, project="long-with-tools",
                                          sort="tokens-desc", limit=20)

        self.assertEqual([session["id"] for session in view["sessions"]],
                         ["long-with-tools"])

    def test_a_focused_session_replaces_the_list_instead_of_appending_to_it(self):
        view = codex_usage.dashboard_view(self.payload, project=None,
                                          focus=self.payload["sessions"][0]["id"])

        self.assertIsNotNone(view["focus"])
        self.assertNotIn('<div class="rows">', codex_usage.dashboard_document(view))


class CodexSessionPageTests(unittest.TestCase):
    HEAVY = {"total_tokens": 900_000, "input_tokens": 880_000, "output_tokens": 120_000}
    QUOTA = {"observed_at": "2026-08-30T10:05:00Z", "plan_type": "plus",
             "primary": {"window_minutes": 10080, "used_percent": 49.0,
                         "resets_at": 1787000000},
             "secondary": None}
    SKIPPED_RECORD = "line 12: duplicate cumulative token counter"

    def _graded(self, session: dict) -> dict:
        session.update(grade_codex_session(session, codex_steps(session)))
        return session

    def test_the_page_carries_the_status_badge_then_the_grade_letter(self):
        session = self._graded(codex_session(
            [([("exec_command", "cargo test")], dict(self.HEAVY))] * 8))
        view = codex_usage.codex_session_view(session, DASHBOARD_STATE, served=False)

        self.assertIn(session["grade"], "ABCDF")
        # The status badge first, then the letter: the Claude session page order.
        self.assertEqual([badge["label"] for badge in view["badges"]],
                         ["exact", session["grade"]])

    def test_the_page_states_the_grade_its_score_and_what_it_could_have_avoided(self):
        session = self._graded(codex_session(
            [([("exec_command", "cargo test")], dict(self.HEAVY))] * 8))
        view = codex_usage.codex_session_view(session, DASHBOARD_STATE, served=False)

        self.assertEqual([stat["label"] for stat in view["triage"]["stats"]],
                         ["Grade", "Score", "Avoidable", "Leads"])
        self.assertEqual([section["title"] for section in view["sections"]],
                         ["What filled the context", "Step by step", "Session record"])

    def test_the_session_record_folds_in_the_quota_and_the_skipped_records(self):
        session = codex_session([([("exec_command", "cargo test")],
                                  {"total_tokens": 900, "input_tokens": 880})])
        session["warnings"] = [self.SKIPPED_RECORD]
        session["last_quota"] = self.QUOTA
        view = codex_usage.codex_session_view(self._graded(session), DASHBOARD_STATE,
                                              served=False)
        record = next(section for section in view["sections"]
                      if section["title"] == "Session record")

        # A 7-day window is named for the duration it states, not for its slot.
        self.assertIn("Weekly window", record["html"])
        self.assertIn("49% used", record["html"])
        self.assertIn("Skipped records", record["html"])
        self.assertIn(self.SKIPPED_RECORD, record["html"])


if __name__ == "__main__":
    unittest.main()
