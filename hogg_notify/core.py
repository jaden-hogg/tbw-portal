# Canonical copy lives in ~/claude-workspace/hogg-notify. Copies in other projects are
# vendored by sync.sh: edit the canonical one, then run sync.sh.
"""Send any Hogg notification email. See ../CLAUDE.md for the why.

    from hogg_notify import send, ThreadRef
    ref = send("faire.mockup_request", subject="...", html="...")
    ... later, possibly in another project ...
    send("faire.proof_ready", html="...", reply_to=ref)

Jaden's Gmail does all the sending: as itself for ops alerts, and as custom@ and sales@
through Gmail's "send mail as" aliases (both are Google Groups, which can't send on their
own). Its NOTIFY_GMAIL_REFRESH_TOKEN can read message headers, which is what makes threading
work: Gmail replaces any Message-ID we set, so after sending we read back the real one.

Failures raise. Nothing here skips silently: a missing token or a rejected send is an
exception with the reason in it, and the caller decides whether that should stop its work.
"""
import base64
import os
import re
import time
from dataclasses import dataclass, asdict
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import List, Optional, Sequence, Tuple

import requests

from .registry import EVENTS

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"

SENDERS = {
    "custom": dict(from_addr=formataddr(("Hogg Custom", "custom@customhoggtumblers.com")),
                   list_id="<staff.customhoggtumblers.com>"),
    "sales": dict(from_addr=formataddr(("Hogg Outfitters", "sales@hoggoutfitters.com")),
                  list_id="<staff.hoggoutfitters.com>"),
    "ops": dict(from_addr="jaden@hoggoutfitters.com", list_id="<ops.hoggoutfitters.com>"),
}

# jaden@'s token with gmail.send + gmail.metadata (mint_token.py). Deliberately not the
# older GMAIL_REFRESH_TOKEN, which is gmail.compose only and can't read the Message-ID back.
TOKEN_ENV = "NOTIFY_GMAIL_REFRESH_TOKEN"


class NotifyError(Exception):
    """Configuration or delivery failure. The message says which and why."""


@dataclass
class ThreadRef:
    """What a later email needs to reply onto this one's thread.

    `message_id` is the real Message-ID Gmail assigned (not one we minted — Gmail discards
    those). `thread_id` is the sending mailbox's thread. `subject` is the subject exactly as
    sent, so the reply's "Re: …" can never drift from it. Store all three wherever you
    already keep state for the order.
    """
    message_id: str
    thread_id: str
    subject: str
    gmail_id: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_fields(cls, message_id, thread_id, subject):
        """A ThreadRef from stored columns, or None when there is nothing to reply to."""
        if not (message_id and subject):
            return None
        return cls(message_id=message_id, thread_id=thread_id or "", subject=subject)


# --- tokens -------------------------------------------------------------------------

_token_cache = {}


