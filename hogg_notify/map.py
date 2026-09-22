# Canonical copy lives in ~/claude-workspace/hogg-notify. Copies in other projects are
# vendored by sync.sh: edit the canonical one, then run sync.sh.
"""Who gets what, generated from the registry so it can't drift from what actually sends.

    python3 -m hogg_notify.map > notifications.html
"""
import html as _html
from collections import OrderedDict

from .registry import EVENTS
from .core import SENDERS


def _addr(from_addr):
    return from_addr.split("<")[-1].rstrip(">")


def render():
    by_project = OrderedDict()
    for key, e in EVENTS.items():
        by_project.setdefault(e["project"], []).append((key, e))

    sections = []
    for project, rows in by_project.items():
        trs = []
        for key, e in rows:
            to = ("<em>the customer</em>" if e["audience"] == "customer" else
                  " ".join(f'<span class="a">{_html.escape(a)}</span>' for a in e["to"]))
            trs.append(
                f'<tr><td><code>{_html.escape(key)}</code><div class="w">{_html.escape(e["when"])}</div></td>'
                f'<td><span class="s {e["sender"]}">{_html.escape(_addr(SENDERS[e["sender"]]["from_addr"]))}</span></td>'
                f'<td>{to}</td></tr>')
        sections.append(f'<h2>{_html.escape(project)}</h2><div class="t"><table>'
                        f'<thead><tr><th>Email</th><th>From</th><th>To</th></tr></thead>'
                        f'<tbody>{"".join(trs)}</tbody></table></div>')

    return f"""<title>Hogg Email Map</title>
<style>
:root{{--bg:#F5F6F4;--paper:#fff;--ink:#1A211E;--muted:#5C6A64;--line:#D6DDDA;--custom:#0E6E66;--sales:#2451A6;--ops:#8A5A00}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#111614;--paper:#181F1C;--ink:#E5EBE8;--muted:#98A5A0;--line:#2D3834;--custom:#5CC7BC;--sales:#8AB0F5;--ops:#E7B45A}}}}
:root[data-theme="dark"]{{--bg:#111614;--paper:#181F1C;--ink:#E5EBE8;--muted:#98A5A0;--line:#2D3834;--custom:#5CC7BC;--sales:#8AB0F5;--ops:#E7B45A}}
body{{background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,sans-serif;padding-inline:16px;padding-block:24px 48px}}
main{{max-width:1100px;margin:0 auto}} h1{{font-size:28px;margin:0 0 6px}} h2{{font-size:17px;margin:28px 0 8px}}
p{{color:var(--muted);margin:0}} .t{{overflow-x:auto;background:var(--paper);border:1px solid var(--line);border-radius:8px}}
table{{border-collapse:collapse;width:100%;min-width:680px}} th,td{{text-align:left;vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--line)}}
th{{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}} code{{font-size:12.5px}} .w{{color:var(--muted);font-size:13px}}
.a{{display:inline-block;font:12px ui-monospace,Menlo,monospace;border:1px solid var(--line);border-radius:4px;padding:1px 6px;margin:1px 2px 1px 0}}
.s{{font:600 12px ui-monospace,Menlo,monospace}} .s.custom{{color:var(--custom)}} .s.sales{{color:var(--sales)}} .s.ops{{color:var(--ops)}}
</style>
<main><h1>Hogg Email Map</h1>
<p>Generated from hogg-notify/hogg_notify/registry.py: every email every project sends, who it comes from, and who gets it.</p>
{"".join(sections)}</main>"""


if __name__ == "__main__":
    print(render())
