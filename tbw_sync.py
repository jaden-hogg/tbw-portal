"""
TBW dashboard sync (hoggapp.tbwbrands.com).

TBW now submits orders in their own dashboard instead of this portal's New Order form.
Every 15 minutes this module:

1. Pulls orders changed since the last run (`GET /api/orders?updated_since=`).
   - A new order is created in ShipStation, its files are copied to Cloudinary (TBW's file
     links need the API token, so they won't open from ShipStation notes or the Production
     dashboard), the box labels are expanded, and then it is acknowledged to TBW, pushed to
     the Production dashboard and emailed to mugs@.
   - A cancelled order is cancelled in ShipStation if it hasn't shipped.
   - Any other change after we have the order is emailed to staff and not applied.
2. Reconciles shipments: whatever ShipStation has shipped (or voided) for these orders is
   posted to (or deleted from) the TBW order, with shipping cost = label cost x 1.2.

ShipStation is the source of truth for "do we already have this order": every order made
here carries `orderKey = tbw-api-<TBW id>`, and createorder upserts on orderKey, so even a
double run can't make a second order. The small state blob in Cloudinary only keeps the
`updated_since` cursor and what was last seen per order, to avoid repeat emails/API calls.

The functions take the app module as `portal` (ShipStation/Cloudinary helpers, the box
package rules, the production push) rather than importing it, so app.py can schedule this
without a circular import.

Dry run (reads TBW + ShipStation, writes nothing anywhere):
    python3 tbw_sync.py --dry-run [--labels-to DIR]
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta

import requests

import hogg_notify as notify
from box_labels import expand_box_labels

TBW_API_BASE  = os.environ.get("TBW_API_BASE", "https://hoggapp.tbwbrands.com/api").rstrip("/")
TBW_API_TOKEN = os.environ.get("TBW_API_TOKEN", "")

ORDER_KEY_PREFIX = "tbw-api-"
COST_MARKUP = 1.2
# First poll ever (no saved cursor): TBW's dashboard went live in October 2026. Re-reading
# from here is harmless — anything already in ShipStation is recognised by orderKey.
DEFAULT_SINCE = "2026-10-01T00:00:00-07:00"
SHIPMENT_LOOKBACK_DAYS = 30

_STATE_PID = "tbw-portal/tbw_sync_state"


def log(msg: str) -> None:
    print(f"[tbw-sync] {msg}", flush=True)


# ── TBW API ───────────────────────────────────────────────────────────────────

class TBWError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"TBW API {status}: {message}")
        self.status = status


def tbw_request(method: str, path: str, **kwargs) -> requests.Response:
    for attempt in range(3):
        try:
            r = requests.request(
                method, f"{TBW_API_BASE}{path}",
                headers={"Authorization": f"Bearer {TBW_API_TOKEN}"},
                timeout=kwargs.pop("timeout", 60), **kwargs,
            )
        except requests.exceptions.RequestException:
            if attempt == 2:
                raise
            time.sleep(3)
            continue
        if r.status_code >= 500 and attempt < 2:
            time.sleep(3)
            continue
        if not r.ok:
            try:
                message = r.json().get("error", r.text[:300])
            except ValueError:
                message = r.text[:300]
            raise TBWError(r.status_code, message)
        return r
    raise RuntimeError("unreachable")


def tbw_orders_since(since: str) -> list[dict]:
    orders: list[dict] = []
    params: dict = {"updated_since": since, "limit": 200}
    while True:
        data = tbw_request("GET", "/orders", params=params).json()
        orders.extend(data.get("orders", []))
        if not data.get("next"):
            return orders
        params = {"cursor": data["next"], "limit": 200}


def tbw_order(order_id: int) -> dict:
    return tbw_request("GET", f"/orders/{order_id}").json()


def tbw_file(url: str) -> bytes:
    # File URLs are absolute and live on the same host, behind the same token.
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {TBW_API_TOKEN}"}, timeout=180)
        except requests.exceptions.RequestException:
            if attempt == 2:
                raise
            time.sleep(5)
            continue
        if r.status_code >= 500 and attempt < 2:
            time.sleep(5)
            continue
        if not r.ok:
            raise TBWError(r.status_code, f"file download failed: {url}")
        return r.content
    raise RuntimeError("unreachable")


# ── State (Cloudinary raw JSON, same pattern as invoice_state) ───────────────

_state_cache: dict | None = None


def load_state(portal) -> dict:
    global _state_cache
    if _state_cache is None:
        url = f"https://res.cloudinary.com/{portal.CLD_CLOUD}/raw/upload/{_STATE_PID}.json"
        try:
            r = requests.get(url, params={"t": int(time.time())}, timeout=15)
            _state_cache = r.json() if r.ok else {}
        except Exception:
            _state_cache = {}
    _state_cache.setdefault("since", DEFAULT_SINCE)
    _state_cache.setdefault("seen", {})       # TBW id -> updated_at last handled
    _state_cache.setdefault("alerted", {})    # TBW id -> signature of the last alert sent
    _state_cache.setdefault("sent", {})       # TBW id -> snapshot of the order as we created it
    _state_cache.setdefault("ship_sig", {})   # TBW id -> signature of the last shipment sync
    return _state_cache


def save_state(portal, state: dict) -> None:
    params = {"public_id": _STATE_PID, "overwrite": "true",
              "invalidate": "true", "timestamp": str(int(time.time()))}
    r = requests.post(
        f"https://api.cloudinary.com/v1_1/{portal.CLD_CLOUD}/raw/upload",
        data={**params, "api_key": portal.CLD_API_KEY, "signature": portal.cloudinary_sign(params)},
        files={"file": ("tbw_sync_state.json", json.dumps(state).encode())},
        timeout=30,
    )
    r.raise_for_status()


# ── ShipStation lookups ───────────────────────────────────────────────────────

def ss_orders_by_number(portal, order_number: str) -> list[dict]:
    """orderNumber is a prefix match in ShipStation v1, so filter to the exact number."""
    data = portal.ss_get("/orders", {"orderNumber": order_number, "pageSize": 100})
    return [o for o in data.get("orders", []) if o.get("orderNumber") == order_number]


def find_ss_order(portal, tbw: dict) -> dict | None:
    """The ShipStation order made from this TBW order, if any (matched on orderKey)."""
    key = f"{ORDER_KEY_PREFIX}{tbw['id']}"
    for number in candidate_numbers(tbw):
        for o in ss_orders_by_number(portal, number):
            if o.get("orderKey") == key:
                return o
    return None


def candidate_numbers(tbw: dict) -> list[str]:
    base = f"TBW-{tbw['po_number']}"
    if tbw.get("is_replacement"):
        return [f"{base}-REPLACEMENT", f"{base}-REPLACEMENT-{tbw['id']}"]
    return [base]


def is_api_order(ss_order: dict) -> bool:
    return (ss_order.get("orderKey") or "").startswith(ORDER_KEY_PREFIX)


# ── Building the ShipStation order ────────────────────────────────────────────

def ss_items(tbw: dict) -> list[dict]:
    """One ShipStation line per TBW line, keyed by the TBW line id (lineItemKey), so a
    shipment's items map straight back to TBW lines. Mugs become our TBW-11oz/TBW-15oz
    SKUs; a box-only replacement keeps the no-SKU "BOX ONLY" product the portal always
    used; everything else (tees, towels, totes...) keeps TBW's own SKU and name."""
    items = []
    for li in tbw["line_items"]:
        size = li.get("mug_size")
        item = {"lineItemKey": str(li["id"]), "quantity": li["quantity"], "unitPrice": 0.00}
        if size and li.get("box_only"):
            item["name"] = f"TBW {size} - BOX ONLY - {li['description']}"
        elif size:
            item["sku"] = f"TBW-{size}"
            item["name"] = li["description"]
        else:
            item["sku"] = li["sku"]
            item["name"] = li["description"]
        items.append(item)
    return items


