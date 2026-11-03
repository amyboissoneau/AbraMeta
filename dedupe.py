"""One-time cleanup: remove duplicate bot replies.

For every comment the bot replied to, checks how many replies the PAGE has
posted underneath. If there is more than one from today (the bot's first day),
keeps the FIRST and deletes the extras. Never touches replies from before
today, so the page's older manual replies are safe.

Run:  python dedupe.py
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from bot import PAGE_ID, graph_delete, graph_get, init_db, log, validate_setup

TODAY = datetime.now(timezone.utc).date().isoformat()


def main() -> None:
    validate_setup()
    db = init_db()
    parents = [r[0] for r in db.execute(
        "SELECT comment_id FROM handled WHERE action = 'replied'"
    ).fetchall()]
    log.info("Checking %d replied-to comments for duplicates...", len(parents))

    removed = 0
    for cid in parents:
        try:
            data = graph_get(f"{cid}/comments",
                             fields="id,message,from{id},created_time", limit=100)
        except RuntimeError as e:
            log.error("Could not read replies of %s: %s", cid, e)
            continue

        # only today's page replies count: duplicates come from the bot's first day, and an
        # older manual reply must never be the one kept or deleted
        today_replies = [c for c in data.get("data", [])
                         if c.get("from", {}).get("id") == PAGE_ID
                         and c.get("created_time", "").startswith(TODAY)]
        if len(today_replies) < 2:
            continue

        today_replies.sort(key=lambda c: c.get("created_time", ""))
        extras = today_replies[1:]   # keep the first, delete the rest
        for extra in extras:
            try:
                graph_delete(extra["id"])
                removed += 1
                log.info("DELETED duplicate under %s: \"%s\"",
                         cid, (extra.get("message") or "")[:70])
                time.sleep(2)
            except RuntimeError as e:
                log.error("Delete failed for %s: %s", extra["id"], e)

    log.info("Dedupe complete: %d duplicate replies removed.", removed)


if __name__ == "__main__":
    main()
