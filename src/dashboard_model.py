"""Common dashboard view model and renderer for local usage sources.

The collectors intentionally keep their own payloads: Claude Code provides
cost and transcript-derived context, while Codex provides observed counters
and quotas.  This module is the boundary between those source payloads and the
single HTML front -- the overview and the session page alike.  Each reader
turns its own evidence into the same normalized model, formatted figures
included, and this module draws it.  Nothing here reads a payload it was not
given: a fact a source cannot observe is stated as missing, never rebuilt.
"""
from __future__ import annotations

from datetime import datetime
from html import escape


MONTHS_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def moment(iso: str | None):
    """One local datetime, or nothing when the record does not carry a usable one."""
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None


def clock(iso: str | None) -> str:
    point = moment(iso)
    if point is None:
        return "\u2014"
    return f"{MONTHS_SHORT[point.month - 1]} {point.day} {point:%H:%M}"


def span_label(start: str | None, end: str | None) -> str:
    """Start and end of a session, with the date written once when it is the same."""
    first, last = moment(start), moment(end)
    if first and last and first.date() == last.date():
        return f"{clock(start)} \u2192 {last:%H:%M}"
    return f"{clock(start)} \u2192 {clock(end)}"


def elapsed_label(start: str | None, end: str | None) -> str:
    first, last = moment(start), moment(end)
    if not first or not last:
        return ""
    minutes = int((last - first).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d}"


def _short(text: str, width: int) -> str:
    """Trim a label to a column, marking the cut rather than hiding it."""
    return text if len(text) <= width else text[:width - 1] + "\u2026"


def compact(value: int | float) -> str:
    value = float(value)
    for suffix, size in ((" G", 1_000_000_000), (" M", 1_000_000), (" k", 1_000)):
        if abs(value) >= size:
            return f"{value / size:.1f}{suffix}"
    return str(int(round(value)))


def _session_badges(*, grade: str | None, exact: bool) -> list[dict]:
    """Return the common session-list badges without inventing source facts."""
    badges = [{
        # The letter is the only coloured mark of the row: down a list, colour
        # should rank sessions by grade, not by how the figure was obtained.
        "kind": f"grade g{grade.lower()}" if grade else "grade grade-none",
        "label": grade or "—",
        "tip": (f"Grade {grade}: what this session could have avoided."
                if grade else
                "No grade is available: this source does not retain the transcript "
                "analysis needed to assign one."),
    }, {
        "kind": "state",
        "label": "Exact" if exact else "Partiel",
        "tip": ("Complete local counters were available for this session."
                if exact else
                "The local record is incomplete, so this session is a partial observation."),
        "available": True,
    }]
    return badges


_GIT_OUTCOMES = ("landed", "reverted", "unmerged", "no-commit")
_GIT_LABELS = {
    "landed": "Landed on the mainline",
    "reverted": "Reverted afterwards",
    "unmerged": "Committed, never merged",
    "no-commit": "No commit",
}
_GIT_SHORT = {"landed": "Landed", "reverted": "Reverted",
              "unmerged": "Unmerged", "no-commit": "No commit"}


def _codex_git_model(payload: dict) -> dict | None:
    """Normalize observed Codex/Git correlations for the shared front."""
    data = payload.get("git")
    if data is None:
        return None
    repos = data.get("repos") or []
    if not repos:
        return {"empty": True}
    outcomes = data["outcomes"]
    total = sum(value["tokens"] for value in outcomes.values())
    landed_commits = sum(
        1 for outcome in data.get("session_outcomes", {}).values()
        for commit in outcome.get("commits", [])
        if commit["landed"] and not commit["reverted"]
    )
    candidates = [
        session for session in payload.get("git_sessions", payload["sessions"])
        if session.get("git", {}).get("status") in _GIT_OUTCOMES and session["tokens"]
    ]
    candidates.sort(key=lambda session: session["tokens"]["total_tokens"], reverse=True)
    top_sessions = [
        {
            "id": session["id"],
            "project": session["group"],
            "short": session["id"][-12:],
            "when": (session["started_at"] or "unknown time").replace("T", " ")[:16],
            "outcome": session["git"]["status"],
            "commits": len(session["git"].get("commits", [])),
            "value": session["tokens"]["total_tokens"],
        }
        for session in candidates if session["git"]["status"] != "landed"
    ][:6]
    return {
        "empty": False,
        "outcomes": outcomes,
        "total": total,
        "repos": repos,
        "landed_commits": landed_commits,
        "top_sessions": top_sessions,
        "outside_git": data.get("outside_git", 0),
        "skipped_sessions": data.get("skipped_sessions", 0),
        "unmatched_commits": data.get("unmatched_commits", 0),
    }