def mug_counts(tbw: dict) -> tuple[int, int]:
    q11 = sum(li["quantity"] for li in tbw["line_items"] if li.get("mug_size") == "11oz")
    q15 = sum(li["quantity"] for li in tbw["line_items"] if li.get("mug_size") == "15oz")
    return q11, q15


def ss_address(tbw: dict) -> dict:
    st = tbw.get("ship_to") or {}
    store = (tbw.get("store_name") or "").strip()
    name = (st.get("name") or st.get("company") or store or "The Buffalo Works").strip()
    company = store if store and store != name else ""
    return {
        "name":        name,
        "company":     company,
        "street1":     st.get("address1") or "",
        "street2":     st.get("address2") or "",
        "city":        st.get("city") or "",
        "state":       st.get("state") or "",
        "postalCode":  st.get("postal_code") or "",
        "country":     st.get("country") or "US",
        "phone":       st.get("phone") or "",
        "residential": False,
    }


def unique_files(tbw: dict) -> list[dict]:
    """TBW sometimes attaches the same file twice (e.g. the PO); keep the first of each name."""
    seen, out = set(), []
    for f in tbw.get("files", []):
        if f["filename"] in seen:
            continue
        seen.add(f["filename"])
        out.append(f)
    return out


def order_payload(portal, tbw: dict, order_number: str, file_urls, warnings) -> dict:
    created = datetime.fromisoformat(tbw["created_at"]).astimezone(portal.ET)
    payload = {
        "orderNumber":    order_number,
        "orderKey":       f"{ORDER_KEY_PREFIX}{tbw['id']}",
        "orderDate":      created.strftime("%Y-%m-%dT%H:%M:%S.0000000"),
        "orderStatus":    "awaiting_shipment",
        "customerEmail":  portal.CUSTOMER_EMAIL,
        "billTo":         ss_address(tbw),
        "shipTo":         ss_address(tbw),
        "items":          ss_items(tbw),
        "amountPaid":     0.00,
        "taxAmount":      0.00,
        "shippingAmount": 0.00,
        "carrierCode":    "fedex",
        "serviceCode":    "fedex_ground",
        "internalNotes":  portal.build_notes(tbw["po_number"], file_urls, warnings, tbw.get("notes")),
    }
    if tbw.get("requested_ship_date"):
        payload["shipByDate"] = f"{tbw['requested_ship_date']}T00:00:00.0000000"
    q11, q15 = mug_counts(tbw)
    # Non-mug items have no weights yet; an order with no mugs is left for staff to weigh.
    if q11 or q15:
        payload.update(portal.build_package(q11, q15))
    return payload


