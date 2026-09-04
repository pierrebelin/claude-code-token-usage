from __future__ import annotations

import unittest

from dashboard_template import render_dashboard_document, source_tabs

from tests import SOURCE


class DashboardDocumentTests(unittest.TestCase):
    def test_the_tabs_mark_the_selected_source_and_keep_the_window(self):
        document = render_dashboard_document(
            SOURCE / "cc-usage.py", "Usage",
            f"<main>overview{source_tabs('claude', 30)}</main>",
        )

        self.assertIn('class="source-tabs"', document)
        self.assertIn('/?source=claude&amp;days=30" aria-current="page"', document)
        self.assertIn('/?source=codex&amp;days=30"', document)
        self.assertIn('class="source-tabs__loader"', document)
        self.assertIn('classList.add("is-loading")', document)

    def test_a_document_without_tabs_carries_no_source_navigation(self):
        document = render_dashboard_document(
            SOURCE / "cc-usage.py", "Usage", "<main>overview</main>")

        self.assertNotIn('<nav class="source-tabs"', document)


if __name__ == "__main__":
    unittest.main()
