#!/usr/bin/env python3
"""One local dashboard for Claude Code and Codex Desktop usage.

The collectors remain independent because their accounting evidence differs.
This loopback-only gateway selects one source at a time and lets the shared
HTML front own the navigation and layout.
"""
from __future__ import annotations

import argparse
import http.server
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import webbrowser


SOURCES = {"claude": "cc-usage.py", "codex": "codex-usage.py"}
# The gateway is served to this machine and to nothing else.
LOOPBACK_HOST = "127.0.0.1"
CLAUDE_SORTS = {"date-desc", "date-asc", "cost-desc", "cost-asc", "project-asc"}
CLAUDE_TURN_SORTS = {"cost-desc", "cost-asc", "added-desc", "context-desc", "turn-asc", "turn-desc"}
CODEX_SORTS = {"tokens-desc", "tokens-asc", "recent-desc", "recent-asc", "project-asc"}
CODEX_STEP_SORTS = {"tokens-desc", "tokens-asc", "output-desc", "input-desc",
                    "step-asc", "step-desc"}


def _value(params: dict[str, list[str]], name: str, limit: int = 160) -> str:
    return ((params.get(name) or [""])[0] or "").strip()[:limit]


def dashboard_command(source: str, output: Path, days: int, path: str,
                      params: dict[str, list[str]]) -> list[str]:
    """Build one validated collector command for a dashboard request."""
    script = Path(__file__).with_name(SOURCES[source])
    command = [sys.executable, str(script), "--dashboard", str(output), "--served",
               "--dashboard-source", source, "--days", str(days)]
    selected = _value(params, "sort", 20)
    limit = _value(params, "max", 4)
    if source == "claude":
        if selected in CLAUDE_SORTS:
            command += ["--sort-sessions", selected]
        turn_sort = _value(params, "tsort", 20)
        if turn_sort in CLAUDE_TURN_SORTS:
            command += ["--sort-turns", turn_sort]
        if query := _value(params, "q"):
            command += ["--filter-sessions", query]
        if query := _value(params, "tq"):
            command += ["--filter-turns", query]
    else:
        if selected in CODEX_SORTS:
            command += ["--sort-sessions", selected]
        group_by = _value(params, "by", 8)
        if group_by in {"repo", "cwd", "dir"}:
            command += ["--by", group_by]
        step_sort = _value(params, "tsort", 20)
        if step_sort in CODEX_STEP_SORTS:
            command += ["--sort-steps", step_sort]
        if query := _value(params, "q"):
            command += ["--project", query]
        if query := _value(params, "tq"):
            command += ["--filter-steps", query]
    if limit.isdigit():
        command += ["--sessions-max", str(max(1, min(60, int(limit))))]
    if path == "/session":
        focus = _value(params, "id", 64)
        if focus and focus.replace("-", "").isalnum():
            command += ["--focus", focus]
    return command


def gateway_server(port: int, default_days: int):
    """One gateway server, bound to ``LOOPBACK_HOST`` and to no other address."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in {"/", "/index.html", "/session"}:
                self.send_error(404, "Nothing here")
                return
            params = urllib.parse.parse_qs(parsed.query)
            source = _value(params, "source", 12) or "claude"
            if source not in SOURCES:
                self.send_error(400, "Unknown source")
                return
            raw_days = _value(params, "days", 8)
            days = max(1, min(3650, int(raw_days))) if raw_days.isdigit() else default_days
            if parsed.path == "/session" and not _value(params, "id", 64):
                self.send_error(400, "Missing session id")
                return
            with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
                output = Path(handle.name)
            try:
                run = subprocess.run(
                    dashboard_command(source, output, days, parsed.path, params),
                    capture_output=True, text=True, timeout=180,
                )
                if run.returncode != 0 or not output.exists() or not output.stat().st_size:
                    self.send_error(500, (run.stderr or run.stdout or "Analysis failed")[-1000:])
                    return
                document = output.read_bytes()
            finally:
                output.unlink(missing_ok=True)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(document)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(document)

    return http.server.ThreadingHTTPServer((LOOPBACK_HOST, port), Handler)


def serve(port: int, default_days: int) -> None:
    """Serve both local collectors through a single source-selecting page."""
    server = gateway_server(port, default_days)
    url = f"http://{LOOPBACK_HOST}:{server.server_port}/"
    print(f"Dashboard served at {url}")
    print("Claude Code is selected by default. Ctrl+C to stop.")
    threading.Timer(.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", nargs="?", const=8787, type=int, metavar="PORT",
                        help="serve the combined dashboard on loopback (default: 8787)")
    parser.add_argument("--days", type=int, default=30,
                        help="default rolling window in days (default: 30)")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")
    if args.serve is None:
        parser.error("--serve is required")
    serve(args.serve, args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
