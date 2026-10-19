# Canonical copy lives in ~/claude-workspace/hogg-notify. Copies in other projects are
# vendored by sync.sh: edit the canonical one, then run sync.sh.
"""Every email any Hogg project sends, and who receives it.

This file is the one place recipients are decided. A project names the event when it sends
(`send("faire.proof_ready", ...)`), and the sender, the recipient list and the List-Id all
come from here, so changing who gets something is a one-line edit followed by sync.sh.

Fields:
  sender    "custom" -> from custom@customhoggtumblers.com  (anything custom-order related)
            "custom_hogg" -> from custom@hoggoutfitters.com  (emails to Faire retailers)
            "custom_tsd"  -> from custom@hoggoutfitters.com as "The Stainless Depot" (TSD Customize customers)
            "sales"  -> from sales@hoggoutfitters.com         (anything sales/wholesale)
            "ops"    -> from jaden@hoggoutfitters.com         (Amazon, FBA, credit, international)
  audience  "staff"    -> recipients are `to` below; the caller cannot add any
            "customer" -> `to` is None; the caller passes the customer's address
  to        list of addresses for staff events
  project   which project sends it
  when      what triggers it, in words (also shown on the generated map)
"""

# Named once so the same people are not retyped in ten places.
JADEN = "jaden@hoggoutfitters.com"
JEREMY = "jeremy@hoggoutfitters.com"
HIEP = "hiep@hoggoutfitters.com"
DARYL = "daryl@hoggoutfitters.com"
JASONK = "jasonk@hoggoutfitters.com"
JIM = "jim@hoggoutfitters.com"
SUPPORT = "support@hoggoutfitters.com"
VIP = "vip@hoggoutfitters.com"

