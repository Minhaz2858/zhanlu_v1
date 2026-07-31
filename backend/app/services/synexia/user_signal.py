"""User-signal detection — figure out if the user asked for a download.

The architecture doc says FINALIZE should emit BOTH an in-chat
ReportCard AND a downloadable artifact, with the surface choice driven
by what the user actually said.  This module is the lexically small,
testable core of that decision.

The check is intentionally cheap (no LLM call) so it can sit in the hot
path of every chat turn without  If we get fancier
intent detection later (e.g. an LLM classifier), it slots in here.

Signal vocabulary
-----------------
The frontend (``MessageBubble.isExportSignal``) and the
``ReportCard`` look at ``user_signal`` to decide which surface to show.
Keep this set small and explicit; new formats must add a new value
**and** extend ``EXPORT_SIGNALS`` below so the frontend picks them up
in one place.

* ``"default"``        — in-chat ReportCard only.
* ``"export"``         — generic downloadable artifact (legacy).
* ``"download"``       — alias of ``export`` (legacy).
* ``"save"``           — alias of ``export`` (legacy).
* ``"export_docx"``    — DOCX file artifact.
* ``"export_pptx"``    — PPTX file artifact.
* ``"export_xlsx"``    — XLSX file artifact.
* ``"export_pdf"``     — PDF file artifact.
* ``"export_md"``      — Markdown file artifact.
"""

from __future__ import annotations

import re

# Case-insensitive keywords the user has to use to flip into
# "downloadable artifact" mode.  Keep this list small and obviously
# user-driven so we don't accidentally route a report to the
# download surface when the user just wants to read it.
#
# We match a *prefix* of each keyword (regex `\b<k>`), so "exported",
# "exporting", "saved", "PDFs" all match.  That matches user intent —
# if someone said "exported last year" they obviously know what
# "export" means, and we want to make the file available.
_DOWNLOAD_KEYWORDS = (
    "export", "download", "save", "pdf", "ppt", "pptx", "xlsx",
    "excel", "spreadsheet", "deck", "presentation", "send me",
    "give me a file", "as a file", "to file",
)

# Compiled regex; prefix match on each keyword (no trailing \b, so
# "exported" and "exports" both match).
_DOWNLOAD_RE = re.compile(
    r"(?i)\b(?:" + "|".join(re.escape(k) for k in _DOWNLOAD_KEYWORDS) + r")",
)


# All signals that should trigger the download surface in the
# frontend.  Frontend code (MessageBubble.isExportSignal,
# ReportCard isExport) must check ``s in EXPORT_SIGNALS`` rather than
# hardcoding a few values, so adding a new format here automatically
# extends the surface without touching React code.
EXPORT_SIGNALS: frozenset[str] = frozenset({
    "export",      # generic
    "download",    # alias
    "save",        # alias
    "export_docx",
    "export_pptx",
    "export_xlsx",
    "export_pdf",
    "export_md",
    "export_html",
    "export_dashboard",
})


def is_export_signal(signal: str | None) -> bool:
    """True if ``signal`` is one of the export/download user signals."""
    if not signal:
        return False
    return signal in EXPORT_SIGNALS


def detect_user_signal(user_message: str) -> str:
    """Return "export" if the user asked for a downloadable artifact, else "default".

    The user_signal is a single-string decision because the architecture
    says FINALIZE emits BOTH surfaces and the FRONTEND picks the primary
    one — we don't have to make the routing decision here, but we do
    have to flag the user's intent so FINALIZE can build the export
    versions of the artifact (PDF/PPTX/XLSX) and the frontend can
    surface the download button.

    Format-specific detection (docx vs pptx vs ...) lives in
    :func:`app.services.synexia.intent_router.detect_file_intent` and
    is consulted separately by FINALIZE.
    """
    if not user_message:
        return "default"
    if _DOWNLOAD_RE.search(user_message):
        return "export"
    return "default"
