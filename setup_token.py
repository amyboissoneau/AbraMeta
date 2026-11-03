"""One-time setup: turn a short-lived token into a permanent Page token.

Takes the short-lived USER token from the Graph API Explorer plus your app's
secret, then:
  1. exchanges the user token for a long-lived one,
  2. lists the Pages you manage,
  3. grabs the PAGE token for the Page you pick,
  4. writes FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN into .env.

Secrets are never printed. The App Secret and a typed user token are read with getpass; a user token from the clipboard is read with pbpaste. Only FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN are written
only into .env — they are never printed.

    python setup_token.py
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

ENV_PATH = Path(__file__).resolve().parent / ".env"
_ENV = dotenv_values(ENV_PATH)   # settings come from .env (or the environment)
APP_ID = (os.environ.get("FB_APP_ID") or _ENV.get("FB_APP_ID") or "").strip()   # your Facebook app's ID
API = "https://graph.facebook.com/v23.0"
TARGET_PAGE_HINT = (os.environ.get("PRIMARY_PAGE_NAME") or _ENV.get("PRIMARY_PAGE_NAME") or "Main page").strip().lower()   # prefer this page when auto-selecting


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


def from_clipboard() -> str:
    return clipboard_text()


def get_user_token() -> str:
    clip = from_clipboard()
    if len(clip) > 100 and " " not in clip:
        use = input("Found what looks like a token on your clipboard. Use it? [Y/n] ").strip().lower()
        if use in ("", "y", "yes"):
            return clip
    return getpass.getpass("Paste the short-lived USER token from the Graph API Explorer: ").strip()


def get_app_secret() -> str:
    return getpass.getpass(
        "Paste your App Secret (developers.facebook.com -> App settings -> Basic -> Show): "
    ).strip()


def call(path: str, **params) -> dict:
    resp = requests.get(f"{API}/{path}", params=params, timeout=30)
    data = resp.json()
    if "error" in data:
        sys.exit(f"\nFacebook error: {data['error'].get('message')}\n"
                 f"(type: {data['error'].get('type')}, code: {data['error'].get('code')})")
    return data


def write_env(page_id: str, page_token: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    for key, value in (("FB_PAGE_ID", page_id), ("FB_PAGE_ACCESS_TOKEN", page_token)):
        if re.search(rf"^{key}=.*$", text, flags=re.M):
            text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def main() -> None:
    print("Setting up your permanent Facebook Page token.\n")
    if not APP_ID:
        sys.exit("FB_APP_ID is not set. Add your Facebook app's ID to .env as FB_APP_ID=..., then run this again.")
    user_token = get_user_token()
    if not user_token:
        sys.exit("No token provided.")
    app_secret = get_app_secret()
    if not app_secret:
        sys.exit("No app secret provided.")

    print("\n1/3 Exchanging for a long-lived user token...")
    long_lived = call(
        "oauth/access_token",
        grant_type="fb_exchange_token",
        client_id=APP_ID,
        client_secret=app_secret,
        fb_exchange_token=user_token,
    )["access_token"]
    print("    done.")

    print("2/3 Looking up the Pages you manage...")
    pages = call("me/accounts", fields="id,name,access_token", access_token=long_lived).get("data", [])
    if not pages:
        sys.exit("    No Pages found. Make sure you granted access to your Page in the popup.")
    for i, p in enumerate(pages, 1):
        print(f"    {i}. {p['name']}  (id {p['id']})")

    if len(pages) == 1:
        chosen = pages[0]
    else:
        auto = [p for p in pages if TARGET_PAGE_HINT in p["name"].lower()]
        default = pages.index(auto[0]) + 1 if len(auto) == 1 else None
        prompt = f"    Which Page should the bot reply as? [{default}] " if default else "    Which Page number? "
        raw = input(prompt).strip()
        idx = int(raw) if raw else default
        if not idx or not (1 <= idx <= len(pages)):
            sys.exit("    Invalid selection.")
        chosen = pages[idx - 1]

    print(f"3/3 Saving the Page token for '{chosen['name']}' into .env...")
    write_env(chosen["id"], chosen["access_token"])
    print("    done.\n")
    print(f"Configured page: {chosen['name']} (id {chosen['id']})")
    print("If Facebook rejects the saved Page token later, re-run setup_token.py for a fresh one.\n")
    print("Next:  python learn_style.py     (teach the bot your reply voice)")
    print("Then:  python bot.py             (dry run: prints comment replies, likes and first comments, and posts none of them)")


if __name__ == "__main__":
    main()