def pick_order_number(portal, tbw: dict) -> str | None:
    """The number for a new ShipStation order. TBW-<PO> as always; a replacement gets
    -REPLACEMENT, and a second replacement for the same PO also gets the TBW id. Returns
    None when a regular PO's number is already taken by an order we didn't make."""
    for number in candidate_numbers(tbw):
        if not ss_orders_by_number(portal, number):
            return number
    return None


def copy_files(portal, tbw: dict, folder: str, dry_run: bool, labels_to: str | None):
    """Copy every file to Cloudinary, expanding the box labels first.
    Returns (file_urls [(name, url)], warnings)."""
    files = unique_files(tbw)
    warnings: list[str] = []
    labels = [f for f in files if f["type"] == "box_label"]
    if len(labels) > 1:
        warnings.append(f"{len(labels)} box label files — only the first was expanded")
    label = labels[0] if labels else None
    # Box labels are mug gift boxes, so match pages only against the mug lines.
    mug_lines = [
        {"sku": li["sku"], "description": li["description"], "qty": li["quantity"]}
        for li in tbw["line_items"] if li.get("mug_size")
    ]

    file_urls: list[tuple[str, str]] = []
    for f in files:
        data = tbw_file(f["url"])
        if label and f["id"] == label["id"] and mug_lines:
            try:
                data, pages, label_warnings = expand_box_labels(data, mug_lines)
                warnings.extend(label_warnings)
                if labels_to:
                    os.makedirs(labels_to, exist_ok=True)
                    with open(os.path.join(labels_to, f["filename"]), "wb") as fh:
                        fh.write(data)
                log(f"PO {tbw['po_number']}: box labels expanded to {pages} pages")
            except Exception as e:  # noqa: BLE001
                warnings.append(f"box label expansion failed, original kept: {e}")
        if dry_run:
            file_urls.append((f["filename"], f["url"]))
            continue
        try:
            file_urls.append((f["filename"], portal.cloudinary_upload(data, f["filename"], folder)))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{f['filename']} couldn't be copied to Cloudinary ({e}); "
                            "open it from the TBW dashboard")
            file_urls.append((f["filename"], f["url"]))
    return file_urls, warnings


# ── Staff email ───────────────────────────────────────────────────────────────