def claude_dashboard_model(payload: dict) -> dict:
    """Transform a Claude analysis payload into the source-neutral front model."""
    totals = payload["totals"]
    return {
        "source": "Claude Code",
        "eyebrow": "Local Claude Code transcripts",
        "title": "Claude Code Token Usage",
        "lede": ("Costs reconstructed locally from session transcripts; nothing "
                 "leaves the machine."),
        "metric_name": "Total cost",
        "stats": [
            ("Total cost", f"${totals['cost_usd']:,.2f}", True),
            ("Sessions", str(totals["sessions"]), False),
            ("Projects", str(len(payload["projects"])), False),
            ("Cache-read tokens", compact(totals["cache_read"]), False),
            ("Tokens produced", compact(totals["output"]), False),
        ],
        "daily": [
            {"label": row["date"], "value": row["cost_usd"],
             "display": f"${row['cost_usd']:,.2f}",
             "meta": f"{row['sessions']} session{'s' if row['sessions'] != 1 else ''}"}
            for row in payload["daily"]
        ],
        "daily_title": "Day by day",
        "daily_empty": "No activity in this window.",
        "projects": [
            {"label": row["name"], "value": row["cost_usd"],
             "display": f"${row['cost_usd']:,.2f}",
             "meta": f"{row['sessions']} session{'s' if row['sessions'] != 1 else ''}"}
            for row in payload["projects"]
        ],
        "projects_title": "Cost by project",
        "sessions": [
            {"anchor": row["short"], "title": f"{row['project']} · {row['short']}",
             "meta": f"{row['start'] or 'unknown time'} · {row['turns']} turns · "
                     f"{row['prompts']} prompts · {row['compactions']} compactions",
             "display": f"${row['cost']:,.2f}",
             "badges": _session_badges(grade=row.get("grade"), exact=row["exact"])}
            for row in payload["sessions"]
        ],
        "sessions_title": "Sessions",
        "git": None,
        "footer": ["Cost at public API list price — not what a subscription bills.",
                   f"Generated {payload['generated']}."],
    }


def codex_dashboard_model(payload: dict) -> dict:
    """Transform a Codex observation payload into the source-neutral front model."""
    tokens = payload["totals"]["tokens"]
    projects = [row for row in payload["projects"] if row.get("is_git_project", True)]
    return {
        "source": "Codex Desktop",
        "eyebrow": "Local Codex Desktop journals",
        "title": "Codex Token Usage",
        "lede": "Usage reconstructed locally from session journals; nothing leaves the machine.",
        "metric_name": "Total tokens",
        "stats": [
            ("Total tokens", compact(tokens["total_tokens"]) if tokens else "—", True),
            ("Sessions", str(payload["totals"]["sessions"]), False),
            ("Projects", str(len(projects)), False),
            ("Cache-read tokens", compact(tokens["cached_input_tokens"]) if tokens else "—", False),
            ("Tokens produced", compact(tokens["output_tokens"]) if tokens else "—", False),
        ],
        "daily": [
            {"label": row["date"], "value": row["tokens"]["total_tokens"],
             "display": compact(row["tokens"]["total_tokens"]),
             "meta": "observed interval totals"}
            for row in payload["daily"]
        ],
        "daily_title": "Day by day",
        "daily_empty": "No observed token interval in this window.",
        "projects": [
            {"label": row["project"],
             "value": row["tokens"]["total_tokens"] if row["tokens"] else 0,
             "display": compact(row["tokens"]["total_tokens"]) if row["tokens"] else "—",
             "meta": f"{row['sessions']} session{'s' if row['sessions'] != 1 else ''}"}
            for row in projects
        ],
        "projects_title": "Cost by project",
        "sessions": [
            {"anchor": row["id"], "title": f"{row['group']} · {row['id'][-12:]}",
             "meta": f"{row['started_at'] or 'unknown time'} · {row.get('model') or 'unknown model'}",
             "display": compact(row["tokens"]["total_tokens"]) if row["tokens"] else "—",
             "badges": _session_badges(
                 grade=row.get("grade"), exact=row["status"] == "ok")}
            for row in payload["sessions"]
        ],
        "sessions_title": "Sessions",
        "git": _codex_git_model(payload),
        "footer": ["Observed tokens and quotas only — not Codex subscription spending.",
                   "Generated locally."],
    }


