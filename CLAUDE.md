# TBW Order Portal

> See `NOTES.md` for why / incident root-causes / reverted approaches.

Web portal for **The Buffalo Works** (wholesale customer, Tyler) to submit purchase
orders. Replaces the old email-parsing workflow. Flask app deployed on Railway.

- **Repo**: GitHub `jaden-hogg/tbw-portal` (private) — **first project deployed off the Mac**
- **Host**: Railway (auto-deploys on push to `main`)
- **URL**: `https://web-production-a69fb.up.railway.app` (rename in Railway → Settings → Networking)
- **Stack**: Flask + gunicorn (gthread), PyMuPDF, Cloudinary, ShipStation v1 API, Anthropic vision

## Flow
1. **Login** — single password (`PORTAL_PASSWORD`), session cookie.
2. **New Order form** — PO number, 15oz qty, 11oz qty, file attachments (single page; JS-driven). A **Replacement Order** toggle swaps the qty fields for a mug/box breakdown — see "Replacement orders" below.
3. **Preview** — sends *only the PO PDF* to `/parse_po` to read the ship-to address (no upload yet). Review shown inline.
4. **Confirm** — browser uploads all files **directly to Cloudinary** (signed, via `/sign_upload`), then POSTs metadata to `/submit`. Files never stream through the Flask worker (that was the cause of upload timeouts).
5. **Background thread** (`submit_order`): downloads the box label from Cloudinary → expands it → re-uploads → creates the ShipStation order with complete notes. The order only lands in ShipStation once everything is ready.
6. **Dashboard** — lists all `TBW-*` orders from ShipStation. Shows **Pending** (in-memory, pre-ShipStation) → **Awaiting Shipment** → **Shipped**. Auto-refreshes while anything is pending. Cancel button for unshipped orders; cancelled orders hidden. Orders shipped 10+ days ago collapse into an Archive section. Shipped rows show tracking (carrier-linked, number plain-text for copy/paste) and cost = label cost × 1.2. The "Ship To" column shows the **destination shop/business name**, extracted from uploaded filenames (pattern: `{PO#} Purchase Order / Pack Slip / Thumbnail 4 {Business Name}.pdf`); falls back to `shipTo.name` if no match.

## Box label expansion (the core feature)
The customer orders one SKU per *design* (each a distinct mug phrase); we fulfill as
rolled-up TBW-11oz / TBW-15oz. The box label PDF has one image per design (no text).
`box_labels.py`:
- Parses PO line items (SKU, description, qty) from the PO PDF.
- Renders each label page, sends it to Claude **vision** with the numbered PO descriptions, gets back the matching index → that design's qty.
- Duplicates each page to its qty into one print-ready PDF.
- **Must save with `tobytes(garbage=4, deflate=True, clean=True)`** to dedupe shared images — without it the file balloons (~20 MB for 162 pages) and exceeds Cloudinary's 10 MB image limit. **Requires PyMuPDF ≥ 1.26** — 1.24.x does not dedupe.
- Matching warnings (unmatched pages/lines, total failure) are written into the ShipStation order notes, not shown to the customer.

## Replacement orders
A **Replacement Order** toggle on the New Order form (default off) covers Tyler requesting replacement mugs and/or boxes for a prior order.
- When on, the normal 15oz/11oz qty fields are replaced with four fields — 11oz Mug Qty, 11oz Box Qty, 15oz Mug Qty, 15oz Box Qty. There's no separate address or line-items field: Tyler re-attaches the same **Purchase Order PDF** (and Box Label PDF, if designs need matching) used for the original order, so address parsing and box-label expansion run through the exact same code path as a regular order (`parse_address_from_pdf` / `parse_po_line_items` / `expand_box_labels` in `submit_order`) — no replacement-specific parsing logic exists.
- PO Number is still required (ties the replacement back to the original order) and the resulting ShipStation `orderNumber` gets a `-REPLACEMENT` suffix (e.g. `TBW-105641-REPLACEMENT`), so it never collides with the original order number.
- Mug items post with the normal SKUs (`TBW-11oz`/`TBW-15oz`). Box-only items have **no ShipStation SKU** (none exists) — they're entered as a manual product named `TBW 11oz - BOX ONLY` / `TBW 15oz - BOX ONLY` with no `sku` field.
- Shipping weight/dimensions (`package_for` in `app.py`) combine mug + box qty per size and reuse the normal `build_package` thresholds.
- **Invoicing**: `is_replacement_order()` checks the `-REPLACEMENT` order-number suffix. Box-only items are detected by `"BOX ONLY"` in the item name (no `sku` to match) — they're counted in the row's `qty` (shipping is still billed on them via the real ShipStation shipment cost) but never priced, so `subtotal` stays mug-only. Mug items on a replacement order are billed at **50% of the normal price** (`price_mult = 0.5` in `invoice_rows_for_week` / `build_all_invoices`).