def _access_token():
    cached = _token_cache.get("token")
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    env = TOKEN_ENV
    missing = [n for n in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", env) if not os.environ.get(n)]
    if missing:
        raise NotifyError(f"Email not configured: {', '.join(missing)} not set")
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "refresh_token",
        "refresh_token": os.environ[env],
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
    }, timeout=20)
    if resp.status_code != 200:
        raise NotifyError(f"Gmail token refresh failed "
                          f"(HTTP {resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    _token_cache["token"] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    return data["access_token"]


# --- building the message -------------------------------------------------------------

def _as_list(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [a.strip() for a in value.split(",") if a.strip()]
    return [a for a in value if a]


def _html_to_text(html):
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(a, b)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _build(from_addr, to, cc, subject, html, text, attachments, headers):
    body = MIMEMultipart("alternative")
    if text is None and html is not None:
        text = _html_to_text(html)
    if text is not None:
        body.attach(MIMEText(text, "plain", "utf-8"))
    if html is not None:
        body.attach(MIMEText(html, "html", "utf-8"))

    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(body)
        for filename, content, mime in attachments:
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            part = MIMEApplication(content, _subtype=subtype or "octet-stream")
            part.replace_header("Content-Type", f"{maintype}/{subtype or 'octet-stream'}")
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
    else:
        msg = body

    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    for k, v in headers.items():
        msg[k] = v
    return msg


# --- sending ------------------------------------------------------------------------------

def send(event: str,
         subject: Optional[str] = None,
         html: Optional[str] = None,
         text: Optional[str] = None,
         to=None,
         cc=None,
         attachments: Optional[Sequence[Tuple[str, bytes, str]]] = None,
         reply_to: Optional[ThreadRef] = None) -> ThreadRef:
    """Send the email for `event` (a key in registry.EVENTS) and return its ThreadRef.

    to           customer events only: the customer's address(es). Staff events take their
                 recipients from the registry and refuse this.
    reply_to     a ThreadRef from an earlier email: this one threads under it, and its
                 subject becomes "Re: <that subject>" (a `subject` passed here is ignored).
    attachments  [(filename, bytes, mime_type), ...]

    Env:
      NOTIFY_DRY_RUN=1          log instead of sending; returns a ThreadRef with no ids
      NOTIFY_REDIRECT_TO=addr   send only to `addr`, with the real recipients in the subject
    """
    if event not in EVENTS:
        raise NotifyError(f"Unknown email event {event!r} — add it to hogg_notify/registry.py")
    entry = EVENTS[event]
    sender = SENDERS[entry["sender"]]

    if entry["audience"] == "staff":
        if to:
            raise NotifyError(f"{event} is a staff email; its recipients come from the registry")
        recipients = list(entry["to"])
    else:
        recipients = _as_list(to)
        if not recipients:
            raise NotifyError(f"{event} is a customer email and needs a `to` address")
    cc_list = _as_list(cc)

    if reply_to is not None:
        base = reply_to.subject
        subject = base if base.lower().startswith("re:") else f"Re: {base}"
    if not subject:
        raise NotifyError(f"{event}: a subject is required")

    redirect = os.environ.get("NOTIFY_REDIRECT_TO", "").strip()
    if redirect and not reply_to:
        subject = f"[TEST → {', '.join(recipients + cc_list)}] {subject}"
    if redirect:
        recipients, cc_list = [redirect], []

    headers = {}
    if entry["audience"] == "staff":
        # Lets a Gmail filter tell our staff notifications apart from anything else sent
        # from the same address (Shopify also sends as custom@): `list:staff.customhoggtumblers.com`.
        headers["List-Id"] = sender["list_id"]
    if reply_to is not None:
        headers["In-Reply-To"] = reply_to.message_id
        headers["References"] = reply_to.message_id

    if os.environ.get("NOTIFY_DRY_RUN", "").strip() in ("1", "true", "yes"):
        print(f"[notify] DRY RUN {event}: {sender['from_addr']} -> {', '.join(recipients)} "
              f"| {subject}", flush=True)
        return ThreadRef(message_id="", thread_id="", subject=subject)

    msg = _build(sender["from_addr"], recipients, cc_list, subject, html, text,
                 attachments, headers)
    payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if reply_to is not None and reply_to.thread_id:
        payload["threadId"] = reply_to.thread_id

    token = _access_token()
    resp = requests.post(f"{GMAIL}/messages/send", json=payload, timeout=30,
                         headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 404 and "threadId" in payload:
        # The stored thread belongs to another mailbox (or was deleted). Send without it;
        # recipients still thread on In-Reply-To/References.
        payload.pop("threadId")
        resp = requests.post(f"{GMAIL}/messages/send", json=payload, timeout=30,
                             headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        raise NotifyError(f"Gmail rejected {event} (HTTP {resp.status_code}): {resp.text[:300]}")
    sent = resp.json()

    message_id = _read_message_id(token, sent["id"], event)

    print(f"[notify] sent {event}: {sender['from_addr']} -> {', '.join(recipients)} "
          f"| {subject} | {message_id or sent['id']}", flush=True)
    return ThreadRef(message_id=message_id, thread_id=sent.get("threadId", ""),
                     subject=subject, gmail_id=sent["id"])


def _read_message_id(token, gmail_id, event):
    """The Message-ID Gmail actually put on the sent email.

    Failing here does not raise: the email has gone, and raising would make a caller retry
    and send it twice. It returns "" instead, which only costs this email its threading.
    """
    try:
        resp = requests.get(f"{GMAIL}/messages/{gmail_id}", timeout=20,
                            params={"format": "metadata", "metadataHeaders": "Message-Id"},
                            headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        for h in resp.json().get("payload", {}).get("headers", []):
            if h.get("name", "").lower() == "message-id":
                return h.get("value", "")
    except Exception as e:
        print(f"[notify] WARNING {event}: sent, but could not read its Message-ID ({e}); "
              f"a reply to it will not thread", flush=True)
    return ""
