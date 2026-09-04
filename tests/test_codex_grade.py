from __future__ import annotations

import unittest

from codex_grade import (attribute_tools, codex_steps, grade_codex_session,
                         junk_target)

from tests.codex_journal import codex_session


class CodexAttributionTests(unittest.TestCase):
    STEPS = (
        ([("read_file", "a.py")], {"total_tokens": 300, "input_tokens": 280}),
        ([("read_file", "b.py"), ("web__run", "a query")],
         {"total_tokens": 500, "input_tokens": 460}),
        ([], {"total_tokens": 200, "input_tokens": 190}),
    )

    def setUp(self):
        self.attributed = {row["tool"]: row
                           for row in attribute_tools(codex_steps(codex_session(list(self.STEPS))))}

    def test_attribution_conserves_every_step_total(self):
        self.assertEqual(sum(row["tokens"] for row in self.attributed.values()), 1000)

    def test_a_step_splits_its_tokens_equally_across_the_calls_it_holds(self):
        self.assertEqual(self.attributed["read_file"]["tokens"], 550)
        self.assertEqual(self.attributed["read_file"]["count"], 2)
        self.assertEqual(self.attributed["web__run"]["tokens"], 250)

    def test_a_step_holding_no_call_lands_on_its_own_row(self):
        self.assertEqual(self.attributed["(no tool call)"]["tokens"], 200)


class CodexGradeTests(unittest.TestCase):
    HEAVY = {"total_tokens": 400_000, "input_tokens": 380_000}
    # Two different URLs share the first forty characters of one curl line; the
    # journal cut both at the same place, so they are not evidence of a repeat.
    TRUNCATED_TARGET = "curl --fail --silent --show-error --loc…"
    JUNK_TARGETS = ("rg -n pattern node_modules/left-pad", "cat ./dist/bundle.js",
                    "open package-lock.json")
    WORKED_TARGETS = ("rg -n pattern src", "")

    def _grade(self, target):
        session = codex_session([([("exec_command", target)], dict(self.HEAVY))] * 6)
        return grade_codex_session(session, codex_steps(session))

    def test_a_truncated_call_target_is_not_counted_as_a_repeat(self):
        graded = self._grade(self.TRUNCATED_TARGET)

        self.assertNotIn("duplicate-reads",
                         [finding["kind"] for finding in graded["triage"]])

    def test_the_same_whole_target_read_again_is_counted_as_a_repeat(self):
        graded = self._grade("cargo test")

        self.assertIn("duplicate-reads",
                      [finding["kind"] for finding in graded["triage"]])

    def test_a_junk_target_is_read_out_of_a_command_fragment(self):
        for target in self.JUNK_TARGETS:
            with self.subTest(target=target):
                self.assertTrue(junk_target(target))
        for target in self.WORKED_TARGETS:
            with self.subTest(target=target):
                self.assertFalse(junk_target(target))

    def test_a_session_with_nothing_to_avoid_scores_nothing_and_grades_a(self):
        session = codex_session([
            ([("read_file", f"file{index}.py")],
             {"total_tokens": 5_000, "input_tokens": 4_800,
              "cached_input_tokens": 4_700, "output_tokens": 100})
            for index in range(6)
        ])
        graded = grade_codex_session(session, codex_steps(session))

        self.assertEqual(graded["score"], 0.0)
        self.assertEqual(graded["grade"], "A")
        self.assertEqual([finding["weight"] for finding in graded["triage"]], [])


if __name__ == "__main__":
    unittest.main()
