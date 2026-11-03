"""Content Scheduler brain: learn what reaches, when to post, and draft copy.

Grounded in the page's own history (snapshots, post_context, and each video's transcript when one exists). 'Best reach' and 'best time' come from that history, with fixed default hours when the history is too thin. Muse Spark drafts the title, description and hashtags from the new video and the page's top-reaching posts.

Nothing here publishes — it only reads history and produces drafts.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timedelta, timezone

# Heuristic reach weights, not measured against real reach: shares 5, comments 3, reactions 1.
_W_REACT, _W_COMMENT, _W_SHARE = 1.0, 3.0, 5.0
from zoneinfo import ZoneInfo as _ZoneInfo
_ET = _ZoneInfo("America/New_York")  # New York time: EST or EDT, switches on its own


def _latest_scores(db: sqlite3.Connection, page_id: str | None = None) -> dict[str, float]:
    """post_id -> reach score, using each post's most recent snapshot."""
    rows = db.execute(
        """SELECT s.post_id, s.comments, s.reactions, s.shares
           FROM snapshots s
           JOIN (SELECT post_id, MAX(taken_at) mx FROM snapshots GROUP BY post_id) l
             ON l.post_id = s.post_id AND l.mx = s.taken_at""").fetchall()
    out = {}
    for pid, c, rx, sh in rows:
        if page_id and not str(pid).startswith(page_id):
            continue
        out[pid] = (rx or 0) * _W_REACT + (c or 0) * _W_COMMENT + (sh or 0) * _W_SHARE
    return out


def top_themes(db: sqlite3.Connection, page_id: str | None = None, n: int = 8) -> list[dict]:
    """Top-reaching posts with their content description — what themes travel."""
    scores = _latest_scores(db, page_id)
    if not scores:
        return []
    desc = {r[0]: r[1] for r in db.execute("SELECT post_id, description FROM post_context")}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    out = []
    for pid, sc in ranked:
        d = (desc.get(pid) or "").strip()
        if not d:
            continue
        out.append({"post_id": pid, "score": round(sc, 1), "desc": d[:400]})
        if len(out) >= n:
            break
    return out


def best_slots(db: sqlite3.Connection, page_id: str | None = None, top: int = 5) -> list[dict]:
    """Rank hour-of-day (ET) by average engagement, bucketed by when the bot cached each post's context (post_context.created_at), not the post's publish time."""
    scores = _latest_scores(db, page_id)
    created = {r[0]: r[1] for r in db.execute(
        "SELECT post_id, created_at FROM post_context WHERE created_at IS NOT NULL")}
    buckets: dict[int, list[float]] = {}
    for pid, sc in scores.items():
        ca = created.get(pid)
        if not ca:
            continue
        try:
            dt = datetime.fromisoformat(ca).astimezone(_ET)
        except Exception:
            continue
        buckets.setdefault(dt.hour, []).append(sc)
    rows = [{"hour": h, "avg_reach": round(sum(v) / len(v), 1), "n": len(v)}
            for h, v in buckets.items() if len(v) >= 2]
    rows.sort(key=lambda r: r["avg_reach"], reverse=True)
    for r in rows:
        h = r["hour"]
        ampm = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        r["label"] = f"{h12} {ampm} ET"
    return rows[:top]


def propose_slots(db, page_id, taken_times: list[datetime] | None = None,
                  count: int = 1, min_gap_hours: int = 3, lead_hours: int = 1) -> list[datetime]:
    """Pick the next best-time slot(s) that don't collide with already-taken times.

    `taken_times` are datetimes already scheduled (from the Graph API) — we never
    put two posts in the same slot or within `min_gap_hours` of each other.
    """
    slots = best_slots(db, page_id)
    best_hours = [s["hour"] for s in slots] or [12, 19, 10]  # sane fallback
    taken = list(taken_times or [])
    now = datetime.now(_ET)
    chosen: list[datetime] = []
    day = now.date()
    for _ in range(14):  # search up to two weeks out
        for h in best_hours:
            cand = datetime(day.year, day.month, day.day, h, 0, tzinfo=_ET)
            if cand < now + timedelta(hours=lead_hours):
                continue
            clash = any(abs((cand - t).total_seconds()) < min_gap_hours * 3600
                        for t in taken + chosen)
            if clash:
                continue
            chosen.append(cand)
            if len(chosen) >= count:
                return chosen
        day = day + timedelta(days=1)
    return chosen


