# Canonical copy lives in ~/claude-workspace/hogg-notify. Copies in other projects are
# vendored by sync.sh: edit the canonical one, then run sync.sh.
"""One look for every staff email whose whole point is "paste this somewhere".

Faire has no messaging API, so a person is the last step on every custom Faire order: they
copy a message and send it in the retailer's chat. These emails are that instruction, built
here once so the reader learns one shape — copy the boxed block, then open the chat — instead
of a slightly different layout per sender.

    html, text = staff_action_email(
        intro="The proof is up for Faire #123 (Some Shop). The retailer hasn't been told.",
        chat_url="https://www.faire.com/brand-portal/messages?retailerToken=r_x",
        chat_message="Hi [name], your mockups are ready ...",
        facts=[("Faire order", "#123"), ("ShipStation", "1829692")],
        footer="Approving is what releases the job to the production calendar.",
    )

`chat_message=None` makes it a plain notice with no copy block — for the emails that only
report something (an order landed, a request was closed) and need nobody to paste anything.
"""

INK = "#1a1a1a"
MUTED = "#6b7280"
LINE = "#d7ddda"
ACCENT = "#0e6e66"
COPY_BG = "#f4faf9"

_FONT = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif")


def _esc(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _facts_html(facts):
    if not facts:
        return ""
    rows = "".join(
        f'<tr><td style="padding:2px 14px 2px 0;color:{MUTED};white-space:nowrap">{_esc(k)}</td>'
        f'<td style="padding:2px 0"><strong>{_esc(v)}</strong></td></tr>'
        for k, v in facts if v not in (None, "")
    )
    return (f'<table style="border-collapse:collapse;font-size:14px;margin:18px 0 0">{rows}</table>'
            if rows else "")


def _facts_text(facts):
    lines = [f"{k+':':<15}{v}" for k, v in (facts or []) if v not in (None, "")]
    return ("\n" + "\n".join(lines)) if lines else ""


def staff_action_email(intro, chat_url=None, chat_message=None, facts=None, footer="",
                       step_one="Copy the message below",
                       step_two="Open this retailer's Faire chat and send it"):
    """Returns `(html, text)`. Both carry the same words in the same order.

    The plain-text half matters as much as the HTML: it is what shows in a notification
    preview, and what a client that strips HTML falls back to. The copy block is fenced with
    rules there, since it has no box to sit in.
    """
    steps = ""
    text_steps = ""
    if chat_message:
        link = (f'<p style="margin:6px 0 0"><a href="{_esc(chat_url)}" '
                f'style="color:{ACCENT};word-break:break-all">{_esc(chat_url)}</a></p>'
                if chat_url else "")
        # Copy first, then open the chat: that is the order a person actually does it in, and
        # opening the chat first means leaving it again to come back for the message.
        steps = f"""
<p style="margin:22px 0 0;font-weight:600">Step 1 &nbsp;{_esc(step_one)}</p>
<div style="border:1px solid {LINE};border-left:4px solid {ACCENT};background:{COPY_BG};
border-radius:6px;margin:10px 0 0;padding:14px 16px;white-space:pre-wrap;font-size:15px;
line-height:1.5">{_esc(chat_message)}</div>
<p style="margin:22px 0 0;font-weight:600">Step 2 &nbsp;{_esc(step_two)}</p>
{link}"""
        rule = "-" * 60
        text_steps = (f"\n\nSTEP 1  {step_one}\n\n{rule}\n{chat_message}\n{rule}"
                      f"\n\nSTEP 2  {step_two}\n"
                      + (f"\n    {chat_url}\n" if chat_url else ""))

    footer_html = (f'<p style="margin:22px 0 0;color:{MUTED};font-size:13px">{_esc(footer)}</p>'
                   if footer else "")
    html = f"""<div style="font-family:{_FONT};color:{INK};font-size:15px;line-height:1.55;max-width:640px">
<p style="margin:0">{intro}</p>{steps}{_facts_html(facts)}{footer_html}
</div>"""

    text = (_strip(intro) + text_steps + _facts_text(facts)
            + (f"\n\n{footer}" if footer else "") + "\n")
    return html, text


def _strip(value):
    """`intro` may carry a little inline HTML (a bold order number); the text half can't."""
    import re
    return re.sub(r"<[^>]+>", "", value).replace("&amp;", "&").replace("&nbsp;", " ")
