"""One-time backfill: reply to comments on the page's biggest 2026 posts.

Finds the most-engaged posts since Jan 1 2026, then runs each comment through Claude's reply drafting, with bot.py's persona and similar skip checks (no Muse triage, video context or Muse review), and POSTS the replies for real. It:

  - never touches a comment the page (or the live bot) already replied to
  - respects the shared reply cap (30 per 15 min, shared via state.db);
    when the cap is hit it waits and resumes, and it sleeps between replies, so a big backfill runs paced
  - paces itself between replies

Run:  python backfill.py            (or via nohup, see README)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import anthropic

from bot import (
    MAX_REPLIES_PER_WINDOW,
    THROTTLE_CODES,
    GraphError,
    PAGE_ID,
    draft_reply,
    fetch_comments,
    graph_get,
    init_db,
    like_comment,
    load_persona,
    log,
    mark_handled,
    page_already_replied,
    claim,
    page_reply_count,
    post_reply,
    record_like,
    release_claim,
    replies_in_window,
    throttle_seconds_remaining,
    trip_throttle,
    note_post_success,
    validate_setup,
)

SINCE = "2026-01-01"
TOP_POSTS = 10
SECONDS_BETWEEN_REPLIES = 8
CAP_WAIT_MINUTES = 3


def fetch_2026_posts_by_engagement() -> list[dict]:
    posts: list[dict] = []
    params = dict(
        fields="id,message,created_time,permalink_url,"
               "comments.summary(true).limit(0),reactions.summary(true).limit(0),shares",
        since=SINCE,
        limit=50,
    )
    data = graph_get(f"{PAGE_ID}/published_posts", **params)
    posts.extend(data.get("data", []))
    # one page of pagination (up to 100 posts) is read; a busier page is only partly covered
    next_url = data.get("paging", {}).get("next")
    if next_url:
        import requests
        more = requests.get(next_url, timeout=30).json()
        posts.extend(more.get("data", []))

    def engagement(p: dict) -> int:
        c = p.get("comments", {}).get("summary", {}).get("total_count", 0)
        r = p.get("reactions", {}).get("summary", {}).get("total_count", 0)
        s = p.get("shares", {}).get("count", 0)
        return c * 3 + r + s * 2   # comments weighted highest

    posts.sort(key=engagement, reverse=True)
    return posts[:TOP_POSTS]


def wait_for_cap(db) -> None:
    while True:
        pause = throttle_seconds_remaining(db)
        if pause > 0:
            log.info("Facebook throttle active — sleeping %.0f min", pause / 60 + 1)
            time.sleep(pause + 60)
            continue
        if replies_in_window(db) >= MAX_REPLIES_PER_WINDOW:
            log.info("Reply cap (%d/window) reached — waiting %d minutes...",
                     MAX_REPLIES_PER_WINDOW, CAP_WAIT_MINUTES)
            time.sleep(CAP_WAIT_MINUTES * 60)
            continue
        break


def already_replied(db, comment_id: str) -> bool:
    row = db.execute(
        "SELECT action FROM handled WHERE comment_id = ?", (comment_id,)
    ).fetchone()
    return bool(row and row[0] == "replied")


def main() -> None:
    validate_setup()
    db = init_db()
    persona = load_persona()

    posts = fetch_2026_posts_by_engagement()
    log.info("Backfilling top %d posts of 2026:", len(posts))
    for p in posts:
        cap = (p.get("message") or "(no caption)").replace("\n", " ")[:70]
        n = p.get("comments", {}).get("summary", {}).get("total_count", 0)
        log.info("  %s | %d comments | %s", p["id"], n, cap)

    replied = skipped = 0
    for post in posts:
        try:
            comments = fetch_comments(post["id"])
        except RuntimeError as e:
            log.error("Cannot read comments on %s: %s", post["id"], e)
            continue
        for comment in comments:
            cid = comment["id"]
            if already_replied(db, cid):
                continue
            if comment.get("from", {}).get("id") == PAGE_ID:
                continue
            if comment.get("is_hidden"):
                continue
            if not (comment.get("message") or "").strip():
                continue
            if page_already_replied(comment):
                mark_handled(db, cid, "skipped", "page already replied")
                continue

            wait_for_cap(db)
            if not claim(db, cid):   # live bot owns or already replied
                continue
            snippet = (comment.get("message") or "").replace("\n", " ")[:70]
            try:
                decision = draft_reply(persona, post, comment)
            except anthropic.APIStatusError as e:
                log.error("Claude error (%s) on %s — skipping for now", e.status_code, cid)
                release_claim(db, cid)
                time.sleep(30)
                continue

            if decision.get("should_like"):
                try:
                    like_comment(cid)
                    record_like(db, cid)
                    log.info("LIKE   %s | \"%s\"", cid, snippet)
                except GraphError as e:
                    if e.code in THROTTLE_CODES:
                        trip_throttle(db, str(e))
                        continue
                    log.error("Like failed on %s: %s", cid, e)

            if not decision.get("should_reply"):
                reason = decision.get("skip_reason") or "model chose to skip"
                log.info("SKIP   %s | \"%s\" | %s", cid, snippet, reason)
                mark_handled(db, cid, "skipped", reason)
                skipped += 1
                continue

            reply = decision["reply"].strip()
            if page_reply_count(cid) > 0:   # fresh check with Facebook itself
                log.info("SKIP   %s | \"%s\" | page already replied (fresh check)", cid, snippet)
                mark_handled(db, cid, "skipped", "page already replied (fresh check)")
                continue
            try:
                post_reply(cid, reply)
            except GraphError as e:
                release_claim(db, cid)
                if e.code in THROTTLE_CODES:
                    trip_throttle(db, str(e))
                    continue
                log.error("Post failed on %s: %s", cid, e)
                time.sleep(30)
                continue
            note_post_success(db)
            log.info("REPLY  %s | \"%s\" -> \"%s\"", cid, snippet, reply)
            mark_handled(db, cid, "replied", reply)
            replied += 1
            time.sleep(SECONDS_BETWEEN_REPLIES)

    log.info("Backfill complete: %d replies posted, %d skipped.", replied, skipped)


if __name__ == "__main__":
    main()