def alert(state: dict, tbw: dict, signature: str, subject: str, body: str, dry_run: bool) -> None:
    """Email staff once per distinct problem on an order (repeat polls stay quiet)."""
    key = str(tbw["id"])
    if state["alerted"].get(key) == signature:
        return
    if dry_run:
        log(f"would email: {subject}\n{body}")
        return
    notify.send("tbw.order_attention", subject=subject, text=body)
    state["alerted"][key] = signature


def received_email(order_number: str, tbw: dict, warnings: list[str]) -> None:
    addr = ss_address(tbw)
    lines = [f"Order: {order_number}", f"PO: {tbw['po_number']}",
             f"Store: {tbw.get('store_name') or ''}",
             f"Ship by: {tbw.get('requested_ship_date') or '-'}", ""]
    for item in ss_items(tbw):
        lines.append(f"{item['name']} ({item.get('sku') or 'no SKU'}):  {item['quantity']}")
    if tbw.get("notes"):
        lines += ["", "Notes:", tbw["notes"]]
    lines += ["", "Ship To:", *[p for p in (addr["name"], addr["company"], addr["street1"],
                                             addr["street2"]) if p],
              f"{addr['city']}, {addr['state']} {addr['postalCode']}"]
    if warnings:
        lines += ["", "Warnings:", *[f"- {w}" for w in warnings]]
    try:
        notify.send("tbw.order_received", subject=f"New TBW Order: {order_number}",
                    text="\n".join(lines))
    except Exception as e:  # noqa: BLE001
        log(f"order email failed for {order_number}: {e}")


def production_push(portal, order_number: str, tbw: dict, file_urls) -> None:
    def first(*types):
        return next((u for n, u in file_urls
                     for f in unique_files(tbw) if f["filename"] == n and f["type"] in types), None)

    items = ss_items(tbw)
    notes = f"PO {tbw['po_number']}"
    if tbw.get("notes"):
        notes += f"\n\n{tbw['notes']}"
    try:
        portal.post_to_production_dashboard({
            "source": "tbw",
            "source_ref": order_number,
            "customer_name": tbw.get("store_name") or "The Buffalo Works",
            "order_date": tbw.get("order_date") or date.today().isoformat(),
            "product_summary": ", ".join(f"{i['name']} x{i['quantity']}" for i in items),
            "line_items": [{"sku": i.get("sku"), "name": i["name"], "variant": None,
                            "quantity": i["quantity"]} for i in items],
            "notes": notes,
            "print_file_url": first("art_transfer", "print_file"),
            "mockup_url": first("thumbnail"),
            "external_status": "awaiting_shipment",
        })
    except Exception as e:  # noqa: BLE001
        log(f"production push failed for {order_number}: {e}")


# ── Orders ────────────────────────────────────────────────────────────────────

def cancel_in_shipstation(portal, ss: dict) -> None:
    order = portal.ss_get(f"/orders/{ss['orderId']}")
    order["orderStatus"] = "cancelled"
    portal.ss_post("/orders/createorder", order)


def snapshot(tbw: dict) -> dict:
    """The parts of a TBW order we copied into ShipStation, to spot later edits by TBW.
    Compared against TBW's own earlier version rather than the ShipStation order, so staff
    edits in ShipStation (or ShipStation's address clean-up) never read as a TBW change."""
    addr = ss_address(tbw)
    return {
        "lines": {str(li["id"]): [li["description"], li["sku"], li["quantity"]]
                  for li in tbw["line_items"]},
        "addr": {k: addr[k] for k in ("name", "street1", "street2", "city", "state", "postalCode")},
        "files": sorted(f["filename"] for f in unique_files(tbw)),
        "notes": tbw.get("notes") or "",
    }


def describe_changes(old: dict, new: dict) -> list[str]:
    changes: list[str] = []
    for key, (desc, sku, qty) in new["lines"].items():
        if key not in old["lines"]:
            changes.append(f"added: {desc} ({sku}) x{qty}")
        elif old["lines"][key][2] != qty:
            changes.append(f"quantity changed: {desc} {old['lines'][key][2]} -> {qty}")
    for key, (desc, _sku, qty) in old["lines"].items():
        if key not in new["lines"]:
            changes.append(f"removed: {desc} x{qty}")
    for field, value in new["addr"].items():
        if (old["addr"].get(field) or "") != (value or ""):
            changes.append(f"ship-to {field}: {old['addr'].get(field) or '-'} -> {value or '-'}")
    for name in sorted(set(new["files"]) - set(old["files"])):
        changes.append(f"new file: {name} (open it from the TBW dashboard)")
    if old["notes"] != new["notes"]:
        changes.append(f"notes now read: {new['notes'] or '(empty)'}")
    return changes


