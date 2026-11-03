"""Add more pages for the assistant to manage.

Prereq (one time per page, done in the browser):
  the page must be opted in to your Facebook app (the one set as FB_APP_ID in .env) —
  Graph API Explorer -> Generate Access Token -> Edit previous settings ->
  check the page. Then copy the fresh USER token to the clipboard.

Usage:
  python add_page.py example-page-name            (page username from its URL)
  python add_page.py <numeric-page-id>         (or a raw page id)
  python add_page.py example-page-one example-page-two (several at once)

Reads the user token from the clipboard, exchanges it for a long-lived one
using FB_APP_SECRET from .env, fetches each page's permanent token, and
registers the page (enabled) in state.db. Tokens never printed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from bot import API_VERSION, FB_APP_ID, init_db

APP_ID = FB_APP_ID
API = f"https://graph.facebook.com/{API_VERSION}"
ENV = Path(__file__).resolve().parent / ".env"


def call(path: str, **params) -> dict:
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def clipboard_text() -> str:
    """The clipboard on macOS (pbpaste), Windows (PowerShell) or Linux (wl-paste or xclip). "" if none works."""
    if sys.platform == "darwin":
        cmds = [["pbpaste"]]
    elif sys.platform.startswith("win"):
        cmds = [["powershell", "-NoProfile", "-Command", "Get-Clipboard"]]
    else:
        cmds = [["wl-paste"], ["xclip", "-selection", "clipboard", "-o"]]
    for cmd in cmds:
        try:
            return subprocess.check_output(cmd, text=True, timeout=10).strip()
        except Exception:
            continue
    return ""

def main() -> None:
    handles = [h.strip().strip("/").split("/")[-1] for h in sys.argv[1:]]
    if not handles:
        sys.exit("Usage: python add_page.py <page-username-or-id> [...]")

    if not APP_ID:
        sys.exit("FB_APP_ID is not set. Add your Facebook app's ID to .env as FB_APP_ID=..., then run this again.")
    env = ENV.read_text(encoding="utf-8")
    m = re.search(r"^FB_APP_SECRET=(.+)$", env, flags=re.M)
    if not m:
        sys.exit("FB_APP_SECRET missing from .env — copy the App Secret to the "
                 "clipboard and have the assistant store it first.")
    secret = m.group(1).strip()

    user_tok = clipboard_text()
    if len(user_tok) < 100 or " " in user_tok:
        sys.exit(f"Clipboard doesn't look like a token (length {len(user_tok)}). "
                 "Copy the user token from the Graph API Explorer first.")

    ll = call("oauth/access_token", grant_type="fb_exchange_token",
              client_id=APP_ID, client_secret=secret, fb_exchange_token=user_tok)
    if "error" in ll:
        sys.exit("Token exchange failed: " + ll["error"].get("message", "?"))
    ll = ll["access_token"]
    print("long-lived token obtained")

    db = init_db()
    for h in handles:
        page = call(h, fields="id,name,access_token", access_token=ll)
        if "access_token" not in page:
            print(f"  X {h}: {page.get('error', {}).get('message', 'no page token')} "
                  f"(is the page opted in to the app?)")
            continue
        db.execute(
            "INSERT OR REPLACE INTO pages (page_id, name, token, enabled) VALUES (?, ?, ?, 1)",
            (page["id"], page["name"], page["access_token"]),
        )
        db.commit()
        print(f"  OK added: {page['name']} (id {page['id']}) — enabled")

    rows = db.execute("SELECT name, enabled FROM pages").fetchall()
    print("\nPages known to the assistant:")
    for name, en in rows:
        print(f"  [{'x' if en else ' '}] {name}")
    print("\nThe bot picks up new pages on its next poll cycle (60 s by default, plus sweep time) — no restart needed. "
          "Toggle pages on/off anytime from the dashboard.")


if __name__ == "__main__":
    main()
