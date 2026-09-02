"""The one A-F scale both readers grade a session on.

A grade reads one session, never the machine, and only the part of that run
which could have been avoided: junk loaded into context, the same target read
twice, a cache rebuilt after a pause, a compaction carried to the end, replies
replayed long after they mattered.  Each weight is a *share* of what the session
itself consumed, so a small run and a huge one are graded on the same scale, and
an expensive session is never penalised for being expensive.

The share is the only thing shared.  What fills it differs by source: the Claude
Code reader measures dollars from its transcripts, the Codex reader attributes
observed tokens over counter intervals.  Both hand this module a share and a
materiality figure in their own unit, and both come back with the same letter.
"""
from __future__ import annotations


GRADE_BANDS = ((3, "A"), (8, "B"), (15, "C"), (24, "D"))
# kind -> factor applied to that share, cap, and the share below which it is free
GRADE_WEIGHTS = {
    "junk-reads": (1.2, 25.0, 0.0),
    "duplicate-reads": (1.2, 20.0, 0.0),
    "cache": (1.0, 20.0, 0.0),
    "compaction": (1.0, 15.0, 0.0),
    "replayed-output": (0.6, 15.0, 20.0),
}
# Below this, a share is still a share but there is nothing to act on: forty
# cents rebuilt in a one-dollar session is a rate, not a problem. The weight
# fades in up to it rather than landing whole. Each source passes its own, in
# its own unit -- dollars for Claude Code, observed tokens for Codex.
GRADE_MATERIAL = 2.0
JUNK_FRAGMENTS = ("/node_modules/", "/.git/", "/dist/", "/build/", "/.next/",
                  "/target/", "/vendor/", "/.venv/", "/site-packages/",
                  "/coverage/", "/__pycache__/", "/.terraform/", "/Pods/",
                  "/.pytest_cache/", "/.mypy_cache/")
JUNK_NAMES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
              "Cargo.lock", "composer.lock", "Gemfile.lock", ".min.js", ".min.css",
              ".js.map", ".css.map")


def is_junk(path: str) -> bool:
    """A path whose content is generated, vendored or locked: never worth context."""
    lowered = path.replace("\\", "/")
    return (any(fragment in lowered for fragment in JUNK_FRAGMENTS)
            or lowered.endswith(JUNK_NAMES))


def grade_for(score: float) -> str:
    for ceiling, letter in GRADE_BANDS:
        if score < ceiling:
            return letter
    return "F"


def grade_weight(kind: str, share: float, amount: float,
                 material: float = GRADE_MATERIAL) -> float:
    """What one finding costs the grade: its share of the session, weighted.

    ``amount`` and ``material`` are in whatever unit the source measures; only
    their ratio is read, so the fade-in behaves the same on dollars and tokens.
    """
    factor, cap, free = GRADE_WEIGHTS.get(kind, (0.0, 0.0, 0.0))
    materiality = min(1.0, amount / material) if material else 1.0
    return min(cap, factor * max(0.0, share - free)) * materiality


def grade_session(detail: dict) -> tuple:
    score = sum(finding["weight"] for finding in detail.get("triage") or [])
    return round(score, 1), grade_for(score)
