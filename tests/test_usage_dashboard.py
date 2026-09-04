from __future__ import annotations

import unittest
from pathlib import Path

from tests.collectors import usage_dashboard


OUTPUT = Path("/tmp/dashboard.html")


class UsageDashboardGatewayTests(unittest.TestCase):
    CLAUDE_PARAMS = {"q": ["project"], "sort": ["cost-desc"]}
    CODEX_PARAMS = {"id": ["session-1"], "sort": ["tokens-desc"]}

    def _command(self, source: str, path: str, params: dict) -> list[str]:
        return usage_dashboard.dashboard_command(source, OUTPUT, 30, path, params)

    def test_the_selected_source_is_forwarded_to_its_own_collector(self):
        for source, path, params in (("claude", "/", self.CLAUDE_PARAMS),
                                     ("codex", "/session", self.CODEX_PARAMS)):
            with self.subTest(source=source):
                command = self._command(source, path, params)
                self.assertIn("--dashboard-source", command)
                self.assertEqual(command[command.index("--dashboard-source") + 1], source)
                self.assertIn(usage_dashboard.SOURCES[source], " ".join(command))

    def test_a_claude_request_forwards_its_session_filter(self):
        command = self._command("claude", "/", self.CLAUDE_PARAMS)

        self.assertIn("--filter-sessions", command)
        self.assertEqual(command[command.index("--filter-sessions") + 1], "project")

    def test_a_session_request_forwards_the_session_it_focuses_on(self):
        command = self._command("codex", "/session", self.CODEX_PARAMS)

        self.assertIn("--focus", command)
        self.assertEqual(command[command.index("--focus") + 1], "session-1")

    def test_an_unknown_sort_is_dropped_rather_than_forwarded(self):
        command = self._command("claude", "/", {"sort": ["rm -rf"]})

        self.assertNotIn("--sort-sessions", command)

    def test_the_gateway_binds_loopback_and_nothing_else(self):
        server = usage_dashboard.gateway_server(0, 30)
        try:
            self.assertEqual(server.server_address[0], usage_dashboard.LOOPBACK_HOST)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
