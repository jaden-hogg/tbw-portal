from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import fitz
import requests
from box_labels import expand_box_labels, parse_po_line_items
from invoice import generate_invoice_pdf
from flask import (
    Flask, Response, flash, redirect, render_template,
    request, session, url_for,
)

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB for multi-file uploads

PORTAL_PASSWORD   = os.environ["PORTAL_PASSWORD"]
SS_KEY            = os.environ["SHIPSTATION_V1_API_KEY"]
SS_SECRET         = os.environ["SHIPSTATION_V1_API_SECRET"]
SS_BASE           = "https://ssapi.shipstation.com"
CUSTOMER_EMAIL    = "tyler@thebuffaloworks.com"
CLD_CLOUD         = os.environ["CLOUDINARY_CLOUD_NAME"]
CLD_API_KEY       = os.environ["CLOUDINARY_API_KEY"]
CLD_API_SECRET    = os.environ["CLOUDINARY_API_SECRET"]
CLD_UPLOAD_URL    = f"https://api.cloudinary.com/v1_1/{CLD_CLOUD}/auto/upload"

# Server-side store for pending orders (avoids 4KB cookie session limit)
_pending_orders: dict[str, dict] = {}

# Orders confirmed but not yet created in ShipStation (uploads/expansion running).
# Keyed by order number ("TBW-105641"); shown as "Pending" on the dashboard.
_submitting: dict[str, dict] = {}

# Short-lived dashboard cache; invalidated on order create/cancel
DASHBOARD_TTL = 120  # seconds
_dashboard_cache: dict = {"data": None, "ts": 0.0}


def invalidate_dashboard_cache() -> None:
    _dashboard_cache["data"] = None
    _dashboard_cache["ts"] = 0.0


# ── Auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Cloudinary ────────────────────────────────────────────────────────────────

def cloudinary_sign(params: dict) -> str:
    """SHA-256 signature over sorted params + api secret (Cloudinary scheme)."""
    sig_str = "&".join(f"{k}={v}" for k, v in sorted(params.items())) + CLD_API_SECRET
    return hashlib.sha256(sig_str.encode()).hexdigest()


def cloudinary_upload(file_bytes: bytes, filename: str, folder: str) -> str:
    """Server-side upload (used for the expanded box label). Returns secure URL.
    Retries on transient network/timeout errors."""
    last_err: Exception | None = None
    for _ in range(3):
        params = {"folder": folder, "timestamp": str(int(time.time()))}
        try:
            resp = requests.post(
                CLD_UPLOAD_URL,
                data={**params, "api_key": CLD_API_KEY, "signature": cloudinary_sign(params)},
                files={"file": (filename, file_bytes)},
                timeout=300,
            )
            if not resp.ok:
                raise RuntimeError(f"Cloudinary {resp.status_code}: {resp.text[:300]}")
            return resp.json()["secure_url"]
        except (requests.exceptions.RequestException, RuntimeError) as e:
            last_err = e
            time.sleep(2)
    raise last_err  # type: ignore[misc]


def cloudinary_download(url: str) -> bytes:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


# ── ShipStation ───────────────────────────────────────────────────────────────

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


def fetch_all_shipments() -> dict[str, dict]:
    """
    Fetch every TBW shipment in one paginated pass, keyed by orderNumber.
    Matching by orderNumber (not orderId) survives order re-imports where the
    orderId changes but the order number stays the same. Most recent non-voided
    shipment per order wins.
    """
    by_order: dict[str, dict] = {}
    page = 1
    while True:
        data = ss_get("/shipments", {
            "orderNumber": "TBW",
            "pageSize": 500,
            "page": page,
            "sortBy": "ShipDate",
            "sortDir": "DESC",
        })
        shipments = data.get("shipments", [])
        for s in shipments:
            if s.get("voided") or not s.get("trackingNumber"):
                continue
            num = s.get("orderNumber", "")
            if num and num not in by_order:  # DESC sort → first seen is most recent
                raw_cost = float(s.get("shipmentCost") or 0)
                by_order[num] = {
                    "tracking":  s.get("trackingNumber", ""),
                    "carrier":   s.get("carrierCode", ""),
                    "cost":      round(raw_cost * 1.2, 2),
                    "ship_date": s.get("shipDate", ""),
                }
        if page >= data.get("pages", 1):
            break
        page += 1
    return by_order


