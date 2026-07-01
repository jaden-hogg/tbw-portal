"""
Box label expansion.

The customer orders one SKU per design (e.g. QUIP-MUG831-BLK, qty 12), but we
fulfill as a single rolled-up SKU per size. The box label PDF has one image per
design with no text, so we can't match by content directly — instead we render
each page, ask Claude vision to read the phrase printed on the mug, and match it
to the PO line-item descriptions. Each label page is then duplicated to match
that design's ordered quantity, producing a single print-ready PDF.
"""

from __future__ import annotations

import base64
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import anthropic
import fitz

# Per the claude-api skill default; override with MATCH_MODEL to cut cost
# (e.g. claude-haiku-4-5 is ~5x cheaper and reads short bold phrases fine).
MATCH_MODEL = os.environ.get("MATCH_MODEL", "claude-opus-4-8")

_client: anthropic.Anthropic | None = None


def _anthropic() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


def parse_po_line_items(pdf_bytes: bytes) -> list[dict]:
    """Extract [{sku, description, qty}] from a purchase order PDF, in document order."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    sku_re = re.compile(r'^[A-Z0-9]+-[A-Z0-9]+-[A-Z]+$')
    price_re = re.compile(r'^\d+\.\d{2}$')

    items: list[dict] = []
    i = 0
    while i < len(lines):
        if sku_re.match(lines[i]):
            sku = lines[i]
            j = i + 1
            desc: list[str] = []
            qty = None
            while j < len(lines):
                # qty is an integer immediately followed by a price line
                if re.match(r'^\d+$', lines[j]) and j + 1 < len(lines) and price_re.match(lines[j + 1]):
                    qty = int(lines[j])
                    break
                desc.append(lines[j])
                j += 1
            items.append({"sku": sku, "description": " ".join(desc), "qty": qty or 0})
            i = j + 2
        else:
            i += 1
    return items


def parse_manual_line_items(text: str) -> list[dict]:
    """Parse manually-typed replacement designs, one per line: '<qty> x <description>'.
    Used instead of parse_po_line_items when there's no PO PDF (replacement orders)."""
    items: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'^(\d+)\s*[x×,-]?\s*(.+)$', line, re.IGNORECASE)
        if m:
            items.append({"sku": "", "description": m.group(2).strip(), "qty": int(m.group(1))})
    return items


def _match_page(page_png: bytes, descriptions: list[str]) -> dict:
    """Ask Claude which description best matches the phrase on this label image."""
    img_b64 = base64.standard_b64encode(page_png).decode()
    numbered = "\n".join(f"{idx}: {d}" for idx, d in enumerate(descriptions))

    resp = _anthropic().messages.create(
        model=MATCH_MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "This image is a coffee mug / gift box design. Read the main phrase "
                        "printed on it. Then pick the single product description below whose "
                        "phrase matches it.\n\n"
                        f"Descriptions:\n{numbered}\n\n"
                        "Return the matching description's index and the phrase you read."
                    ),
                },
            ],
        }],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "phrase": {"type": "string"},
                    },
                    "required": ["index", "phrase"],
                    "additionalProperties": False,
                },
            },
        },
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def expand_box_labels(pdf_bytes: bytes, line_items: list[dict]) -> tuple[bytes, int, list[str]]:
    """
    Return (expanded_pdf_bytes, total_pages, warnings).

    Each page of the box label PDF is matched to a PO line item by reading the
    design phrase, then duplicated to that line item's quantity.
    """
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    descriptions = [li["description"] for li in line_items]
    warnings: list[str] = []

    if not descriptions:
        raise ValueError("No PO line items to match against")

    # Render every page to a PNG (low DPI is enough to read the phrase)
    pages_png = [src.load_page(i).get_pixmap(dpi=110).tobytes("png") for i in range(src.page_count)]

    # Match pages to descriptions in parallel
    def worker(args):
        i, png = args
        try:
            result = _match_page(png, descriptions)
            return i, result.get("index", -1), result.get("phrase", "")
        except Exception as e:  # noqa: BLE001
            return i, -1, f"error: {e}"

    matches: dict[int, int] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, index, info in ex.map(worker, list(enumerate(pages_png))):
            matches[i] = index
            if index < 0 and info:
                errors.append(info)

    matched_count = sum(1 for idx in matches.values() if 0 <= idx < len(line_items))

    # Total failure (e.g. missing API key) — return original, one concise warning
    if matched_count == 0:
        reason = errors[0] if errors else "no pages matched"
        return pdf_bytes, src.page_count, [
            f"box labels NOT expanded — matching failed ({reason})"
        ]

    # Build the expanded PDF
    out = fitz.open()
    used_indexes: list[int] = []
    for i in range(src.page_count):
        index = matches.get(i, -1)
        if 0 <= index < len(line_items):
            qty = line_items[index]["qty"] or 1
            used_indexes.append(index)
        else:
            qty = 1
            warnings.append(f"page {i + 1}: no match, printed 1 copy")
        for _ in range(qty):
            out.insert_pdf(src, from_page=i, to_page=i)

    # Flag PO lines that never got a label, or duplicate matches
    matched_set = set(used_indexes)
    unmatched = [li for idx, li in enumerate(line_items) if idx not in matched_set and li["qty"]]
    if unmatched:
        warnings.append(f"{len(unmatched)} PO line(s) had no matching label — check the result")
    if len(used_indexes) != len(set(used_indexes)):
        warnings.append("two or more label pages matched the same design — check the result")

    # Deduplicate shared image objects so N copies of a page don't bloat the file
    return out.tobytes(garbage=4, deflate=True, clean=True), out.page_count, warnings
