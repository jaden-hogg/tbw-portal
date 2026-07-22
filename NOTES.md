# TBW Order Portal — Notes

Why / decisions / incident root-causes / reverted approaches. Not auto-loaded — see project CLAUDE.md for current-state reference.

## Invoice #19 data loss, 2026-07-22
`/invoices/save` (status dropdown) wrote `state[week_iso] = {"status": status}`,
replacing the entire week record instead of merging into it. This silently
wiped the manually-merged, frozen `final: true` record for 2026-07-10 (the
2026-07-13 fix that folded TBW-BLANKS/TBW-105755 into invoice #19 — see
CLAUDE.md's Invoices tab section) down to just `{"status": "submitted"}`.
Once the frozen rows were gone, those two orders fell back to live
computation under the standard Friday-rollover rule (`ship_friday()`) — since
they shipped *on* Friday 7/10, that correctly rolled them to the 7/17 bucket,
bumping them onto invoice #20 instead of #19 where they'd actually been
invoiced. This is what looked like "invoices 19 and 20 changed."

The Friday-rollover rule itself was never the bug — it behaved exactly as
designed once its override was gone. Fixed by (1) restoring the frozen
2026-07-10 record (BLANKS + 105755 merged back in, total $675.70) and (2)
changing `/invoices/save` to spread the existing record before setting
`status`, so a status change can never again drop `final`/`number`/`total`/`rows`.

If a similar "a frozen/manually-merged week reverted to live numbers"
symptom recurs, check whether `/invoices/save` (or any other write path to
`invoice_state`) clobbered the record before assuming the bucketing logic
changed.

## Production dashboard push silently broken, 2026-07-22
`PRODUCTION_PORTAL_URL` in Railway Variables was set to
`https://custom-order-portal-production.up.railway.app/admin/production` —
already including the `/admin/production` path. `push_to_production_dashboard()`
builds the request as `f"{PRODUCTION_PORTAL_URL}/admin/production-orders"`, so
every push actually hit
`.../admin/production/admin/production-orders` — not a real route, 404.
`requests.post()` doesn't raise on a non-2xx response on its own, and this
function never called `.raise_for_status()` or checked `resp.status_code`, so
every single TBW push silently no-op'd with nothing in the logs to point at
it. Confirmed live: TBW-105766 through TBW-105769 (at least) never created a
row in custom-order-portal at all — not "stale," genuinely never pushed.

Fixed two ways: (1) corrected the Railway variable to the bare domain, no
path, and (2) added `resp.raise_for_status()` after the push so a future
misconfiguration (wrong URL, rotated/wrong token, etc.) shows up as a real,
logged exception instead of a silent gap. The four missing orders were
manually backfilled into custom-order-portal via its ingest endpoint,
reconstructing customer_name/line_items/notes/print_file_url from each
order's real ShipStation `internalNotes`/items (customer_name via the same
`_shop_from_text()` filename-parsing logic this app already uses).

If a "some pusher's rows just aren't showing up in custom-order-portal, no
errors" symptom recurs anywhere in the workspace (not just this project),
check the exact `PRODUCTION_PORTAL_URL` value for a baked-in path first —
per this app's own CLAUDE.md, it must be the bare domain, no trailing
path/slash, since every pusher appends its own path.

## Production dashboard push had the wrong file mapping, 2026-07-22
Found while backfilling the four orders above and comparing against what
actually landed: `push_to_production_dashboard()` sent the **Box Label**
Cloudinary URL as `print_file_url` (matched via `"box label" in name.lower()`),
never sent `mockup_url` at all, and hardcoded every line item's `sku` to
`None`. None of that is what the dashboard's Products table needs — a
"print file" there means the actual print-ready design (**Mug Art
Transfers**), a "mockup" means the customer-facing preview (**Thumbnails**),
and the SKU column just displays whatever string is there as plain text (no
catalog entry required, confirmed by reading `_resolve_item_print_method()`
in custom-order-portal, which hardcodes Sublimation for `source == 'tbw'`
regardless of `sku` — populating the real SKU can't break print-method
resolution). Fixed by matching `"art transfer"` → `print_file_url`,
`"thumbnail"` → `mockup_url`, and sending the real `sku` (`TBW-11oz`/
`TBW-15oz`, already known from `build_order_items()`) instead of `None`.
Scoped entirely inside `push_to_production_dashboard()` — doesn't touch
`build_notes()`/the ShipStation `internalNotes` blob or any filename shown
elsewhere in this app; Box Labels still aren't sent anywhere (no field for
them on non-`amazon_fba` sources).

The four already-backfilled orders (105766–105769) were re-pushed with the
corrected mapping — if any earlier historical order still shows the mockup
column as a broken image icon or "—" for SKU, it predates this fix and
would need the same manual re-push, not a code change.