# ── PDF parsing (address only) ────────────────────────────────────────────────

def parse_address_from_pdf(pdf_bytes: bytes) -> dict:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = "\n".join(page.get_text() for page in doc)

    result: dict = {
        "ship_name":    None,
        "ship_street1": None,
        "ship_street2": None,
        "ship_city":    None,
        "ship_state":   None,
        "ship_zip":     None,
        "ship_country": "US",
    }

    addr_match = re.search(
        r'(?:ship\s*to)\s*[:\n](.*?)(?:\n{2,}|\Z)',
        text, re.IGNORECASE | re.DOTALL,
    )
    if not addr_match:
        return result

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


@app.route("/sign_upload", methods=["POST"])
@login_required
def sign_upload():
    """Return signed params so the browser can upload files straight to Cloudinary."""
    folder = (request.get_json(silent=True) or {}).get("folder", "TBW-Orders")
    timestamp = str(int(time.time()))
    params = {"folder": folder, "timestamp": timestamp}
    return {
        "url":       CLD_UPLOAD_URL,
        "api_key":   CLD_API_KEY,
        "timestamp": timestamp,
        "folder":    folder,
        "signature": cloudinary_sign(params),
    }


@app.route("/")
@login_required
def index():
    return render_template("upload.html")


def build_notes(po_number: str, file_urls: list[tuple[str, str]], warnings: list[str]) -> str:
    sections = [f"PO {po_number}"]
    if file_urls:
        sections.append("FILES\n\n" + "\n\n".join(f"{name}\n{url}" for name, url in file_urls))
    if warnings:
        sections.append("NOTES\n" + "\n".join(f"- {w}" for w in warnings))
    return "\n\n".join(sections)


def build_order_items(parsed: dict) -> list[dict]:
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
    return items


def submit_order(order_number: str, parsed: dict) -> None:
    """
    Background task run after confirm. Uploads every file to Cloudinary
    (expanding the box label first), then creates the ShipStation order with the
    complete notes. The order only lands in ShipStation once everything is ready.
    On success the pending entry is cleared; on failure it's marked failed.
    """
    folder = parsed["folder"]
    box_label = parsed.get("box_label")
    po_bytes = parsed.get("po_bytes")
    warnings: list[str] = []
    file_urls = list(parsed["file_urls"])  # already uploaded by the browser

    try:
        # 1. Expand the box label (download original, expand, re-upload), then
        #    swap its link in file_urls for the expanded version.
        if box_label and po_bytes:
            try:
                original = cloudinary_download(box_label["url"])
                line_items = parse_po_line_items(po_bytes)
                expanded, _total, warnings = expand_box_labels(original, line_items)
                new_url = cloudinary_upload(expanded, box_label["name"], folder)
                file_urls = [
                    (n, new_url if n == box_label["name"] else u)
                    for n, u in file_urls
                ]
            except Exception as e:  # noqa: BLE001
                warnings = [f"box label expansion failed, original kept: {e}"]

        # 2. Create the ShipStation order with complete notes
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000")
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
        ss_post("/orders/createorder", {
            "orderNumber":    order_number,
            "orderDate":      now,
            "orderStatus":    "awaiting_shipment",
            "customerEmail":  CUSTOMER_EMAIL,
            "billTo":         ship_to,
            "shipTo":         ship_to,
            "items":          build_order_items(parsed),
            "amountPaid":     0.00,
            "taxAmount":      0.00,
            "shippingAmount": 0.00,
            "internalNotes":  build_notes(parsed["po_number"], file_urls, warnings),
        })

        # 3. Success — drop the pending entry and refresh the dashboard
        _submitting.pop(order_number, None)
        invalidate_dashboard_cache()
    except Exception as e:  # noqa: BLE001
        entry = _submitting.get(order_number)
        if entry:
            entry["status"] = "failed"
            entry["error"] = str(e)


