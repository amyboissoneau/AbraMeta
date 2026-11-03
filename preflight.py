"""Abra preflight: is everything the demo needs actually working right now?

    ./venv/bin/python preflight.py          # free checks only (no paid model calls)
    ./venv/bin/python preflight.py --live   # also makes one small Muse, Claude and Graph call

Prints PASS, INFO, WARN or FAIL for each check with one line of what to do (INFO means expected for this version, not a fault). It never prints
a key, token or secret value: messages pass through redact(), which blanks the values of the SECRET_NAMES keys and any key= or token= URL parameter.
Exit code is 0 when nothing FAILs, 1 otherwise.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent
DB = BASE / "state.db"
ENV = {k: v for k, v in dotenv_values(BASE / ".env").items() if v}
# the dashboard port: ABRA_PORT from .env or the command line (same name liveview.py reads), default 8765
DASHBOARD = f"http://127.0.0.1:{ENV.get('ABRA_PORT') or os.environ.get('ABRA_PORT') or '8765'}"
LAUNCHD_PREFIX = ENV.get("LAUNCHD_PREFIX") or os.environ.get("LAUNCHD_PREFIX") or "com.abra"
SECRET_NAMES = [
    "META_MUSE_API_KEY", "ANTHROPIC_API_KEY", "FB_PAGE_ACCESS_TOKEN", "BASETEN_API_KEY",
    "FB_APP_SECRET", "ELEVENLABS_API_KEY", "FIREWORKS_API_KEY", "MODAL_API_KEY",
]
RESULTS: list[tuple[str, str, str]] = []


def redact(text: str) -> str:
    """Strip anything that looks like a secret from text before it is printed."""
    text = re.sub(r"(access_token|token|key)=[^&\s'\"]+", r"\1=***", str(text))
    for name in SECRET_NAMES:
        val = ENV.get(name) or os.environ.get(name)
        if val:
            text = text.replace(val, "***")
    return text[:200]


def record(status: str, name: str, detail: str) -> None:
    RESULTS.append((status, name, redact(detail)))
    print(f"[{status:4}] {name}: {redact(detail)}")


def check(name, fn):
    try:
        status, detail = fn()
    except Exception as e:  # never let one check crash the rest
        status, detail = "FAIL", f"{type(e).__name__}: {e}"
    record(status, name, detail)


# ---------- free checks ----------

def check_dashboard():
    r = requests.get(f"{DASHBOARD}/", timeout=5)
    if r.status_code == 200:
        return "PASS", f"dashboard answers at {DASHBOARD}"
    return "FAIL", f"dashboard returned HTTP {r.status_code}. Start it with: {('python' if sys.platform.startswith('win') else 'python3')} liveview.py"


def check_bot():
    # the bot is optional for judges (24/7 comment replies on the owner's page); the owner's Mac runs it under launchd
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return "INFO", f"bot not checked (launchd is macOS only). The bot is optional for judges; to run it: {('python' if sys.platform.startswith('win') else 'python3')} bot.py"
    line = next((l for l in out.splitlines() if f"{LAUNCHD_PREFIX}.fbcommentbot" in l and "liveview" not in l and "keepawake" not in l), "")
    if line and not line.split()[0].startswith("-"):
        return "PASS", "bot service is running (launchd)"
    return "INFO", f"bot is not running. It is optional for judges (it only runs 24/7 comment replies); to run it: {('python' if sys.platform.startswith('win') else 'python3')} bot.py"


def check_keys():
    missing = [n for n in ("META_MUSE_API_KEY", "ANTHROPIC_API_KEY", "FB_PAGE_ACCESS_TOKEN", "FB_PAGE_ID")
               if not (ENV.get(n) or os.environ.get(n))]
    if missing:
        # the dashboard runs without keys (Try it without keys in the README); only the AI features need them
        # kept under 200 characters: record() trims longer messages
        return "WARN", (f"not set: {', '.join(missing)}. The keys are optional. "
                        "Add them to .env, or under Connect your AI keys in the dashboard.")
    optional = [n for n in ("BASETEN_API_KEY",) if not (ENV.get(n) or os.environ.get(n))]
    if optional:
        return "WARN", f"all required keys set; optional not set: {', '.join(optional)} (open-weight triage falls back to Muse Spark)"
    return "PASS", "all keys are set (names checked, values not printed)"


def check_ffmpeg():
    path = shutil.which("ffmpeg") or next((p for p in ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg") if os.path.exists(p)), None)
    if path:
        return "PASS", f"ffmpeg found (Smooth cut and Stories need it)"
    hint = {"darwin": "brew install ffmpeg", "win32": "winget install --id Gyan.FFmpeg (then open a new terminal)"}.get(sys.platform, "sudo apt install ffmpeg")
    return "FAIL", f"ffmpeg not found. Install it ({hint}) or Smooth cut and Stories will not work"


def check_db():
    if not DB.exists():
        return "WARN", "state.db does not exist yet; it is created on first run"
    db = sqlite3.connect(str(DB))
    try:
        handled = db.execute("SELECT COUNT(*) FROM handled").fetchone()[0]
        due_stories = db.execute("SELECT COUNT(*) FROM story_queue WHERE posted_at IS NULL AND error IS NULL").fetchone()[0]
    finally:
        db.close()
    return "PASS", f"state.db readable ({handled} comments handled, {due_stories} Stories waiting to post)"


def check_internet():
    hosts = {"gesture model (MediaPipe)": "https://storage.googleapis.com/",
             "voices (edge-tts)": "https://speech.platform.bing.com/",
             "Meta Muse API": "https://api.meta.ai/"}
    bad = []
    for label, url in hosts.items():
        try:
            requests.get(url, timeout=5)
        except Exception:
            bad.append(label)
    if bad:
        return "WARN", f"no answer from: {', '.join(bad)}. Hands-free, voices or Muse calls may fail"
    return "PASS", "gesture model, voice and Meta hosts all answer"


def _create_template_ids() -> list:
    import json
    genres = json.loads((BASE / "film_genres.json").read_text(encoding="utf-8"))
    return [g["id"] for g in genres]


def check_film_genres():
    ids = _create_template_ids()
    if not ids:
        # an empty list is the switch: the Create templates are off for this version (see the README)
        return "INFO", "Create templates are switched off for this version (film_genres.json is empty). This is expected, not a fault"
    if len(ids) == 4:
        return "PASS", f"four Create templates present: {', '.join(ids)}"
    return "WARN", f"expected four Create templates, found {len(ids)}: {', '.join(ids)}"


# ---------- live checks (paid or network calls to Meta / Anthropic / Facebook) ----------

def check_muse_live():
    key = ENV.get("META_MUSE_API_KEY") or os.environ.get("META_MUSE_API_KEY")
    if not key:
        return "FAIL", "META_MUSE_API_KEY is not set"
    r = requests.post("https://api.meta.ai/v1/responses",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": "muse-spark-1.3", "reasoning": {"effort": "minimal"},
                            "max_output_tokens": 300, "instructions": "Reply with one word.",
                            "input": "Say OK."}, timeout=30)
    if r.status_code in (401, 402, 403):
        return "FAIL", f"Muse rejected the key or billing (HTTP {r.status_code}). Check dev.meta.ai billing; the bot stops using Muse until restart"
    if r.status_code != 200:
        return "FAIL", f"Muse answered HTTP {r.status_code}"
    text = str(r.json())
    return ("PASS", "Muse Spark answered") if "OK" in text.upper() else ("WARN", "Muse answered but with no readable text")


def check_claude_live():
    key = ENV.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return "FAIL", "ANTHROPIC_API_KEY is not set"
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": "claude-haiku-5-5", "max_tokens": 5,
                            "messages": [{"role": "user", "content": "Say OK."}]}, timeout=30)
    if r.status_code == 200:
        return "PASS", "Claude answered"
    return "FAIL", f"Claude answered HTTP {r.status_code}. Check the key and billing at console.anthropic.com"


def check_graph_live():
    token = ENV.get("FB_PAGE_ACCESS_TOKEN") or os.environ.get("FB_PAGE_ACCESS_TOKEN")
    page = ENV.get("FB_PAGE_ID") or os.environ.get("FB_PAGE_ID")
    if not token or not page:
        return "FAIL", "FB_PAGE_ACCESS_TOKEN or FB_PAGE_ID is not set"
    r = requests.get(f"https://graph.facebook.com/v23.0/{page}", params={"fields": "name", "access_token": token}, timeout=20)
    if r.status_code == 200 and r.json().get("name"):
        return "PASS", f"Facebook Page reachable: {r.json()['name']}"
    err = (r.json().get("error") or {}).get("message", "") if r.headers.get("content-type", "").startswith("application/json") else ""
    return "FAIL", f"Facebook refused the Page token (HTTP {r.status_code}) {err[:120]}. Re-run setup_token.py"


def check_dashboard_muse_paths():
    if not _create_template_ids():
        return "INFO", "Create is switched off for this version, so the Create plan check is skipped. This is expected, not a fault"
    r = requests.post(f"{DASHBOARD}/film/plan", json={"genre": "ugc", "inputs": {"product": "a quick test", "benefit": "none"}}, timeout=90)
    if r.status_code == 401:
        return "WARN", "Create plan needs a Facebook sign-in in the dashboard browser; sign in, then run this check again"
    data = r.json() if r.status_code == 200 else {}
    hook = (data.get("hook") or "").strip()
    if hook:
        return "PASS", "Create teleprompter plan came back with text (Muse path works end to end)"
    return "FAIL", "Create plan came back empty. Muse is not answering through the dashboard; restart the dashboard and check the Muse key"


def main() -> int:
    live = "--live" in sys.argv
    print("Abra preflight" + (" (live: includes paid calls)" if live else " (free checks; add --live for paid calls)"))
    print("-" * 70)
    for name, fn in [("Dashboard", check_dashboard), ("Bot service", check_bot), ("Keys", check_keys),
                     ("ffmpeg", check_ffmpeg), ("Database", check_db), ("Create templates", check_film_genres),
                     ("Internet", check_internet)]:
        check(name, fn)
    if live:
        for name, fn in [("Muse (live)", check_muse_live), ("Claude (live)", check_claude_live),
                         ("Facebook Page (live)", check_graph_live), ("Create plan (live)", check_dashboard_muse_paths)]:
            check(name, fn)
    else:
        print("[SKIP] Muse, Claude, Facebook Page and Create plan: run with --live to check them")
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    infos = [r for r in RESULTS if r[0] == "INFO"]
    print("-" * 70)
    print(f"{len(RESULTS) - len(fails) - len(warns) - len(infos)} pass, {len(infos)} info, {len(warns)} warn, {len(fails)} fail")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