def _daily_chart(points: list[dict], title: str) -> str:
    """Render the shared no-JavaScript daily trend chart."""
    width, height, left, right, top, bottom = 1000, 220, 58, 16, 16, 28
    inner_width, inner_height = width - left - right, height - top - bottom
    peak = max(point["value"] for point in points) or 1
    span = max(1, len(points) - 1)

    def x(index: int) -> float:
        return left + (inner_width / 2 if len(points) == 1 else index * inner_width / span)

    def y(value: float) -> float:
        return top + inner_height - value * inner_height / peak

    path = " ".join(f'{"L" if index else "M"}{x(index):.1f},{y(point["value"]):.1f}'
                    for index, point in enumerate(points))
    svg = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
           '<defs><linearGradient id="areaFade" x1="0" y1="0" x2="0" y2="1">'
           '<stop class="fade-top" offset="0"/><stop class="fade-bottom" offset="1"/>'
           '</linearGradient></defs>']
    for fraction in (0, .5, 1):
        line_y = top + inner_height * fraction
        svg += [f'<line class="grid-line" x1="{left}" y1="{line_y:.1f}" x2="{width - right}" y2="{line_y:.1f}"/>',
                f'<text class="axis" x="{left - 9}" y="{line_y + 4:.1f}" text-anchor="end">'
                f'{escape(compact(round(peak * (1 - fraction))))}</text>']
    svg += [f'<path class="area" d="{path} L{x(len(points) - 1):.1f},{top + inner_height} '
            f'L{x(0):.1f},{top + inner_height} Z"/>',
            f'<path class="spark" d="{path}"/>',
            f'<circle class="cap" cx="{x(len(points) - 1):.1f}" '
            f'cy="{y(points[-1]["value"]):.1f}" r="4"/>']
    marks = range(len(points)) if len(points) <= 6 else (0, span // 2, span)
    for index in marks:
        anchor = "start" if index == 0 else "end" if index == span else "middle"
        svg.append(f'<text class="axis" x="{x(index):.1f}" y="{height - 7}" '
                   f'text-anchor="{anchor}">{escape(points[index]["label"])}</text>')
    svg.append('</svg>')
    return '<div class="chart">' + ''.join(svg) + '</div>'


def _render_git_outcomes(git: dict, links: dict[str, str]) -> str:
    """Render the shared Git-outcome section for observed token sources."""
    out = ['<section><h2>What it left in git</h2>']
    if git["empty"]:
        return ''.join(out + ['<p class="note">No session in this window ran inside a git repository, '
                               'so there is nothing to correlate.</p></section>'])

    total = git["total"] or 1
    outcomes = git["outcomes"]
    count = len(_GIT_OUTCOMES)
    out.append('<div class="stack">')
    for index, name in enumerate(_GIT_OUTCOMES):
        value = outcomes[name]["tokens"]
        if value:
            shade = 18 + index / max(1, count - 1) * 52
            out.append(f'<span style="width:{value / total * 100:.2f}%;background:color-mix(in oklab, '
                       f'var(--accent) {100 - shade:.0f}%, var(--sunk))" title="{escape(_GIT_LABELS[name])} '
                       f'— {escape(compact(value))}"></span>')
    out.append('</div><div class="legend">')
    for index, name in enumerate(_GIT_OUTCOMES):
        value = outcomes[name]["tokens"]
        shade = 18 + index / max(1, count - 1) * 52
        out.append(f'<span><span class="swatch" style="background:color-mix(in oklab, '
                   f'var(--accent) {100 - shade:.0f}%, var(--sunk))"></span>{escape(_GIT_LABELS[name])} '
                   f'· {escape(compact(value))} · {value / total * 100:.0f}%</span>')
    correlated = sum(value["sessions"] for value in outcomes.values())
    outside_mainline = total - outcomes["landed"]["tokens"]
    per_commit = (outcomes["landed"]["tokens"] / git["landed_commits"]
                  if git["landed_commits"] else None)
    facts = [
        ("Sessions correlated", str(correlated)),
        ("Commits landed", str(git["landed_commits"])),
        ("Tokens per landed commit", compact(per_commit) if per_commit is not None else "—"),
        ("Tokens outside mainline", compact(outside_mainline)),
    ]
    out.append('</div><div class="facts">')
    out.extend(f'<div class="fact"><span class="eyebrow">{escape(label)}</span><b>{escape(value)}</b></div>'
               for label, value in facts)
    out.append('</div><div class="scroll"><table><thead><tr>'
               '<th>Repo</th><th>Mainline</th><th class="n">Sess.</th>'
               + ''.join(f'<th class="n">{escape(_GIT_SHORT[name])}</th>' for name in _GIT_OUTCOMES)
               + '<th class="n">Tokens</th></tr></thead><tbody>')
    for repo in git["repos"]:
        cells = ''.join(f'<td class="n">{repo["outcomes"][name]["sessions"]}</td>'
                        for name in _GIT_OUTCOMES)
        tokens = sum(value["tokens"] for value in repo["outcomes"].values())
        out.append(f'<tr><td>{escape(repo["name"])}</td><td><span class="branch">'
                   f'{escape(repo["mainline"])}</span></td><td class="n">{repo["sessions"]}</td>'
                   f'{cells}<td class="n">{escape(compact(tokens))}</td></tr>')
    out.append('</tbody></table></div>')

    top_sessions = git["top_sessions"]
    if top_sessions:
        out.append('<span class="eyebrow">Largest sessions with nothing on the mainline</span><div class="rows">')
        peak = max(session["value"] for session in top_sessions) or 1
        for rank, session in enumerate(top_sessions, start=1):
            meta = f'{session["when"]} · {_GIT_LABELS[session["outcome"]]}'
            if session["commits"]:
                meta += f' · {session["commits"]} commit' + ("s" if session["commits"] != 1 else "")
            body = (f'<span class="row-fill"></span><span class="rank">{rank:02d}</span>'
                    f'<span class="row-main"><span class="row-title"><span class="proj">'
                    f'{escape(session["project"])}</span><span class="id">{escape(session["short"])}</span>'
                    f'</span><span class="row-meta">{escape(meta)}</span></span>'
                    f'<span class="row-cost">{escape(compact(session["value"]))}</span>')
            href = links.get(session["id"])
            share = max(1.5, session["value"] / peak * 100)
            out.append(f'<a class="row" style="--share:{share:.1f}%" href="{escape(href)}">{body}</a>'
                       if href else f'<div class="row" style="--share:{share:.1f}%">{body}</div>')
        out.append('</div>')

    notes = []
    if git["unmatched_commits"]:
        notes.append(f'{git["unmatched_commits"]} commit(s) were authored outside every session')
    if git["outside_git"]:
        notes.append(f'{git["outside_git"]} session(s) ran outside a git repo')
    if git["skipped_sessions"]:
        notes.append(f'{git["skipped_sessions"]} session(s) in smaller repos were left out')
    tail = (' ' + '; '.join(notes) + '.') if notes else ''
    out.append('<p class="note">Outcomes come from local Git, correlated on time: a commit authored while '
               'a session ran — from two minutes before its first turn to half an hour after its last — is '
               'attributed to it, then checked against the mainline. A session without a commit is not waste '
               'on its own: reading, debugging and planning end that way. Observed tokens are not a productivity '
               f'score; what matters is the volume appearing here over time.{escape(tail)}</p></section>')
    return ''.join(out)


def render_dashboard_overview(view: dict, *, source_controls: str = "", period_controls: str = "", session_controls: str = "",
                              sessions_note: str = "", session_links: dict[str, str] | None = None,
                              after_projects: str = "", details: str = "") -> str:
    """Render the normalized front; optional details remain source-specific data."""
    links = session_links or {}
    out = ['<main class="wrap" id="top">', '<header>',
           f'<span class="eyebrow">{escape(view["eyebrow"])}</span>',
           f'<h1>{escape(view["title"])}</h1>',
           f'<p class="lede">{escape(view["lede"])}</p>']
    if source_controls:
        out.append(source_controls)
    if period_controls:
        out.append(period_controls)
    out.append('<div class="headline">')
    for label, value, lead in view["stats"]:
        out.append(f'<div class="stat{" lead" if lead else ""}"><span class="eyebrow">'
                   f'{escape(label)}</span><b>{escape(value)}</b></div>')
    out.extend(['</div>', '</header>'])

    daily = view["daily"]
    out.append(f'<section><h2>{escape(view["daily_title"])}</h2>')
    if daily:
        out.append(_daily_chart(daily, view["daily_title"]))
    else:
        out.append(f'<p class="note">{escape(view["daily_empty"])}</p>')
    out.append('</section>')

    out.append(f'<section><h2>{escape(view["sessions_title"])}</h2>')
    if session_controls:
        out.append(session_controls)
    if sessions_note:
        out.append(sessions_note)
    out.append('<div class="overview-rows">')
    for index, row in enumerate(view["sessions"], start=1):
        href = links.get(row["anchor"], f'#s-{row["anchor"]}')
        badges = ''.join(
            f'<span class="chip {escape(badge["kind"])} src" data-tip="{escape(badge["tip"])}">'
            f'{escape(badge["label"])}</span>'
            for badge in row.get("badges", [])
        )
        out.append(f'<a class="overview-row" href="{escape(href)}"><span class="overview-rank">{index:02d}</span>'
                   f'<span class="overview-main"><b>{escape(row["title"])}{badges}</b>'
                   f'<small>{escape(row["meta"])}</small></span>'
                   f'<strong class="overview-value">{escape(row["display"])}</strong></a>')
    out.append('</div></section>')

    out.append(f'<section><h2>{escape(view["projects_title"])}</h2><div class="bars">')
    peak = max((row["value"] for row in view["projects"]), default=1) or 1
    for row in view["projects"]:
        width = max(.6, row["value"] * 100 / peak) if row["value"] else .6
        out.append(f'<div class="bar-row"><span>{escape(row["label"])}</span><div><i style="width:{width:.1f}%"></i></div>'
                   f'<b>{escape(row["display"])}<small>{escape(row["meta"])}</small></b></div>')
    out.append('</div></section>')
    if view.get("git") is not None:
        out.append(_render_git_outcomes(view["git"], links))
    if after_projects:
        out.append(after_projects)
    if details:
        out.append(details)
    out.append(footer(view["footer"]) + '</main>')
    return '\n'.join(out)


def footer(lines: list) -> str:
    """The source's standing caveats, repeated under every page it renders."""
    return ('<footer>' + ''.join(f'<div>{escape(line)}</div>' for line in lines)
            + '</footer>')


# --- one session, rendered the same way for every source --------------------
# A session page answers the same questions whatever produced it: what it was,
# what it consumed, how its context grew, what filled it, and step by step what
# happened.  Only the evidence differs, so the layout is shared here and each
# reader supplies formatted facts rather than a second page.


def _hint(text: str | None) -> str:
    """A section's explanation, folded into one mark beside its title."""
    if not text:
        return ""
    tip = escape(text)
    return (f'<span class="help src" tabindex="0" aria-label="{tip}" '
            f'data-tip="{tip}">?</span>')


def _tipped(label: str, tip: str | None, css: str = "eyebrow") -> str:
    """A label that explains itself on hover, or plain text when it cannot."""
    if not tip:
        return f'<span class="{css}">{escape(label)}</span>'
    return (f'<span class="{css} src" tabindex="0" data-tip="{escape(tip)}">'
            f"{escape(label)}</span>")


def chip(badge: dict, focusable: bool = True) -> str:
    """One status pill, with the explanation it carries when it has one."""
    tip = badge.get("tip")
    focus = ' tabindex="0"' if focusable and tip else ""
    marker = f' data-tip="{escape(tip)}"' if tip else ""
    return (f'<span class="chip {escape(badge["kind"])}{" src" if tip else ""}"'
            f'{focus}{marker}>{escape(badge["label"])}</span>')


def _column(column) -> str:
    """A column header that explains itself on hover, reusing the .src tooltip."""
    if not isinstance(column, dict):
        column = {"label": str(column)}
    cell = '<th class="n">' if column.get("n") else "<th>"
    label = escape(column.get("label", ""))
    if tip := column.get("tip"):
        label = f'<span class="src" tabindex="0" data-tip="{escape(tip)}">{label}</span>'
    return f"{cell}{label}</th>"


def _cell(value) -> str:
    """A table cell; a mapping carries alignment, a tooltip or a colour swatch."""
    if not isinstance(value, dict):
        return f"<td>{escape(str(value))}</td>"
    classes = " ".join(name for name, flag in
                       (("n", value.get("n")), ("label", value.get("label"))) if flag)
    attributes = f' class="{classes}"' if classes else ""
    if title := value.get("title"):
        attributes += f' title="{escape(title)}"'
    inner = escape(str(value.get("text", "")))
    if tip := value.get("tip"):
        inner = f'<span class="src" tabindex="0" data-tip="{escape(tip)}">{inner}</span>'
    if swatch := value.get("swatch"):
        inner = f'<span class="swatch" style="background:{swatch}"></span>{inner}'
    return f"<td{attributes}>{inner}</td>"


def _table(columns: list, rows: list) -> str:
    return ('<div class="scroll"><table><thead><tr>'
            + "".join(_column(column) for column in columns)
            + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(_cell(cell) for cell in row) + "</tr>"
                      for row in rows)
            + "</tbody></table></div>")