EVENTS = {
    # --- custom-order-portal: staff --------------------------------------------------
    "portal.new_submission": dict(
        sender="custom", audience="staff", to=[HIEP, JADEN, JEREMY],
        project="custom-order-portal", when="A new custom order request is submitted"),
    "portal.reorder": dict(
        sender="custom", audience="staff", to=[HIEP, JADEN, JEREMY],
        project="custom-order-portal", when="A customer reorders a past custom order"),
    "portal.edit_request": dict(
        sender="custom", audience="staff", to=[HIEP, JADEN],
        project="custom-order-portal",
        when="A customer asks for a mockup revision or adds files after their first upload"),
    "portal.artwork_uploaded": dict(
        sender="custom", audience="staff", to=[HIEP, JADEN],
        project="custom-order-portal",
        when="A customer's first artwork upload on a quote that had none"),
    "portal.chat_message": dict(
        sender="custom", audience="staff", to=[HIEP, JADEN],
        project="custom-order-portal",
        when="A customer sends a message in the chat on their order page"),
    "portal.artwork_released": dict(
        sender="custom", audience="staff", to=[HIEP, JADEN],
        project="custom-order-portal",
        when="A better-file request ran out (24h after the final warning): printing with the file we have"),

    # --- custom-order-portal: Faire custom (see FAIRE_CUSTOM.md) --------------------
    "faire.proof_ready": dict(
        sender="custom", audience="staff", to=[SUPPORT, JADEN],
        project="custom-order-portal",
        when="Proof pushed on a real Faire order; staff paste the message into Faire chat"),
    "faire.preorder_created": dict(
        sender="custom", audience="staff", to=[JADEN, JEREMY],
        project="custom-order-portal",
        when="Staff raise a Faire pre-order quote; staff paste the link into Faire chat"),
    # The approval chase on a real Faire order (see FAIRE_CUSTOM.md). Same audience as the
    # proof-ready email, since it is the same person pasting the same conversation onward.
    "faire.approval_reminder": dict(
        sender="custom", audience="staff", to=[SUPPORT, JADEN],
        project="custom-order-portal",
        when="24h after a Faire proof went up with no approval — short nudge to paste"),
    "faire.approval_final": dict(
        sender="custom", audience="staff", to=[SUPPORT, JADEN],
        project="custom-order-portal",
        when="48h after that nudge: the proof auto-approves and goes to production"),

    # Before any proof exists: the retailer hasn't sent artwork yet, so there is nothing to
    # draw and the job cannot start. Same audience as that order's other emails.
    "faire.artwork_reminder": dict(
        sender="custom", audience="staff", to=[SUPPORT, JADEN],
        project="custom-order-portal",
        when="Every 24h while a paid Faire order still has no artwork"),
    "faire.artwork_final": dict(
        sender="custom", audience="staff", to=[SUPPORT, JADEN],
        project="custom-order-portal",
        when="7 days with no artwork: the quote is closed and the Faire order needs cancelling"),
    "faire.preorder_artwork_reminder": dict(
        sender="custom", audience="staff", to=[JADEN, JEREMY],
        project="custom-order-portal",
        when="Every 24h while a Faire pre-order still has no artwork"),
    "faire.preorder_artwork_final": dict(
        sender="custom", audience="staff", to=[JADEN, JEREMY],
        project="custom-order-portal",
        when="7 days with no artwork on a pre-order: the request is closed"),

    "faire.preorder_proof_ready": dict(
        sender="custom", audience="staff", to=[JADEN, JEREMY],
        project="custom-order-portal",
        when="Proof pushed on a Faire pre-order; staff paste the message into Faire chat"),

    # A pre-order's chase: they have a mockup but have not bought yet.
    "faire.preorder_reminder": dict(
        sender="custom", audience="staff", to=[JADEN, JEREMY],
        project="custom-order-portal",
        when="2, 6 and 12 days after a pre-order's proof went up with no order placed"),
    "faire.preorder_closed": dict(
        sender="custom", audience="staff", to=[JADEN, JEREMY],
        project="custom-order-portal",
        when="14 days after a pre-order's proof went up with no order — the quote is closed"),

    # --- custom-order-portal: customers ---------------------------------------------
    "portal.customer.submission_received": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="Confirms a custom order request was received"),
    "portal.customer.mockup_ready": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="Mockup pushed for the customer to review"),
    "portal.customer.order_ready": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="Order is ready"),
    "portal.customer.invoice": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="Invoice pushed to the customer"),
    "portal.customer.artwork_reminder": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="Reminder to upload artwork"),
    "portal.customer.checkout_reminder": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="Reminder to complete checkout"),
    "portal.customer.quote_expired": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="Quote closed after 30 days without checkout"),
    "portal.customer.tsd_order_received": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="TSD \"Let Us Customize It\" order received"),
    "portal.customer.tsd_mockup_ready": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="TSD customize proof ready to approve"),
    "portal.customer.tsd_approval_reminder": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="Daily reminder before a TSD proof auto-approves"),
    "portal.customer.tsd_auto_approved": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="TSD proof auto-approved at 7 days"),
    # Like the rest of the portal's customer mail, these two go out through Resend (custom@),
    # not this helper; listed so the map shows them. Never from jaden@ (Jaden, 2026-10-08).
    "portal.customer.chat_message": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="Staff sent a message in the chat on the customer's order page (a notice; the reply happens on the page)"),
    "portal.customer.artwork_request": dict(
        sender="custom", audience="customer", to=None, project="custom-order-portal",
        when="An ordered job needs a better artwork file: the request, 2 follow-ups and a final warning"),

    # --- custom-order-portal: Faire retailers (see FAIRE_CUSTOM.md) --------------------
    # Sent to the order's Faire address (usually an @relay.faire.com proxy, which Faire
    # forwards). One Gmail thread per order: every one of these replies onto the first.
    "faire.customer.order_received": dict(
        sender="custom_hogg", audience="customer", to=None, project="custom-order-portal",
        when="A custom Faire order arrives: thanks + their order page (asks for artwork, or "
             "says the mockup is in progress or already approved)"),
    "faire.customer.artwork_reminder": dict(
        sender="custom_hogg", audience="customer", to=None, project="custom-order-portal",
        when="Days 1, 2, 3 and 5 after the order while there is still no artwork"),
    "faire.customer.proof_ready": dict(
        sender="custom_hogg", audience="customer", to=None, project="custom-order-portal",
        when="A proof is sent (Send & mark mockup complete)"),
    "faire.customer.approval_reminder": dict(
        sender="custom_hogg", audience="customer", to=None, project="custom-order-portal",
        when="24h after a proof with no approval"),
    "faire.customer.auto_approved": dict(
        sender="custom_hogg", audience="customer", to=None, project="custom-order-portal",
        when="48h after that nudge: the proof is approved for them and goes to production"),
    "faire.customer.chat_message": dict(
        sender="custom_hogg", audience="customer", to=None, project="custom-order-portal",
        when="Staff sent a message in the chat on the retailer's order page"),
    "faire.customer.cancelled": dict(
        sender="custom_hogg", audience="customer", to=None, project="custom-order-portal",
        when="Staff cancel a placed order from its day-7 no-artwork row: cancelled for inactivity"),

    # --- custom-order-portal: TSD Customize customers (TSD_CUSTOMIZE.md, the email flow) -
    # One Gmail thread per order, like Faire's.
    "tsd.customer.order_received": dict(
        sender="custom_tsd", audience="customer", to=None, project="custom-order-portal",
        when="A paid TSD custom order is pulled in: thanks + their proof page (asks for artwork if none came)"),
    "tsd.customer.artwork_reminder": dict(
        sender="custom_tsd", audience="customer", to=None, project="custom-order-portal",
        when="Days 1, 2, 3 and 5 with no artwork (day 5 warns we'll cancel)"),
    "tsd.customer.cancelled": dict(
        sender="custom_tsd", audience="customer", to=None, project="custom-order-portal",
        when="Day 7 with no artwork: cancelling for inactivity, they can place a new order"),
    "tsd.customer.proof_ready": dict(
        sender="custom_tsd", audience="customer", to=None, project="custom-order-portal",
        when="A proof is sent (Send & mark mockup complete)"),
    "tsd.customer.approval_reminder": dict(
        sender="custom_tsd", audience="customer", to=None, project="custom-order-portal",
        when="24h after a proof with no approval"),
    "tsd.customer.auto_approved": dict(
        sender="custom_tsd", audience="customer", to=None, project="custom-order-portal",
        when="48h after that nudge: approved for them, into production"),
    "tsd.customer.chat_message": dict(
        sender="custom_tsd", audience="customer", to=None, project="custom-order-portal",
        when="Staff sent a message in the chat on the customer's proof page"),

    # --- custom-order-portal: ops -----------------------------------------------------
    "portal.fba_batch": dict(
        sender="ops", audience="staff", to=[VIP, JASONK, JADEN],
        project="custom-order-portal", when="FBA batch shipment email (packing slip attached)"),
    "portal.fba_batch_nj": dict(
        sender="ops", audience="staff", to=[DARYL, JADEN],
        project="custom-order-portal", when="FBA batch shipment email for the NJ warehouse"),

    # --- faire-fulfillment-sync -------------------------------------------------------
    "faire.artwork_request": dict(
        sender="custom", audience="staff", to=[SUPPORT, JADEN],
        project="custom-order-portal",
        when="A Faire order needs a better artwork file: a message to paste into Faire chat (request, follow-ups, final warning)"),
    "faire.mockup_request": dict(
        sender="custom", audience="staff", to=[SUPPORT, JADEN],
        project="faire-fulfillment-sync",
        when="A custom Faire order is detected; staff paste the upload link into Faire chat"),
    "faire.preorder_order_placed": dict(
        sender="custom", audience="staff", to=[JADEN, JEREMY],
        project="faire-fulfillment-sync",
        when="The real Faire order arrives for a pre-order (reply on the pre-order's thread)"),
    # Every custom order that gets held, whichever store sold it — Faire, the Custom Shop or
    # TSD. These are the people who make the decorated goods, so they are told about anything
    # that needs decorating regardless of where it came from.
    "custom.order_held": dict(
        sender="custom", audience="staff", to=[JADEN, DARYL, HIEP, JASONK, JEREMY],
        project="faire-fulfillment-sync",
        when="A custom order is held in ShipStation so it can't ship undecorated"),
    # Replies on that email's thread for a TSD order still without artwork, so the same people
    # see it (Jaden, 2026-10-16). Sent by custom-order-portal (services/prepaid_flow.py).
    "tsd.no_artwork_call": dict(
        sender="custom", audience="staff", to=[JADEN, DARYL, HIEP, JASONK, JEREMY],
        project="custom-order-portal",
        when="Day 4 with no artwork on a TSD order: call the customer (or email them, with no phone)"),
    "tsd.no_artwork_cancel": dict(
        sender="custom", audience="staff", to=[JADEN, DARYL, HIEP, JASONK, JEREMY],
        project="custom-order-portal",
        when="Day 7 with no artwork on a TSD order: reach out once more, or cancel it"),
    "faire.watched_collection": dict(
        sender="custom", audience="staff", to=[JADEN, DARYL, HIEP, JASONK],
        project="faire-fulfillment-sync", when="A Faire order includes a watched-collection SKU"),
    "faire.international": dict(
        sender="ops", audience="staff",
        to=[SUPPORT, JASONK, VIP],
        project="faire-fulfillment-sync", when="An international Faire order is detected"),

    # --- wholesale-portal -------------------------------------------------------------
    "wholesale.customer.access": dict(
        sender="sales", audience="customer", to=None, project="wholesale-portal",
        when="Wholesale account is ready"),
    "wholesale.customer.pending": dict(
        sender="sales", audience="customer", to=None, project="wholesale-portal",
        when="Wholesale application received"),
    "wholesale.customer.declined": dict(
        sender="sales", audience="customer", to=None, project="wholesale-portal",
        when="Wholesale application declined"),
    "wholesale.approved": dict(
        sender="sales", audience="staff", to=[JADEN, JEREMY], project="wholesale-portal",
        when="A wholesale application was approved"),
    "wholesale.review_needed": dict(
        sender="sales", audience="staff", to=[JADEN, JEREMY], project="wholesale-portal",
        when="A wholesale application needs a person to review it"),
    "wholesale.failure": dict(
        sender="sales", audience="staff", to=[JADEN, JEREMY], project="wholesale-portal",
        when="Building a wholesale account failed"),
    "wholesale.price_sync_attention": dict(
        sender="ops", audience="staff", to=[JADEN], project="wholesale-portal",
        when="The B2B price sync failed or stopped and needs a person (never routine runs)"),
    "wholesale.tax_exempt_handoff": dict(
        sender="sales", audience="staff", to=["tax.exempt@hoggoutfitters.com"],
        project="wholesale-portal", when="Resale certificate handed to tax.exempt@ for review"),

    # --- tbw-portal ---------------------------------------------------------------------
    "tbw.order_received": dict(
        sender="custom", audience="staff", to=["mugs@hoggoutfitters.com"],
        project="tbw-portal", when="A Buffalo Works PO lands in ShipStation"),
    "tbw.order_attention": dict(
        sender="custom", audience="staff", to=["mugs@hoggoutfitters.com"],
        project="tbw-portal",
        when="TBW dashboard sync: an order was cancelled, edited after we received it, or "
             "couldn't be synced"),

    # --- tribe-portal -------------------------------------------------------------------
    "tribe.order_received": dict(
        sender="custom", audience="staff", to=["mugs@hoggoutfitters.com"],
        project="tribe-portal", when="A Tribe order lands in ShipStation"),
    "tribe.customer.monthly_invoice": dict(
        sender="sales", audience="customer", to=None, project="tribe-portal",
        when="1st of the month: last month's Tribe invoice, spreadsheet + card payment link"),
    "tribe.invoice_failed": dict(
        sender="ops", audience="staff", to=[JADEN], project="tribe-portal",
        when="The monthly Tribe invoice could not be built or sent (retries hourly)"),

    # --- ops scripts on the Mac -----------------------------------------------------------
    "mfn.alert": dict(
        sender="ops", audience="staff", to=[JADEN, "ryan@hoggoutfitters.com"],
        project="amazon-mfn-sync", when="MFN inventory sync failed or listings went out of stock"),
    "amazon.cancel_alert": dict(
        sender="ops", audience="staff", to=[JEREMY, JADEN, "rianne@onspree.com"],
        project="amazon-cancel-alerts", when="Amazon orders the buyer asked to cancel"),
    "amazon.fba_workflow": dict(
        sender="ops", audience="staff", to=[JADEN],
        project="Amazon-Inventory", when="Amazon FBA inbound workflow, FedEx step"),
    "amazon.daily_report": dict(
        sender="ops", audience="staff", to=[JADEN, JEREMY],
        project="amazon-daily-report", when="Daily Amazon revenue trends email for yesterday"),
    "amazon.daily_products": dict(
        sender="ops", audience="staff", to=[JADEN, JEREMY],
        project="amazon-daily-report", when="Daily Amazon products-sold email for yesterday"),
    "amazon.intraday_products": dict(
        sender="ops", audience="staff", to=[JADEN, JEREMY],
        project="amazon-daily-report", when="Amazon products sold today so far, every 3 hours 9am–9pm CT"),
    "amazon.multipack_request": dict(
        sender="ops", audience="staff", to=["ryan@hoggoutfitters.com", JIM, "zach@hoggoutfitters.com"],
        project="Amazon-Listing-Workstation",
        when="A new Amazon multipack needs its SKU + UPC (Ryan adds them in the Custom UPCs and SKUs "
             "sheet); the same thread gets a reply once they're in, with the SKUs to set up"),
    "amazon.multipack_sent": dict(
        sender="ops", audience="staff", to=[JADEN], project="Amazon-Listing-Workstation",
        when="A multipack queued to send itself once Ryan's SKU/UPC came in: sent, or why it wasn't"),
    "amazon.panda_boom_weekly": dict(
        sender="ops", audience="staff", to=[JADEN, JEREMY],
        project="Amazon-Listing-Workstation",
        when="Weekly (Mon 10am CT): Panda Boom ASINs' search rank, term clicks/purchases and sales"),
    "shipstation.credit_alert": dict(
        sender="ops", audience="staff", to=[JADEN, JEREMY],
        project="shipstation-tools", when="A net-terms customer placed an order (credit tracker)"),
    "shipstation.rebox_report": dict(
        sender="ops", audience="staff", to=[JEREMY, JIM, JASONK],
        project="shipstation-tools",
        when="Weekly (Fri 7am CT): labels voided and reprinted in the past week, what box changed, and the SKUs behind added boxes"),
}
