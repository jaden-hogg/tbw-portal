"""Store TBW's Hogg App API token in Keychain as TBW_API_TOKEN and test it.

Run in Terminal.app:  python3 ~/claude-workspace/tbw-portal/scripts/set_tbw_api_token.py
"""
import json
import os
import subprocess
import urllib.request

NAME = "TBW_API_TOKEN"
EXPORT = f"export {NAME}=$(_kc {NAME})\n"

token = input(f"Paste the TBW API token, then press Return: ").strip()
if not token:
    raise SystemExit("No token entered; nothing changed.")

subprocess.run(["security", "add-generic-password", "-U", "-a", os.environ["USER"],
                "-s", NAME, "-w", token], check=True)
print(f"Saved {NAME} to Keychain.")

zshrc = os.path.expanduser("~/.zshrc")
with open(zshrc) as f:
    has_line = EXPORT.strip() in f.read()
if not has_line:
    with open(zshrc, "a") as f:
        f.write(EXPORT)
    print("Added the export line to ~/.zshrc.")

req = urllib.request.Request("https://hoggapp.tbwbrands.com/api/ping",
                             headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        print("Ping:", json.load(r))
except urllib.error.HTTPError as e:
    print(f"Ping failed: HTTP {e.code} {e.read().decode()[:200]}")