@app.route("/parse_po", methods=["POST"])
@login_required
def parse_po():
    """Parse the ship-to address from the PO PDF for the inline preview.
    Receives only the PO file (small) — nothing is uploaded to Cloudinary."""
    f = request.files.get("po")
    if not f:
        return {"address": {}}
    try:
        return {"address": parse_address_from_pdf(f.read())}
    except Exception:
        return {"address": {}}


@app.route("/submit", methods=["POST"])
@login_required
def submit():
    """Called after the browser uploads files to Cloudinary on confirm.
    Receives metadata only, then creates the order in the background."""
    data = request.get_json(silent=True) or {}
    po_number = (data.get("po_number") or "").strip()
    uploaded = data.get("files") or []  # [{"name","url"}]

    try:
        qty_15oz = int(data.get("qty_15oz") or 0)
        qty_11oz = int(data.get("qty_11oz") or 0)
    except (ValueError, TypeError):
        return {"error": "Quantities must be whole numbers."}, 400

    if not po_number:
        return {"error": "PO number is required."}, 400
    if qty_15oz == 0 and qty_11oz == 0:
        return {"error": "Enter a quantity for at least one SKU."}, 400
    if not uploaded:
        return {"error": "No files were uploaded."}, 400

    file_urls = [(f["name"], f["url"]) for f in uploaded]
    po_url = next(
        (f["url"] for f in uploaded
         if "purchase order" in f["name"].lower() and f["name"].lower().endswith(".pdf")),
        None,
    )
    address: dict = {}
    po_bytes = None
    if po_url:
        try:
            po_bytes = cloudinary_download(po_url)
            address = parse_address_from_pdf(po_bytes)
        except Exception:
            pass

    box_label = None
    if po_bytes:
        box_label = next(
            (f for f in uploaded
             if "box label" in f["name"].lower() and f["name"].lower().endswith(".pdf")),
            None,
        )

    parsed = {
        "po_number":    po_number,
        "qty_15oz":     qty_15oz,
        "qty_11oz":     qty_11oz,
        "ship_name":    address.get("ship_name"),
        "ship_street1": address.get("ship_street1"),
        "ship_street2": address.get("ship_street2"),
        "ship_city":    address.get("ship_city"),
        "ship_state":   address.get("ship_state"),
        "ship_zip":     address.get("ship_zip"),
        "ship_country": address.get("ship_country", "US"),
        "folder":       f"TBW-Orders/PO-{po_number}",
        "file_urls":    file_urls,
        "po_bytes":     po_bytes if box_label else None,
        "box_label":    box_label,
    }

    order_number = f"TBW-{po_number}"
    _submitting[order_number] = {
        "orderNumber": order_number,
        "orderDate":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "shipTo": {
            "name":  parsed["ship_name"] or "The Buffalo Works",
            "city":  parsed["ship_city"] or "",
            "state": parsed["ship_state"] or "",
        },
        "items":  build_order_items(parsed),
        "status": "pending",
        "error":  None,
    }
    threading.Thread(
        target=submit_order, args=(order_number, parsed), daemon=True
    ).start()

    flash(f"Order {order_number} received — submitting to ShipStation.", "success")
    return {"redirect": url_for("dashboard")}


@app.route("/cancel/<int:order_id>", methods=["POST"])
@login_required
def cancel(order_id: int):
    try:
        # Fetch the full order, flip status to cancelled, re-post (createorder upserts by orderId)
        order = ss_get(f"/orders/{order_id}")
        if order.get("orderStatus") in ("shipped", "cancelled"):
            flash(f"Order {order.get('orderNumber')} cannot be cancelled — already {order['orderStatus']}.", "warning")
            return redirect(url_for("dashboard"))
        order["orderStatus"] = "cancelled"
        ss_post("/orders/createorder", order)
        invalidate_dashboard_cache()
        flash(f"Order {order.get('orderNumber')} cancelled.", "success")
    except requests.HTTPError as e:
        flash(f"ShipStation error: {e.response.text[:300]}", "danger")
    except Exception as e:
        flash(str(e), "danger")
    return redirect(url_for("dashboard"))


