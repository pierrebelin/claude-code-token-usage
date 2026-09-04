from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from codex_log import TokenUsage

from tests.collectors import FIXTURES, codex_usage


COMMITTED_AT = datetime(2026, 8, 30, 9, 5, tzinfo=timezone.utc)
# The fixture repository answers to itself alone: no machine-wide Git config.
GIT_IDENTITY = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}


def _git(root: Path, *arguments: str, at: datetime | None = None) -> None:
    stamp = {} if at is None else {"GIT_AUTHOR_DATE": at.isoformat(),
                                   "GIT_COMMITTER_DATE": at.isoformat()}
    subprocess.run(("git", "-C", str(root), *arguments), check=True,
                   capture_output=True,
                   env={**os.environ, **GIT_IDENTITY, **stamp})


def _repository(root: Path) -> None:
    """One repository whose single commit lands on ``main`` inside the window."""
    _git(root, "init", "--quiet")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    (root / "worked.txt").write_text("worked\n", encoding="utf-8")
    _git(root, "add", "worked.txt")
    _git(root, "commit", "--quiet", "-m", "Do the work", at=COMMITTED_AT)


def _session(rollout_id: str, project: str, tokens: TokenUsage):
    return codex_usage.CodexSession(
        rollout_id=rollout_id, thread_id=f"thread-{rollout_id}", source="active",
        project=project, cwd=project, started_at="2026-08-30T09:00:00+00:00",
        ended_at="2026-08-30T09:10:00+00:00", model=None, model_provider=None,
        status="ok", tokens=tokens, intervals=(), tools=(), last_quota=None,
        warnings=(),
    )


