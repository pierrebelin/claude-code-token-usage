"""Shared standalone-document assembly for the local usage dashboards.

The doctype, safety markers and visual foundations live here.  Source payloads
are normalized by ``dashboard_model.py`` before their shared overview renders.
"""
from __future__ import annotations

from pathlib import Path


BODY_MARKER = "<!--__BODY__-->"
SHARED_STYLES_MARKER = "/*__SHARED_STYLES__*/"
SKELETON_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
"""
SKELETON_MID = """</head>
<body>
"""
SKELETON_TAIL = """</body>
</html>
"""


def source_tabs(source: str | None, days: int | None) -> str:
    """Return the shared source switcher for the combined live dashboard."""
    if source not in {"claude", "codex"}:
        return ""
    window = f"&amp;days={days}" if days else ""

    def tab(key: str, label: str) -> str:
        current = ' aria-current="page"' if key == source else ""
        return f'<a href="/?source={key}{window}"{current}>{label}</a>'

    return ('<nav class="source-tabs" aria-label="Usage source" aria-busy="false">'
            '<span class="source-tabs__choices">'
            + tab("claude", "Claude Code") + tab("codex", "Codex Desktop")
            + '</span><span class="source-tabs__loader" role="status" aria-live="polite">'
            '<span class="source-tabs__spinner" aria-hidden="true"></span>Loading…</span></nav>'
            '<script>document.addEventListener("click",function(e){var a=e.target.closest(".source-tabs a");'
            'if(!a||e.defaultPrevented||e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||'
            'a.getAttribute("aria-current")==="page")return;e.preventDefault();var n=a.closest(".source-tabs");'
            'if(n.classList.contains("is-loading"))return;n.classList.add("is-loading");'
            'n.setAttribute("aria-busy","true");window.setTimeout(function(){window.location.assign(a.href)},80)})</script>')


def render_dashboard_document(
    script_path: Path,
    title: str,
    body: str,
) -> str:
    """Inline the one dashboard front into a standalone document.

    Source controls belong to the overview body, so focused session documents
    do not inherit them accidentally.
    """
    template_path = script_path.with_name("usage-dashboard-template.html")
    shared_styles_path = script_path.with_name("usage-dashboard-base.css")
    try:
        fragment = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"template not found: {template_path}") from exc
    try:
        shared_styles = shared_styles_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"shared dashboard styles not found: {shared_styles_path}") from exc
    if BODY_MARKER not in fragment:
        raise SystemExit(f"invalid template: missing body marker: {template_path}")
    if SHARED_STYLES_MARKER not in fragment:
        raise SystemExit(f"invalid template: missing shared styles marker: {template_path}")
    if "<title>" not in fragment:
        raise SystemExit(f"invalid template: missing title marker: {template_path}")
    head, tail = fragment.replace("__TITLE__", title).replace(
        SHARED_STYLES_MARKER, shared_styles).split(BODY_MARKER, 1)
    return SKELETON_HEAD + head + SKELETON_MID + body + tail + SKELETON_TAIL