def fetch_orders() -> list[dict]:
    data = ss_get("/orders", {
        "orderNumber": "TBW",
        "pageSize": 500,
        "sortBy": "OrderDate",
        "sortDir": "DESC",
    })
    return [o for o in data.get("orders", []) if o["orderStatus"] != "cancelled"]


def pending_rows(existing_numbers: set[str]) -> list[dict]:
    """Build display rows for orders confirmed but not yet in ShipStation."""
    rows: list[dict] = []
    for number, rec in list(_submitting.items()):
        if number in existing_numbers:
            _submitting.pop(number, None)  # already landed in ShipStation
            continue
        rows.append({
            "orderNumber": rec["orderNumber"],
            "orderDate":   rec["orderDate"],
            "shipTo":      rec["shipTo"],
            "items":       rec["items"],
            "orderStatus": "_failed" if rec.get("status") == "failed" else "_pending",
            "_error":      rec.get("error"),
            "_tracking":   "", "_carrier": "", "_cost": 0.0, "_ship_date": "",
        })
    return rows


@app.route("/dashboard")
@login_required
def dashboard():
    # ShipStation-derived rows are cached; pending rows are merged fresh below.
    if _dashboard_cache["data"] and (time.time() - _dashboard_cache["ts"]) < DASHBOARD_TTL:
        active, archived = _dashboard_cache["data"]
    else:
        active, archived = [], []
        try:
            # Orders and shipments are independent — fetch both at once
            with ThreadPoolExecutor(max_workers=2) as ex:
                orders_future    = ex.submit(fetch_orders)
                shipments_future = ex.submit(fetch_all_shipments)
                orders    = orders_future.result()
                shipments = shipments_future.result()

            cutoff = datetime.now(timezone.utc) - timedelta(days=10)
            for order in orders:
                info = shipments.get(order.get("orderNumber", ""), {})
                ship_date = info.get("ship_date", "")
                order["_tracking"]  = info.get("tracking", "")
                order["_carrier"]   = info.get("carrier", "")
                order["_cost"]      = info.get("cost", 0.0)
                order["_ship_date"] = ship_date[:10] if ship_date else ""

                if order["orderStatus"] == "shipped" and ship_date:
                    try:
                        sd = datetime.fromisoformat(ship_date[:19]).replace(tzinfo=timezone.utc)
                        if sd <= cutoff:
                            archived.append(order)
                            continue
                    except Exception:
                        pass
                active.append(order)

            _dashboard_cache["data"] = (active, archived)
            _dashboard_cache["ts"]   = time.time()
        except Exception as e:
            flash(f"Could not load orders: {e}", "danger")

    # Merge in pending/failed orders not yet visible in ShipStation
    existing = {o["orderNumber"] for o in active} | {o["orderNumber"] for o in archived}
    active = pending_rows(existing) + active

    return render_template("dashboard.html", active=active, archived=archived)


# ── Invoices ──────────────────────────────────────────────────────────────────

PRICE_11OZ = 3.50
PRICE_15OZ = 4.00

# Last invoice number used. Seeded at the current value (15); editable per-invoice.
# Resets on redeploy — the number field on the form is the source of truth.
_last_invoice_no = 15


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _week_display(d: date) -> str:
    return f"{d.strftime('%B')} {_ordinal(d.day)}, {d.year}"


def most_recent_friday(today: date | None = None) -> date:
    today = today or datetime.now(timezone.utc).date()
    return today - timedelta(days=(today.weekday() - 4) % 7)