def handle_order(portal, state: dict, tbw: dict, dry_run: bool, labels_to: str | None) -> None:
    po = tbw["po_number"]
    if tbw.get("test"):
        log(f"PO {po}: test order, skipped")
        return
    ss = find_ss_order(portal, tbw)

    if tbw["status"] == "cancelled":
        if not ss or ss["orderStatus"] == "cancelled":
            return
        if ss["orderStatus"] == "shipped":
            alert(state, tbw, "cancelled-after-ship",
                  f"TBW cancelled {ss['orderNumber']} after it shipped",
                  f"TBW cancelled PO {po} in their dashboard, but {ss['orderNumber']} has already "
                  "shipped in ShipStation. Nothing was changed.", dry_run)
            return
        if dry_run:
            log(f"PO {po}: would cancel {ss['orderNumber']} in ShipStation")
            return
        cancel_in_shipstation(portal, ss)
        log(f"PO {po}: cancelled {ss['orderNumber']} in ShipStation")
        alert(state, tbw, "cancelled", f"TBW order cancelled: {ss['orderNumber']}",
              f"TBW cancelled PO {po}, so {ss['orderNumber']} was cancelled in ShipStation.",
              dry_run)
        return

    if ss:
        if not tbw.get("hogg_order_number"):
            if dry_run:
                log(f"PO {po}: would acknowledge {ss['orderNumber']} to TBW")
            else:
                tbw_request("POST", f"/orders/{tbw['id']}/ack",
                            json={"hogg_order_number": ss["orderNumber"]})
        key = str(tbw["id"])
        new = snapshot(tbw)
        old = state["sent"].get(key)
        changes = describe_changes(old, new) if old else []
        if changes and ss["orderStatus"] != "cancelled":
            sig = hashlib.sha1("\n".join(changes).encode()).hexdigest()[:12]
            alert(state, tbw, sig, f"TBW changed {ss['orderNumber']} after we received it",
                  f"TBW edited PO {po} in their dashboard after it was created in ShipStation "
                  f"({ss['orderStatus']}). These changes were NOT applied — update the "
                  "ShipStation order by hand if they're right:\n\n"
                  + "\n".join(f"- {c}" for c in changes), dry_run)
        if not dry_run:
            state["sent"][key] = new
        return

    if tbw["status"] == "shipped":
        log(f"PO {po}: already closed on TBW's side, not created")
        return

    order_number = pick_order_number(portal, tbw)
    if not order_number:
        alert(state, tbw, "number-taken", f"TBW order {po} not created: TBW-{po} already exists",
              f"PO {po} came in from the TBW dashboard, but ShipStation already has an order "
              f"TBW-{po} that this sync didn't make (probably from the old portal form). "
              "Nothing was created. Check whether it's the same order.", dry_run)
        return

    folder = f"TBW-Orders/{order_number.replace('TBW-', 'PO-', 1)}"
    file_urls, warnings = copy_files(portal, tbw, folder, dry_run, labels_to)
    payload = order_payload(portal, tbw, order_number, file_urls, warnings)

    if dry_run:
        items = "; ".join(f"{i.get('sku') or '(no sku)'} x{i['quantity']} {i['name'][:40]}"
                          for i in payload["items"])
        ship = payload["shipTo"]
        log(f"WOULD CREATE {order_number} | ship by {payload.get('shipByDate', '-')[:10]} | "
            f"{ship['name']} / {ship['company']}, {ship['city']} {ship['state']} | "
            f"weight {payload.get('weight', {}).get('value', 'none')} oz | {items}"
            + (f" | warnings: {warnings}" if warnings else ""))
        return

    created = portal.ss_post("/orders/createorder", payload)
    state["sent"][str(tbw["id"])] = snapshot(tbw)
    log(f"PO {po}: created {order_number} (ShipStation order {created.get('orderId')})")
    tbw_request("POST", f"/orders/{tbw['id']}/ack", json={"hogg_order_number": order_number})
    received_email(order_number, tbw, warnings)
    production_push(portal, order_number, tbw, file_urls)
    portal.invalidate_dashboard_cache()