_DRAFT_INSTRUCTIONS = (
    "You are Muse, a Facebook growth strategist for a creator. Your copy MUST be about the "
    "SPECIFIC subject of THIS video — read its transcript and on-screen description carefully "
    "and write titles that reference what is actually said/shown. Do NOT write generic hooks "
    "that could fit any video. Use the creator's TOP-REACHING past posts only to match tone, "
    "format and energy — never to borrow their topics.\n"
    "Return STRICT JSON only, no prose:\n"
    '{"titles": ["hook option 1","option 2","option 3"], '
    '"description": "2-3 sentence caption about THIS video, natural, no hashtag spam", '
    '"hashtags": ["#tag", ...up to 8 tags relevant to THIS video]}\n'
    "SEARCH: start each title with the main phrase someone would type to find this video, inside the first "
    "40 characters, in plain descriptive words. Never claim search volume or trends you were not given.\n"
    "Each title is a short scroll-stopping hook (<70 chars) grounded in the actual content. "
    "No clickbait lies, max 1-2 emojis, never beg for likes/follows. If the transcript is "
    "empty, base everything on the on-screen description.\n"
    "ABSOLUTE RULES — never break these:\n"
    "1) NEVER add any credit, attribution, source, or @-mention — no 'credit to', no creator "
    "handles, no @usernames, in the titles OR the caption, EVER. Not even if you think you "
    "know who made it.\n"
    "2) NEVER invent specific facts, numbers, measurements, distances, speeds, dates or "
    "statistics. Only state a number if it clearly appears in the transcript or the on-screen "
    "description above. If you are unsure, write the hook WITHOUT a number."
)


def draft_copy(muse_call, db, transcript: str, content_desc: str,
               page_id: str | None = None) -> dict:
    """Muse Spark drafts title/description/hashtags, grounded in top performers.

    `muse_call` is bot._muse_call (instructions, input_text, db, kind) -> str.
    Returns {} if Muse is unavailable so the caller can fall back.
    """
    themes = top_themes(db, page_id, n=6)
    theme_blob = "\n".join(f"- (reach {t['score']}) {t['desc'][:160]}" for t in themes) or "(no history yet)"
    inp = (f"NEW VIDEO transcript:\n{(transcript or '(no speech)')[:1500]}\n\n"
           f"NEW VIDEO on-screen description:\n{(content_desc or '(none)')[:600]}\n\n"
           f"This page's TOP-REACHING past posts:\n{theme_blob}")
    raw = ""
    try:
        raw = muse_call(_DRAFT_INSTRUCTIONS, inp, db, "schedule_copy") or ""
    except Exception:
        raw = ""
    if not raw:
        return {}
    # tolerate code fences / stray text around the JSON
    s, e = raw.find("{"), raw.rfind("}")
    if s >= 0 and e > s:
        try:
            d = json.loads(raw[s:e + 1])
            d["titles"] = [t for t in d.get("titles", []) if t][:3]
            d["hashtags"] = [h if h.startswith("#") else "#" + h for h in d.get("hashtags", [])][:8]
            return d
        except Exception:
            pass
    return {}


if __name__ == "__main__":  # quick self-test against the live DB (no network for data parts)
    db = sqlite3.connect("state.db")
    pid = "example-page-id"   # put your own page id here to test against your data
    print("=== TOP THEMES (what reaches) ===")
    for t in top_themes(db, pid, 5):
        print(f"  reach {t['score']:>7}  {t['desc'][:70]}")
    print("\n=== BEST SLOTS (when to post) ===")
    for s in best_slots(db, pid):
        print(f"  {s['label']:>8}  avg reach {s['avg_reach']:>7}  (n={s['n']})")
    print("\n=== PROPOSED NEXT 3 SLOTS (conflict-free) ===")
    for dt in propose_slots(db, pid, taken_times=[], count=3):
        print("  ", dt.strftime("%a %b %d"), f"{dt.hour % 12 or 12} {dt.strftime('%p')} ET")
