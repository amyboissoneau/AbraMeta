"""Builds the sample database for the hosted judge demo.

Everything here is INVENTED for demonstration: the page, the creator, the numbers and the spend.
No real account, token, key or personal data is written. Run once at image build time:

    python judge/seed.py          # writes state.db in the project folder
"""
import math
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# This script writes a database. It must only ever run inside the judge build, never on the owner's Mac.
if os.environ.get("ABRA_JUDGE_MODE") != "1":
    sys.exit("refusing to run: the sample seed only runs inside the judge build (ABRA_JUDGE_MODE=1)")
sys.path.insert(0, str(ROOT))

import bot as B  # noqa: E402  (creates the same tables the real app uses)

PAGE_ID = "demo-page-0001"
PAGE_NAME = "Demo Creator Page (sample data)"
CREATOR = "Demo Creator"
DAYS = 45
random.seed(7)  # the same sample every build


def main() -> None:
    db = B.init_db()
    now = datetime.now(timezone.utc)
    db.execute("CREATE TABLE IF NOT EXISTS page_insights (taken_at TEXT, page_id TEXT, metric TEXT, value INTEGER, "
               "PRIMARY KEY (taken_at, page_id, metric))")
    db.execute("CREATE TABLE IF NOT EXISTS profile (fb_id TEXT PRIMARY KEY, name TEXT, email TEXT, "
               "picture_path TEXT, connected_at TEXT)")
    db.execute("INSERT OR REPLACE INTO profile (fb_id, name, email, picture_path, connected_at) "
               "VALUES (?, ?, '', '', ?)", ("demo-creator", CREATOR, now.isoformat()))
    db.execute("CREATE TABLE IF NOT EXISTS pages (page_id TEXT PRIMARY KEY, name TEXT, token TEXT, "
               "enabled INTEGER NOT NULL DEFAULT 1)")
    # a placeholder token: this build never calls Facebook, and the bot is not run here
    db.execute("INSERT OR REPLACE INTO pages (page_id, name, token, enabled) VALUES (?, ?, ?, 1)",
               (PAGE_ID, PAGE_NAME, "sample-only-no-real-token"))

    # daily insights: a gentle growth curve with some weekly rhythm
    metrics = {
        "page_views_total": (900, 14),
        "page_video_views": (4200, 60),
        "page_post_engagements": (520, 9),
        "page_follows": (38, 0.4),
    }
    for back in range(DAYS, -1, -1):
        day = (date.today() - timedelta(days=back)).isoformat()
        growth = 1 + (DAYS - back) / DAYS * 0.35
        weekly = 1 + 0.18 * math.sin((DAYS - back) / 7 * 2 * math.pi)
        for metric, (base, noise) in metrics.items():
            value = round(base * growth * weekly + random.uniform(-noise, noise) * 10)
            db.execute("INSERT OR REPLACE INTO page_insights (taken_at, page_id, metric, value) "
                       "VALUES (?, ?, ?, ?)", (day, PAGE_ID, metric, max(0, value)))

    # sample spend for the Money view: a few calls a day for two weeks (estimates, like the real ledger)
    kinds = [("claude", "reply", 900, 220, 0.0045), ("claude", "vision", 2400, 300, 0.012),
             ("muse", "triage", 600, 60, 0.0009), ("muse", "wonder_analyst", 3000, 700, 0.0060),
             ("muse", "schedule_copy", 1200, 300, 0.0023)]
    for back in range(14, -1, -1):
        for provider, kind, tin, tout, cost in kinds:
            n = random.randint(2, 9)
            when = (now - timedelta(days=back, hours=random.randint(0, 20))).isoformat()
            db.execute("INSERT INTO spend VALUES (?, ?, ?, ?, ?, ?)",
                       (when, provider, kind, tin * n, tout * n, round(cost * n, 6)))

    db.commit()
    db.close()
    print("sample database written:", ROOT / "state.db")


if __name__ == "__main__":
    main()
