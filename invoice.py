"""
Invoice PDF generation for The Buffalo Works.

Replicates the HOGG invoice template: logo, bill-to block, a per-PO line table
(Purchase Order # / Qty / Price / Sub-Total / Shipping / Order Total) with a
totals row, and the ACH/wire payment footer.
"""

from __future__ import annotations

import os
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.lib.styles import ParagraphStyle

NAVY = colors.HexColor("#1f3a5f")
LOGO_PATH = os.path.join(os.path.dirname(__file__), "static", "hogg_logo.png")

BILL_TO = [
    "ATTN: Joanne Olds",
    "The Buffalo Works",
    "7900 Excelsior Blvd",
    "Hopkins MN, 55343",
]

FOOTER_LINES = [
    "Full Payment is required upfront by ACH or Wire.",
    "Account Name: Hogg Outfitters, LLC",
    "Bank Address: 270 Park Ave, New York, NY 10172",
    "Bank ACH ABA Number: 021202337",
    "Bank Wire ABA Number: 021000021",
    "Bank Account Number: 132853091",
    "Please be sure to include your company name and invoice number in the memo field.",
]


def _money(v: float) -> str:
    return f"${v:,.2f}"


def generate_invoice_pdf(invoice_no: str, week_ended: str, total_label: str, rows: list[dict]) -> bytes:
    """
    rows: [{po, qty, price, subtotal, shipping, total}], amounts as floats.
    week_ended: display string, e.g. "June 5th, 2026".
    total_label: totals-row label, e.g. "06/05/2026 Total".
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
    )

    styles = {
        "right":  ParagraphStyle("right", fontName="Helvetica", fontSize=9, alignment=2, leading=12),
        "right_b":ParagraphStyle("right_b", fontName="Helvetica-Bold", fontSize=20, alignment=2, textColor=NAVY, leading=26),
        "right_s":ParagraphStyle("right_s", fontName="Helvetica", fontSize=9, alignment=2, textColor=colors.grey),
        "small":  ParagraphStyle("small", fontName="Helvetica", fontSize=9, leading=12),
        "foot":   ParagraphStyle("foot", fontName="Helvetica", fontSize=8, alignment=2, leading=11),
        "foot_b": ParagraphStyle("foot_b", fontName="Helvetica-Bold", fontSize=8, alignment=2, leading=11),
    }

    elements: list = []

    # ── Header: logo (left) + company/invoice (right) ──
    company = (
        '<font name="Helvetica-Bold" size="13">HOGG OUTFITTERS, LLC</font><br/>'
        '5820 W Fuqua St<br/>Houston TX 77085'
    )
    logo = Image(LOGO_PATH, width=2.6 * inch, height=0.82 * inch)
    header = Table(
        [[logo, Paragraph(company, styles["right"])]],
        colWidths=[3.4 * inch, 3.9 * inch],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(header)
    elements.append(Spacer(1, 14))

    elements.append(Paragraph("INVOICE", styles["right_b"]))
    elements.append(Spacer(1, 4))
    elements.append(Paragraph(f"INVOICE # {invoice_no}", styles["right_s"]))
    elements.append(Spacer(1, 24))

    # ── Bill-to + week ──
    elements.append(Paragraph("<br/>".join(BILL_TO), styles["small"]))
    elements.append(Spacer(1, 16))
    elements.append(Paragraph(f"Week ended {week_ended}", styles["small"]))
    elements.append(Paragraph("Due upon receipt", styles["small"]))
    elements.append(Spacer(1, 20))

    # ── Line items table ──
    head = ["Purchase Order #", "Qty", "Price", "Sub - Total", "Shipping", "Order Total"]
    data = [head]
    sub_sum = ship_sum = total_sum = 0.0
    for r in rows:
        data.append([
            str(r["po"]),
            str(r["qty"]),
            _money(r["price"]) if r.get("price") else "",
            _money(r["subtotal"]),
            _money(r["shipping"]),
            _money(r["total"]),
        ])
        sub_sum   += r["subtotal"]
        ship_sum  += r["shipping"]
        total_sum += r["total"]

    data.append([
        total_label,
        "", "", _money(sub_sum), _money(ship_sum), _money(total_sum),
    ])

    col_widths = [2.7 * inch, 0.6 * inch, 0.7 * inch, 1.0 * inch, 0.9 * inch, 1.2 * inch]
    table = Table(data, colWidths=col_widths)
    table.setStyle(TableStyle([
        # header row
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("ALIGN", (0, 0), (0, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 10),
        # body
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("GRID", (0, 1), (-1, -1), 0.5, colors.HexColor("#444444")),
        ("TOPPADDING", (0, 1), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
        # totals row
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (0, -1), (0, -1), "LEFT"),
    ]))
    elements.append(table)

    # Footer is drawn at a fixed position near the page bottom (so it never
    # overflows regardless of how many line items there are).
    def _footer(canvas, _doc):
        canvas.saveState()
        x = letter[0] - 0.6 * inch  # right margin
        y = 1.05 * inch
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawRightString(x, y, FOOTER_LINES[0])
        canvas.setFont("Helvetica", 8)
        for line in FOOTER_LINES[1:]:
            y -= 0.16 * inch
            canvas.drawRightString(x, y, line)
        canvas.restoreState()

    doc.build(elements, onFirstPage=_footer)
    return buf.getvalue()
