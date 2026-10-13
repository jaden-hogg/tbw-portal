# Hogg ↔ TBW Dashboard API — what Hogg needs

**Who does what.** Hogg's system pulls new orders from the TBW dashboard, creates them in
Hogg's own ShipStation, and posts shipments back. TBW's dashboard doesn't need a ShipStation
connection or ShipStation credentials. It only needs the endpoints below. Hogg checks for
new orders about every 15 minutes.

## Auth
- HTTPS only. A bearer token for Hogg, sent as `Authorization: Bearer <token>`. TBW can revoke and reissue it.
- File downloads accept the same token. Signed URLs that stay valid for at least 7 days also work.
- Errors come back as JSON `{"error": "..."}` with a real 4xx/5xx status code, never an HTML page with a 200.

## 1. List orders: `GET /api/orders?updated_since=<ISO 8601>`
Returns every order created *or changed* since that time, oldest first. Paginated with
`limit` plus a `next` cursor (or `page`). Hogg stores the latest `updated_at` it has seen
and asks again from there.

Also needed: `GET /api/orders/{id}`, which returns one order in the same shape.

### Order object
| Field | Notes |
|---|---|
| `id` | Stable and unique. Never reused. |
| `order_number` | Human-facing number. |
| `po_number` | |
| `status` | `submitted`, `cancelled`, `shipped` (TBW sets `shipped` once Hogg posts a shipment). |
| `created_at`, `updated_at` | ISO 8601 with time zone. Any edit, including a cancel, bumps `updated_at`. |
| `requested_ship_date` | Nullable, `YYYY-MM-DD`. |
| `is_replacement` | Boolean. When true, also send `replaces_order_id`. |
| `notes` | Free-text notes for Hogg. Nullable. |
| `test` | Boolean. Test orders are read but never shipped. Lets us test end to end safely. |
| `ship_to` | `name`, `company` (the destination shop), `address1`, `address2`, `city`, `state`, `postal_code`, `country` (2-letter ISO code), `phone`, `email` (optional). |
| `line_items` | See below. |
| `files` | See below. |

### Line item
| Field | Notes |
|---|---|
| `id` | Stable within the order. |
| `sku`, `description`, `quantity` | TBW's own SKU and name. Hogg keeps these as-is on everything except mugs. |
| `mug_size` | `"11oz"`, `"15oz"`, or `null` for non-mug items. Hogg maps mugs to its own SKUs from this field, so please send it explicitly rather than leaving Hogg to work it out from the SKU. |
| `box_only` | Boolean. True for a replacement box with no mug. |
| `unit_weight_oz` | Optional, for non-mug items (tees, towels, totes): the packed weight of one unit. |
| `box_label_page` | Optional, 1-based page number in the order's box label PDF that belongs to this line. If you have it, Hogg can skip its image-matching step. If not, leave it null and Hogg matches pages itself. |
| `print_file_url`, `thumbnail_url` | Optional, per line, if they exist per design. |

### File
`{ "type": "...", "filename": "...", "url": "...", "line_item_id": null }`
`type` is one of `purchase_order`, `packing_slip`, `thumbnail`, `art_transfer`, `box_label`,
`print_file`, `other`. `line_item_id` is optional, for when a file belongs to one line.

## 2. Acknowledge: `POST /api/orders/{id}/ack`
Body: `{ "hogg_order_number": "TBW-12345" }`. Sent once the order is in Hogg's
ShipStation, so the dashboard can show "Received by Hogg". Repeating the call is harmless.

## 3. Shipment: `POST /api/orders/{id}/shipments`
```json
{
  "shipment_id": "hogg-ss-987654",
  "ship_date": "2026-10-13",
  "carrier": "ups",
  "service": "UPS Ground",
  "tracking_numbers": ["1Z..."],
  "shipping_cost": 18.42
}
```
- `shipping_cost` is in USD and **already includes Hogg's markup**. TBW uses it as-is. Unit/print pricing stays on TBW's side.
- One order can have **more than one shipment** (split or partial). Each one has its own `shipment_id`.
- **Repeat-safe:** posting the same `shipment_id` again updates that shipment rather than adding a second one.

## 4. Void a shipment: `DELETE /api/orders/{id}/shipments/{shipment_id}`
Hogg sometimes voids a label and reprints it in a different box. The voided shipment and
its cost should disappear, and the replacement arrives as a new shipment.

## Changes after an order is acknowledged
Cancels and edits should bump `updated_at`. Hogg cancels in ShipStation if the order
hasn't shipped yet. Edits to line items or the address after acknowledgement go to a
person at Hogg to review rather than being applied automatically, so please also tell
Hogg directly when that happens.
