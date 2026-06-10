# TBW Order Portal

Web portal for **The Buffalo Works** (wholesale customer, Tyler) to submit purchase
orders. Replaces the old email-parsing workflow. Flask app deployed on Railway.

- **Repo**: GitHub `jaden-hogg/tbw-portal` (private)
- **Host**: Railway (auto-deploys on push to `main`)
- **URL**: `https://web-production-a69fb.up.railway.app` (rename in Railway → Settings → Networking)
- **Stack**: Flask + gunicorn (gthread), PyMuPDF, Cloudinary, ShipStation v1 API, Anthropic vision

## Flow
1. **Login** — single password (`PORTAL_PASSWORD`), session cookie.
2. **New Order form** — PO number, 15oz qty, 11oz qty, file attachments (single page; JS-driven).
3. **Preview** — sends *only the PO PDF* to `/parse_po` to read the ship-to address (no upload yet). Review shown inline.
4. **Confirm** — browser uploads all files **directly to Cloudinary** (signed, via `/sign_upload`), then POSTs metadata to `/submit`. Files never stream through the Flask worker (that was the cause of upload timeouts).
5. **Background thread** (`submit_order`): downloads the box label from Cloudinary → expands it → re-uploads → creates the ShipStation order with complete notes. The order only lands in ShipStation once everything is ready.
6. **Dashboard** — lists all `TBW-*` orders from ShipStation. Shows **Pending** (in-memory, pre-ShipStation) → **Awaiting Shipment** → **Shipped**. Auto-refreshes while anything is pending. Cancel button for unshipped orders; cancelled orders hidden. Orders shipped 10+ days ago collapse into an Archive section. Shipped rows show tracking (carrier-linked, number plain-text for copy/paste) and cost = label cost × 1.2.

## Box label expansion (the core feature)
The customer orders one SKU per *design* (each a distinct mug phrase); we fulfill as
rolled-up TBW-11oz / TBW-15oz. The box label PDF has one image per design (no text).
`box_labels.py`:
- Parses PO line items (SKU, description, qty) from the PO PDF.
- Renders each label page, sends it to Claude **vision** with the numbered PO descriptions, gets back the matching index → that design's qty.
- Duplicates each page to its qty into one print-ready PDF.
- **Must save with `tobytes(garbage=4, deflate=True, clean=True)`** to dedupe shared images — without it the file balloons (~20 MB for 162 pages) and exceeds Cloudinary's 10 MB image limit. **Requires PyMuPDF ≥ 1.26** — 1.24.x does not dedupe.
- Matching warnings (unmatched pages/lines, total failure) are written into the ShipStation order notes, not shown to the customer.

## Invoices tab (`invoice.py` + routes in `app.py`)
Weekly invoice tracker for The Buffalo Works, reconstructed from ShipStation.
- **One invoice per week**, Saturday–Friday, bucketed by **order date** (not ship date) with a **Friday-noon-ET cutoff**: orders placed after noon ET on Friday roll to the following week. The current week only becomes the active invoice once Friday noon ET passes.
- ShipStation account is **Eastern time**; portal-created orders stamp `orderDate` in ET so the cutoff is accurate.
- **Numbering**: sequential over weeks-that-have-orders, starting `FIRST_INVOICE_FRIDAY = 2026-01-23` (#1). Weeks with no orders are skipped (no number). Prices hardcoded: 11oz $3.50, 15oz $4.00; shipping = label cost × 1.2; total = subtotal + shipping.
- **Hardcoded reconciliation** (`_COMBINED_INTO`): weeks 3/20 and 3/27 fold into the 4/03 invoice (#6) because they were invoiced together — this lands 6/05 on #15 to match the real records. Add to that dict to merge more weeks.
- **Display**: most recent 4 invoices active, the rest in a collapsible Archive. Invoice # is read-only. Status dropdown (color-coded: amber Ready / blue Submitted / green Received) auto-saves on change. Default: most recent = Ready, all older = Received. Per-week **PDF** download generates the branded HOGG invoice (`invoice.py`, reportlab, logo in `static/hogg_logo.png`).
- **Durable state** in Cloudinary raw JSON (`tbw-portal/invoice_state`, read via stable `.json` URL, in-memory cached): per-week `{status, number, total, rows, final}`.
- **Auto-finalize**: an APScheduler job (in-process on Railway, `--workers 1`) fires **Fri 12:00 ET** and freezes the just-closed week (number/total/line items) into the store so a sent invoice can't shift if orders are edited later. Idempotent, 1h misfire grace; if missed, the week still computes live.

## ShipStation notes format
`PO <number>` + a `FILES` section (filename + Cloudinary URL per line) + a `NOTES`
section only if there were box-label warnings. The box-label link points to the
expanded PDF.

## Environment variables (set in Railway → Variables, NOT Keychain)
| Variable | Purpose |
|---|---|
| `PORTAL_PASSWORD` | dashboard login |
| `FLASK_SECRET_KEY` | session signing (`python3 -c "import secrets; print(secrets.token_hex(32))"`) |
| `SHIPSTATION_V1_API_KEY` / `SHIPSTATION_V1_API_SECRET` | ShipStation |
| `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | file storage |
| `ANTHROPIC_API_KEY` | box-label vision matching |
| `MATCH_MODEL` | vision model (set to `claude-haiku-4-5` — cheap, accurate for bold phrases) |

Cloudinary folder per order: `TBW-Orders/PO-<number>/`.

## Known limitations
- **Pending state is in-memory.** If Railway restarts in the ~20s window between Confirm and the order landing in ShipStation, that order is lost (customer saw "received" but it never lands). Acceptable for low volume; would need a durable queue to fix.
- Gunicorn runs `--workers 1` so the in-memory pending store + dashboard cache stay consistent; `--threads 8` keeps it responsive.
- Cloudinary free tier: 10 MB max image file size (why dedup matters).

## Conventions specific to this project
- Start command is forced via `railway.json` (Railway was ignoring Procfile changes). To change gunicorn flags, edit `railway.json` `deploy.startCommand`. A Custom Start Command set in the Railway dashboard overrides `railway.json`.
- Files upload browser → Cloudinary directly; the server only ever handles small metadata + small PDF downloads (PO, box label). Never route large uploads through the Flask worker.
