"""PDF rendering for the Stage 2 e-Stamp Master Agreement.

Reuses the `_Doc` cursor from invoice_pdf.py rather than a second copy of
the same pagination/font plumbing — invoices and agreements are different
documents, but "write a line, wrap it, move down, start a new page when
full" is identical machinery either way.

The PDF rendered here is what gets uploaded to Digio for signing (see
app/api/v1/contracts.py::_initiate_esign) and is exactly the text the
worker is shown on-screen before starting the signing session — never a
second, independently-formatted copy that could drift from what they
actually agreed to.
"""
from __future__ import annotations

import io

from app.core.company import get_company
from app.services.invoice_pdf import _BODY, _Doc, _TITLE, _wrap


def render_agreement_pdf(*, title: str, body_text: str) -> bytes:
    """Render `body_text` (the output of app.core.contracts.render_stage2)
    as a simple, readable A4 PDF: a title block, then the agreement text
    wrapped to the page width, paginating automatically via `_Doc`.
    """
    company = get_company()
    buf = io.BytesIO()
    doc = _Doc(buf)

    doc.rule(heavy=True)
    doc.centered(company.legal_name, bold=True, size=_TITLE)
    doc.centered(title, bold=True, size=_BODY)
    doc.rule(heavy=True)
    doc.text("")

    # 95 chars/line comfortably fits the Courier body font within the A4
    # margins _Doc already accounts for.
    for paragraph in body_text.split("\n"):
        if not paragraph.strip():
            doc.text("")
            continue
        for line in _wrap(paragraph, 95):
            doc.text(line)

    doc.c.showPage()
    doc.c.save()
    return buf.getvalue()
