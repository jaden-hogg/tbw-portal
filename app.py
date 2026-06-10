from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import wraps

import fitz
import requests
from box_labels import expand_box_labels, parse_po_line_items
from flask import (
    Flask, flash, redirect, render_template,
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

def cloudinary_upload(file_bytes: bytes, filename: str, folder: str) -> str:
    """Upload a file to Cloudinary and return its secure URL."""
    timestamp = str(int(time.time()))
    # Only sign params that Cloudinary includes in signature verification
    params = {"folder": folder, "timestamp": timestamp}
    sig_str = "&".join(f"{k}={v}" for k, v in sorted(params.items())) + CLD_API_SECRET
    signature = hashlib.sha256(sig_str.encode()).hexdigest()

    resp = requests.post(
        CLD_UPLOAD_URL,
        data={**params, "api_key": CLD_API_KEY, "signature": signature},
        files={"file": (filename, file_bytes)},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["secure_url"]


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


@app.route("/")
@login_required
def index():
    return render_template("upload.html")


def build_notes(po_number: str, file_urls: list[tuple[str, str]], warnings: list[str]) -> str:
    notes = f"PO {po_number}"
    if warnings:
        notes += "\n\nBox label notes:\n" + "\n".join(f"- {w}" for w in warnings)
    if file_urls:
        notes += "\n\n" + "\n".join(f"{name}:\n{url}" for name, url in file_urls)
    return notes


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
    box_label_name = parsed.get("box_label_name")
    po_bytes = parsed.get("po_bytes")
    warnings: list[str] = []
    file_urls: list[tuple[str, str]] = []

    try:
        # 1. Upload files (expanding the box label PDF first)
        for name, data in parsed["all_files"]:
            upload_bytes = data
            if name == box_label_name and po_bytes:
                try:
                    line_items = parse_po_line_items(po_bytes)
                    upload_bytes, _total, warnings = expand_box_labels(data, line_items)
                except Exception as e:  # noqa: BLE001
                    upload_bytes = data  # fall back to original labels
                    warnings = [f"expansion failed, original labels kept: {e}"]
            url = cloudinary_upload(upload_bytes, name, folder)
            file_urls.append((name, url))

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


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    po_number = request.form.get("po_number", "").strip()
    qty_15oz  = request.form.get("qty_15oz", "0").strip()
    qty_11oz  = request.form.get("qty_11oz", "0").strip()
    files     = request.files.getlist("files")

    if not po_number:
        flash("PO number is required.", "danger")
        return redirect(url_for("index"))

    try:
        qty_15oz = int(qty_15oz) if qty_15oz else 0
        qty_11oz = int(qty_11oz) if qty_11oz else 0
    except ValueError:
        flash("Quantities must be whole numbers.", "danger")
        return redirect(url_for("index"))

    if qty_15oz == 0 and qty_11oz == 0:
        flash("Enter a quantity for at least one SKU.", "danger")
        return redirect(url_for("index"))

    if not files or all(f.filename == "" for f in files):
        flash("Please attach at least one file.", "danger")
        return redirect(url_for("index"))

    # Read all files into memory first
    files_data: list[list] = [[f.filename, f.read()] for f in files if f.filename]

    def _find(keyword: str) -> bytes | None:
        return next(
            (b for n, b in files_data
             if keyword in n.lower() and n.lower().endswith(".pdf")),
            None,
        )

    # Parse ship-to address from the purchase order PDF
    address: dict = {}
    po_bytes = _find("purchase order")
    if po_bytes:
        try:
            address = parse_address_from_pdf(po_bytes)
        except Exception:
            pass

    # Identify the box label (expanded in the background after confirm)
    box_label_name: str | None = None
    if po_bytes:
        for name, _ in files_data:
            if "box label" in name.lower() and name.lower().endswith(".pdf"):
                box_label_name = name
                break

    # No Cloudinary uploads here — everything is uploaded in the background after
    # confirm so the customer only waits on the file transfer itself.
    token = str(uuid.uuid4())
    _pending_orders[token] = {
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
        "all_files":    [(n, d) for n, d in files_data],
        "file_names":   [n for n, _ in files_data],
        "po_bytes":     po_bytes if box_label_name else None,
        "box_label_name": box_label_name,
    }
    session["order_token"] = token
    return render_template("preview.html", parsed=_pending_orders[token])


@app.route("/confirm", methods=["POST"])
@login_required
def confirm():
    token = session.pop("order_token", None)
    parsed = _pending_orders.pop(token, None) if token else None
    if not parsed:
        flash("Session expired — please re-upload.", "warning")
        return redirect(url_for("index"))

    po_number = parsed["po_number"]
    order_number = f"TBW-{po_number}"

    # Register as Pending and process in the background: uploads + box-label
    # expansion run first, then the order is created in ShipStation. The order
    # only appears in ShipStation once everything is ready.
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

    flash(f"Order {order_number} received — processing files and submitting to ShipStation.", "success")
    return redirect(url_for("dashboard"))


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