def sync_orders(portal, state: dict, dry_run: bool, labels_to: str | None) -> None:
    orders = tbw_orders_since(state["since"])
    failed_at: list[str] = []
    for tbw in orders:
        key = str(tbw["id"])
        # updated_since is inclusive, so the last order of each run comes back once more.
        if state["seen"].get(key) == tbw["updated_at"]:
            continue
        try:
            handle_order(portal, state, tbw, dry_run, labels_to)
            state["seen"][key] = tbw["updated_at"]
        except Exception as e:  # noqa: BLE001
            failed_at.append(tbw["updated_at"])
            log(f"PO {tbw.get('po_number')}: FAILED {e}")
            try:
                alert(state, tbw, f"failed:{tbw['updated_at']}",
                      f"TBW order {tbw.get('po_number')} couldn't be synced",
                      f"The TBW dashboard sync hit an error on PO {tbw.get('po_number')} and "
                      f"will keep retrying every 15 minutes:\n\n{e}", dry_run)
            except Exception as e2:  # noqa: BLE001
                log(f"alert email failed too: {e2}")
    if orders:
        # Never move the cursor past an order that failed, so it's retried next run.
        times = failed_at or [o["updated_at"] for o in orders]
        pick = min if failed_at else max
        state["since"] = pick(times, key=lambda t: datetime.fromisoformat(t))


# ── Shipments ─────────────────────────────────────────────────────────────────

