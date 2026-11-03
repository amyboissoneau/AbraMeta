"""Daily engagement analytics for the primary page.

Takes a snapshot of the engagement on the page's 50 most recent posts (comments / reactions /
shares) plus the page follower count, stores it in state.db, and prints a
report comparing against the previous snapshot:

  - page follower change
  - per-post engagement growth since last snapshot
  - what the assistant did in the last 24h (replies posted, comments liked)
  
Run daily:  python analytics.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bot
from bot import fetch_recent_posts, graph_get, init_db, load_pages, validate_setup

POSTS_TO_TRACK = 50


INSIGHT_METRICS = ["page_views_total", "page_video_views", "page_post_engagements", "page_follows", "monetization_approximate_earnings"]


def snapshot_insights(db) -> None:
    """Store daily Facebook Insights values for every enabled page."""
    db.execute("""CREATE TABLE IF NOT EXISTS page_insights (
        taken_at TEXT, page_id TEXT, metric TEXT, value INTEGER,
        PRIMARY KEY (taken_at, page_id, metric))""")
    for pid, name, token in load_pages(db):
        bot.PAGE_ID, bot.PAGE_TOKEN = pid, token
        for metric in INSIGHT_METRICS:
            try:
                r = graph_get(f"{pid}/insights", metric=metric, period="day")
                for v in r.get("data", [{}])[0].get("values", []):
                    db.execute("INSERT OR REPLACE INTO page_insights VALUES (?, ?, ?, ?)",
                               (v["end_time"][:10], pid, metric, v.get("value", 0)))
            except RuntimeError:
                pass
    db.commit()
    bot.PAGE_ID, bot.PAGE_TOKEN = bot.PRIMARY_PAGE_ID, bot.PRIMARY_PAGE_TOKEN


def print_insights(db) -> None:
    for pid, name, _tok in load_pages(db):
        print(f"\nViews & reach — {name} (last 7 days):")
        for metric in INSIGHT_METRICS:
            rows = db.execute(
                "SELECT taken_at, value FROM page_insights WHERE page_id=? AND metric=? "
                "ORDER BY taken_at DESC LIMIT 7", (pid, metric)).fetchall()
            if rows:
                trend = ", ".join(f"{d[5:]}: {v}" for d, v in reversed(rows))
                print(f"  {metric:26s} {trend}")


def take_snapshot(db) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    for pid, name, token in load_pages(db):
        bot.PAGE_ID, bot.PAGE_TOKEN = pid, token
        try:
            data = graph_get(
                f"{pid}/published_posts",
                fields="id,message,created_time,"
                       "comments.summary(true).limit(0),reactions.summary(true).limit(0),shares",
                limit=POSTS_TO_TRACK,
            )
        except RuntimeError:
            continue
        for p in data.get("data", []):
            db.execute(
                "INSERT OR REPLACE INTO snapshots (taken_at, post_id, comments, reactions, shares) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    today,
                    p["id"],
                    p.get("comments", {}).get("summary", {}).get("total_count", 0),
                    p.get("reactions", {}).get("summary", {}).get("total_count", 0),
                    p.get("shares", {}).get("count", 0),
                ),
            )
    bot.PAGE_ID, bot.PAGE_TOKEN = bot.PRIMARY_PAGE_ID, bot.PRIMARY_PAGE_TOKEN
    try:
        page = graph_get(bot.PRIMARY_PAGE_ID, fields="followers_count,fan_count")
        followers = page.get("followers_count") or page.get("fan_count")
        db.execute("INSERT OR REPLACE INTO page_snapshots (taken_at, followers) VALUES (?, ?)",
                   (today, followers))
    except RuntimeError:
        pass
    db.commit()
    return today


def previous_snapshot_date(db, today: str) -> str | None:
    row = db.execute(
        "SELECT MAX(taken_at) FROM snapshots WHERE taken_at < ?", (today,)
    ).fetchone()
    return row[0] if row and row[0] else None


def caption_for(post_id: str, cache={}) -> str:
    if post_id not in cache:
        try:
            p = graph_get(post_id, fields="message")
            cache[post_id] = (p.get("message") or "(no caption)").replace("\n", " ")[:55]
        except RuntimeError:
            cache[post_id] = post_id
    return cache[post_id]


def main() -> None:
    validate_setup()
    db = init_db()
    today = take_snapshot(db)
    prev = previous_snapshot_date(db, today)

    print(f"\n=== Engagement report — {today} ===")

    # Followers
    rows = db.execute("SELECT taken_at, followers FROM page_snapshots ORDER BY taken_at").fetchall()
    if rows:
        latest = rows[-1]
        base = next((r for r in reversed(rows[:-1])), None)
        delta = f" ({latest[1]-base[1]:+d} since {base[0]})" if base and base[1] and latest[1] else ""
        print(f"Followers: {latest[1]}{delta}")

    # Bot activity last 24h
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    replies = db.execute("SELECT COUNT(*) FROM handled WHERE action='replied' AND handled_at > ?", (cutoff,)).fetchone()[0]
    likes = db.execute("SELECT COUNT(*) FROM liked WHERE liked_at > ?", (cutoff,)).fetchone()[0]
    skips = db.execute("SELECT COUNT(*) FROM handled WHERE action='skipped' AND handled_at > ?", (cutoff,)).fetchone()[0]
    print(f"Assistant activity (24h): {replies} replies posted, {likes} comments liked, {skips} skipped")

    # Per-post growth vs previous snapshot
    if prev:
        print(f"\nEngagement growth since {prev}:")
        cur = {r[0]: r[1:] for r in db.execute(
            "SELECT post_id, comments, reactions, shares FROM snapshots WHERE taken_at=?", (today,))}
        old = {r[0]: r[1:] for r in db.execute(
            "SELECT post_id, comments, reactions, shares FROM snapshots WHERE taken_at=?", (prev,))}
        grew = []
        for pid, (c, r, s) in cur.items():
            oc, orr, os_ = old.get(pid, (0, 0, 0))
            dc, dr, ds = c - oc, r - orr, s - os_
            if dc or dr or ds:
                grew.append((dc + dr + ds, pid, dc, dr, ds, c, r, s))
        grew.sort(reverse=True)
        if not grew:
            print("  (no measurable change yet)")
        for _, pid, dc, dr, ds, c, r, s in grew[:10]:
            print(f"  +{dc} comments, +{dr} reactions, +{ds} shares "
                  f"(now {c}/{r}/{s}) | {caption_for(pid)}")
        totals = [sum(x) for x in zip(*[(g[2], g[3], g[4]) for g in grew])] if grew else [0, 0, 0]
        print(f"  TOTAL: +{totals[0]} comments, +{totals[1]} reactions, +{totals[2]} shares")
    else:
        print("\nFirst snapshot recorded — growth deltas start tomorrow.")

    snapshot_insights(db)
    print_insights(db)
    print("\nDone. Full data in state.db (snapshots + page_insights tables).")


if __name__ == "__main__":
    main()