class CodexRolloutDiscoveryTests(unittest.TestCase):
    def test_the_date_window_applies_to_the_file_name_before_any_journal_is_opened(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active, archived = root / "sessions", root / "archived"
            active.mkdir()
            archived.mkdir()
            shutil.copyfile(FIXTURES / "short.jsonl",
                            active / "rollout-2026-08-20T09-00-00-old.jsonl")
            shutil.copyfile(FIXTURES / "long-with-tools.jsonl",
                            archived / "rollout-2026-08-30T09-00-00-new.jsonl")

            rollouts = codex_usage.discover_rollouts(
                (("active", active), ("archived", archived)), since=date(2026, 8, 30))

        self.assertEqual([(item.source, item.started_on) for item in rollouts],
                         [("archived", date(2026, 8, 30))])

    def test_a_worktree_collapses_to_the_repository_that_owns_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, worktree = root / "repository", root / "worker"
            repository.mkdir()
            (repository / ".git").mkdir()
            worktree.mkdir()
            (worktree / ".git").write_text(
                f"gitdir: {repository / '.git' / 'worktrees' / 'worker'}\n",
                encoding="utf-8")

            self.assertEqual(codex_usage.project_root(str(worktree)), str(repository))


class CodexPayloadTests(unittest.TestCase):
    ROLLOUTS = (("long-with-tools.jsonl", "active", date(2026, 8, 30)),
                ("archived.jsonl", "archived", date(2026, 8, 1)),
                ("short.jsonl", "active", date(2026, 8, 31)),
                ("zero-counters.jsonl", "active", date(2026, 8, 27)),
                ("without-counters.jsonl", "active", date(2026, 8, 31)))
    # Each session totals its journal's own last cumulative snapshot: 270, 121
    # and 150. Nothing is left behind a baseline.
    TOTAL_TOKENS = {"input_tokens": 415, "cached_input_tokens": 105,
                    "cache_write_input_tokens": 20, "output_tokens": 126,
                    "reasoning_output_tokens": 27, "total_tokens": 541}
    UNUSABLE = (("session-zero", "no_exploitable_intervals"),
                ("session-no-counters", "no_complete_counters"))

    def setUp(self):
        rollouts = [codex_usage.RolloutFile(FIXTURES / name, source, started_on)
                    for name, source, started_on in self.ROLLOUTS]
        self.sessions = codex_usage.analyze_rollouts(rollouts)
        self.payload = codex_usage.payload_for(self.sessions, date(2026, 8, 1),
                                               date(2026, 8, 31))

    def _row(self, thread_id: str) -> dict:
        return next(row for row in self.payload["sessions"]
                    if row["thread_id"] == thread_id)

    def test_every_journal_is_counted_and_only_the_readable_ones_carry_tokens(self):
        self.assertEqual(self.payload["totals"]["sessions"], 5)
        self.assertEqual(self.payload["totals"]["sessions_with_tokens"], 3)
        self.assertEqual(self.payload["totals"]["sessions_without_exploitable_counters"], 2)
        self.assertEqual(self.payload["totals"]["tokens"], self.TOTAL_TOKENS)

    def test_a_session_without_usable_counters_states_it_instead_of_reading_zero(self):
        for thread_id, status in self.UNUSABLE:
            with self.subTest(session=thread_id):
                row = self._row(thread_id)
                self.assertEqual(row["status"], status)
                self.assertIsNone(row["tokens"])

    def test_the_days_are_grouped_on_the_moment_each_interval_ended(self):
        self.assertEqual([row["date"] for row in self.payload["daily"]],
                         ["2026-08-01", "2026-08-30", "2026-08-31"])

    def test_a_session_carries_the_tool_that_ran_and_its_bounded_target(self):
        row = self._row("session-long")

        self.assertEqual([tool["tool"] for tool in row["tools"]],
                         ["exec_command", "web.run", "exec"])
        self.assertEqual([tool["label"] for tool in row["tools"]], [
            "exec_command(rg -n pattern src)", "web.run(an invented search)",
            "exec(const total = 1 + 1;)"])
        self.assertEqual(row["last_quota"]["primary"]["window_minutes"], 300)

    def test_the_payload_is_serializable_as_it_stands(self):
        json.dumps(self.payload)

    def test_no_api_equivalent_is_offered_without_an_unambiguous_model_mapping(self):
        self.assertNotIn("api_equivalent", self.payload)
        self.assertTrue(all("api_equivalent" not in session
                            for session in self.payload["sessions"]))
        self.assertNotIn("api_equivalent", vars(codex_usage.parse_args(["--days", "1"])))

    def test_the_terminal_summary_states_the_observed_totals(self):
        with redirect_stdout(io.StringIO()) as output:
            codex_usage.print_summary(self.payload, sessions=4, daily=True, tools=True)
        terminal = output.getvalue()

        self.assertIn("Input uncached 415", terminal)
        self.assertIn("Total 541", terminal)
        self.assertIn("Last observed quota", terminal)
        self.assertIn("Tool calls", terminal)


class CodexGitOutcomesTests(unittest.TestCase):
    WORKED_TOKENS = TokenUsage(10, 3, 0, 4, 1, 14)
    CHATTED_TOKENS = TokenUsage(1, 0, 0, 0, 0, 1)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repository, self.elsewhere = root / "repository", root / "elsewhere"
        self.repository.mkdir()
        self.elsewhere.mkdir()
        _repository(self.repository)
        self.worked = _session("rollout-worked", str(self.repository), self.WORKED_TOKENS)
        self.chatted = _session("rollout-chatted", str(self.elsewhere), self.CHATTED_TOKENS)
        self.addCleanup(self.directory.cleanup)

    def test_a_commit_inside_the_session_window_lands_on_the_session(self):
        git = codex_usage.git_outcomes([self.worked])

        self.assertEqual(git["outcomes"]["landed"], {"sessions": 1, "tokens": 14})
        outcome = git["session_outcomes"]["rollout-worked"]
        self.assertEqual(outcome["status"], "landed")
        self.assertEqual([(len(commit["sha"]), commit["landed"], commit["reverted"])
                          for commit in outcome["commits"]], [(8, True, False)])
        self.assertEqual(git["repos"][0]["mainline"], "main")

    def test_a_session_run_outside_a_repository_is_named_as_such(self):
        git = codex_usage.git_outcomes([self.worked, self.chatted])

        self.assertEqual(git["session_outcomes"]["rollout-chatted"]["status"],
                         "outside_git")
        self.assertEqual(git["outside_git"], 1)

    def test_correlating_leaves_the_token_totals_untouched(self):
        git = codex_usage.git_outcomes([self.worked])
        payload = codex_usage.payload_for([self.worked], date(2026, 8, 30),
                                          date(2026, 8, 30), git=git)

        self.assertEqual(payload["totals"]["tokens"]["total_tokens"], 14)
        self.assertEqual(payload["sessions"][0]["git"]["status"], "landed")
        self.assertTrue(payload["projects"][0]["is_git_project"])

    def test_the_dashboard_keeps_only_the_sessions_git_can_account_for(self):
        git = codex_usage.git_outcomes([self.worked, self.chatted])
        payload = codex_usage.payload_for([self.worked, self.chatted],
                                          date(2026, 8, 30), date(2026, 8, 30), git=git)
        view = codex_usage.dashboard_view(payload, project=None)

        self.assertEqual(view["totals"]["sessions"], 1)
        self.assertEqual(view["totals"]["tokens"]["total_tokens"], 14)
        self.assertEqual([row["id"] for row in view["sessions"]], ["rollout-worked"])


class CodexCommandTests(unittest.TestCase):
    def test_the_dashboard_window_and_session_count_have_defaults(self):
        args = codex_usage.parse_args([])

        self.assertEqual(args.days, 30)
        self.assertEqual(args.sessions_max, 5)

    def test_no_git_produces_a_payload_that_states_no_git_result_at_all(self):
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "sessions"
            active.mkdir()
            shutil.copyfile(FIXTURES / "long-with-tools.jsonl",
                            active / "rollout-2026-08-30T09-00-00-long.jsonl")
            roots = (("active", active),)
            with patch.object(codex_usage, "SESSION_ROOTS", roots):
                payload = codex_usage.collect_payload(
                    date(2026, 8, 30), date(2026, 8, 30), None, "repo", no_git=True)

        self.assertIsNone(payload["git"])
        self.assertEqual(payload["totals"]["sessions"], 1)

    def test_the_dashboard_server_binds_loopback_and_nothing_else(self):
        server = codex_usage.dashboard_server(0, None, 7, None, "repo", True)
        try:
            self.assertEqual(server.server_address[0], codex_usage.LOOPBACK_HOST)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
