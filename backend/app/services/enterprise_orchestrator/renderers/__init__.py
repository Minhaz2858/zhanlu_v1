"""Enterprise report renderers (spec §4, §10).

Two render targets are provided for the enterprise payload produced by
``synthesize_enterprise_report``:

* :func:`render_enterprise_docx` — a 6-section .docx with a Data Lineage
  appendix (source SQL + execution log + row count per facet).
* :func:`render_enterprise_html` — inline chat markdown with a collapsed
  ``<details>`` lineage block.
"""
from app.services.enterprise_orchestrator.renderers.docx import (
    render_enterprise_docx,
)
from app.services.enterprise_orchestrator.renderers.html_chat import (
    render_enterprise_html,
)

__all__ = ["render_enterprise_docx", "render_enterprise_html"]
