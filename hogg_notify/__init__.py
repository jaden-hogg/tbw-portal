# Canonical copy lives in ~/claude-workspace/hogg-notify. Copies in other projects are
# vendored by sync.sh: edit the canonical one, then run sync.sh.
from .core import send, ThreadRef, NotifyError, SENDERS
from .registry import EVENTS
from .templates import staff_action_email

__all__ = ["send", "ThreadRef", "NotifyError", "SENDERS", "EVENTS", "staff_action_email"]