def invoice_rows_for_week(week_end: date) -> list[dict]:
    """Pull shipped TBW orders in the 7 days ending week_end, pre-filled for invoicing."""
    week_start = week_end - timedelta(days=6)
    with ThreadPoolExecutor(max_workers=2) as ex:
        orders = ex.submit(fetch_orders).result()
        shipments = ex.submit(fetch_all_shipments).result()

    rows: list[dict] = []
    for o in orders:
        info = shipments.get(o.get("orderNumber", ""), {})
        sd = info.get("ship_date", "")
        if not sd:
            continue
        try:
            d = date.fromisoformat(sd[:10])
        except ValueError:
            continue
        if not (week_start <= d <= week_end):
            continue

        q11 = q15 = 0
        for it in o.get("items", []):
            sku = (it.get("sku") or "").lower()
            if "11oz" in sku:
                q11 += it.get("quantity", 0)
            elif "15oz" in sku:
                q15 += it.get("quantity", 0)

        subtotal = q11 * PRICE_11OZ + q15 * PRICE_15OZ
        shipping = round(info.get("cost", 0.0), 2)
        price = PRICE_11OZ if (q11 and not q15) else PRICE_15OZ if (q15 and not q11) else 0.0
        rows.append({
            "po":       o["orderNumber"].replace("TBW-", ""),
            "qty":      q11 + q15,
            "price":    price,
            "subtotal": round(subtotal, 2),
            "shipping": shipping,
            "total":    round(subtotal + shipping, 2),
        })

    rows.sort(key=lambda r: r["po"])
    return rows


@app.route("/invoices")
@login_required
def invoices():
    week_str = request.args.get("week")
    try:
        week_end = date.fromisoformat(week_str) if week_str else most_recent_friday()
    except ValueError:
        week_end = most_recent_friday()

    rows = []
    try:
        rows = invoice_rows_for_week(week_end)
    except Exception as e:
        flash(f"Could not load orders: {e}", "danger")

    return render_template(
        "invoices.html",
        rows=rows,
        week_value=week_end.isoformat(),
        week_display=_week_display(week_end),
        total_label=f"{week_end.strftime('%m/%d/%Y')} Total",
        invoice_no=_last_invoice_no + 1,
    )


@app.route("/invoices/generate", methods=["POST"])
@login_required
def invoices_generate():
    global _last_invoice_no
    invoice_no = request.form.get("invoice_no", "").strip()
    week_display = request.form.get("week_display", "").strip()
    total_label = request.form.get("total_label", "Total").strip()

    pos       = request.form.getlist("po")
    qtys      = request.form.getlist("qty")
    prices    = request.form.getlist("price")
    subtotals = request.form.getlist("subtotal")
    shippings = request.form.getlist("shipping")
    totals    = request.form.getlist("total")

    def _f(v):
        try:
            return float(str(v).replace("$", "").replace(",", "").strip() or 0)
        except ValueError:
            return 0.0

    rows = []
    for i in range(len(pos)):
        if not pos[i].strip():
            continue
        rows.append({
            "po":       pos[i].strip(),
            "qty":      qtys[i].strip() if i < len(qtys) else "",
            "price":    _f(prices[i]) if i < len(prices) else 0.0,
            "subtotal": _f(subtotals[i]) if i < len(subtotals) else 0.0,
            "shipping": _f(shippings[i]) if i < len(shippings) else 0.0,
            "total":    _f(totals[i]) if i < len(totals) else 0.0,
        })

    if not rows:
        flash("No line items to invoice.", "warning")
        return redirect(url_for("invoices"))

    pdf = generate_invoice_pdf(invoice_no, week_display, total_label, rows)

    try:
        _last_invoice_no = max(_last_invoice_no, int(invoice_no))
    except ValueError:
        pass

    return Response(
        pdf,
        mimetype="application/pdf",
        headers={
            "Content-Disposition":
                f'attachment; filename="Hogg Invoicing - The Buffalo Works - {invoice_no}.pdf"'
        },
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