def _stack(parts: list) -> str:
    """The one-line proportion bar above a breakdown table."""
    return ('<div class="stack">' + "".join(
        f'<span style="width:{part["share"]:.2f}%;background:{part["color"]}" '
        f'title="{escape(part["title"])}"></span>' for part in parts) + "</div>")


def shade(index: int, count: int) -> str:
    """The shared ramp used by every breakdown, from accent to sunk."""
    mix = 18 + (index / max(1, count - 1)) * 52
    return f"color-mix(in oklab, var(--accent) {100 - mix:.0f}%, var(--sunk))"


def _session_curve(chart: dict) -> str:
    """The context, step by step, with the commands that moved it.

    A table sorted by cost tells you which call was expensive. It cannot show
    the shape: the startup plateau, the step a big read leaves behind, the cliff
    a compaction cuts. That shape is what the curve is for. Each reader supplies
    its own points and readouts; the drawing is the same.
    """
    points = chart.get("points") or []
    if len(points) < 2:
        return ""
    # The bottom padding holds two rows below the plot: the step axis, then the
    # strip the hover readout is written into. Keeping the readout out of the
    # plot is the point of it — a line of text laid over the curve competes with
    # it, and on a dense session it lands on the very peak being read.
    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 322, 58, 16, 64, 68
    inner_w, inner_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    axis_y = PAD_T + inner_h + 19
    strip_y, strip_h = PAD_T + inner_h + 30, 26
    peak = max(point["value"] for point in points) or 1
    span = max(1, len(points) - 1)
    uid = escape(chart.get("id", "session"))

    def px(index: int) -> float:
        return PAD_L + (index / span) * inner_w

    def py(value: float) -> float:
        return PAD_T + inner_h - (value / peak) * inner_h

    svg = [f'<svg viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="{escape(chart.get("label", "Context size step by step"))}">',
           f'<defs><linearGradient id="ctxFade-{uid}" x1="0" y1="0" x2="0" y2="1">'
           '<stop class="ctx-fade-top" offset="0"/>'
           '<stop class="ctx-fade-bottom" offset="1"/></linearGradient></defs>']
    for fraction in (0, 0.5, 1):
        y = PAD_T + inner_h * fraction
        svg.append(f'<line class="grid-line" x1="{PAD_L}" y1="{y:.1f}" '
                   f'x2="{W - PAD_R}" y2="{y:.1f}"/>')
        svg.append(f'<text class="axis" x="{PAD_L - 10}" y="{y + 3.5:.1f}" '
                   f'text-anchor="end">{escape(compact(peak * (1 - fraction)))}</text>')

    path = " ".join(f'{"L" if index else "M"}{px(index):.1f},{py(point["value"]):.1f}'
                    for index, point in enumerate(points))
    svg.append(f'<path d="{path} L{px(span):.1f},{PAD_T + inner_h:.1f} '
               f'L{PAD_L},{PAD_T + inner_h:.1f} Z" fill="url(#ctxFade-{uid})"/>')
    svg.append(f'<path class="spark" d="{path}"/>')
    svg.append(f'<rect class="readout-strip" x="{PAD_L}" y="{strip_y}" '
               f'width="{inner_w}" height="{strip_h}" rx="7"/>')
    idle = chart.get("idle") or "hover the curve for the step behind any point"
    svg.append(f'<text class="readout-idle" x="{PAD_L + 12}" '
               f'y="{strip_y + 17.5:.1f}">{escape(idle)}</text>')

    # Every re-baselining, named: a compaction is not a rewind.
    for index, point in enumerate(points):
        if not point.get("reset") or index == 0:
            continue
        x = px(index)
        anchor = "end" if x > PAD_L + inner_w * 0.85 else "start"
        shift = -4 if anchor == "end" else 4
        svg.append(f'<line class="reset-line" x1="{x:.1f}" y1="{PAD_T}" '
                   f'x2="{x:.1f}" y2="{PAD_T + inner_h:.1f}"/>')
        svg.append(f'<text class="reset-tag" x="{x + shift:.1f}" y="16" '
                   f'text-anchor="{anchor}">{escape(point.get("reset_tag", "reset"))}'
                   "</text>")

    # The handful of steps that actually moved the curve, labelled in place. Only
    # real events qualify: what every step replays is not one, and two labels
    # closer than a fifth of the width would overprint each other.
    chosen: list[tuple[int, dict]] = []
    ranked = sorted(enumerate(points), key=lambda pair: -pair[1].get("added", 0))
    for index, point in ranked:
        if point.get("reset") or point.get("dull") or not point.get("added"):
            continue
        if any(abs(px(index) - px(other)) < inner_w * 0.2 for other, _p in chosen):
            continue
        chosen.append((index, point))
        if len(chosen) == 4:
            break
    for row, (index, point) in enumerate(sorted(chosen)):
        x, y = px(index), py(point["value"])
        top = PAD_T - 32 + (row % 2) * 15
        anchor = "end" if x > PAD_L + inner_w * 0.72 else "start"
        shift = -7 if anchor == "end" else 7
        svg.append(f'<line class="peak-stem" x1="{x:.1f}" y1="{y:.1f}" '
                   f'x2="{x:.1f}" y2="{top + 3}"/>')
        svg.append(f'<circle class="peak-dot" cx="{x:.1f}" cy="{y:.1f}" r="3.5"/>')
        svg.append(f'<text class="peak-tag" x="{x + shift:.1f}" y="{top + 6}" '
                   f'text-anchor="{anchor}">{escape(_short(point["label"], 34))}</text>')

    # One hover band per step, each with the readout it reveals. A native <title>
    # would be lighter, but it takes a second to appear and the rest of the page
    # answers instantly; the guide, the dot and the line of text are pure CSS.
    band = inner_w / max(1, span)
    for index, point in enumerate(points):
        x, y = px(index), py(point["value"])
        # The band reaches down over the strip, so moving onto the line of text
        # does not dismiss it halfway through.
        svg.append(f'<rect class="hit" x="{x - band / 2:.1f}" y="{PAD_T}" '
                   f'width="{band:.2f}" '
                   f'height="{strip_y + strip_h - PAD_T:.1f}"/>')
        # The text sits at a fixed spot rather than following the cursor: it can
        # never run off the edge, and it does not jitter while being read.
        svg.append(
            f'<g class="readout">'
            f'<line class="guide" x1="{x:.1f}" y1="{PAD_T}" '
            f'x2="{x:.1f}" y2="{PAD_T + inner_h:.1f}"/>'
            f'<circle class="guide-dot" cx="{x:.1f}" cy="{y:.1f}" r="4"/>'
            f'<text class="readout-tag" x="{PAD_L + 12}" '
            f'y="{strip_y + 17.5:.1f}">{escape(point["readout"])}</text></g>')
    for index in (0, span // 2, span):
        anchor = "start" if index == 0 else "end" if index == span else "middle"
        svg.append(f'<text class="axis" x="{px(index):.1f}" y="{axis_y}" '
                   f'text-anchor="{anchor}">{escape(points[index]["x"])}</text>')
    svg.append("</svg>")

    legend = "".join(
        f'<span><i{f' class="{mark}"' if mark else ""}></i>{escape(text)}</span>'
        for mark, text in chart.get("legend") or [])
    legend = f'<div class="legend">{legend}</div>' if legend else ""
    return f'<div class="chart">{"".join(svg)}</div>{legend}'


def _session_triage(triage: dict) -> str:
    """The session's grade, and the leads it is made of."""
    out = ['<section class="triage">',
           f'<h2>{escape(triage["title"])}{_hint(triage.get("hint"))}</h2>']
    if stats := triage.get("stats") or []:
        out.append('<div class="headline tipped">')
        for stat in stats:
            tone = stat.get("tone") or ""
            out.append(f'<div class="stat{" lead" if tone else ""}">'
                       + _tipped(stat["label"], stat.get("tip"))
                       + (f'<b class="{escape(tone)}">' if tone else "<b>")
                       + f'{escape(str(stat["value"]))}</b></div>')
        out.append("</div>")
    findings = triage.get("findings") or []
    if not findings:
        return "".join(out + [f'<p class="note">{escape(triage["note"])}</p></section>'])
    out.append('<div class="triage-list">')
    for rank, finding in enumerate(findings, 1):
        items = "".join(f"<li>{escape(item)}</li>" for item in finding.get("items") or [])
        out.append(
            '<article class="triage-item">'
            f'<span class="triage-rank">{rank:02d}</span>'
            '<div class="triage-main">'
            f'<div class="triage-title"><b>{escape(finding["value"])}</b>'
            f'<span class="triage-share">{escape(finding["share"])}</span></div>'
            f'<p>{escape(finding["reason"])}</p>'
            + (f'<ul class="triage-items">{items}</ul>' if items else "")
            + f'<p class="triage-advice">{escape(finding["advice"])}</p>'
            '<ul class="triage-tips">'
            + "".join(f"<li>{escape(tip)}</li>" for tip in finding.get("tips") or [])
            + "</ul></div></article>")
    return "".join(out + ["</div></section>"])


def _session_section(section: dict, standalone: bool) -> str:
    """One section of the session page: a breakdown, a timeline or source facts."""
    out = ["<section>"]
    if title := section.get("title"):
        hint = _hint(section.get("hint")) if standalone else ""
        out.append(f"<h2>{escape(title)}{hint}</h2>")
    if standalone and (controls := section.get("controls")):
        out.append(f'<div class="filters">{controls}</div>')
    if stack := section.get("stack"):
        out.append(_stack(stack))
    if rows := section.get("rows"):
        out.append(_table(section["columns"], rows))
    elif section.get("columns"):
        out.append(f'<p class="empty">{escape(section.get("empty", "Nothing here."))}</p>')
    if markup := section.get("html"):
        out.append(markup)
    out.extend(f'<p class="note">{escape(note)}</p>' for note in section.get("notes") or [])
    out.append("</section>")
    return "".join(out)


def render_session_view(view: dict, standalone: bool = True) -> str:
    """Render one session from the normalized model both readers produce.

    ``standalone`` is the focused page: it carries the identity header, the
    grade and the per-section explanations. Folded into an offline overview,
    the same session keeps its figures and drops that framing.
    """
    out = []
    if standalone:
        if back := view.get("back"):
            out.append(f'<a class="back" href="{escape(back["href"])}">'
                       f'{escape(back["label"])}</a>')
        out.append('<header class="hero"><div class="hero-left">')
        out.append(f'<div class="hero-top"><span class="eyebrow">'
                   f'{escape(view["eyebrow"])}</span>'
                   f'<span class="id">{escape(view["id"])}</span>'
                   + "".join(f'<span class="branch">{escape(tag)}</span>'
                             for tag in view.get("tags") or []) + "</div>")
        out.append(f'<h1>{escape(view["title"])}</h1>')
        metric = view["metric"]
        prefix = (f'<span>{escape(metric["prefix"])}</span>'
                  if metric.get("prefix") else "")
        unit = (f'<span class="unit">{escape(metric["unit"])}</span>'
                if metric.get("unit") else "")
        out.append(f'<div class="identity"><span class="hero-cost">{prefix}'
                   f'{escape(metric["value"])}{unit}</span>'
                   + "".join(chip(badge) for badge in view.get("badges") or []) + "</div>")
        run = []
        for item in view.get("run") or []:
            value = f'<b>{escape(item["value"])}</b> ' if item.get("value") else ""
            run.append(f'<span>{value}{escape(item["label"])}</span>')
        out.append('</div><div class="hero-meta">' + "".join(run) + "</div></header>")

    out.append('<div class="facts">')
    for fact in view["facts"]:
        out.append(f'<div class="fact">{_tipped(fact["label"], fact.get("tip"))}'
                   f'<b>{escape(str(fact["value"]))}</b></div>')
    out.append("</div>")

    if standalone and view.get("triage"):
        out.append(_session_triage(view["triage"]))

    if chart := view.get("chart"):
        curve = _session_curve(chart)
        if curve:
            hint = _hint(chart.get("hint")) if standalone else ""
            out.append(f'<section><h2>{escape(chart["title"])}{hint}</h2>{curve}</section>')

    out.extend(_session_section(section, standalone)
               for section in view.get("sections") or [])
    out.extend(f'<p class="note">{escape(note)}</p>' for note in view.get("notes") or [])
    return "\n".join(out)


def metadata_list(rows: list) -> str:
    """The session record both readers keep: one label, one observed value."""
    return '<dl class="meta">' + "".join(
        f"<dt>{escape(str(label))}</dt><dd>{escape(str(value))}</dd>"
        for label, value in rows) + "</dl>"