def ss_api_shipments(portal) -> dict[str, list[dict]]:
    """Recent TBW shipments in ShipStation (voided included), for orders this sync made,
    grouped by orderNumber."""
    start = (datetime.now(portal.ET) - timedelta(days=SHIPMENT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    by_order: dict[str, list[dict]] = {}
    page = 1
    while True:
        data = portal.ss_get("/shipments", {
            "orderNumber": "TBW", "shipDateStart": start, "includeShipmentItems": "true",
            "pageSize": 500, "page": page,
        })
        for s in data.get("shipments", []):
            if (s.get("orderKey") or "").startswith(ORDER_KEY_PREFIX):
                by_order.setdefault(s["orderNumber"], []).append(s)
        if page >= data.get("pages", 1):
            return by_order
        page += 1


def desired_shipments(tbw: dict, ss_shipments: list[dict]) -> list[dict]:
    """What TBW should have recorded, worked out from ShipStation alone. Each label that
    ships something not yet covered is its own TBW shipment, with its line quantities.
    A label that adds nothing new (an extra box of a multi-box shipment, or a label with no
    item detail after the order's already covered) is folded into the previous shipment:
    its tracking number is added and its cost summed."""
    remaining = {str(li["id"]): li["quantity"] for li in tbw["line_items"]}
    out: list[dict] = []
    live = sorted((s for s in ss_shipments if not s.get("voided") and s.get("trackingNumber")),
                  key=lambda s: s["shipmentId"])
    for s in live:
        cost = round(float(s.get("shipmentCost") or 0) * COST_MARKUP, 2)
        qty: dict[str, int] = {}
        for it in s.get("shipmentItems") or []:
            key = str(it.get("lineItemKey") or "")
            take = min(it.get("quantity") or 0, remaining.get(key, 0))
            if take > 0:
                qty[key] = qty.get(key, 0) + take
        if not s.get("shipmentItems") and not out:
            qty = {k: v for k, v in remaining.items() if v > 0}  # whole order in one label
        if qty:
            for k, v in qty.items():
                remaining[k] -= v
            out.append({
                "shipment_id": f"ss-{s['shipmentId']}",
                "ship_date": (s.get("shipDate") or "")[:10],
                "carrier": s.get("carrierCode") or "",
                "service": s.get("serviceCode") or "",
                "tracking_numbers": [s["trackingNumber"]],
                "shipping_cost": cost,
                "line_items": [{"id": int(k), "quantity": v} for k, v in qty.items()],
            })
        elif out:
            prev = out[-1]
            if s["trackingNumber"] not in prev["tracking_numbers"]:
                prev["tracking_numbers"].append(s["trackingNumber"])
            prev["shipping_cost"] = round(prev["shipping_cost"] + cost, 2)
    return out


def sync_shipments(portal, state: dict, dry_run: bool) -> None:
    for order_number, ss_shipments in ss_api_shipments(portal).items():
        tbw_id = int(ss_shipments[0]["orderKey"][len(ORDER_KEY_PREFIX):])
        sig = hashlib.sha1(json.dumps(
            sorted((s["shipmentId"], bool(s.get("voided")), s.get("trackingNumber"),
                    s.get("shipmentCost")) for s in ss_shipments)).encode()).hexdigest()[:12]
        if state["ship_sig"].get(str(tbw_id)) == sig:
            continue
        try:
            tbw = tbw_order(tbw_id)
            want = {d["shipment_id"]: d for d in desired_shipments(tbw, ss_shipments)}
            have = {s["shipment_id"]: s for s in tbw.get("shipments", [])
                    if str(s.get("shipment_id", "")).startswith("ss-")}
            for sid in have.keys() - want.keys():
                if dry_run:
                    log(f"{order_number}: would void shipment {sid} on TBW")
                    continue
                try:
                    tbw_request("DELETE", f"/orders/{tbw_id}/shipments/{sid}")
                    log(f"{order_number}: voided shipment {sid} on TBW")
                except TBWError as e:
                    if e.status == 409:  # already on a TBW invoice
                        alert(state, tbw, f"void-invoiced:{sid}",
                              f"Voided label on {order_number} is already on a TBW invoice",
                              f"Shipment {sid} on {order_number} was voided in ShipStation, but "
                              "TBW has already invoiced it, so it couldn't be removed. Ask TBW "
                              "to void that invoice; the sync will retry.", dry_run)
                    raise
            for sid, d in want.items():
                h = have.get(sid)
                if h and (sorted(h.get("tracking_numbers") or []) == sorted(d["tracking_numbers"])
                          and round(float(h.get("shipping_cost") or 0), 2) == d["shipping_cost"]
                          and h.get("ship_date") == d["ship_date"]):
                    continue
                if dry_run:
                    log(f"{order_number}: would post shipment {d}")
                    continue
                try:
                    tbw_request("POST", f"/orders/{tbw_id}/shipments", json=d)
                    log(f"{order_number}: posted shipment {sid} "
                        f"({', '.join(d['tracking_numbers'])}, ${d['shipping_cost']})")
                except TBWError as e:
                    if e.status in (409, 422):
                        alert(state, tbw, f"ship-post:{sid}:{e.status}",
                              f"Couldn't send tracking for {order_number} to TBW",
                              f"TBW refused shipment {sid} on {order_number}: {e}\n\n"
                              f"What we tried to send:\n{json.dumps(d, indent=1)}", dry_run)
                    raise
            if not dry_run:
                state["ship_sig"][str(tbw_id)] = sig
        except Exception as e:  # noqa: BLE001
            log(f"{order_number}: shipment sync FAILED {e}")


# ── Entry points ──────────────────────────────────────────────────────────────

_running = False


def sync_once(portal, dry_run: bool = False, labels_to: str | None = None) -> None:
    global _running
    if not TBW_API_TOKEN:
        log("TBW_API_TOKEN not set; skipped")
        return
    if _running:
        return
    _running = True
    try:
        state = load_state(portal)
        before = json.dumps(state, sort_keys=True)
        try:
            sync_orders(portal, state, dry_run, labels_to)
        except Exception as e:  # noqa: BLE001
            log(f"order sync FAILED {e}")
        try:
            sync_shipments(portal, state, dry_run)
        except Exception as e:  # noqa: BLE001
            log(f"shipment sync FAILED {e}")
        if not dry_run and json.dumps(state, sort_keys=True) != before:
            save_state(portal, state)
    finally:
        _running = False


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Run the TBW dashboard sync once.")
    ap.add_argument("--dry-run", action="store_true", help="read only; write nothing anywhere")
    ap.add_argument("--labels-to", help="with --dry-run, also expand box labels into this folder")
    args = ap.parse_args()
    if not args.dry_run:
        sys.exit("Only --dry-run is supported from the command line; the live sync runs on Railway.")
    # app.py needs these to import; they're unused here.
    os.environ.setdefault("FLASK_SECRET_KEY", "dry-run")
    os.environ.setdefault("PORTAL_PASSWORD", "dry-run")
    # app.py schedules a live sync when it sees the token; this module already read it above.
    os.environ.pop("TBW_API_TOKEN", None)
    import app as portal_module
    _state_cache = {}  # dry run starts from the default cursor, ignoring saved state
    sync_once(portal_module, dry_run=True, labels_to=args.labels_to)
