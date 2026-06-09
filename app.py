from __future__ import annotations

import hmac
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import wraps

import fitz
import requests
from flask import (
    Flask, flash, redirect, render_template,
    request, session, url_for,
)

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB

PORTAL_PASSWORD = os.environ["PORTAL_PASSWORD"]
SS_KEY          = os.environ["SHIPSTATION_V1_API_KEY"]
SS_SECRET       = os.environ["SHIPSTATION_V1_API_SECRET"]
SS_BASE         = "https://ssapi.shipstation.com"
CUSTOMER_EMAIL  = "tyler@thebuffaloworks.com"


# ── Auth ─────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── ShipStation helpers ───────────────────────────────────────────────────────

def ss_get(path: str, params: dict | None = None) -> dict:
    r = requests.get(
        f"{SS_BASE}{path}",
        auth=(SS_KEY, SS_SECRET),
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def ss_post(path: str, payload: dict) -> dict:
    for attempt in range(3):
        r = requests.post(
            f"{SS_BASE}{path}",
            auth=(SS_KEY, SS_SECRET),
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code == 429:
            wait = int(r.headers.get("X-Rate-Limit-Reset", 60)) + 2
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("ShipStation rate limit exceeded after 3 retries")


def fetch_tracking(order_id: int) -> tuple[int, str, str]:
    """Return (order_id, tracking_number, carrier_code) for a shipped order."""
    try:
        data = ss_get("/shipments", {"orderId": order_id, "pageSize": 5})
        for s in data.get("shipments", []):
            if not s.get("voided") and s.get("trackingNumber"):
                return order_id, s["trackingNumber"], s.get("carrierCode", "")
    except Exception:
        pass
    return order_id, "", ""


# ── PDF parsing ───────────────────────────────────────────────────────────────

def parse_po_pdf(pdf_bytes: bytes) -> dict:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = "\n".join(page.get_text() for page in doc)

    result: dict = {
        "po_number":    None,
        "ship_name":    None,
        "ship_street1": None,
        "ship_street2": None,
        "ship_city":    None,
        "ship_state":   None,
        "ship_zip":     None,
        "ship_country": "US",
        "qty_15oz":     0,
        "qty_11oz":     0,
    }

    # PO number — label appears before value in the PDF layout
    m = re.search(r'PO NUMBER:[^\d]*(\d+)', text)
    if m:
        result["po_number"] = m.group(1).strip()

    # Ship-to address block
    addr_match = re.search(
        r'(?:ship\s*to)\s*[:\n](.*?)(?:\n{2,}|\Z)',
        text, re.IGNORECASE | re.DOTALL,
    )
    if addr_match:
        block = addr_match.group(1)
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        addr_lines = []
        for line in lines:
            if re.match(r'^(?:Terms|Product ID|Description|Qty|Pric|Net \d+)', line, re.IGNORECASE):
                break
            addr_lines.append(line)
        if addr_lines:
            result["ship_name"] = addr_lines[0]
        if len(addr_lines) >= 2:
            result["ship_street1"] = addr_lines[1]
        for line in addr_lines[2:]:
            csz = re.match(r'^(.*?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$', line)
            if csz:
                result["ship_city"]  = csz.group(1).strip()
                result["ship_state"] = csz.group(2)
                result["ship_zip"]   = csz.group(3)
                break
            elif not result["ship_street2"] and not re.search(r'\d{5}', line):
                result["ship_street2"] = line

    # SKU quantities — handles "QUIP 11oz B&W: 12", "TBW 15oz WH: 48", "11oz B&W - 144"
    notes_match = re.search(
        r'additional\s+notes?\s*[:\n](.*?)(?:\n{2,}|\Z)',
        text, re.IGNORECASE | re.DOTALL,
    )
    search_area = notes_match.group(1) if notes_match else text

    def sum_qty(area: str, oz: int) -> int:
        return sum(
            int(m.group(1))
            for m in re.finditer(rf'\b{oz}\s*oz\b[^:\-\n]*[:\-]\s*(\d+)', area, re.IGNORECASE)
        )

    result["qty_15oz"] = sum_qty(search_area, 15)
    result["qty_11oz"] = sum_qty(search_area, 11)
    # Fall back to full text if notes section had nothing
    if result["qty_15oz"] == 0 and notes_match:
        result["qty_15oz"] = sum_qty(text, 15)
    if result["qty_11oz"] == 0 and notes_match:
        result["qty_11oz"] = sum_qty(text, 11)

    return result


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hmac.compare_digest(pw.encode(), PORTAL_PASSWORD.encode()):
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("Incorrect password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("upload.html")


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    f = request.files.get("po_file")
    if not f or not f.filename.lower().endswith(".pdf"):
        flash("Please upload a PDF file.", "danger")
        return redirect(url_for("index"))

    try:
        parsed = parse_po_pdf(f.read())
    except Exception as e:
        flash(f"Failed to parse PDF: {e}", "danger")
        return redirect(url_for("index"))

    if not parsed["po_number"]:
        flash("Could not find a PO number in the PDF.", "danger")
        return redirect(url_for("index"))

    if parsed["qty_15oz"] == 0 and parsed["qty_11oz"] == 0:
        flash("Could not read SKU quantities from the PDF. Please contact support.", "danger")
        return redirect(url_for("index"))

    session["pending_order"] = parsed
    return render_template("preview.html", parsed=parsed)


@app.route("/confirm", methods=["POST"])
@login_required
def confirm():
    parsed = session.pop("pending_order", None)
    if not parsed:
        flash("Session expired — please re-upload the PO.", "warning")
        return redirect(url_for("index"))

    po_number = parsed["po_number"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000")

    items = []
    if parsed["qty_15oz"] > 0:
        items.append({
            "lineItemKey": "1", "sku": "TBW-15oz", "name": "TBW 15oz",
            "quantity": parsed["qty_15oz"], "unitPrice": 0.00,
        })
    if parsed["qty_11oz"] > 0:
        items.append({
            "lineItemKey": "2", "sku": "TBW-11oz", "name": "TBW 11oz",
            "quantity": parsed["qty_11oz"], "unitPrice": 0.00,
        })

    ship_to = {
        "name":        parsed["ship_name"]    or "The Buffalo Works",
        "street1":     parsed["ship_street1"] or "",
        "street2":     parsed["ship_street2"] or "",
        "city":        parsed["ship_city"]    or "",
        "state":       parsed["ship_state"]   or "",
        "postalCode":  parsed["ship_zip"]     or "",
        "country":     parsed["ship_country"] or "US",
        "residential": False,
    }

    payload = {
        "orderNumber":    f"TBW-{po_number}",
        "orderDate":      now,
        "orderStatus":    "awaiting_shipment",
        "customerEmail":  CUSTOMER_EMAIL,
        "billTo":         ship_to,
        "shipTo":         ship_to,
        "items":          items,
        "amountPaid":     0.00,
        "taxAmount":      0.00,
        "shippingAmount": 0.00,
        "internalNotes":  f"Submitted via TBW Portal | PO {po_number}",
    }

    try:
        result = ss_post("/orders/createorder", payload)
        flash(f"Order {result.get('orderNumber', 'TBW-' + po_number)} submitted successfully.", "success")
    except requests.HTTPError as e:
        flash(f"ShipStation error: {e.response.text[:300]}", "danger")
        return redirect(url_for("index"))
    except Exception as e:
        flash(str(e), "danger")
        return redirect(url_for("index"))

    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    orders = []
    try:
        data = ss_get("/orders", {
            "customerEmail": CUSTOMER_EMAIL,
            "pageSize": 100,
            "sortBy": "OrderDate",
            "sortDir": "DESC",
        })
        orders = data.get("orders", [])

        # Fetch tracking numbers for shipped orders in parallel
        shipped_ids = [o["orderId"] for o in orders if o["orderStatus"] == "shipped"]
        tracking: dict[int, tuple[str, str]] = {}
        if shipped_ids:
            with ThreadPoolExecutor(max_workers=10) as ex:
                futures = {ex.submit(fetch_tracking, oid): oid for oid in shipped_ids}
                for future in as_completed(futures):
                    oid, tn, carrier = future.result()
                    tracking[oid] = (tn, carrier)

        for order in orders:
            tn, carrier = tracking.get(order["orderId"], ("", ""))
            order["_tracking"] = tn
            order["_carrier"]  = carrier

    except Exception as e:
        flash(f"Could not load orders: {e}", "danger")

    return render_template("dashboard.html", orders=orders)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
