from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import wraps

import fitz
import requests
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


def fetch_shipment_info(order_id: int) -> tuple[int, str, str, float, str]:
    """Return (order_id, tracking, carrier, cost_with_markup, ship_date)."""
    try:
        data = ss_get("/shipments", {"orderId": order_id, "pageSize": 5})
        for s in data.get("shipments", []):
            if not s.get("voided") and s.get("trackingNumber"):
                raw_cost = float(s.get("shipmentCost") or 0)
                return (
                    order_id,
                    s.get("trackingNumber", ""),
                    s.get("carrierCode", ""),
                    round(raw_cost * 1.2, 2),
                    s.get("shipDate", ""),
                )
    except Exception:
        pass
    return order_id, "", "", 0.0, ""


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

    # Upload files to Cloudinary and parse address from any PO PDF
    folder = f"TBW-Orders/PO-{po_number}"
    file_urls: list[tuple[str, str]] = []  # (filename, url)
    address: dict = {}

    for f in files:
        if not f.filename:
            continue
        file_bytes = f.read()

        # Parse address from the purchase order PDF
        if not address and "purchase order" in f.filename.lower() and f.filename.lower().endswith(".pdf"):
            try:
                address = parse_address_from_pdf(file_bytes)
            except Exception:
                pass

        try:
            url = cloudinary_upload(file_bytes, f.filename, folder)
            file_urls.append((f.filename, url))
        except Exception as e:
            flash(f"Failed to upload {f.filename}: {e}", "danger")
            return redirect(url_for("index"))

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
        "file_urls":    file_urls,
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

    # Build notes with file links
    file_lines = " | ".join(
        f"[{name}] {url}" for name, url in parsed.get("file_urls", [])
    )
    notes = f"PO {po_number} | {file_lines}" if file_lines else f"PO {po_number}"

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
        "internalNotes":  notes,
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
    active: list[dict] = []
    archived: list[dict] = []
    try:
        data = ss_get("/orders", {
            "orderNumber": "TBW",
            "pageSize": 500,
            "sortBy": "OrderDate",
            "sortDir": "DESC",
        })
        orders = data.get("orders", [])

        shipped_ids = [o["orderId"] for o in orders if o["orderStatus"] == "shipped"]
        shipment_info: dict[int, tuple[str, str, float, str]] = {}
        if shipped_ids:
            with ThreadPoolExecutor(max_workers=10) as ex:
                futures = {ex.submit(fetch_shipment_info, oid): oid for oid in shipped_ids}
                for future in as_completed(futures):
                    oid, tn, carrier, cost, ship_date = future.result()
                    shipment_info[oid] = (tn, carrier, cost, ship_date)

        cutoff = datetime.now(timezone.utc) - timedelta(days=10)
        for order in orders:
            tn, carrier, cost, ship_date = shipment_info.get(order["orderId"], ("", "", 0.0, ""))
            order["_tracking"]  = tn
            order["_carrier"]   = carrier
            order["_cost"]      = cost
            order["_ship_date"] = ship_date[:10] if ship_date else ""

            if order["orderStatus"] == "shipped":
                # Prefer shipment ship_date, fall back to order modifyDate
                date_str = ship_date or order.get("modifyDate", "")
                try:
                    sd = datetime.fromisoformat(date_str[:19]).replace(tzinfo=timezone.utc)
                    if sd <= cutoff:
                        archived.append(order)
                        continue
                except Exception:
                    pass
            active.append(order)

    except Exception as e:
        flash(f"Could not load orders: {e}", "danger")

    return render_template("dashboard.html", active=active, archived=archived)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
