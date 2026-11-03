"""Abra watchdog: keeps the dashboard and the bot answering, and warns about low disk space.

Runs every two minutes from launchd (see the watchdog plist you install for LAUNCHD_PREFIX). It never deletes
anything. It only restarts the two services and writes warnings to watchdog.log.

  - Dashboard: 3 failed checks in a row -> restart the dashboard service.
  - Bot: no new log line for 30 minutes -> restart the bot service (a hung bot does not
    crash, so launchd's KeepAlive alone would never restart it).
  - Disk: under 1 GB free -> warning in watchdog.log and a macOS notification.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent
STATE = BASE / ".watchdog.json"
LOG = BASE / "watchdog.log"
DASHBOARD = f"http://127.0.0.1:{(dotenv_values(BASE / '.env').get('ABRA_PORT') or os.environ.get('ABRA_PORT') or '8765').strip()}/"   # ABRA_PORT, default 8765
BOT_LOG = BASE / "bot.log"
UID = str(os.getuid()) if hasattr(os, "getuid") else "501"   # launchd only exists on macOS
LAUNCHD_PREFIX = (dotenv_values(BASE / ".env").get("LAUNCHD_PREFIX") or os.environ.get("LAUNCHD_PREFIX") or "com.abra").strip()
LIVEVIEW = f"gui/{UID}/{LAUNCHD_PREFIX}.fbcommentbot.liveview"
BOT = f"gui/{UID}/{LAUNCHD_PREFIX}.fbcommentbot"
BOT_SILENT_LIMIT = 30 * 60   # seconds with no new bot log line before a restart
DISK_WARN_BYTES = 1 * 1024 ** 3   # warn only when the Mac is close to full
DASHBOARD_FAILS_LIMIT = 3


def log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"dashboard_fails": 0}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state), encoding="utf-8")


def restart(label: str, why: str) -> None:
    if sys.platform != "darwin":   # restarts go through launchd, which only exists on macOS
        log(f"NOT RESTARTED (macOS launchd only): {label}: {why}")
        return
    subprocess.run(["launchctl", "kickstart", "-k", label], capture_output=True)
    log(f"RESTART {label}: {why}")


def check_dashboard(state: dict) -> None:
    try:
        ok = requests.get(DASHBOARD, timeout=8).status_code == 200
    except Exception:
        ok = False
    if ok:
        state["dashboard_fails"] = 0
        return
    state["dashboard_fails"] = state.get("dashboard_fails", 0) + 1
    log(f"dashboard not answering ({state['dashboard_fails']}/{DASHBOARD_FAILS_LIMIT})")
    if state["dashboard_fails"] >= DASHBOARD_FAILS_LIMIT:
        restart(LIVEVIEW, "dashboard stopped answering")
        state["dashboard_fails"] = 0


def check_bot() -> None:
    if not BOT_LOG.exists():
        return
    silent = time.time() - BOT_LOG.stat().st_mtime
    if silent > BOT_SILENT_LIMIT:
        restart(BOT, f"no bot log line for {int(silent // 60)} minutes")


def check_disk(state: dict) -> None:
    free = shutil.disk_usage(BASE).free
    if free < DISK_WARN_BYTES:
        msg = f"LOW DISK: {free / 1024 ** 3:.1f} GB free. Free space now or the app can stop writing."
        log(msg)
        if time.time() - state.get("last_disk_alert", 0) > 3600:   # one notification an hour, not every run
            if sys.platform == "darwin":   # macOS notification; other systems read watchdog.log
                subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Abra watchdog"'],
                               capture_output=True)
            state["last_disk_alert"] = time.time()
            save_state(state)


def main() -> None:
    state = load_state()
    check_dashboard(state)
    check_bot()
    check_disk(state)
    save_state(state)


if __name__ == "__main__":
    main()