## Invoices tab (`invoice.py` + routes in `app.py`)
Weekly invoice tracker for The Buffalo Works, reconstructed from ShipStation.
- **One invoice per week**, Saturday–Friday, bucketed by **ship date** (`ship_friday()` in `app.py`, changed 2026-07-10 from order date — orders that don't ship until a later week now land on that later week's invoice instead of being billed with $0 shipping on the week they were placed). Orders with no ship date yet (unshipped) don't belong to any week — they simply show up once they ship, even weeks later.
- **A Friday ship date always rolls to the following week's invoice**, never its own (`ship_friday()`). ShipStation's `shipDate` is date-only (no time-of-day), so this applies uniformly to every Friday shipment regardless of what time it actually shipped — there's no data to distinguish "before the cutoff" from "after." Since this can push a same-day Friday shipment (e.g. `TBW-BLANKS` shipping today) into next week's bucket before that week's own natural Sat-Thu window has even started, `build_all_invoices()`'s internal computation window extends one week past `most_recent_friday()` so that bucket's total is captured (see `_week_visible` below for when it actually appears).
- The current week only becomes the active invoice once Friday noon ET passes (`most_recent_friday()`, a real-time gate on when books close — unrelated to any individual order's date).
- **`_week_visible()`**: a week's invoice row is hidden from the list/archive/PDF entirely until **8am ET on its own ending Friday**, even if ship-date bucketing has already populated its total (per the point above, the upcoming week's bucket can fill in before that week exists at all). Keeps a partial, still-changing upcoming invoice out of sight until it's actually ready to work — reveals same-day at 8am, ahead of the 12pm auto-finalize.
- ShipStation account is **Eastern time**; `shipDate` from the shipments API is treated as ET-naive, same as `orderDate` elsewhere in the app.
- **Numbering**: sequential over weeks-that-have-orders, starting `FIRST_INVOICE_FRIDAY = 2026-01-23` (#1). Weeks with no orders are skipped (no number). Prices hardcoded: 11oz $3.50, 15oz $4.00; shipping = label cost × 1.2; total = subtotal + shipping.
- **Hardcoded reconciliation** (`_COMBINED_INTO`): weeks 3/20 and 3/27 fold into the 4/03 invoice (#6) because they were invoiced together — this lands 6/05 on #15 to match the real records. Add to that dict to merge more weeks.
- **Manual merge, 2026-07-13** (data-only, not `_COMBINED_INTO`): #18 (week ending 6/26) and #19 (week ending 7/10) each absorb two orders that Friday-ship-date rollover had pushed one week later than they were actually invoiced — TBW-105713/105714 (shipped 6/26) folded into #18, and TBW-105755/TBW-BLANKS (shipped 7/10) folded into #19. Done by hand-writing frozen `final: true` rows straight into `invoice_state`'s `2026-06-26` and `2026-07-10` keys (no code path does this automatically); the `2026-07-03` key was deleted outright rather than left to recompute to $0. If this situation recurs, expect the same manual state edit rather than a `_COMBINED_INTO` entry, since the orders being folded in don't share the target week's native Friday.
- **Display**: most recent 4 invoices active, the rest in a collapsible Archive. Invoice # is read-only. Status dropdown (color-coded: amber Ready / blue Submitted / green Received) auto-saves on change. Default: most recent = Ready, all older = Received. Per-week **PDF** download generates the branded HOGG invoice (`invoice.py`, reportlab, logo in `static/hogg_logo.png`).
- **Durable state** in Cloudinary raw JSON (`tbw-portal/invoice_state`, read via stable `.json` URL, in-memory cached): per-week `{status, number, total, rows, final}`.
- **Auto-finalize**: an APScheduler job (in-process on Railway, `--workers 1`) fires **Fri 12:00 ET** and freezes the just-closed week (number/total/line items) into the store so a sent invoice can't shift if orders are edited later. Idempotent, 1h misfire grace; if missed, the week still computes live. A finalized week keeps its slot in the list even though its own orders are excluded from the live per-order total (see double-count guard below) — `build_all_invoices()` unions in any week already marked `final` in the state, otherwise it would vanish entirely once fully claimed.
- **Ship-date bucketing is the settled, permanent rule** (no longer transitional): every order buckets by its actual ship date, Sat–Thu landing on that week's Friday, a Friday ship date always rolling to the *following* week's invoice (`ship_friday()`). There's no Recompute/un-finalize escape hatch anymore — that button was only useful while the bucketing logic itself was still changing, and it's been removed now that it isn't.
- **Double-count guard** (`_claimed_pos()`): an order already locked into some *other* finalized week's frozen `rows` is excluded from every live (non-final) week's computation, keyed by PO number. Prevents an order from ever being counted twice across two finalized weeks (e.g. after a manual merge like the 6/26+7/03 → #18 fix).

## ShipStation order dimensions (`build_package` in `app.py`)
Box size and weight are set automatically at order creation based on qty.

**11oz** (12.6 oz each):
| Qty | Box (L×W×H inches) |
|-----|---------------------|
| ≤ 8 | 10×10×10 |
| ≤ 16 | 13×12×9 |
| ≤ 32 | 16×14×10 |
| ≤ 44 | 18×16×14 |
| > 44 | 18×16×14 + total weight — add packages manually for multi-shipment |

**15oz** (1.1 lb = 17.6 oz each):
| Qty | Box (L×W×H inches) |
|-----|---------------------|
| ≤ 8 | 10×10×10 |
| ≤ 14 | 13×12×9 |
| ≤ 36 | 18×16×14 |
| > 36 | 25×11×11 + total weight — build packages manually for multi-shipment |

Mixed 11oz + 15oz orders default to 18×16×14 with combined weight.

## ShipStation notes format
An optional `CUSTOMER NOTES` section (from the New Order form's free-text Notes field,
2026-07-16) at the very top, only if Tyler filled it in — then `PO <number>` + a `FILES`
section (filename + Cloudinary URL per line) + a `NOTES` section only if there were
box-label warnings. The box-label link points to the expanded PDF. Same field exists on
the Replacement Order flow (shared form fields below the qty inputs).

## Order notification email
When a ShipStation order is successfully created, an email is sent to `mugs@hoggoutfitters.com` (subject: `New TBW Order: TBW-XXXXX`) with the PO number, quantities, and ship-to address. Uses Gmail OAuth2 via the same raw-token pattern as the rest of the workspace. If Gmail credentials are missing, the notification silently skips (does not block order creation).

## Push to the Production Dashboard (2026-07-16)
Right after the ShipStation order lands (`submit_order()`, same success path as the notification email above), also POSTs the order into `custom-order-portal`'s production dashboard (`push_to_production_dashboard()`) — `source: "tbw"`, `source_ref` = the ShipStation `orderNumber`, customer name (via the same `_shop_from_text()` helper the dashboard's own "Ship To" column already uses), product summary from `build_order_items()`, PO number as notes, and the **already-expanded** box-label Cloudinary URL as `print_file_url` (found the same way `box_label` is detected elsewhere in this file — `"box label" in name.lower()` + `.pdf`). This is why the push happens from here rather than the dashboard re-parsing `internalNotes` later: this app already has the clean structured data in hand at this exact point.

Silently skips (no exception, no blocked order) if `PRODUCTION_PORTAL_URL`/`PRODUCTION_INGEST_TOKEN` aren't set — same "missing creds = no-op" convention as the Gmail notification just above. Auth is `X-Production-Token`, a token distinct from any other credential in this app.

**Extended (2026-07-16)** to also send a structured `line_items` list (`[{sku: null, name, variant: null, quantity}]` — one entry per `build_order_items()` line; no `sku` since `TBW-11oz`/`TBW-15oz` aren't in the dashboard's own product catalog) so the dashboard can show a real product-breakdown dropdown instead of parsing the flat text summary. `notes` now also includes the New Order form's free-text Customer Notes field (`parsed["customer_notes"]`) when Tyler filled it in, alongside the PO number — this is the "order notes" the dashboard surfaces for TBW rows.

## Environment variables (set in Railway → Variables)
| Variable | Purpose |
|---|---|
| `PORTAL_PASSWORD` | dashboard login — Railway only, not in Keychain |
| `FLASK_SECRET_KEY` | session signing (`python3 -c "import secrets; print(secrets.token_hex(32))"`) — Railway only |
| `SHIPSTATION_V1_API_KEY` / `SHIPSTATION_V1_API_SECRET` | ShipStation — also in Keychain (shared workspace credential) |
| `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | file storage — the `hogg-outfitters` account, also in Keychain under these exact names (shared with `image-pipeline`), so scripts run locally can read/write this app's Cloudinary-hosted `invoice_state` JSON directly without Railway CLI access |
| `PRODUCTION_PORTAL_URL` / `PRODUCTION_INGEST_TOKEN` | custom-order-portal's production dashboard push (optional — silently skipped if unset). `PRODUCTION_PORTAL_URL` must be the **bare Railway domain, no path** — the push code appends `/admin/production-orders` itself (see NOTES.md's 2026-07-22 incident for what happens if it isn't) |
| `ANTHROPIC_API_KEY` | box-label vision matching |
| `MATCH_MODEL` | vision model (set to `claude-haiku-4-5` — cheap, accurate for bold phrases) |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` | Gmail OAuth2 for order notifications |

Cloudinary folder per order: `TBW-Orders/PO-<number>/`.

## Known limitations
- **Pending state is in-memory.** If Railway restarts in the ~20s window between Confirm and the order landing in ShipStation, that order is lost (customer saw "received" but it never lands). Acceptable for low volume; would need a durable queue to fix.
- Gunicorn runs `--workers 1` so the in-memory pending store + dashboard cache stay consistent; `--threads 8` keeps it responsive.
- Cloudinary free tier: 10 MB max image file size (why dedup matters).

## Conventions specific to this project
- Start command is forced via `railway.json` (Railway was ignoring Procfile changes). To change gunicorn flags, edit `railway.json` `deploy.startCommand`. A Custom Start Command set in the Railway dashboard overrides `railway.json`.
- Files upload browser → Cloudinary directly; the server only ever handles small metadata + small PDF downloads (PO, box label). Never route large uploads through the Flask worker.
