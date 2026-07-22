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
