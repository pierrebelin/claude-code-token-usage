from __future__ import annotations

import unittest

from session_grade import grade_for, grade_weight


class SessionGradeScaleTests(unittest.TestCase):
    # (score, letter) at each band and on either side of every boundary
    BANDS = ((0, "A"), (2.9, "A"), (3, "B"), (7.9, "B"), (8, "C"),
             (14.9, "C"), (15, "D"), (23.9, "D"), (24, "F"))

    def test_a_score_lands_in_the_band_its_boundaries_state(self):
        for score, letter in self.BANDS:
            with self.subTest(score=score):
                self.assertEqual(grade_for(score), letter)

    def test_an_equal_share_weighs_the_same_whatever_unit_carries_it(self):
        # The unit differs -- dollars for the Claude Code reader, observed tokens
        # for the Codex one -- but an equal share weighs the same.
        self.assertEqual(grade_weight("cache", 12.0, 4.0, 2.0),
                         grade_weight("cache", 12.0, 400_000, 200_000))


if __name__ == "__main__":
    unittest.main()
