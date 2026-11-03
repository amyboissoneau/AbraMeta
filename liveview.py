"""Live feed + video targeting dashboard for the Facebook assistant.

Tiny local web server: http://localhost:8765 (or the port set by ABRA_PORT)
- Live stream of the bot's replies / likes / skips
- "Target videos" panel with thumbnails and two tabs:
    Most Recent — latest 50 posts
    Most Viral — past year's posts ranked by engagement (auto-updating)
  Check videos + Submit => the bot gives them priority engagement.
Dashboard on localhost; the phone link on your wifi is HTTPS and requires the access token.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
from zoneinfo import ZoneInfo as _ZI
import ssl
import sys
import subprocess
import meta_live
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bot as B

BASE = Path(__file__).resolve().parent
# ABRA_JUDGE_MODE=1 is the hosted sample build for the hackathon judges (see Dockerfile): no owner keys,
# no token gate, sample data, listens on all addresses on the host's PORT, HTTPS is terminated by the host.
JUDGE_MODE = os.environ.get("ABRA_JUDGE_MODE", "0") == "1"
HOST = os.environ.get("ABRA_HOST", "127.0.0.1")
# Ports come from the command line or .env (ABRA_PORT, ABRA_HTTPS_PORT). A blank value means the default.
# The hosted judge build also takes the host's PORT variable when ABRA_PORT is not set.
_DEFAULT_PORT = (os.environ.get("PORT") or "8765") if JUDGE_MODE else "8765"
PORT = int(os.environ.get("ABRA_PORT") or _DEFAULT_PORT)
# same app over TLS (self-signed cert in certs/) so phones can use the mic (getUserMedia needs a secure context)
HTTPS_PORT = int(os.environ.get("ABRA_HTTPS_PORT") or "8766")
LINE = re.compile(
    r"^(?P<time>\d\d:\d\d:\d\d) INFO (?P<kind>REPLY|LIKE|SKIP|DRYRUN|DRYLIKE)\s+"
    r"(?P<cid>\S+) \| \"(?P<comment>.*?)\"(?: (?:->|\|) \"?(?P<detail>.*?)\"?)?$"
)

POST_FIELDS = ("id,message,created_time,permalink_url,full_picture,shares,"
               "comments.summary(true).limit(0),reactions.summary(true).limit(0)")

_cache: dict = {"recent": {"at": 0.0, "data": []}, "viral": {"at": 0.0, "data": []},
                "trending": {"at": 0.0, "data": []}}


def db_conn() -> sqlite3.Connection:
    db = sqlite3.connect(BASE / "state.db", timeout=15, isolation_level=None)
    db.execute("""CREATE TABLE IF NOT EXISTS targets (
        post_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, added_at TEXT)""")
    return db


def _settings(db):
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")


def engagement_state() -> dict:
    db = db_conn()
    _settings(db)
    enabled = db.execute("SELECT COUNT(*) FROM pages WHERE enabled=1").fetchone()[0]
    db.close()
    return {"paused": enabled == 0}


def set_engagement(paused: bool) -> None:
    db = db_conn()
    _settings(db)
    if paused:
        ids = [r[0] for r in db.execute("SELECT page_id FROM pages WHERE enabled=1")]
        if ids:
            db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('eng_saved', ?)",
                       (json.dumps(ids),))
        db.execute("UPDATE pages SET enabled=0")
    else:
        row = db.execute("SELECT value FROM settings WHERE key='eng_saved'").fetchone()
        ids = json.loads(row[0]) if row and row[0] else []
        if ids:
            for pid in ids:
                db.execute("UPDATE pages SET enabled=1 WHERE page_id=?", (pid,))
        else:
            db.execute("UPDATE pages SET enabled=1")
    db.close()


PUB_MODES = ("review", "auto")  # review = approve each; auto = full auto

def get_pub_mode(page_id: str) -> str:
    db = db_conn()
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    row = db.execute("SELECT value FROM settings WHERE key=?", (f"pub_mode:{page_id}",)).fetchone()
    db.close()
    m = (row[0] if row else "review")
    return m if m in PUB_MODES else "review"

def set_pub_mode(page_id: str, mode: str) -> None:
    if mode not in PUB_MODES:
        mode = "review"
    db = db_conn()
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f"pub_mode:{page_id}", mode))
    db.close()


DM_MODES = ("auto", "flag", "off")

def get_dm_mode(page_id: str) -> str:
    db = db_conn()
    row = db.execute("SELECT value FROM settings WHERE key=?", (f"dm_mode:{page_id}",)).fetchone()
    db.close()
    m = (row[0] if row else "auto")
    return m if m in DM_MODES else "auto"

def set_dm_mode(page_id: str, mode: str) -> None:
    if mode not in DM_MODES:
        mode = "auto"
    db = db_conn()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f"dm_mode:{page_id}", mode))
    db.close()


def dm_recent(limit: int = 30) -> dict:
    """Recent DM activity (what came in, what Nico did) for the dashboard."""
    db = db_conn()
    try:
        rows = db.execute("SELECT page_id,sender,incoming,category,action,reply,at FROM dm_log "
                          "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        rows = []
    db.close()
    names = {}
    try:
        for pid, name, _t in enabled_pages():
            names[pid] = name
    except Exception:
        pass
    log = [{"page": names.get(r[0], r[0]), "sender": r[1], "incoming": r[2], "category": r[3],
            "action": r[4], "reply": r[5], "at": r[6]} for r in rows]
    return {"log": log,
            "replied": sum(1 for r in log if r["action"] == "replied"),
            "flagged": sum(1 for r in log if r["action"] == "flagged"),
            "modes": [{"id": pid, "name": name, "mode": get_dm_mode(pid)} for pid, name, _t in
                      (enabled_pages() if 'enabled_pages' in globals() else [])]}


def list_pages() -> list[dict]:
    db = db_conn()
    db.execute("""CREATE TABLE IF NOT EXISTS pages (
        page_id TEXT PRIMARY KEY, name TEXT, token TEXT, enabled INTEGER NOT NULL DEFAULT 1)""")
    if B.PRIMARY_PAGE_ID:   # only a real configured page; a GET must not write a placeholder row
        db.execute("INSERT OR IGNORE INTO pages (page_id, name, token, enabled) VALUES (?, ?, ?, 1)",
                   (B.PRIMARY_PAGE_ID, B.PRIMARY_PAGE_NAME, B.PRIMARY_PAGE_TOKEN))
    rows = db.execute("SELECT page_id, name, enabled FROM pages").fetchall()
    db.close()
    return [{"id": r[0], "name": r[1] or r[0], "enabled": bool(r[2]),
             "pub_mode": get_pub_mode(r[0])} for r in rows]


def enabled_pages() -> list[tuple[str, str, str]]:
    db = db_conn()
    rows = db.execute("SELECT page_id, name, token FROM pages WHERE enabled=1").fetchall()
    db.close()
    return [(r[0], r[1] or r[0], r[2] or B.PRIMARY_PAGE_TOKEN) for r in rows]


def set_page_enabled(page_id: str, enabled: bool) -> None:
    db = db_conn()
    db.execute("UPDATE pages SET enabled=? WHERE page_id=?", (1 if enabled else 0, page_id))
    db.close()


def _no_page_connected() -> bool:
    """True when this copy has no Facebook page token at all (env or saved pages)."""
    try:
        return not B.PRIMARY_PAGE_TOKEN and not _page_tokens()
    except Exception:
        return False


def _page_token(page_id: str) -> str:
    db = db_conn()
    row = db.execute("SELECT token FROM pages WHERE page_id=?", (page_id,)).fetchone()
    db.close()
    return (row[0] if row and row[0] else B.PRIMARY_PAGE_TOKEN)


def _ai_key_status() -> dict:
    """Which AI providers have a key set — booleans only, never the key values."""
    return {"anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "muse": bool(os.environ.get("META_MUSE_API_KEY"))}


def _save_ai_keys(form: dict) -> int:
    """Write the user's own API keys into this machine's .env (BYOK onboarding).
    Only the two known keys, non-empty. Values are never logged or echoed. Values are never logged or echoed."""
    env_path = BASE / ".env"
    pairs = {"ANTHROPIC_API_KEY": (form.get("anthropic") or [""])[0].strip(),
             "META_MUSE_API_KEY": (form.get("muse") or [""])[0].strip()}
    pairs = {k: v for k, v in pairs.items() if v}
    if not pairs:
        return 0
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out, seen = [], set()
    for ln in lines:
        key = ln.split("=", 1)[0].strip() if ("=" in ln and not ln.lstrip().startswith("#")) else None
        if key in pairs:
            out.append(f"{key}={pairs[key]}"); seen.add(key)
        else:
            out.append(ln)
    for k, v in pairs.items():
        if k not in seen:
            out.append(f"{k}={v}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except Exception:
        pass
    for k, v in pairs.items():      # let the running dashboard pick them up now
        os.environ[k] = v
    if "META_MUSE_API_KEY" in pairs:
        B.MUSE_API_KEY = pairs["META_MUSE_API_KEY"]
    if "ANTHROPIC_API_KEY" in pairs:
        B._OWNER_CLAUDE = B.anthropic.Anthropic(api_key=pairs["ANTHROPIC_API_KEY"])
    return len(pairs)


def _scheduled_times(page_id: str) -> list:
    """Already-scheduled post times (datetimes) so we never double-book a slot."""
    import requests as _r
    out = []
    try:
        tok = _page_token(page_id)
        for edge in ("scheduled_posts", "video_reels"):
            try:
                r = _r.get(f"{B.GRAPH}/{page_id}/{edge}",
                           params={"fields": "scheduled_publish_time", "access_token": tok, "limit": 50},
                           timeout=20).json()
                for row in r.get("data", []):
                    ts = row.get("scheduled_publish_time")
                    if ts:
                        out.append(datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(_ZI("America/New_York")))
            except Exception:
                pass
    except Exception:
        pass
    return out


def _ensure_content_queue(db):
    B.ensure_content_queue(db)   # the table and its older-database column upgrades live in bot.py, with the rest of the schema


def upcoming_content() -> dict:
    """Scheduled reels (content_queue) + pending story clips, for the Upcoming view."""
    db = db_conn()
    _ensure_content_queue(db)
    reels = []
    for r in db.execute("SELECT page_id,title,scheduled_for,thumbnail,stories,status FROM content_queue "
                        "WHERE status IN ('scheduled','dry_scheduled') ORDER BY scheduled_for DESC LIMIT 25"):
        reels.append({"page_id": r[0], "title": r[1], "scheduled_for": r[2],
                      "thumbnail": r[3], "stories": r[4] or 0, "status": r[5]})
    stories = []
    try:
        for r in db.execute("SELECT page_id, slot FROM story_queue WHERE posted_at IS NULL AND error IS NULL "
                            "AND (path LIKE '%story_%') ORDER BY slot LIMIT 30"):
            stories.append({"page_id": r[0], "slot": r[1]})
    except Exception:
        pass
    db.close()
    return {"reels": reels, "stories": stories}


def _ensure_checklist(db):
    db.execute("CREATE TABLE IF NOT EXISTS checklist (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "text TEXT NOT NULL, done INTEGER DEFAULT 0, created_at TEXT)")


def checklist_list() -> dict:
    db = db_conn(); _ensure_checklist(db)
    rows = db.execute("SELECT id, text, done FROM checklist ORDER BY done, id DESC").fetchall()
    db.close()
    return {"items": [{"id": r[0], "text": r[1], "done": bool(r[2])} for r in rows]}


def checklist_add(text) -> dict:
    text = (str(text or "")).strip()[:200]
    if not text:
        return {"ok": False}
    db = db_conn(); _ensure_checklist(db)
    cur = db.execute("INSERT INTO checklist (text, done, created_at) VALUES (?,0,?)",
                     (text, datetime.now(timezone.utc).isoformat()))
    iid = cur.lastrowid
    db.close()
    return {"ok": True, "id": iid, "text": text, "done": False}


def checklist_toggle(iid, done) -> dict:
    try:
        iid = int(iid)
    except (TypeError, ValueError):
        return {"ok": False}
    db = db_conn(); _ensure_checklist(db)
    db.execute("UPDATE checklist SET done=? WHERE id=?", (1 if done else 0, iid))
    db.close()
    return {"ok": True}


def checklist_del(iid) -> dict:
    try:
        iid = int(iid)
    except (TypeError, ValueError):
        return {"ok": False}
    db = db_conn(); _ensure_checklist(db)
    db.execute("DELETE FROM checklist WHERE id=?", (iid,))
    db.close()
    return {"ok": True}


def confirm_and_schedule(body: dict, dry_run: bool = False) -> str:
    """Approve a draft -> schedule the Reel on Facebook -> cut & queue 3 Stories.

    The creator's click (review mode) or a page's auto mode is the approval; this
    is where Sage schedules the Reel and queues its Stories for the bot to post. dry_run makes NO FB writes.
    """
    import content_publish as pub
    db = db_conn()
    _ensure_content_queue(db)
    pid = str(body.get("page_id", "")) or B.PRIMARY_PAGE_ID
    title = body.get("title", ""); desc = body.get("description", "")
    tags = body.get("hashtags", []); slot = body.get("slot", "")
    vfile = os.path.basename(body.get("video_file", "") or "")
    thumb = os.path.basename(body.get("thumbnail", "") or "")
    if vfile:   # the same video is never queued twice (a retry or a second click must not post it again)
        dup = db.execute("SELECT status FROM content_queue WHERE video_file=? AND status!='error' LIMIT 1", (vfile,)).fetchone()
        if dup:
            db.close()
            return f"error: this video is already queued ({dup[0]}). It is not scheduled twice; remove the earlier item first."
    cur = db.execute(
        "INSERT INTO content_queue (page_id,title,description,hashtags,slot,thumbnail,"
        "video_file,status,created_at,as_ad) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, title, desc, json.dumps(tags), slot, thumb, vfile, "approved",
         datetime.now(timezone.utc).isoformat(), 1 if body.get("as_ad") else 0))
    row_id = cur.lastrowid
    db.commit()

    vpath = BASE / "assets" / "drafts" / vfile if vfile else None
    if not vpath or not vpath.exists():
        db.execute("UPDATE content_queue SET status='error' WHERE id=?", (row_id,))
        db.commit(); db.close()
        return "error: the uploaded video was not found — re-drop it"

    caption = pub.compose_caption(title, desc, tags)
    thumb_path = str(BASE / "assets" / "drafts" / thumb) if thumb else ""
    # Reel captions: the same burn-in the Stories use, rendered to a copy so the raw take is untouched.
    # It fails closed: if the captions cannot be made, nothing is scheduled and the creator is told why.
    reel_path = str(vpath); cap_note = ""; cap_file = None
    if body.get("captions") and str(body.get("transcript") or "").strip():
        import content_pipeline as cp
        cap_file = BASE / "assets" / "drafts" / f"cap_{row_id}.mp4"
        try:
            cp.burn_reel_captions(str(vpath), str(body.get("transcript")), str(cap_file))
            reel_path = str(cap_file)
        except cp.CaptionSkip as e:
            cap_note = f" (posted without captions: {str(e)[:120]})"   # not portrait: the captions layer does not fit it
        except Exception as e:
            db.execute("UPDATE content_queue SET status='error' WHERE id=?", (row_id,))
            db.commit(); db.close()
            return "error: the captions could not be added, so nothing was scheduled. Untick Captions to schedule it without them. (" + str(e)[:120] + ")"
    try:
        res = pub.publish_reel(pid, _page_token(pid), reel_path, caption, slot,
                               dry_run=dry_run, thumb_path=thumb_path)
    except Exception as e:
        # the row was queued as approved above: an unexpected failure must not leave it approved
        db.execute("UPDATE content_queue SET status='error' WHERE id=?", (row_id,))
        db.commit(); db.close()
        return "error scheduling: " + str(e)[:150]
    finally:
        if cap_file is not None:
            try: os.remove(cap_file)
            except OSError: pass
    if not res.get("ok"):
        db.execute("UPDATE content_queue SET status='error' WHERE id=?", (row_id,))
        db.commit(); db.close()
        return "error scheduling: " + str(res.get("error", "unknown"))[:150]

    sched_for = res.get("scheduled_for") or slot
    db.execute("UPDATE content_queue SET status=?, video_id=?, scheduled_for=? WHERE id=?",
               ("dry_scheduled" if dry_run else "scheduled", res.get("video_id", ""), sched_for, row_id))
    db.commit()

    # Koa auto-protects it: when the Reel is scheduled, Koa adds it to the timestamped content-rights ledger.
    if not dry_run:
        try: rights_add(title or vfile or "Untitled post", note="auto-logged on publish")
        except Exception: pass

    # build Stories from the footage (posts handled by the story queue), using the
    # chosen template. In dry-run we skip queueing so nothing auto-posts during a test.
    n_stories = 0
    story_template = body.get("story_template", "smart")
    teaser_style = 1
    if story_template == "reaction_teaser":   # one-click: teaser (Pop style) + your reaction
        story_template = "teaser"; teaser_style = 1
    if story_template.startswith("teaser") and story_template[-1:].isdigit():
        teaser_style = max(1, min(3, int(story_template[-1]))); story_template = "teaser"
    if story_template not in ("chopped", "teaser", "smart", "none"):
        story_template = "smart"
    if not dry_run and story_template != "none":
        try:
            import content_pipeline as cp
            overlay_path = ""
            _fx = body.get("story_fx")
            if _fx:   # a cinematic effect on the Stories — chosen name, or auto-matched
                prompt = cp.fx_prompt(_fx if isinstance(_fx, str) else "", body.get("content_desc", "") or title)
                dest = str(BASE / "assets" / "drafts" / f"fx_{row_id}.jpg")
                overlay_path = cp.generate_overlay(prompt, dest, db=db) or ""
            reaction_path = ""         # creator's reaction sticker cutout
            rx_name = os.path.basename(body.get("reaction", "") or "")
            if rx_name.startswith("rx_") and rx_name.endswith(".png"):
                cand = REACT_DIR / rx_name
                if cand.exists():
                    reaction_path = str(cand)

            def _f(key, default):
                try:
                    return float(body.get(key))
                except (TypeError, ValueError):
                    return default
            rx_x = _f("reaction_x", None); rx_y = _f("reaction_y", None)
            rx_scale = _f("reaction_scale", None)
            n_stories = cp.make_stories(str(vpath), pid, db=db, base_time_iso=sched_for,
                                        template=story_template, clickbait=title,
                                        teaser_style=teaser_style, overlay_path=overlay_path,
                                        reaction_path=reaction_path, track=bool(body.get("track")),
                                        reaction_x=rx_x, reaction_y=rx_y, reaction_scale=rx_scale,
                                        captions=bool(body.get("captions")),
                                        transcript=(body.get("transcript") or ""))
            db.execute("UPDATE content_queue SET stories=? WHERE id=?", (n_stories, row_id))
            db.commit()
        except Exception as e:
            B.log.warning("story cut failed: %s", str(e)[:120])
    db.close()

    when = ""
    try:
        when = " for " + _et_label(datetime.fromisoformat(sched_for).astimezone(
            _ZI("America/New_York")))
    except Exception:
        pass
    tail = f" + {n_stories} stories" if n_stories else ""
    pre = "DRY-RUN ✓ would schedule" if dry_run else "Scheduled ✓ the Reel"
    return f"{pre}{when}{tail}{cap_note}"


def _graph_all_pages(page_id: str = None, **params) -> list[dict]:
    """Fetch published_posts from every enabled page (or one specific page when page_id
    is given — enabled or not), tagging each with the page name."""
    out = []
    if page_id:
        db = db_conn()
        row = db.execute("SELECT name, token FROM pages WHERE page_id=?", (page_id,)).fetchone()
        db.close()
        pages = [(page_id, (row[0] if row else page_id), (row[1] if row and row[1] else B.PRIMARY_PAGE_TOKEN))]
    else:
        pages = enabled_pages()
    for pid, name, token in pages:
        B.PAGE_ID, B.PAGE_TOKEN = pid, token
        try:
            data = B.graph_get(f"{pid}/published_posts", **params)
            for p in data.get("data", []):
                p["_page"] = name
                out.append(p)
        except Exception:
            pass
    B.PAGE_ID, B.PAGE_TOKEN = B.PRIMARY_PAGE_ID, B.PRIMARY_PAGE_TOKEN
    return out


def _shape(p: dict) -> dict:
    c = p.get("comments", {}).get("summary", {}).get("total_count", 0)
    r = p.get("reactions", {}).get("summary", {}).get("total_count", 0)
    s = p.get("shares", {}).get("count", 0)
    return {
        "id": p["id"],
        "caption": (p.get("message") or "(no caption)")[:110],
        "created": (p.get("created_time") or "")[:10],
        "ts": p.get("created_time") or "",
        "comments": c,
        "reactions": r,
        "shares": s,
        "score": c * 3 + r + s * 2,
        "thumb": p.get("full_picture") or "",
        "url": p.get("permalink_url", ""),
        "page": p.get("_page", ""),
        "created_ts": p.get("created_time", ""),
    }


def _trending(shaped: list[dict]) -> list[dict]:
    """Rank by growth vs the oldest snapshot within the past 7 days.
    Also records today's snapshot so growth data accrues automatically."""
    today = datetime.now(timezone.utc).date().isoformat()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    db = db_conn()
    for p in shaped: # keep the snapshot history fed
        db.execute("INSERT OR REPLACE INTO snapshots (taken_at, post_id, comments, reactions, shares) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (today, p["id"], p["comments"], p["reactions"], p["shares"]))
    out = []
    have_history = False
    for p in shaped:
        row = db.execute(
            "SELECT comments, reactions, shares FROM snapshots "
            "WHERE post_id=? AND taken_at>=? AND taken_at<? ORDER BY taken_at LIMIT 1",
            (p["id"], week_ago, today)).fetchone()
        if row:
            have_history = True
            dc, dr, ds = p["comments"]-row[0], p["reactions"]-row[1], p["shares"]-row[2]
        else:
            dc = dr = ds = 0
        q = dict(p, d_comments=max(0, dc), d_reactions=max(0, dr), d_shares=max(0, ds))
        q["growth"] = max(0, dc)*3 + max(0, dr) + max(0, ds)*2
        out.append(q)
    db.close()
    out.sort(key=lambda p: p["growth"], reverse=True)
    return [dict(p, no_history=(not have_history)) for p in out[:40]]


def fetch_posts(sort: str, page_id: str = None) -> list[dict]:
    """'recent' = latest 50. 'viral' = past year by totals. 'trending' = weekly growth.
    page_id (optional) restricts to one page (enabled or not)."""
    key = sort if not page_id else f"{sort}:{page_id}"
    slot = _cache.get(key)
    if slot is None:
        slot = _cache[key] = {"at": 0.0, "data": []}
    if _time.time() - slot["at"] > 180:
        try:
            if sort in ("recent", "trending"):
                raw = _graph_all_pages(page_id=page_id, fields=POST_FIELDS, limit=50)
                shaped = [_shape(p) for p in raw]
                shaped.sort(key=lambda p: p["created"], reverse=True)
                slot["data"] = _trending(shaped) if sort == "trending" else shaped[:60]
            else:
                since = (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()
                raw = _graph_all_pages(page_id=page_id, fields=POST_FIELDS, since=since, limit=100)
                shaped = [_shape(p) for p in raw]
                shaped.sort(key=lambda p: p["score"], reverse=True)
                slot["data"] = shaped[:40]
            slot["at"] = _time.time()
        except Exception:
            pass
    db = db_conn()
    targeted = {r[0] for r in db.execute("SELECT post_id FROM targets WHERE enabled=1")}
    db.close()
    return [dict(p, targeted=(p["id"] in targeted)) for p in slot["data"]]


def apply_target_changes(add: list[str], remove: list[str]) -> int:
    db = db_conn()
    now = datetime.now(timezone.utc).isoformat()
    for pid in add:
        db.execute("INSERT OR REPLACE INTO targets (post_id, enabled, added_at) VALUES (?, 1, ?)",
                   (pid, now))
    for pid in remove:
        db.execute("DELETE FROM targets WHERE post_id=?", (pid,))
    n = db.execute("SELECT COUNT(*) FROM targets WHERE enabled=1").fetchone()[0]
    db.close()
    return n


def publish_photo(page_id: str, dest: str, caption: str, blob: bytes, filename: str) -> str:
    import requests as rq
    db = db_conn()
    row = db.execute("SELECT token, name FROM pages WHERE page_id=?", (page_id,)).fetchone()
    db.close()
    if not row:
        return "unknown page"
    token, pname = row
    files = {"source": (filename or "photo.jpg", blob)}
    if dest == "story":
        r = rq.post(f"https://graph.facebook.com/v23.0/{page_id}/photos",
                    data={"published": "false", "access_token": token},
                    files=files, timeout=60).json()
        if "error" in r:
            return "upload failed: " + r["error"].get("message", "?")
        r2 = rq.post(f"https://graph.facebook.com/v23.0/{page_id}/photo_stories",
                     data={"photo_id": r["id"], "access_token": token}, timeout=60).json()
        if "error" in r2:
            return "story publish failed: " + r2["error"].get("message", "?")
        return f" ✓ posted to {pname} STORY"
    r = rq.post(f"https://graph.facebook.com/v23.0/{page_id}/photos",
                data={"message": caption, "access_token": token},
                files=files, timeout=60).json()
    if "error" in r:
        return "post failed: " + r["error"].get("message", "?")
    return f" ✓ posted to {pname} feed (photo id {r.get('post_id') or r.get('id')})"


SB_STATE = {"status": "idle", "text": "", "date": ""}
GEN_STATE = {"status": "idle", "done": 0, "total": 0, "error": "", "date": ""}
GEN_DIR = BASE / "generated"
REACT_DIR = BASE / "assets" / "reactions"   # creator reaction-sticker cutouts (PNG)


def list_reactions() -> list[str]:
    """Saved reaction-sticker PNG filenames (newest first)."""
    try:
        files = [p for p in REACT_DIR.glob("*.png")]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [p.name for p in files]
    except Exception:
        return []


def add_reaction(blob: bytes, suffix: str = ".jpg") -> dict:
    """Run an uploaded selfie through SAM -> borderless cutout -> save a reaction sticker.

    Returns {ok, name} or {ok:False, error}. Nothing here posts anything.
    """
    import uuid
    import sam_cutout as sc
    REACT_DIR.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex
    src = REACT_DIR / f"_src_{stem}{suffix if suffix in ('.jpg','.jpeg','.png','.webp') else '.jpg'}"
    try:
        src.write_bytes(blob)
        dest = REACT_DIR / f"rx_{stem}.png"
        db = sqlite3.connect(str(BASE / "state.db"))
        try:
            out = sc.make_reaction_sticker(str(src), str(dest), concept="person", db=db)
        finally:
            db.close()
        if not out or not dest.exists():
            return {"ok": False, "error": "no person found in that photo — try a clearer one"}
        return {"ok": True, "name": dest.name}
    except Exception as e:
        B.log.warning("add_reaction failed: %s", str(e)[:150])
        return {"ok": False, "error": "could not process that photo"}
    finally:
        try:
            src.unlink()
        except Exception:
            pass


_WATCH_CACHE = {"at": 0.0, "data": None}


def reel_watch_insights(limit: int = 10, max_age: int = 600) -> list[dict]:
    """Per-reel watch-through across enabled pages: plays, avg watch time, % of the
    video actually watched, and total view-time. This is the retention signal Meta
    exposes for Reels (per-second curves + demographics were deprecated). Cached."""
    now = _time.time()
    if _WATCH_CACHE["data"] is not None and now - _WATCH_CACHE["at"] < max_age:
        return _WATCH_CACHE["data"]
    out = []
    for pid, name, token in enabled_pages():
        B.PAGE_ID, B.PAGE_TOKEN = pid, token
        try:
            vids = B.graph_get(f"{pid}/videos",
                               fields="id,title,description,length,views,created_time,permalink_url",
                               limit=limit).get("data", [])
        except Exception:
            vids = []
        for v in vids:
            vid = v["id"]; ln = v.get("length", 0) or 0
            try:
                d = B.graph_get(f"{vid}/video_insights",
                                metric="post_video_avg_time_watched,post_video_view_time,blue_reels_play_count").get("data", [])
                m = {x["name"]: (x["values"][0]["value"] if x.get("values") else None) for x in d}
            except Exception:
                m = {}
            awt = m.get("post_video_avg_time_watched")      # ms
            vt = m.get("post_video_view_time")               # ms
            plays = m.get("blue_reels_play_count")
            pct = round(awt / 1000.0 / ln * 100, 1) if (awt and ln) else None
            out.append({
                "page": name, "id": vid,
                "title": ((v.get("title") or v.get("description") or "").strip().split(chr(10))[0][:72] or "(reel)"),
                "created": (v.get("created_time") or "")[:10], "len": round(ln),
                "views": v.get("views"), "plays": plays,
                "avg_watch": round(awt / 1000, 1) if awt else None,
                "watched_pct": pct, "view_min": round(vt / 1000 / 60) if vt else None,
                "url": (v.get("permalink_url") or ""),
            })
    B.PAGE_ID, B.PAGE_TOKEN = B.PRIMARY_PAGE_ID, B.PRIMARY_PAGE_TOKEN
    scored = [r for r in out if r["watched_pct"] is not None]
    scored.sort(key=lambda r: r["watched_pct"], reverse=True)
    rest = [r for r in out if r["watched_pct"] is None]
    data = scored + rest
    _WATCH_CACHE["at"] = now; _WATCH_CACHE["data"] = data
    return data


_MON_CACHE = {"at": 0.0, "pages": None}   # per-page metric breakdown; filters aggregate from this
# last-good monetization headline values — the Graph insights calls (minutes viewed,
# earnings) intermittently return nothing; we persist the last real values so a KPI
# keeps its last good value when a fetch fails or returns nothing (survives restarts too).
_MON_LAST_PATH = BASE / ".mon_cache.json"
try:
    _MON_LAST = json.loads(_MON_LAST_PATH.read_text(encoding="utf-8")) if _MON_LAST_PATH.exists() else {}
except Exception:
    _MON_LAST = {}


def _mon_fetch_pages(max_age: int = 900) -> dict:
    """Per-page monetization metrics, cached. Returns {pid: {name, followers, mins60,
    earn_day, earn_30}} with per-page last-good backfill so a flaky Graph fetch never
    zeroes a KPI. A page-filtered view then aggregates from this cache — no refetch."""
    now = _time.time()
    if _MON_CACHE["pages"] is not None and now - _MON_CACHE["at"] < max_age:
        return _MON_CACHE["pages"]
    try:
        pages = enabled_pages()
    except Exception:
        pages = []
    if not pages:
        pages = [(B.PRIMARY_PAGE_ID, B.PRIMARY_PAGE_NAME, B.PRIMARY_PAGE_TOKEN)]
    until = int(now); since = until - 60 * 86400
    out = {}; dirty = False
    for pid, nm, tok in pages:
        B.PAGE_ID, B.PAGE_TOKEN = pid, tok
        followers = mins60 = earn_day = earn_30 = None
        try:
            fc = B.graph_get(pid, fields="followers_count").get("followers_count")
            if fc is not None: followers = int(fc)
        except Exception:
            pass
        try:
            d = B.graph_get(f"{pid}/insights", metric="page_video_view_time", period="day",
                            since=since, until=until).get("data", [])
            vals = [v.get("value", 0) or 0 for v in (d[0].get("values", []) if d else [])]
            if vals: mins60 = int(round(sum(vals) / 1000 / 60))   # ms -> minutes
        except Exception:
            pass
        try:
            d = B.graph_get(f"{pid}/insights", metric="monetization_approximate_earnings", period="day",
                            since=since, until=until).get("data", [])
            vals = [v.get("value", 0) or 0 for v in (d[0].get("values", []) if d else [])]
            if vals: earn_day = round(vals[-1], 2); earn_30 = round(sum(vals[-30:]), 2)
        except Exception:
            pass
        last = _MON_LAST.get(pid) if isinstance(_MON_LAST.get(pid), dict) else {}
        if followers is None: followers = last.get("followers")
        if mins60 is None: mins60 = last.get("mins60")
        if earn_day is None: earn_day = last.get("earn_day")
        if earn_30 is None: earn_30 = last.get("earn_30")
        rec = {"followers": followers, "mins60": mins60, "earn_day": earn_day, "earn_30": earn_30}
        if _MON_LAST.get(pid) != rec:
            _MON_LAST[pid] = rec; dirty = True
        out[pid] = {"name": nm or pid, **rec}
    B.PAGE_ID, B.PAGE_TOKEN = B.PRIMARY_PAGE_ID, B.PRIMARY_PAGE_TOKEN   # restore
    if dirty:
        try: _MON_LAST_PATH.write_text(json.dumps(_MON_LAST), encoding="utf-8")
        except Exception: pass
    _MON_CACHE["at"] = now; _MON_CACHE["pages"] = out
    return out


def monetization_status(page_ids=None, max_age: int = 900) -> dict:
    """Monetization cockpit aggregated across the selected pages (default: all enabled).
    `page_ids` = iterable of page IDs to include, or None for every enabled page. Returns
    eligibility progress (in-stream ads / subscriptions / Stars) + earnings, plus the full
    page list and the active selection so the UI can offer a collective-or-per-page filter."""
    per = _mon_fetch_pages(max_age)
    all_ids = list(per.keys())
    sel = [pid for pid in (list(page_ids) if page_ids else all_ids) if pid in per] or all_ids

    def agg(metric):
        vals = [per[pid].get(metric) for pid in sel]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        return round(sum(vals), 2) if metric.startswith("earn") else int(round(sum(vals)))

    followers = agg("followers"); mins60 = agg("mins60")
    earn_day = agg("earn_day"); earn_30 = agg("earn_30")
    programs = [
        {"name": "In-stream ads (Reels)", "note": "ads in your videos",
         "reqs": [{"label": "Followers", "need": 10000, "have": followers},
                  {"label": "Minutes viewed (60d)", "need": 600000, "have": mins60}]},
        {"name": "Fan subscriptions", "note": "monthly supporter perks",
         "reqs": [{"label": "Followers", "need": 10000, "have": followers}]},
        {"name": "Stars", "note": "tips on Reels & live",
         "reqs": [{"label": "Followers", "need": 500, "have": followers}]},
    ]
    for p in programs:
        for r in p["reqs"]:
            r["pct"] = min(100, round((r["have"] or 0) / r["need"] * 100)) if r["need"] else 0
        p["eligible"] = all((r["have"] or 0) >= r["need"] for r in p["reqs"])
    return {"followers": followers, "mins60": mins60, "earn_day": earn_day, "earn_30": earn_30,
            "programs": programs,
            "pages": [{"id": pid, "name": per[pid]["name"]} for pid in all_ids],
            "selected": sel}


_CAL_CACHE = {"at": 0.0, "data": None}


def content_calendar(max_age: int = 300) -> dict:
    """Everything on the schedule: upcoming reels/stories + recently published posts,
    tagged by date for a calendar/planner grid. Cached."""
    now = _time.time()
    if _CAL_CACHE["data"] is not None and now - _CAL_CACHE["at"] < max_age:
        return _CAL_CACHE["data"]
    items = []
    db = db_conn()
    try:
        rows = db.execute("SELECT title, scheduled_for, slot, status, thumbnail FROM content_queue "
                          "WHERE status IN ('scheduled','approved','dry_scheduled') ORDER BY id DESC LIMIT 40").fetchall()
        for title, sched, slot, status, thumb in rows:
            when = sched or slot or ""
            items.append({"date": when[:10], "when": when, "title": (title or "Scheduled reel")[:70],
                          "status": "scheduled", "type": "reel",
                          "thumb": ("/draft/" + thumb) if thumb else "", "url": ""})
    except Exception:
        pass
    try:
        rows = db.execute("SELECT page_id, path, slot FROM story_queue WHERE posted_at IS NULL LIMIT 40").fetchall()
        for pid, path, slot in rows:
            items.append({"date": (slot or "")[:10], "when": slot, "title": "Story",
                          "status": "scheduled", "type": "story", "thumb": "", "url": ""})
    except Exception:
        pass
    db.close()
    try:
        for pid, name, token in enabled_pages():
            B.PAGE_ID, B.PAGE_TOKEN = pid, token
            try:
                data = B.graph_get(f"{pid}/published_posts",
                                   fields="id,message,created_time,full_picture,permalink_url",
                                   limit=25).get("data", [])
            except Exception:
                data = []
            for p in data:
                items.append({"date": (p.get("created_time") or "")[:10], "when": p.get("created_time"),
                              "title": (p.get("message") or "(post)").strip()[:70] or "(post)",
                              "status": "published", "type": "post",
                              "thumb": p.get("full_picture") or "", "url": p.get("permalink_url") or ""})
    except Exception:
        pass
    B.PAGE_ID, B.PAGE_TOKEN = B.PRIMARY_PAGE_ID, B.PRIMARY_PAGE_TOKEN
    data = {"items": items}
    _CAL_CACHE["at"] = now; _CAL_CACHE["data"] = data
    return data


_ANALYST_CACHE = {"at": 0.0, "data": None}
_IDEAS_CACHE = {"at": 0.0, "data": None}


def _muse_wonder(instructions: str, user_text: str, effort: str = "minimal", db=None, task: str = "analysis", timeout: int = 120) -> str:
    """Call Meta's flagship Muse Wonder for grounded reasoning. Returns text."""
    import requests as _rq
    if not B.muse_key():
        return ""
    body = {"model": "muse-wonder-1.0",
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": instructions + "\n\n" + user_text}]}],
            "reasoning": {"effort": effort}}
    try:
        r = meta_live.post("muse-wonder-1.0", task, "https://api.meta.ai/v1/responses",
                     headers={"Authorization": f"Bearer {B.muse_key()}", "Content-Type": "application/json"},
                     json=body, timeout=timeout)
        j = r.json()
        txt = ""
        for item in j.get("output", []):
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    txt += c.get("text", "")
        try:
            if db is not None:
                B.record_spend(db, "muse", "wonder_analyst", 0, 0, 0.02)
        except Exception:
            pass
        return txt.strip()
    except Exception as e:
        B.log.warning("Muse Wonder call failed: %s", str(e)[:140])
        return ""


def _muse_spark(instructions: str, user_text: str, effort: str = "minimal",
                max_tokens: int = 700, db=None, task: str = "writing") -> str:
    """Call Meta's fast Muse Spark (muse-spark-1.3) for quick, in-voice writing. Returns text."""
    import requests as _rq
    if not B.muse_key():
        return ""
    body = {"model": "muse-spark-1.3",
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": instructions + "\n\n" + user_text}]}],
            "reasoning": {"effort": effort},
            "max_output_tokens": max_tokens}
    try:
        r = meta_live.post("muse-spark-1.3", task, "https://api.meta.ai/v1/responses",
                     headers={"Authorization": f"Bearer {B.muse_key()}", "Content-Type": "application/json"},
                     json=body, timeout=60)
        j = r.json()
        txt = ""
        for item in j.get("output", []):
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    txt += c.get("text", "")
        try:
            if db is not None:
                B.record_spend(db, "muse", "film_script", 0, 0, 0.01)
        except Exception:
            pass
        return txt.strip()
    except Exception as e:
        B.log.warning("Muse Spark call failed: %s", str(e)[:140])
        return ""


def _spark_script(plan, db=None) -> dict:
    """Muse Spark rewrites the director's plan into tight, in-voice teleprompter lines.
    Returns {hook, lines[]} aligned 1:1 to the plan's beats, or {} on any problem."""
    beats = plan.get("beats") or []
    if not beats:
        return {}
    persona = ""
    try:
        if B.PERSONA_PATH.exists():
            persona = B.PERSONA_PATH.read_text(encoding="utf-8")[:1200]
    except Exception:
        pass
    numbered = chr(10).join(
        "%d) %s: %s" % (i + 1, (b.get("cue") or ""), (b.get("line") or ""))
        for i, b in enumerate(beats))
    instr = (
        "You are Sage's scriptwriter for " + B.CREATOR_REF + ". You get a video game-plan: a hook and "
        "numbered beats, each with a MOMENT and a draft line. Rewrite ONLY the words the creator reads off a "
        "teleprompter: punchy, natural, first-person, in their casual voice, 1-2 short sentences per "
        "beat. Keep the SAME beats in the SAME order and the same meaning. No stage directions, no "
        "emoji, nothing political or religious. Reply with ONLY strict JSON, no prose, no code "
        'fence: {"hook":"<spoken hook>","lines":["<beat 1>","<beat 2>", ...]} with exactly one '
        "line per beat, in order."
    )
    user = "HOOK: %s\nBEATS:\n%s" % (plan.get("hook") or "", numbered)
    if persona:
        user += "\n\nHER VOICE (match it):\n" + persona
    raw = _muse_spark(instr, user, effort="minimal", max_tokens=700, db=db)
    if not raw:
        return {}
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw.strip()).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        t = m.group(0)
    try:
        d = json.loads(t)
    except Exception:
        return {}
    return {"hook": str(d.get("hook") or "")[:120],
            "lines": [str(x)[:280] for x in (d.get("lines") or []) if str(x).strip()]}


def rank_hooks(titles, content_desc="", transcript="", db=None) -> dict:
    """Muse Wonder (Meta's flagship) ranks the generated title options and picks the
    hook most likely to win reach, grounded in the video content. Returns
    {winner:int, reason:str, scores:[{i,score,why}]}."""
    titles = [t for t in (titles or []) if t][:4]
    if len(titles) < 2:
        return {"winner": 0, "reason": "", "scores": []}
    instr = ("You are Sage, a Facebook Reels growth strategist. You are given several "
             "title/hook options for ONE short video, plus what the video shows and says. "
             "Pick the ONE hook most likely to win reach on Facebook: strong curiosity gap, "
             "front-loaded payoff, concrete and specific, no clickbait that the video does "
             "not deliver. Reply with ONLY strict JSON, no prose, no code fence: "
             '{"winner": <0-based index of the best title>, '
             '"reason": "<=12 words why it wins>", '
             '"scores": [{"i": <index>, "score": <0-100>, "why": "<=8 words>"}]}')
    lines = ["Title options:"] + [f"[{i}] {t}" for i, t in enumerate(titles)]
    if content_desc:
        lines.append("\nVideo shows: " + content_desc[:500])
    if transcript:
        lines.append("Video says: " + transcript[:500])
    raw = _muse_wonder(instr, "\n".join(lines), effort="low", db=db, task="title options")
    import re as _re
    try:
        m = _re.search(r"\{.*\}", raw, _re.S)
        j = json.loads(m.group(0)) if m else {}
    except Exception:
        j = {}
    win = j.get("winner")
    try:
        win = int(win)
    except Exception:
        win = 0
    if win < 0 or win >= len(titles):
        win = 0
    scores = j.get("scores") if isinstance(j.get("scores"), list) else []
    return {"winner": win, "reason": (j.get("reason") or "")[:120], "scores": scores}


# The concierge never waits on Facebook. Its Facebook-backed context is built in the background and the
# last snapshot is used for every answer; the first answer after a restart or a page change may say the
# numbers are still loading.
_CTX = {"at": 0.0, "lines": [], "busy": False}
_CTX_LOCK = threading.Lock()
_CTX_TTL = 120.0

def _ctx_build_lines() -> list:
    """The Facebook-backed part of the concierge context. Slow (Graph calls): background threads only."""
    ctx = []
    try:
        m = monetization_status()
        if m and m.get("followers") is not None:
            ctx.append(f"Earnings ${m.get('earn_day')} today / ${m.get('earn_30')} last 30 days; "
                       f"{m.get('followers')} followers; {m.get('mins60')} minutes viewed in 60 days.")
            elig = [p.get("name") for p in m.get("programs", []) if p.get("eligible") and p.get("name")]
            if elig:
                ctx.append("Eligible for: " + ", ".join(elig) + ".")
    except Exception:
        pass
    try:
        w = [x for x in reel_watch_insights() if x.get("watched_pct") is not None]
        if w:
            avg = round(sum(x["watched_pct"] for x in w) / len(w), 1)
            top = w[0]
            ttl = (top.get("title") or "")[:48]
            ctx.append(f"Retention: average hold {avg}% across {len(w)} reels; best hold is "
                       f"'{ttl}' at {top['watched_pct']}% ({top.get('views') or 0} views, {top.get('len')}s).")
            byv = max(w, key=lambda x: (x.get("views") or 0))
            if byv is not top:
                vt = (byv.get("title") or "")[:48]
                ctx.append(f"Biggest reach: '{vt}' {byv.get('views') or 0} views at {byv['watched_pct']}% hold.")
    except Exception:
        pass
    try:
        cal = content_calendar()
        sched = [i for i in cal.get("items", []) if i.get("status") == "scheduled"]
        if sched:
            nxt = "; ".join(f"{(i.get('title') or 'post')[:36]} ({i.get('when') or i.get('date')})"
                            for i in sched[:3])
            ctx.append(f"{len(sched)} posts scheduled. Next: {nxt}.")
        else:
            ctx.append("Nothing scheduled right now.")
    except Exception:
        pass
    return ctx


def _ctx_refresh() -> None:
    try:
        lines = _ctx_build_lines()
        with _CTX_LOCK:
            _CTX["lines"] = lines
            _CTX["at"] = _time.time()
    except Exception as e:
        B.log.warning("concierge context refresh failed: %s", str(e)[:140])
    finally:
        with _CTX_LOCK:
            _CTX["busy"] = False


def _ctx_snapshot() -> tuple:
    """(lines, fresh). Returns at once; starts one background refresh when the snapshot is stale."""
    with _CTX_LOCK:
        fresh = (_time.time() - _CTX["at"]) < _CTX_TTL
        start = (not fresh) and (not _CTX["busy"])
        if start:
            _CTX["busy"] = True
        lines = list(_CTX["lines"])
    if start:
        threading.Thread(target=_ctx_refresh, daemon=True).start()
    return lines, fresh


# ---- questions about one specific video: matched from what the app already stores; no network call here ----
_VID_STOP = frozenset("the a an and or of to in on at for with my me i is are was were be been what which who how did do does "
                      "this that these those it its about video videos reel reels post posts clip tell show give you your "
                      "best top most biggest latest last newest recent some any".split())
_VID_INDEX = {"at": 0.0, "items": []}
_VID_INDEX_TTL = 60.0
_TR_TRIED = set()
_TR_LOCK = threading.Lock()

def _vid_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 2 and w not in _VID_STOP}

def _video_index() -> list:
    """Every post the app has described, with what it already knows: description, transcript (None if not
    transcribed yet), latest engagement and the number of comments replied to. Reads state.db only."""
    now = _time.time()
    if _VID_INDEX["items"] and now - _VID_INDEX["at"] < _VID_INDEX_TTL:
        return _VID_INDEX["items"]
    db = db_conn()
    try:
        rows = db.execute(
            "SELECT pc.post_id, pc.description, pt.transcript FROM post_context pc "
            "LEFT JOIN post_transcripts pt ON pt.post_id = pc.post_id WHERE pc.description IS NOT NULL").fetchall()
        snaps = {r[0]: (r[1], r[2], r[3]) for r in db.execute(
            "SELECT post_id, comments, reactions, shares FROM snapshots s WHERE taken_at = "
            "(SELECT MAX(taken_at) FROM snapshots WHERE post_id = s.post_id)").fetchall()}
        replies = {r[0]: r[1] for r in db.execute(
            "SELECT substr(comment_id, 1, instr(comment_id, '_') - 1), COUNT(*) FROM handled "
            "WHERE action='replied' GROUP BY 1").fetchall()}
    finally:
        db.close()
    items = []
    for pid, desc, tr in rows:
        c, rx, sh = snaps.get(pid, (None, None, None))
        items.append({"post_id": pid, "desc": (desc or "")[:600], "transcript": tr,
                      "comments": c, "reactions": rx, "shares": sh,
                      "replies": replies.get(pid.split("_")[-1], 0)})
    _VID_INDEX.update(at=now, items=items)
    return items

def _video_matches(message: str, index: list, limit: int = 2) -> list:
    """The posts a question is about: word overlap with the description and transcript, or the most
    engaged posts for 'best / most / top video' questions."""
    q = _vid_words(message)
    low = (message or "").lower()
    if re.search(r"\b(best|top|most|biggest|highest|worst|lowest|least)\b", low) and re.search(r"\b(video|reel|post|clip)s?\b", low):
        ranked = sorted(index, key=lambda it: -((it["reactions"] or 0) + 2 * (it["comments"] or 0) + 3 * (it["shares"] or 0)))
        return ranked[:limit]
    if not q:
        return []
    scored = []
    for it in index:
        hit = len(q & _vid_words(it["desc"] + " " + (it["transcript"] or "")))
        if hit:
            scored.append((hit, it["reactions"] or 0, it))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [it for _, _, it in scored[:limit]]

def _start_transcript(pid: str) -> None:
    """Transcribes one video in the background (Muse voice, cached). The next question gets the transcript."""
    with _TR_LOCK:
        if pid in _TR_TRIED:
            return
        _TR_TRIED.add(pid)
    def run():
        db = None
        try:
            db = db_conn()
            page = pid.split("_")[0]
            row = db.execute("SELECT token FROM pages WHERE page_id=?", (page,)).fetchone()
            if not row or not row[0]:
                return
            saved = (B.PAGE_ID, B.PAGE_TOKEN)
            B.PAGE_ID, B.PAGE_TOKEN = page, row[0]
            try:
                B.get_video_transcript(db, {"id": pid})
            finally:
                B.PAGE_ID, B.PAGE_TOKEN = saved
        except Exception as e:
            B.log.warning("video transcript failed: %s", str(e)[:140])
        finally:
            if db is not None:
                db.close()
    threading.Thread(target=run, daemon=True).start()

def _video_context_lines(message: str) -> list:
    """Lines for the concierge about the video (or videos) the question is about. Never waits on Facebook."""
    idx = _video_index()
    if not idx:
        return []
    picks = _video_matches(message, idx)
    if not picks:
        return []
    out = [f"VIDEO details (you have described {len(idx)} posts; watch time is only stored for the newest reels):"]
    for it in picks:
        eng = "reactions %s, comments %s, shares %s (latest count)" % (
            it["reactions"] if it["reactions"] is not None else "not measured yet",
            it["comments"] if it["comments"] is not None else "-",
            it["shares"] if it["shares"] is not None else "-")
        out.append(f"- {it['post_id']}: {it['desc'][:500]} Engagement: {eng}. Replies you have sent to its comments: {it['replies']}.")
        if it["transcript"] is None:
            out.append("  Transcript: not made yet. It is being made now, so ask again in a minute.")
            _start_transcript(it["post_id"])
        elif it["transcript"].strip():
            out.append(f"  What is said in it: {it['transcript'][:700]}")
        else:
            out.append("  What is said in it: no speech found (or this post has no video).")
    return out


def assistant_reply(message: str, history=None, persona=None) -> str:
    """The floating Abra concierge: the creator says what they need in plain language and
    Muse Wonder answers, grounded in the crew's capabilities + a light live snapshot.
    Reels and Stories are staged for approval in review mode; comment replies post under the reply limits. The concierge never claims it posted."""
    db = db_conn()
    page_rows = []
    try:
        page_rows = [(r[0], r[1]) for r in db.execute("SELECT page_id, name FROM pages").fetchall()]
        pages = [(name or pid) for pid, name in page_rows]
    except Exception:
        pages = []
    db.close()
    ctx, fresh = _ctx_snapshot()
    if not fresh and not ctx:
        ctx.append("Facebook numbers are still loading; say so if the creator asks for numbers.")
    try:
        dbx = db_conn()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rn = dbx.execute("SELECT COUNT(*) FROM handled WHERE action='replied' AND "
                         "substr(handled_at,1,10)=?", (today,)).fetchone()[0]
        try:
            dn = dbx.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        except Exception:
            dn = 0
        dbx.close()
        ctx.append(f"{rn} comments replied to today.")
    except Exception:
        pass
    try:
        paused = engagement_state().get("paused")
        ctx.append("Engagement is PAUSED right now \u2014 the crew is not replying to comments or DMs "
                   "until it is resumed from the sidebar button."
                   if paused else "Engagement is live \u2014 the crew is replying.")
        pm = {"review": "approve-each (review)", "auto": "full auto"}
        dm = {"auto": "auto-reply (needs Messenger access)", "flag": "flag only", "off": "off"}
        mbits = [f"{(name or pid)}: posts {pm.get(get_pub_mode(pid), get_pub_mode(pid))}, "
                 f"DMs {dm.get(get_dm_mode(pid), get_dm_mode(pid))}" for pid, name in page_rows]
        if mbits:
            ctx.append("Current modes \u2014 " + "; ".join(mbits) + ".")
    except Exception:
        pass
    try:
        ctx.extend(_video_context_lines(message))
    except Exception as e:
        B.log.warning("video lookup failed: %s", str(e)[:140])
    persona = (persona or "abra").strip().lower()
    if persona not in ("nico", "sage", "koa"):
        persona = "abra"
    _PERSONA = {
        "nico": "You are Nico. Speak in the FIRST PERSON as Nico \u2014 warm, upbeat and "
                "community-minded. Your job is the creator's comments: you reply in "
                "their own voice, like the good ones and skip spam and hate. Answer engagement "
                "questions yourself; for money or video strategy, give a quick take and say Koa "
                "or Sage can take it from there.",
        "sage": "You are Sage. Speak in the FIRST PERSON as Sage \u2014 sharp, encouraging and "
                "data-driven. Your job is turning a dropped video into a post, picking the hook it expects to do best, "
                "scheduling the Reel (captioned when captions are on, for portrait video), cutting captioned Stories and "
                "reading the analytics. Always frame feedback positively \u2014 celebrate what is "
                "working and coach the next step; never use harsh words like 'fail' or 'kill'. For "
                "money or comment replies, give a quick take and hand off to Koa or Nico.",
        "koa": "You are Koa. Speak in the FIRST PERSON as Koa \u2014 steady, protective and "
               "numbers-first. Your job is earnings, the AI-cost ledger, monetization eligibility, "
               "contracts and page standing. For posting or comments, give a quick take and hand "
               "off to Sage or Nico.",
    }
    if persona == "abra":
        identity = ("You are Abra, the AI concierge inside the Abra app that runs a Facebook "
                    "creator's engagement with a three-member AI crew, built on Meta's Muse models and Claude:\n")
    else:
        identity = (_PERSONA[persona] + " You are one of the Abra app's three-member AI crew "
                    "that runs a Facebook creator's engagement, built on Meta's Muse models and Claude:\n")
    instr = (
        identity +
        "- Nico: replies to comments in the creator's own voice, likes good ones, skips spam and hate.\n"
        "- Sage: watches a dropped video, writes the post + picks the hook it expects to do best, "
        "schedules the Reel (with captions when they are on), and cuts captioned Stories.\n"
        "- Koa: earnings, AI-cost ledger, contract review, page standing.\n"
        "HOW ABRA WORKS (use this to welcome or explain things, in plain language): Abra runs the "
        "creator's Facebook page with the three-member crew above, built on Meta's Muse models and Claude. "
        "AI usage is tracked as an estimate in Money & Legal. Getting started takes two steps: connect a "
        "Facebook page, then drop a video on the Launchpad — Sage turns it into a captioned post with a "
        "hook and a scheduled Reel, while Nico replies to comments in the creator's own voice. The creator is "
        "always in control: in REVIEW mode Reels and Stories wait for approval, comment replies post under the "
        "reply limits, AUTO mode lets the crew schedule Reels on its own, and they can switch modes or pause a "
        "page anytime. By voice, the creator can say \"Abra\" then: record, undo, \"create a video that is two minutes\", pause or resume engagement, turn spoken replies on or off, open or close the chat, \"talk to Nico\" (or Sage or Koa), \"show the best time\" and similar, plus take numbers (\"delete take two\", \"send take one to Sage\"), \"use the front camera\", \"zoom in\" or \"zoom out\", \"room for my hand bigger\" or \"set room to 150\", \"mirror on\" or \"mirror off\", \"brighter\" or \"darker\" and \"set brightness to 40\" where the camera allows it, \"switch microphone\" or \"use the second microphone\" (voice itself always listens on this computer's default microphone), \"talk time\" (an estimate from the sound, never a claim about who is speaking), \"approve replies on\" or \"off\" (replies wait for your approval before they post), \"approve the next reply\" (Abra reads it back and waits for \"Abra confirm\"), \"write a script about\" a topic (Sage writes it, and it lands in the teleprompter), \"make it thirty seconds\", \"use that\" and \"undo script\", \"next line\", \"scroll down\" and \"scroll up\" (also during a take), \"faster\", \"slower\", \"bigger text\", \"edit script\" and \"save script\" for the teleprompter, and \"cancel the countdown\". Anything on screen can be named the same way."
        "page anytime. Troubleshooting: if comments are not getting answered, engagement may be paused for that "
        "page; if posts are not going out, the page is in review mode waiting for approval on the Launchpad tab. "
        "When someone asks what Abra is, how it works, what it costs, or how to begin, answer warmly from this "
        "— do "
        "not just point them at a tab.\n"
        "The creator talks to you about what they need. You have a LIVE SNAPSHOT of their real "
        "numbers below — when they ask about reach, retention, earnings, what to post, their "
        "schedule or comments, ANSWER with the specific figures and a concrete takeaway, in "
        "1-4 short sentences. Do NOT just send them to a tab for details — give the real answer "
        "first, using the actual numbers; you may add a brief 'full breakdown is in <tab>' only "
        "after. When they ask for something to be DONE, say which crew member handles it and what "
        "happens next. Tabs: Launchpad (drop a NEW video to turn it into a post), Engagement (live "
        "comment replies), Boost Videos (prioritize engagement on ALREADY-posted videos), "
        "Calendar, Analytics, Money. Reels and Stories are always "
        "STAGED for their approval in review mode; comment replies follow the reply limits. Never say you already posted, sent, or "
        "scheduled something yourself. "
        + "When a VIDEO is listed in the snapshot, answer about that video from its details only, and say plainly when a detail is not there yet. "
        + (f"Their pages: {', '.join(pages)}. " if pages else "")
        + ("LIVE SNAPSHOT — " + " ".join(ctx) if ctx else "")
        + " When it helps to take the creator somewhere or run a read, add ONE final line in the "
        "exact form 'ACTION: <cmd>' where <cmd> is one of: open:launchpad, open:engagement, "
        "open:boost, open:calendar, open:analytics, open:money, open:pages, read "
        "(open Analytics and run a fresh Sage read), or none. Use an action only when it clearly "
        "helps and matches what they asked. NEVER use an action that posts, sends, schedules, or "
        "changes anything — only open a tab or run a read. "
        "Then add one more final line 'CREW: <who>' naming the ONE crew member whose job this "
        "request is about — nico (comments & DMs), sage (writing/scheduling posts, video, "
        "analytics reads), koa (money, earnings, contracts, page standing) "
        "— or none for general chit-chat. Neither the ACTION nor the CREW line is shown to them."
    )
    if not B.muse_key():   # no Muse key: the canned reply says what to add (no model call)
        return {"reply": CHAT_NO_KEY_REPLY, "action": None, "agent": None, "persona": persona}
    convo = ""
    for h in (history or [])[-6:]:
        role = "Creator" if h.get("role") == "user" else "Abra"
        convo += f"{role}: {h.get('content', '')}\n"
    convo += f"Creator: {message}\nAbra:"
    db2 = db_conn()
    try:
        raw = _muse_wonder(instr, convo, effort="low", db=db2, task="concierge", timeout=45)
    finally:
        db2.close()
    # split out the ACTION directive (whitelist: open:<view> / read only)
    allowed = {"launchpad", "engagement", "boost", "calendar", "analytics", "money", "deals", "pages"}
    action = None
    agent = None
    keep = []
    for ln in (raw or "").splitlines():
        s = ln.strip()
        if s.upper().startswith("ACTION:"):
            cmd = s.split(":", 1)[1].strip().lower()
            if cmd.startswith("open:"):
                view = cmd.split(":", 1)[1].strip()
                if view in allowed:
                    action = {"type": "open", "view": view}
            elif cmd == "read":
                action = {"type": "read"}
        elif s.upper().startswith("CREW:"):
            who = s.split(":", 1)[1].strip().lower()
            if who in {"nico", "sage", "koa"}:
                agent = who
        else:
            keep.append(ln)
    reply = "\n".join(keep).strip() or "I'm right here — tell me what you need and I'll get the crew on it."
    if persona != "abra":
        agent = persona
    return {"reply": reply, "action": action, "agent": agent, "persona": persona}


def muse_analyst(max_age: int = 1800, force: bool = False) -> dict:
    """Muse Wonder reads the page's real data and returns a sharp strategic 'read'. Cached."""
    now = _time.time()
    if not force and _ANALYST_CACHE["data"] is not None and now - _ANALYST_CACHE["at"] < max_age:
        return _ANALYST_CACHE["data"]
    # gather real data
    watch = reel_watch_insights()
    mon = monetization_status()
    wlines = "; ".join(f"{w['len']}s {w['watched_pct']}% ({w.get('views') or 0} views)"
                       for w in watch if w.get("watched_pct") is not None) or "(no reel watch data)"
    db = db_conn()
    reach = {}
    try:
        for m in ("page_video_views", "page_post_engagements", "page_follows"):
            rows = db.execute("SELECT SUM(value) FROM page_insights WHERE metric=? GROUP BY taken_at ORDER BY taken_at DESC LIMIT 7",
                              (m,)).fetchall()
            reach[m] = [r[0] for r in rows][::-1]
    except Exception:
        pass
    db.close()
    blob = (f"Followers: {mon.get('followers')}. Minutes viewed (60d): {mon.get('mins60')}. "
            f"Earnings: ${mon.get('earn_day')}/day, ${mon.get('earn_30')}/30d.\n"
            f"Reels (length, watched-through%, views), best-hold first: {wlines}\n"
            f"Recent daily video views, all pages summed: {reach.get('page_video_views')}\n"
            f"Recent daily post engagements, all pages summed: {reach.get('page_post_engagements')}")
    instructions = (
        "You are Sage, the creator's warm, upbeat Reels coach. Read this REAL data and write a short, "
        "encouraging 'read': 2-3 specific takeaways that LEAD with what's working and cite the actual "
        "numbers, then one concrete next step framed as an exciting opportunity. "
        "TONE: supportive, motivating, confident — a coach who believes in them. Celebrate the wins "
        "first; frame every improvement as 'lean into this' or 'here's your next win', never as a "
        "failure, loss, or problem. NEVER use harsh or violent words — no 'kill', 'die', 'dying', "
        "'dead', 'failure', 'leak', 'dilute', 'bleeding', 'weak', or 'bad'. Stay specific and grounded "
        "in the numbers (encouraging, not vague or fluffy). No preamble. Use short bold headers.")
    db = db_conn()
    text = _muse_wonder(instructions, blob, effort="minimal", db=db, task="Sage read")
    db.close()
    data = {"text": text, "at": now}
    if text:
        _ANALYST_CACHE["at"] = now; _ANALYST_CACHE["data"] = data
    return data


def _parse_ideas(raw: str) -> list:
    """Pull a JSON array of {hook, why} out of Muse's reply, tolerating code fences or stray prose."""
    if not raw:
        return []
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw.strip()).strip()
    m = re.search(r"\[.*\]", t, re.S)
    if m:
        t = m.group(0)
    try:
        arr = json.loads(t)
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        hook = str(it.get("hook") or it.get("title") or "").strip()[:70]
        why = str(it.get("why") or it.get("reason") or "").strip()[:130]
        if hook:
            out.append({"hook": hook, "why": why})
    return out[:3]


def muse_ideas(max_age: int = 1800, force: bool = False) -> dict:
    """Muse Wonder proposes 3 concrete 'what to film next' ideas from the creator's real numbers.
    This is the top of the loop (reflection -> the next intention) and the Insights loop-back. Cached."""
    now = _time.time()
    if not force and _IDEAS_CACHE["data"] is not None and now - _IDEAS_CACHE["at"] < max_age:
        return _IDEAS_CACHE["data"]
    watch = reel_watch_insights()
    mon = monetization_status()
    tops = [w for w in watch if w.get("watched_pct") is not None][:5]
    toplines = "; ".join(
        f"'{(w.get('title') or 'untitled')[:42]}' holds {w['watched_pct']}% ({w.get('views') or 0} views)"
        for w in tops) or "(no reel data yet)"
    blob = (f"Followers: {mon.get('followers')}. Earnings: ${mon.get('earn_30')}/30 days.\n"
            f"Their best reels (title, watch-through, views): {toplines}")
    instructions = (
        "You are Sage, the creator's upbeat Reels coach. From this creator's REAL performance data, "
        "propose exactly 3 specific videos they should film NEXT. Each idea must be concrete and grounded "
        "in what already works for THEM \u2014 echo a winning theme or format from their top reels, or fill "
        "an obvious gap. Return ONLY a JSON array of exactly 3 objects, each with two keys: "
        "\"hook\" (a punchy 4-9 word title they could film) and \"why\" (one short sentence citing the real "
        "reason \u2014 a number or a winning theme). Encouraging, specific, never harsh. "
        "No preamble, no markdown fences \u2014 output JSON only.")
    db = db_conn()
    raw = _muse_wonder(instructions, blob, effort="low", db=db, task="what to film next")
    db.close()
    ideas = _parse_ideas(raw)
    data = {"ideas": ideas, "at": now}
    if ideas:
        _IDEAS_CACHE["at"] = now
        _IDEAS_CACHE["data"] = data
    return data


def _parse_react(raw: str) -> dict:
    """Pull {hook, beats:[{moment,take}], compliance} out of Wonder's reply, tolerating fences/prose."""
    if not raw:
        return {"hook": "", "beats": [], "compliance": ""}
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw.strip()).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        t = m.group(0)
    try:
        d = json.loads(t)
    except Exception:
        return {"hook": "", "beats": [], "compliance": ""}
    beats = []
    for b in (d.get("beats") or [])[:5]:
        if isinstance(b, dict) and (b.get("moment") or b.get("take")):
            beats.append({"moment": str(b.get("moment") or "")[:140],
                          "take": str(b.get("take") or "")[:220]})
    return {"hook": str(d.get("hook") or "")[:100], "beats": beats,
            "compliance": str(d.get("compliance") or "")[:200]}


def muse_react_plan(link: str, note: str) -> dict:
    """Sage directs a platform-smart, ORIGINAL reaction: the beats worth reacting to, a unique take
    for each, a hook, and a compliance brief. Driven by the link + the creator's note (no scraping)."""
    link = (str(link or "")).strip()[:500]
    note = (str(note or "")).strip()[:1400]
    src = ("Link they're reacting to: " + link + "\n" if link else "") +           ("What it is / why it's reaction-worthy: " + note if note else "")
    if not src.strip():
        return {"hook": "", "beats": [], "compliance": ""}
    instructions = (
        "You are Sage, a sharp reaction-video director. A creator wants to film a REACTION to the "
        "content below. Aim for a TRANSFORMATIVE reaction: the creator's face is "
        "about 1/3 of the frame, they talk for at least 1/3 of the runtime, and they add a genuinely "
        "UNIQUE take \u2014 not just narrating what happens. Plan their reaction so it's original and "
        "monetizable. Return ONLY JSON, no fences: "
        "{\"hook\": \"<a punchy reaction-video title>\", "
        "\"beats\": [{\"moment\": \"<a specific moment or point to react to>\", "
        "\"take\": \"<the creator's unique, bold-but-true angle on it, one punchy sentence they could say>\"}], "
        "\"compliance\": \"<one line reminding them to keep their face ~1/3 of frame, talk 1/3+, and make it their own>\"}. "
        "Give 3-4 beats. Be specific and original, never generic filler. JSON only.")
    db = db_conn()
    raw = _muse_wonder(instructions, src, effort="low", db=db, task="hook and beats")
    db.close()
    return _parse_react(raw)


try:
    _FG = json.load(open(str(BASE / "film_genres.json"), encoding="utf-8"))
    FILM_GENRES = {g["id"]: g for g in _FG}
    FILM_GENRE_ORDER = [g["id"] for g in _FG]
except Exception:
    FILM_GENRES, FILM_GENRE_ORDER = {}, []


def _parse_film(raw: str) -> dict:
    if not raw:
        return {"hook": "", "beats": [], "compliance": ""}
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw.strip()).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        t = m.group(0)
    try:
        d = json.loads(t)
    except Exception:
        return {"hook": "", "beats": [], "compliance": ""}
    beats = []
    for b in (d.get("beats") or [])[:6]:
        if isinstance(b, dict) and (b.get("cue") or b.get("line")):
            beats.append({"cue": str(b.get("cue") or "")[:60], "line": str(b.get("line") or "")[:280]})
    return {"hook": str(d.get("hook") or "")[:120], "beats": beats,
            "compliance": str(d.get("compliance") or "")[:220]}


def _clock12(t) -> str:
    """'9:05 PM' without the %-I flag that Windows does not support."""
    return f"{t.hour % 12 or 12}:{t.minute:02d} {t.strftime('%p')}"


def _et_label(t) -> str:
    """'Tue Nov 4, 9 PM ET' without the %-d / %-I flags."""
    return f"{t.strftime('%a %b')} {t.day}, {t.hour % 12 or 12} {t.strftime('%p')} ET"


def _ensure_lan_cert() -> None:
    """Creates the self-signed certificate that phones need for the camera (HTTPS), once. Needs the
    cryptography package; without it the HTTPS listener stays off and the dashboard says so."""
    cert, key = BASE / "certs" / "cert.pem", BASE / "certs" / "key.pem"
    if cert.exists() and key.exists():
        return
    try:
        import ipaddress
        import socket
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        addrs = {"127.0.0.1"}
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.connect(("8.8.8.8", 80)); addrs.add(sk.getsockname()[0]); sk.close()
        except Exception:
            pass
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Abra local")])
        now = _dt.now(_tz.utc)
        sans = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(a)) for a in sorted(addrs)]
        pem_cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
                    .serial_number(x509.random_serial_number()).not_valid_before(now - _td(days=1))
                    .not_valid_after(now + _td(days=365)).add_extension(x509.SubjectAlternativeName(sans), critical=False)
                    .sign(k, hashes.SHA256()))
        cert.parent.mkdir(exist_ok=True)
        cert.write_bytes(pem_cert.public_bytes(serialization.Encoding.PEM))
        key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                        serialization.NoEncryption()))
        try:
            key.chmod(0o600)
        except Exception:
            pass
        B.log.info("created the self-signed LAN certificate in certs/")
    except ImportError:
        B.log.info("phone camera needs HTTPS: install the cryptography package (pip install -r requirements.txt)")
    except Exception as e:
        B.log.warning("could not create the LAN certificate: %s", str(e)[:140])


class _AbraServer(ThreadingHTTPServer):
    # Windows lets a second program take a port that is already in use when address reuse is on
    allow_reuse_address = not sys.platform.startswith("win")


def _lan_url() -> str:
    import socket
    ip = "127.0.0.1"
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("8.8.8.8", 80)); ip = sk.getsockname()[0]; sk.close()
    except Exception:
        pass
    return "https://%s:%d/" % (ip, HTTPS_PORT)


# --- tiny signaling relay: lets a phone act as a wireless camera for the desktop recorder ---
# The phone (role "cam") is the WebRTC offerer; the desktop (role "app") answers. Offers, answers
# and ICE candidates are relayed here per session. In-memory only, pruned after 5 min idle.
_RTC = {}
_RTC_LOCK = threading.Lock()


def _rtc_prune(now):
    stale = [k for k, v in _RTC.items() if now - v.get("ts", 0) > 300]
    for k in stale:
        _RTC.pop(k, None)


def rtc_send(sess, frm, data):
    import time as _t
    if not sess or not isinstance(data, dict):
        return
    dest = "app" if frm == "cam" else "cam"
    now = _t.time()
    with _RTC_LOCK:
        _rtc_prune(now)
        if sess not in _RTC and len(_RTC) >= _RTC_MAX_SESSIONS:
            return   # too many open camera sessions: refuse new ones
        slot = _RTC.setdefault(sess, {"app": [], "cam": [], "ts": now})
        slot["ts"] = now
        slot[dest].append(data)
        slot[dest][:] = slot[dest][-40:]  # never let a never-polled queue grow unbounded


def rtc_poll(sess, role):
    import time as _t
    if role not in ("app", "cam") or not sess:
        return []
    now = _t.time()
    with _RTC_LOCK:
        _rtc_prune(now)
        slot = _RTC.get(sess)
        if not slot:
            return []
        slot["ts"] = now
        msgs = slot[role]
        slot[role] = []
        return msgs


# ---- Teleprompter script: kept on this server (state.db, git-ignored), and Sage can write one from a summary or links ----
PROMPTER_MAX = 20000
_PROMPTER_DDL = B.PROMPTER_DDL   # one definition, created at startup by bot.ensure_schema()


def prompter_get() -> dict:
    empty = {"script": "", "speed": 3, "size": 14, "saved": False}
    if JUDGE_MODE:
        return empty
    try:
        db = db_conn()
        db.execute(_PROMPTER_DDL)
        r = db.execute("SELECT body, speed, size, updated_at FROM prompter WHERE key='default'").fetchone()
        db.close()
    except Exception:
        return empty
    if not r:
        return empty
    return {"script": r[0] or "", "speed": int(r[1] or 3), "size": int(r[2] or 14), "saved": True, "updated_at": r[3] or ""}


def prompter_save(body: dict) -> dict:
    if JUDGE_MODE:
        return {"ok": False, "reason": "The judge copy does not keep scripts."}
    script = str(body.get("script") or "")[:PROMPTER_MAX]
    try:
        speed = max(1, min(5, int(body.get("speed") or 3)))
        size = max(12, min(22, int(body.get("size") or 14)))
    except (TypeError, ValueError):
        speed, size = 3, 14
    db = db_conn()
    db.execute(_PROMPTER_DDL)
    if not script.strip():
        db.execute("DELETE FROM prompter WHERE key='default'")
    else:
        db.execute("INSERT INTO prompter (key, body, speed, size, updated_at) VALUES ('default', ?, ?, ?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET body=excluded.body, speed=excluded.speed, "
                   "size=excluded.size, updated_at=excluded.updated_at",
                   (script, speed, size, datetime.now(timezone.utc).isoformat()))
    db.commit(); db.close()
    return {"ok": True}


def _public_url(url: str):
    """The URL only if it names a public web address. Private, loopback, link-local and reserved
    addresses are refused, so a link cannot reach the creator's own network."""
    import socket, ipaddress
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        return None
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return None
    return url


def fetch_page_text(url: str, limit: int = 4000) -> str:
    """The visible text of a public web page, for Sage to write from. No redirects, no big pages, no private addresses."""
    import requests
    from html import unescape
    safe = _public_url(url)
    if not safe:
        raise ValueError("That link is not a public web page.")
    r = requests.get(safe, timeout=10, allow_redirects=False, stream=True,
                     headers={"User-Agent": "Abra-script-helper/1.0", "Accept": "text/html,text/plain"})
    if r.status_code != 200:
        raise ValueError("the page did not load (HTTP %d). Paste its text instead" % r.status_code)
    if "html" not in r.headers.get("Content-Type", "") and "text/plain" not in r.headers.get("Content-Type", ""):
        raise ValueError("that link is not a text page. Paste its text instead")
    raw = b""
    for chunk in r.iter_content(65536):
        raw += chunk
        if len(raw) > 1000000:
            break
    body = raw.decode(r.encoding or "utf-8", errors="replace")
    body = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return " ".join(unescape(body).split())[:limit]


def prompter_write(summary: str, links, seconds) -> dict:
    """Sage writes a spoken script from the creator's summary and any public links. Muse Spark writes it."""
    import json as _json
    summary = str(summary or "").strip()[:2000]
    try:
        seconds = max(15, min(180, int(seconds or 60)))
    except (TypeError, ValueError):
        seconds = 60
    sources, notes = [], []
    for u in (links or [])[:3]:
        u = str(u or "").strip()
        if not u:
            continue
        try:
            sources.append((u, fetch_page_text(u)))
        except Exception as e:
            notes.append("Could not read " + u[:80] + ": " + str(e)[:140])
    if not summary and not any(t for _, t in sources):
        return {"lines": [], "error": "Tell Sage what the video is about, or add a link to read.", "notes": notes}
    if not B.muse_key():
        return {"lines": [], "error": "Add your Muse key to have Sage write a script. You can still paste one yourself.", "notes": notes}
    parts = []
    if summary:
        parts.append("Creator's summary of the video:\n" + summary)
    for u, t in sources:
        parts.append("Source page (" + u[:200] + "):\n" + t)
    instr = ("You write a short spoken script for " + B.CREATOR_REF + " to read on camera. About " + str(int(seconds * 2.4)) +
             " words in total, split into short lines of one or two sentences each, in their casual first-person voice. "
             "Use only facts from the summary or the source pages, in your own words; do not copy page text and do not "
             "invent facts. No stage directions, no emoji, nothing political or religious. Reply with ONLY strict JSON, "
             'no prose, no code fence: {"title":"<short title>","lines":["<line 1>","<line 2>"]}')
    db = db_conn()
    try:
        raw = _muse_spark(instr, "\n\n".join(parts), effort="low", max_tokens=1500, db=db, task="Teleprompter script")
    finally:
        db.close()
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = _json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    lines = [str(x).strip()[:280] for x in (data.get("lines") or []) if str(x).strip()][:40]
    if not lines:
        return {"lines": [], "error": "Sage could not write that one. Add a little more detail and try again.", "notes": notes}
    return {"title": str(data.get("title") or "")[:120], "lines": lines, "by": "Muse Spark",
            "sources": [u for u, _ in sources], "notes": notes}


# ---- Replies waiting for approval (per page, off by default). The bot queues the drafted reply; nothing posts until
# the creator approves it here, and the reply goes out with that page's own token.
def reply_list() -> dict:
    empty = {"pending": [], "pages": []}
    if JUDGE_MODE:
        return empty
    db = db_conn()
    try:
        db.execute(B.REPLY_QUEUE_DDL)
        rows = db.execute("SELECT id, page_id, snippet, reply, created_at FROM reply_queue WHERE status='pending' "
                          "ORDER BY id DESC LIMIT 50").fetchall()
        pages = db.execute("SELECT page_id, name FROM pages WHERE enabled=1").fetchall()
        out_pages = []
        for pid, name in pages:
            r = db.execute("SELECT value FROM settings WHERE key=?", ("reply_approval:%s" % pid,)).fetchone()
            out_pages.append({"page_id": str(pid), "name": name or "", "approval": bool(r and r[0] == "1")})
    finally:
        db.close()
    return {"pending": [{"id": r[0], "page_id": r[1], "snippet": r[2] or "", "reply": r[3] or "", "created_at": r[4] or ""} for r in rows],
            "pages": out_pages}


def reply_set_approval(page_id: str, on: bool) -> dict:
    if not re.fullmatch(r"\d+", str(page_id or "")):
        return {"ok": False, "msg": "Unknown page."}
    if JUDGE_MODE:
        return {"ok": False, "msg": "The judge copy does not post replies."}
    db = db_conn()
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("reply_approval:%s" % page_id, "1" if on else "0"))
    db.commit(); db.close()
    return {"ok": True, "msg": "Replies to this page " + ("wait for your approval." if on else "post as before.")}


def reply_decide(row_id, action: str) -> dict:
    import requests as _rq
    if JUDGE_MODE:
        return {"ok": False, "msg": "The judge copy does not post replies."}
    try:
        row_id = int(row_id)
    except (TypeError, ValueError):
        return {"ok": False, "msg": "That reply is not in the list."}
    db = db_conn()
    try:
        db.execute(B.REPLY_QUEUE_DDL)
        row = db.execute("SELECT comment_id, page_id, reply, status FROM reply_queue WHERE id=?", (row_id,)).fetchone()
        if not row or row[3] != "pending":
            return {"ok": False, "msg": "That reply is no longer waiting for you."}
        now = datetime.now(timezone.utc).isoformat()
        if action == "reject":
            db.execute("UPDATE reply_queue SET status='rejected', decided_at=? WHERE id=? AND status='pending'", (now, row_id))
            db.commit()
            return {"ok": True, "msg": "Rejected. Nothing was posted."}
        if action != "approve":
            return {"ok": False, "msg": "Unknown action."}
        cur = db.execute("UPDATE reply_queue SET status='posting', decided_at=? WHERE id=? AND status='pending'", (now, row_id))
        db.commit()
        if cur.rowcount == 0:
            return {"ok": False, "msg": "That reply was already handled."}
        comment_id, page_id, reply = row[0], str(row[1]), row[2]
        try:
            r = _rq.post("https://graph.facebook.com/v23.0/%s/comments" % comment_id,
                         data={"message": reply, "access_token": _page_token(page_id)}, timeout=30)
            data = r.json() if r.content else {}
            if r.status_code == 200 and data.get("id"):
                db.execute("UPDATE reply_queue SET status='posted', error='' WHERE id=?", (row_id,))
                out = {"ok": True, "msg": "Posted."}
            else:
                err = str((data.get("error") or {}).get("message") or ("HTTP %d" % r.status_code))[:200]
                db.execute("UPDATE reply_queue SET status='error', error=? WHERE id=?", (err, row_id))
                out = {"ok": False, "msg": "Facebook did not accept the reply: " + err}
        except Exception as e:
            db.execute("UPDATE reply_queue SET status='error', error=? WHERE id=?", (str(e)[:200], row_id))
            out = {"ok": False, "msg": "The reply could not be sent. Try again."}
        db.commit()
        return out
    finally:
        db.close()


def muse_film_plan(genre, inputs) -> dict:
    """Sage writes a teleprompter plan for the chosen genre preset (reaction, ugc) from the
    creator's inputs. Unified shape {hook, beats[{cue,line}], compliance}."""
    g = FILM_GENRES.get(str(genre or ""))
    if not g:
        return {"hook": "", "beats": [], "compliance": ""}
    parts = []
    for f in g.get("inputs", []):
        v = str((inputs or {}).get(f.get("key")) or "").strip()[:1400]
        if v:
            parts.append(str(f.get("label", "")) + ": " + v)
    user_text = chr(10).join(parts)
    if not user_text.strip():
        return {"hook": "", "beats": [], "compliance": ""}
    db = db_conn()
    # Stage 1 - Muse Wonder directs: hook strategy, beat structure, compliance.
    raw = _muse_wonder(g.get("director_prompt", ""), user_text, effort="low", db=db, task="Create plan")
    out = _parse_film(raw)
    if not out.get("compliance") and g.get("compliance_tip"):
        out["compliance"] = g["compliance_tip"]
    out["by"] = "Muse Wonder" if out.get("beats") else ""
    # Stage 2 - Muse Spark scripts the teleprompter lines in the creator's voice.
    if out.get("beats"):
        try:
            sp = _spark_script(out, db=db)
            if sp:
                if sp.get("hook"):
                    out["hook"] = sp["hook"][:120]
                sl = sp.get("lines") or []
                for i, b in enumerate(out["beats"]):
                    if i < len(sl) and str(sl[i]).strip():
                        b["line"] = str(sl[i]).strip()[:280]
                out["by"] = "Muse Wonder + Muse Spark"
        except Exception:
            pass
    db.close()
    return out


def parse_storyboard_prompts(text: str) -> list[str]:
    """Pull the image prompts out of a storyboard markdown."""
    import re as _re
    prompts = []
    # match '**Image prompt**...' / '**Prompt A ...:**' / '**Prompt B ...:**' blocks
    pattern = _re.compile(
        r"\*\*(?:Image prompt|Prompt A[^*]*|Prompt B[^*]*)\*\*[:\s—-]*(.+?)(?=\n\s*\n|\n\s*[\d*#-]|\Z)",
        _re.S | _re.I)
    for m in pattern.finditer(text):
        p = " ".join(m.group(1).split())
        if len(p) > 40:
            prompts.append(p[:900])
    return prompts


def generate_photos_async(count: int, ref: bytes, ref_mime: str) -> None:
    import threading
    from datetime import date as _date

    def run():
        try:
            sb = storyboard_current()
            prompts = parse_storyboard_prompts(sb.get("text", ""))
            if not prompts:
                GEN_STATE.update(status="error",
                                 error="No storyboard prompts found — generate a storyboard first.")
                return
            todo = prompts[:count]
            GEN_STATE.update(status="generating", done=0, total=len(todo), error="",
                             date=_date.today().isoformat())
            day_dir = GEN_DIR / _date.today().isoformat()
            day_dir.mkdir(parents=True, exist_ok=True)
            # Standalone photo generation has been retired — the creator studio now
            # builds its imagery from video frames, Story FX overlays (Muse Image; one live test call passed, Nov 2) and SAM cutouts.
            GEN_STATE.update(status="error",
                             error="Photo generation has been retired; drop a video in Launchpad instead.")
            return
        except Exception as e:
            GEN_STATE.update(status="error", error=str(e)[:300])

    if GEN_STATE["status"] == "generating":
        return
    threading.Thread(target=run, daemon=True).start()


def generated_list() -> list[dict]:
    from datetime import date as _date
    day_dir = GEN_DIR / _date.today().isoformat()
    out = []
    if day_dir.exists():
        for p in sorted(day_dir.glob("*.png")):
            out.append({"name": p.name, "url": f"/generated/{day_dir.name}/{p.name}"})
        for p in sorted(day_dir.glob("*.error.txt")):
            out.append({"name": p.name, "error": p.read_text(encoding="utf-8")[:150]})
    return out


def storyboard_today_path():
    return BASE / "storyboards" / (datetime.now().date().isoformat() + ".md")


REALISM_FOOTER = """

---
## Before you queue the photos — 10-second realism check (zoom in!)
Hands & fingers · teeth · hairline edges · jewelry/earrings · background text ·
same-person consistency across shots. If a photo fails any of these, leave it out of the queue or retake it
it — Stories are viewed full-screen on phones.
"""


def generate_storyboard_async(notes: str = "", photo: bytes = b"",
                              photo_type: str = "image/jpeg",
                              dual: bool = False) -> None:
    import threading

    def run():
        try:
            import storyboard as sbmod
            import anthropic, base64
            now = datetime.now()
            prompt = sbmod.PROMPT.format(weekday=now.strftime("%A"), month=now.strftime("%B"), creator=B.CREATOR_REF)
            if dual:
                prompt += ("\n\nIMPORTANT — TWO PAGES, ONE STORYLINE: The creator posts the same "
                           "day's story on two Facebook pages, but the photos must not be "
                           "identical. For EACH hour output the moment line once, then TWO "
                           "image prompts:\n"
                           " - **Prompt A (" + B.PRIMARY_PAGE_NAME + " page):** the scene as described.\n"
                           " - **Prompt B (" + B.SECOND_PAGE_NAME + " page):** the SAME moment and "
                           "setting seconds later — shift the camera angle or distance, a "
                           "slightly different pose or gaze, a small natural difference "
                           "(mug now half-empty, hair tucked). Same outfit, same location, "
                           "same lighting — clearly the same real moment, visibly not the "
                           "same photograph.")
            if notes.strip():
                prompt += ("\n\nThe creator's ACTUAL plans for today — build the storyboard around "
                           "these real moments at sensible hours, and fill the remaining "
                           "hours with normal at-home/errand moments that connect them "
                           "naturally:\n" + notes.strip()[:2000])
            content = [{"type": "text", "text": prompt}]
            if photo:
                content.insert(0, {"type": "image", "source": {
                    "type": "base64", "media_type": photo_type,
                    "data": base64.standard_b64encode(photo).decode()}})
                content.append({"type": "text", "text":
                    "The attached photo is the creator TODAY. Describe their hair, outfit style and "
                    "overall look from this photo inside every image prompt so the whole "
                    "day stays visually consistent with how they actually look today "
                    "(adjust outfit logically for gym/evening moments)."})
            client = anthropic.Anthropic()
            msg = client.messages.create(
                model="claude-sonnet-5", max_tokens=4000,
                messages=[{"role": "user", "content": content}],
            )
            text = next(b.text for b in msg.content if b.type == "text")
            p = storyboard_today_path()
            p.parent.mkdir(exist_ok=True)
            p.write_text(f"# Story storyboard — {now.strftime('%A %Y-%m-%d')}"
                         + (" — DUAL (both pages)" if dual else "")
                         + f"\n\n{text}\n{REALISM_FOOTER}", encoding="utf-8")
            text = text + REALISM_FOOTER
            SB_STATE.update(status="done", text=text, date=now.date().isoformat())
        except Exception as e:
            err = str(e)
            if "credit balance is too low" in err:
                err = "Anthropic credits are empty — top up at platform.claude.com → Billing, then try again."
            SB_STATE.update(status="error", text=err[:300])

    if SB_STATE["status"] == "generating":
        return
    SB_STATE.update(status="generating", text="")
    threading.Thread(target=run, daemon=True).start()


def storyboard_current() -> dict:
    p = storyboard_today_path()
    if SB_STATE["status"] in ("idle", "done") and p.exists():
        return {"status": "done", "text": p.read_text(encoding="utf-8"), "date": datetime.now().date().isoformat()}
    return dict(SB_STATE)


STORY_DIR = BASE / "story_queue"
STORY_HOURS = list(range(6, 22)) # 6:00 .. 21:00 local


def queue_add(page_id: str, blobs: list[tuple[str, bytes]]) -> str:
    import uuid
    STORY_DIR.mkdir(exist_ok=True)
    db = db_conn()
    taken = {r[0] for r in db.execute(
        "SELECT slot FROM story_queue WHERE page_id=? AND posted_at IS NULL AND error IS NULL",
        (page_id,))}
    now = datetime.now()
    slots, day = [], now.date()
    from datetime import timedelta as _td
    while len(slots) < len(blobs):
        for h in STORY_HOURS:
            t = datetime(day.year, day.month, day.day, h, 0)
            iso = t.strftime("%Y-%m-%dT%H:%M")
            if t > now and iso not in taken and len(slots) < len(blobs):
                slots.append(iso)
        day += _td(days=1)
    for (name, blob), slot in zip(blobs, slots):
        ext = (name.rsplit(".", 1)[-1] if "." in name else "jpg")[:5]
        p = STORY_DIR / f"{uuid.uuid4().hex}.{ext}"
        p.write_bytes(blob)
        db.execute("INSERT INTO story_queue (page_id, path, slot) VALUES (?, ?, ?)",
                   (page_id, str(p), slot))
    db.close()
    first = slots[0][11:16] if slots else ""
    return f" ✓ {len(blobs)} photo{'s' if len(blobs)!=1 else ''} queued — first posts at {first}"


def queue_list() -> list[dict]:
    db = db_conn()
    db.execute("""CREATE TABLE IF NOT EXISTS story_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT, page_id TEXT NOT NULL, path TEXT NOT NULL,
        slot TEXT NOT NULL, posted_at TEXT, error TEXT)""")
    pages = dict(db.execute("SELECT page_id, name FROM pages").fetchall())
    rows = db.execute(
        "SELECT id, page_id, slot, posted_at, error FROM story_queue "
        "ORDER BY slot DESC LIMIT 40").fetchall()
    db.close()
    out = []
    for i, pid, slot, posted, err in rows:
        state = "posted" if posted else ("error" if err else "queued")
        out.append({"id": i, "page": pages.get(pid, pid), "slot": slot.replace("T", " "),
                    "state": state, "err": (err or "")[:80]})
    return sorted(out, key=lambda x: x["slot"])


def queue_delete(qid: int) -> None:
    db = db_conn()
    row = db.execute("SELECT path, posted_at FROM story_queue WHERE id=?", (qid,)).fetchone()
    if row and not row[1]:
        db.execute("DELETE FROM story_queue WHERE id=?", (qid,))
        try: pathlib_Path(row[0]).unlink(missing_ok=True)
        except Exception: pass
    db.close()


from pathlib import Path as pathlib_Path

PLAN = [ # (label, page-name match, window start hour ET, window end hour ET, due hour)
    (B.SECOND_PAGE_NAME + " — morning reel", B.SECOND_PAGE_NAME, 5, 15, 10),
    (B.PRIMARY_PAGE_NAME + " — daily reel", B.PRIMARY_PAGE_NAME, 5, 23, 12.5),
    (B.SECOND_PAGE_NAME + " — evening reel", B.SECOND_PAGE_NAME, 15, 24, 19),
]


def _page_match(post_page: str, want: str) -> bool:
    if want == B.PRIMARY_PAGE_NAME:
        return post_page == B.PRIMARY_PAGE_NAME
    return B.SECOND_PAGE_NAME in post_page


def schedule_status() -> dict:
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    posts = fetch_posts("recent")
    def local(ts):
        try:
            return datetime.fromisoformat(ts.replace("+0000", "+00:00")).astimezone(et)
        except Exception:
            return None
    todays = [(p, local(p.get("created_ts", ""))) for p in posts]
    todays = [(p, t) for p, t in todays if t and t.date() == now.date()]
    slots, used = [], set()
    for label, want, h0, h1, due in PLAN:
        hit = next(((p, t) for p, t in todays
                    if p["id"] not in used and _page_match(p["page"], want)
                    and h0 <= t.hour < h1), None)
        if hit:
            used.add(hit[0]["id"])
            slots.append({"label": label, "state": "done",
                          "info": _clock12(hit[1]) + " — " + hit[0]["caption"][:40]})
        elif now.hour + now.minute/60 < due:
            slots.append({"label": label, "state": "upcoming",
                          "info": f"due ~{int(due)}:{'30' if due%1 else '00'} ET"})
        else:
            slots.append({"label": label, "state": "missed", "info": "not posted yet today"})
    # streak: consecutive days (starting yesterday going back) meeting plan
    by_day = {}
    for p in posts:
        t = local(p.get("created_ts", ""))
        if t:
            d = by_day.setdefault(t.date().isoformat(), {"show": 0, "ab": 0})
            d["show" if _page_match(p["page"], B.SECOND_PAGE_NAME) else "ab"] += 1
    streak, day = 0, now.date()
    from datetime import timedelta as _td
    day -= _td(days=1)
    while True:
        d = by_day.get(day.isoformat())
        if d and d["show"] >= 2 and d["ab"] >= 1:
            streak += 1
            day -= _td(days=1)
        else:
            break
    today_counts = by_day.get(now.date().isoformat(), {"show": 0, "ab": 0})
    return {"slots": slots, "streak": streak,
            "today": f"{today_counts['show']}/2 {B.SECOND_PAGE_NAME} · {today_counts['ab']}/1 {B.PRIMARY_PAGE_NAME}"}


def _tail_lines(p, max_bytes: int = 400_000) -> list[str]:
    """Read only the end of a log file — bot.log can grow huge."""
    size = p.stat().st_size
    with open(p, "rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        return fh.read().decode(errors="replace").splitlines()


def parse_events(max_events: int = 60) -> list[dict]:
    # Log lines carry clock times only (no dates), so sorting across files mixes
    # months-old backfill events into "now". Instead: skip files that haven't
    # been written in 24h, and keep each file's own order (live bot last).
    events = []
    cutoff = datetime.now().timestamp() - 24 * 3600
    for src, label in (("backfill.log", "backfill"), ("dryrun.log", "dry run"), ("bot.log", "live bot")):
        p = BASE / src
        if not p.exists() or p.stat().st_mtime < cutoff:
            continue
        try:
            lines = _tail_lines(p)
        except OSError:
            continue
        for raw in lines[-800:]:
            m = LINE.match(raw.strip())
            if m:
                d = m.groupdict()
                d["source"] = label
                events.append(d)
    return events[-max_events:]


import requests as _rq

_earn_cache: dict = {} # post_id -> {"at": ts, "usd": float}


def _page_tokens() -> dict:
    db = db_conn()
    rows = db.execute("SELECT page_id, token FROM pages").fetchall()
    db.close()
    return dict(rows)


def post_earnings(post_id: str, token: str) -> float:
    """Lifetime approximate earnings for one post (cached 1h). 0 when
    unavailable — not every post is monetized."""
    hit = _earn_cache.get(post_id)
    if hit and _time.time() - hit["at"] < 3600:
        return hit["usd"]
    usd = 0.0
    try:
        r = _rq.get(
            f"https://graph.facebook.com/{B.API_VERSION}/{post_id}/insights",
            params={"metric": "monetization_approximate_earnings",
                    "access_token": token}, timeout=15).json()
        vals = (r.get("data") or [{}])[0].get("values") or []
        if vals:
            usd = round(float(vals[-1].get("value") or 0), 2)
    except Exception:
        pass
    _earn_cache[post_id] = {"at": _time.time(), "usd": usd}
    return usd


def page_earnings_summary() -> dict:
    """Earned USD today / 7 days / 30 days across pages, from page_insights."""
    out = {"today": 0.0, "week": 0.0, "month": 0.0}
    try:
        db = db_conn()
        today = datetime.now(timezone.utc).date().isoformat()
        week = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
        month = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        for key, since in (("today", today), ("week", week), ("month", month)):
            row = db.execute(
                "SELECT SUM(value) FROM page_insights WHERE metric="
                "'monetization_approximate_earnings' AND taken_at>=?", (since,)).fetchone()
            out[key] = round(row[0] or 0, 2)
        db.close()
    except Exception:
        pass
    return out


def expense_tracker() -> dict:
    """Earnings vs AI/content-tool spending for the Insights expense tracker.
    Earnings = real FB monetization_approximate_earnings (page_insights).
    Spending = the logged `spend` ledger (estimated Claude, Muse and voice API cost, USD)."""
    out = {"earned": {"today": 0.0, "week": 0.0, "month": 0.0},
           "spent": {"today": 0.0, "week": 0.0, "month": 0.0},
           "net": {"today": 0.0, "week": 0.0, "month": 0.0},
           "breakdown": [], "currency": "USD"}
    try:
        out["earned"] = page_earnings_summary()
    except Exception:
        pass
    try:
        db = db_conn()
        today = datetime.now(timezone.utc).date().isoformat()
        week = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
        month = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        for key, since in (("today", today), ("week", week), ("month", month)):
            row = db.execute(
                "SELECT SUM(cost) FROM spend WHERE substr(ts,1,10)>=?",
                (since,)).fetchone()
            out["spent"][key] = round(row[0] or 0, 2)
        rows = db.execute(
            "SELECT provider, kind, COUNT(*), SUM(cost) FROM spend "
            "WHERE substr(ts,1,10)>=? GROUP BY provider, kind "
            "ORDER BY SUM(cost) DESC LIMIT 8", (month,)).fetchall()
        db.close()
        for pr, kind, n, cost in rows:
            out["breakdown"].append({"provider": pr, "kind": kind,
                                     "calls": n, "cost": round(cost or 0, 4)})
    except Exception:
        pass
    try:
        db = db_conn()
        since30 = (datetime.now(timezone.utc) - timedelta(days=29)).date().isoformat()
        earn_rows = dict(db.execute(
            "SELECT substr(taken_at,1,10) d, ROUND(SUM(value),2) FROM page_insights "
            "WHERE metric='monetization_approximate_earnings' AND substr(taken_at,1,10)>=? GROUP BY d",
            (since30,)).fetchall())
        spend_rows = dict(db.execute(
            "SELECT substr(ts,1,10) d, ROUND(SUM(cost),4) FROM spend "
            "WHERE substr(ts,1,10)>=? GROUP BY d", (since30,)).fetchall())
        split_rows = db.execute(
            "SELECT provider, SUM(cost) FROM spend WHERE substr(ts,1,10)>=? GROUP BY provider",
            (since30,)).fetchall()
        db.close()
        start = datetime.now(timezone.utc).date() - timedelta(days=29)
        daily = []
        for i in range(30):
            day = (start + timedelta(days=i)).isoformat()
            daily.append({"d": day, "earned": float(earn_rows.get(day) or 0),
                          "spent": float(spend_rows.get(day) or 0)})
        out["daily"] = daily
        out["split"] = {pr: round(c or 0, 2) for pr, c in split_rows}
    except Exception:
        pass
    for k in ("today", "week", "month"):
        out["net"][k] = round(out["earned"].get(k, 0) - out["spent"].get(k, 0), 2)
    return out


def best_time_to_post() -> dict:
    """When the page's posts actually perform best, from its real post history.
    Buckets every post by ET weekday + time-of-day block and ranks by average
    engagement score (c*3 + r + s*2). Honest: descriptive of the page's own data,
    labelled with the sample size. No external 'best times' claims."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    blocks = [("Morning", "6-11a", 6, 11), ("Midday", "11a-3p", 11, 15),
              ("Afternoon", "3-6p", 15, 18), ("Evening", "6-10p", 18, 22),
              ("Late", "10p-6a", 22, 6)]
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def block_idx(h):
        for i, (_, _, a, b) in enumerate(blocks):
            if a <= b:
                if a <= h < b:
                    return i
            else:  # wraps midnight (Late)
                if h >= a or h < b:
                    return i
        return len(blocks) - 1

    # combine the viral (up to a year) + recent post sets, dedupe by id
    seen, posts = set(), []
    for sort in ("viral", "recent"):
        try:
            for pp in fetch_posts(sort):
                pid = pp.get("id")
                if pid and pid not in seen:
                    seen.add(pid)
                    posts.append(pp)
        except Exception:
            pass

    cells = {}  # (day,block) -> [scores]
    dated = []
    for pp in posts:
        ts = pp.get("created_ts", "")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("+0000", "+00:00")).astimezone(et)
        except Exception:
            continue
        sc = pp.get("score")
        if sc is None:
            sc = (pp.get("comments", 0) * 3 + pp.get("reactions", 0)
                  + pp.get("shares", 0) * 2)
        d, b = dt.weekday(), block_idx(dt.hour)
        cells.setdefault((d, b), []).append(sc)
        dated.append((dt, sc))

    n = len(dated)
    out = {"posts_analyzed": n, "tz": "ET", "days": days,
           "blocks": [{"name": nm, "range": rg} for nm, rg, _, _ in blocks],
           "grid": [], "by_day": [], "by_block": [], "best": None,
           "headline": "", "confidence": "low", "span_days": 0}
    if not n:
        out["headline"] = ("Post a few times and Abra will learn your best "
                           "windows from what actually performs.")
        return out

    overall = sum(sc for _, sc in dated) / n
    span = (max(d for d, _ in dated) - min(d for d, _ in dated)).days
    out["span_days"] = span

    def avg(lst):
        return round(sum(lst) / len(lst), 1) if lst else 0.0

    # grid of avg score per (day,block) with counts
    grid = []
    best = None
    for d in range(7):
        row = []
        for b in range(len(blocks)):
            vals = cells.get((d, b), [])
            a = avg(vals)
            row.append({"avg": a, "n": len(vals)})
            if len(vals) >= 3 and (best is None or a > best["avg"]):
                best = {"day": days[d], "block": blocks[b][0],
                        "range": blocks[b][1], "avg": a, "n": len(vals)}
        grid.append(row)
    out["grid"] = grid

    # 1-D rankings (more samples per bucket = more trustworthy)
    by_day = []
    for d in range(7):
        vals = [sc for (dd, _), lst in cells.items() if dd == d for sc in lst]
        by_day.append({"day": days[d], "n": len(vals), "avg": avg(vals)})
    by_block = []
    for b in range(len(blocks)):
        vals = [sc for (_, bb), lst in cells.items() if bb == b for sc in lst]
        by_block.append({"block": blocks[b][0], "range": blocks[b][1],
                         "n": len(vals), "avg": avg(vals)})
    out["by_day"] = by_day
    out["by_block"] = by_block

    bd = max((x for x in by_day if x["n"] >= 2), key=lambda x: x["avg"], default=None)
    bb = max((x for x in by_block if x["n"] >= 2), key=lambda x: x["avg"], default=None)
    out["best"] = best or (bd and {"day": bd["day"], "block": (bb or {}).get("block", ""),
                                   "range": (bb or {}).get("range", ""), "avg": bd["avg"],
                                   "n": bd["n"]})
    out["confidence"] = "high" if n >= 25 else ("medium" if n >= 10 else "low")

    sweet = out["best"]
    if sweet and overall > 0:
        mult = sweet["avg"] / overall
        where = f"{sweet['day']} {sweet['block'].lower()} ({sweet['range']} ET)"
        if mult >= 1.15:
            out["headline"] = f"Your {where} posts pull about {mult:.1f}x your typical engagement."
        else:
            out["headline"] = f"{where} is your strongest window so far."
    else:
        out["headline"] = f"Based on {n} of your recent and top-performing posts."
    return out


def sync_ad_reels() -> None:
    """A reel queued as an ad is flagged once it is live: its permalink is matched to the Page post."""
    try:
        db = db_conn()
        rows = db.execute("SELECT id, video_id FROM content_queue WHERE as_ad=1 AND ad_linked=0 "
                          "AND video_id IS NOT NULL AND video_id != ''").fetchall()
        db.close()
    except Exception:
        return
    if not rows:
        return
    try:
        posts = fetch_posts("recent")
    except Exception:
        return
    for rid, vid in rows:
        for pst in posts:
            url = pst.get("url") or ""
            if ("/reel/%s" % vid) in url or ("/videos/%s" % vid) in url:
                set_ad_post(str(pst["id"]), True)
                db = db_conn()
                db.execute("UPDATE content_queue SET ad_linked=1 WHERE id=?", (rid,))
                db.commit(); db.close()
                break


def ad_summary() -> dict:
    """Ad posts (flagged on the Boost tab) with what the crew did on their comments.
    Counts come from the bot's records: fans_log (commenters the crew processed), handled (replies, holds), liked."""
    sync_ad_reels()
    ids = ad_post_ids()
    if not ids:
        return {"ads": []}
    try:
        recent = {str(x.get("id")): x for x in fetch_posts("recent")}
    except Exception:
        recent = {}
    db = db_conn()
    out = []
    for pid in ids:
        p = recent.get(pid, {})
        comments = db.execute("SELECT COUNT(*) FROM fans_log WHERE post_id=?", (pid,)).fetchone()[0]
        replied = db.execute("SELECT COUNT(*) FROM fans_log f JOIN handled h ON h.comment_id=f.comment_id "
                             "WHERE f.post_id=? AND h.action='replied'", (pid,)).fetchone()[0]
        held = db.execute("SELECT COUNT(*) FROM fans_log f JOIN handled h ON h.comment_id=f.comment_id "
                          "WHERE f.post_id=? AND h.action='skipped' AND h.reply_text LIKE 'held by Muse review%'",
                          (pid,)).fetchone()[0]
        liked = db.execute("SELECT COUNT(*) FROM fans_log f JOIN liked l ON l.comment_id=f.comment_id "
                           "WHERE f.post_id=?", (pid,)).fetchone()[0]
        out.append({"id": pid, "caption": (p.get("caption") or "")[:90], "page": p.get("page") or "",
                    "created": (p.get("created") or "")[:10], "url": p.get("url") or "",
                    "comments": comments, "replied": replied, "held": held, "liked": liked})
    db.close()
    return {"ads": out}


def ad_post_ids() -> list:
    """Post ids the page is running an ad on (flagged from the Boost tab)."""
    try:
        db = db_conn()
        row = db.execute("SELECT value FROM settings WHERE key = 'ad_posts'").fetchone()
        db.close()
        return [str(x) for x in json.loads(row[0])] if row and row[0] else []
    except Exception:
        return []


def set_ad_post(pid: str, on: bool) -> None:
    ids = ad_post_ids()
    if on and pid not in ids:
        ids.append(pid)
    if not on:
        ids = [x for x in ids if x != pid]
    db = db_conn()
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ad_posts', ?)", (json.dumps(ids),))
    db.commit(); db.close()


def review_stats() -> dict:
    """Muse review of Claude's reply drafts: how many were checked, and how many were held back."""
    try:
        db = db_conn()
        checks = db.execute("SELECT COUNT(*) FROM spend WHERE kind='review'").fetchone()[0]
        held = db.execute("SELECT COUNT(*) FROM handled WHERE action='skipped' AND reply_text LIKE 'held by Muse review%'").fetchone()[0]
        db.close()
    except Exception:
        checks, held = 0, 0
    return {"checks": int(checks), "held": int(held)}


def build_ledger_pdf(month: str) -> bytes:
    """Monthly AI/content-tools expense report for the creator's accountant.
    month = "YYYY-MM". Estimates from the logged spend ledger."""
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle)

    db = db_conn()
    B.ensure_schema(db)   # a fresh database still gives a valid PDF with zero rows
    rows = db.execute(
        "SELECT substr(ts,1,10) d, provider, kind, COUNT(*), SUM(tin), SUM(tout), "
        "SUM(cost) FROM spend WHERE substr(ts,1,7)=? "
        "GROUP BY d, provider, kind ORDER BY d, provider, kind", (month,)).fetchall()
    summary = db.execute(
        "SELECT provider, kind, COUNT(*), SUM(tin), SUM(tout), SUM(cost) FROM spend "
        "WHERE substr(ts,1,7)=? GROUP BY provider, kind ORDER BY provider, kind",
        (month,)).fetchall()
    earned = db.execute(
        "SELECT ROUND(SUM(value),2) FROM page_insights WHERE "
        "metric='monetization_approximate_earnings' AND substr(taken_at,1,7)=?",
        (month,)).fetchone()[0] or 0
    db.close()

    pretty = {"claude": "Anthropic Claude (comment replies & video analysis)",
              "muse": "Meta Muse (comment screening & easy replies)"}
    vendor = {"claude": "Anthropic PBC", "muse": "Meta Platforms, Inc."}

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.8*inch,
                            bottomMargin=0.8*inch)
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8,
                           textColor=colors.HexColor("#6b5d4e"))
    story = [
        Paragraph(("Demo Creator — SAMPLE DATA (invented numbers)" if JUDGE_MODE else ((B.CREATOR_NAME + " — Creator Business") if B.CREATOR_NAME else "Creator Business")), styles["Title"]),
        Paragraph(f"AI &amp; Content Tools — Expense Report, {month}", styles["Heading2"]),
        Paragraph(f"Generated {datetime.now().strftime('%B %d, %Y')} by Abra",
                  small),
        Spacer(1, 14),
        Paragraph("Summary by service", styles["Heading3"]),
    ]
    data = [["Service", "Vendor", "Calls", "Tokens in", "Tokens out", "Cost (USD)"]]
    total = 0.0
    cellstyle = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8, leading=10)
    for pr, kind, n, tin, tout, cost in summary:
        total += cost or 0
        data.append([Paragraph(f"{pretty.get(pr, pr)} — {kind}", cellstyle),
                     Paragraph(vendor.get(pr, ""), cellstyle),
                     str(n), f"{tin or 0:,}", f"{tout or 0:,}", f"${cost or 0:.2f}"])
    data.append(["TOTAL", "", "", "", "", f"${total:.2f}"])
    t = Table(data, colWidths=[2.8*inch, 1.4*inch, 0.55*inch, 0.8*inch, 0.8*inch, 0.85*inch])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3E6D4")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#9A6B43")),
        ("GRID", (0, 0), (-1, -2), 0.3, colors.HexColor("#E0D0BA")),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
    ]))
    story += [t, Spacer(1, 16), Paragraph("Daily detail", styles["Heading3"])]
    data2 = [["Date", "Service", "Calls", "Cost (USD)"]]
    for d, pr, kind, n, tin, tout, cost in rows:
        data2.append([d, f"{pr} — {kind}", str(n), f"${cost or 0:.2f}"])
    if len(data2) == 1:
        data2.append(["(no logged activity this month)", "", "", ""])
    t2 = Table(data2, colWidths=[1.1*inch, 3.2*inch, 0.7*inch, 1.0*inch])
    t2.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3E6D4")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#E0D0BA")),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
    ]))
    story += [
        t2, Spacer(1, 16),
        Paragraph(f"For reference — Facebook creator earnings recorded this month "
                  f"(approximate, per Meta Insights): <b>${earned:,.2f}</b>",
                  styles["Normal"]),
        Spacer(1, 10),
        Paragraph("Note: figures are estimates: Claude is priced from token usage at "
                  "list prices; many Muse and voice calls use flat per-call estimates (ledger tracking began Oct 17, 2026). For filing, "
                  "reconcile against the official invoices: Anthropic "
                  "(console.anthropic.com → Billing), Meta Model API (dev.meta.ai "
                  "→ Billing).", small),
    ]
    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HACKATHON SPRINT FEATURE: any-creator onboarding (/connect)
# A creator signs in with Facebook, picks their page(s), and the crew
# activates for them. Demo scope: works for app admins/testers until Meta
# App Review approves the permissions for the general public.
# ---------------------------------------------------------------------------
import secrets as _secrets
ABRA_TOKEN = _secrets.token_urlsafe(32)   # access gate: localhost trusted, LAN (phone cam) must carry this

# ---- Stats privacy: account data is served only to a browser that finished Facebook sign-in ----
# /connect/finish sets the abra_fb cookie. Localhost and the LAN token are not enough on their own:
# "Look around" (no Facebook) never gets stats.
FB_STATS_GET = {"/pages", "/rights", "/storyqueue", "/schedule", "/schedule/upcoming", "/engagement",
                "/watchinsights", "/dms", "/monetization", "/reviewstats", "/expenses", "/besttime",
                "/calendar", "/analyst", "/trend", "/trees", "/ledger.pdf", "/deals", "/fanlove",
                "/budget", "/data", "/posts", "/ads/summary", "/targets", "/ideas", "/crewread",
                "/profile", "/avatar"}
FB_STATS_PREFIX = ("/spotlight",)
# Writes, posts, paid calls and key saves: all need the same Facebook sign-in.
FB_STATS_POST = {"/assistant", "/assistant/voice", "/assistant/tts", "/pitch",
                 "/postphoto", "/schedule/confirm", "/schedule/analyze", "/queuegenerated",
                 "/storyqueue", "/storyqueue_del", "/genphotos", "/reactions/add", "/reactions/del",
                 "/storyboard", "/film/plan", "/react/plan", "/rights/protect", "/rights/dispute",
                 "/contractreview", "/rankhook", "/deals", "/engagement", "/pages", "/pubmode",
                 "/dmmode", "/targets", "/ads/flag", "/connect/ai"}
FB_STATS_POST_PREFIX = ("/checklist/",)
FB_COOKIE = "abra_fb"
FB_COOKIE_DAYS = 30


def fb_session_value() -> str:
    """The abra_fb cookie value: an HMAC of a per-install secret kept in state.db (settings)."""
    import hmac as _hmac, hashlib as _hashlib
    db = db_conn()
    try:
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        row = db.execute("SELECT value FROM settings WHERE key='fb_session_secret'").fetchone()
        if row:
            secret = row[0]
        else:
            secret = _secrets.token_urlsafe(32)
            db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('fb_session_secret', ?)", (secret,))
            db.commit()
    finally:
        db.close()
    return _hmac.new(secret.encode(), b"abra-fb-signed-in", _hashlib.sha256).hexdigest()


def fb_cookie_header(clear: bool = False) -> str:
    if clear:
        return FB_COOKIE + "=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
    return FB_COOKIE + "=" + fb_session_value() + "; Path=/; HttpOnly; SameSite=Lax; Max-Age=" + str(FB_COOKIE_DAYS * 86400)


def fb_stats_request(path: str, post: bool) -> bool:
    p = urlparse(path).path
    if post:
        return p in FB_STATS_POST or p.startswith(FB_STATS_POST_PREFIX)
    return p in FB_STATS_GET or p.startswith(FB_STATS_PREFIX)


# ---- Visitor sessions: a visitor can paste their own Muse and/or Claude key for the Create steps ----
# Kept only in this server's memory for that browser session (12 hours, or until a restart). Never written to
# .env or state.db, never logged, never sent back to the page. Only the routes in VISITOR_NEEDS accept them.
VISITOR_COOKIE = "abra_visitor"
VISITOR_TTL = 12 * 3600
VISITOR_NEEDS = {"/film/plan": ("muse",), "/prompter/write": ("muse",), "/react/plan": ("muse",), "/schedule/analyze": ("muse", "claude")}
_VISITOR_KEYS = {}   # session id -> {"muse": key, "claude": key, "at": time}
_VISITOR_LOCK = threading.Lock()


def _visitor_update(sid: str, updates: dict) -> str:
    """Merge keys into a session (a new session if sid is empty or unknown). Returns the session id."""
    with _VISITOR_LOCK:
        now = _time.time()
        for k in [k for k, v in _VISITOR_KEYS.items() if now - v["at"] > VISITOR_TTL]:
            _VISITOR_KEYS.pop(k, None)
        if not sid or sid not in _VISITOR_KEYS:
            sid = _secrets.token_urlsafe(24)
            _VISITOR_KEYS[sid] = {"at": now}
        _VISITOR_KEYS[sid].update(updates)
        _VISITOR_KEYS[sid]["at"] = now
    return sid


def _visitor_get(sid: str) -> dict:
    with _VISITOR_LOCK:
        v = _VISITOR_KEYS.get(sid)
        if not v:
            return {}
        if _time.time() - v["at"] > VISITOR_TTL:
            _VISITOR_KEYS.pop(sid, None)
            return {}
        return {k: v[k] for k in ("muse", "claude") if v.get(k)}


def _visitor_drop(sid: str) -> None:
    with _VISITOR_LOCK:
        _VISITOR_KEYS.pop(sid, None)


def _check_muse_key(key: str) -> str:
    """One tiny Muse call with the visitor's key: "ok", "rejected" or "unreachable". The key is never logged."""
    try:
        r = meta_live.post("muse-spark-1.3", "key check", "https://api.meta.ai/v1/responses",
                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                     json={"model": "muse-spark-1.3", "reasoning": {"effort": "minimal"},
                           "max_output_tokens": 16, "instructions": "Reply with one word.", "input": "Say OK."},
                     timeout=30)
    except Exception:
        return "unreachable"
    if r.status_code == 200:
        return "ok"
    if r.status_code in (401, 402, 403):
        return "rejected"
    return "unreachable"


def _check_claude_key(key: str) -> str:
    """One tiny Claude call with the visitor's key: "ok", "rejected" or "unreachable". The key is never logged."""
    try:
        r = _rq.post("https://api.anthropic.com/v1/messages",
                     headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                     json={"model": "claude-haiku-5-5", "max_tokens": 5,
                           "messages": [{"role": "user", "content": "Say OK."}]}, timeout=30)
    except Exception:
        return "unreachable"
    if r.status_code == 200:
        return "ok"
    if r.status_code in (400, 401, 402, 403):
        return "rejected"
    return "unreachable"


def _connected_page_ids() -> set:
    """Page ids already connected to this install (empty on a fresh install)."""
    db = db_conn()
    try:
        return {r[0] for r in db.execute("SELECT page_id FROM pages").fetchall()}
    except Exception:
        return set()
    finally:
        db.close()


def _manages_connected_page(page_ids) -> bool:
    """A Facebook sign-in is accepted only if that account manages the connected page (or nothing is connected yet)."""
    connected = _connected_page_ids()
    return (not connected) or bool(connected & set(page_ids))
from urllib.parse import urlencode, parse_qs, urlparse

FB_APP_ID = B.FB_APP_ID
from html import escape as _html_escape
PAGE_TITLE = _html_escape(("Abra — " + B.CREATOR_NAME) if B.CREATOR_NAME else "Abra")
OAUTH_REDIRECT = f"http://localhost:{PORT}/oauth/callback"   # register this address in the Facebook app if ABRA_PORT changes
# NOTE: pages_messaging is intentionally omitted. It is an "invalid scope" until the
# Messenger product is added to the Facebook app (dashboard → Add Product → Messenger).
# The DM auto-reply code is built and dormant; re-add "pages_messaging," to this string
# the moment Messenger is enabled, and the /connect flow will request it again.
OAUTH_SCOPES = ("pages_show_list,pages_read_engagement,pages_read_user_content,"
                "pages_manage_engagement,pages_manage_posts,read_insights,"
                "business_management")
# Instagram scopes (instagram_basic, instagram_manage_comments,
# instagram_manage_insights) are not requested yet; they wait on the Instagram
# product + Meta App Review before they can be requested; add back once approved.
_oauth_states: set = set()

_ONB_CSS = """
:root {{ color-scheme:light; }} * {{ box-sizing:border-box; }}
body {{ margin:0; font:15px/1.6 -apple-system,'SF Pro Text',sans-serif; color:#262B36;
  display:flex; align-items:center; justify-content:center; min-height:100vh; padding:24px;
  background:
    radial-gradient(900px 620px at 50% 28%, rgba(255,255,255,.82), transparent 62%),
    linear-gradient(135deg,#EAEEF4 0%,#D0D8E4 48%,#B7C1D1 100%); background-attachment:fixed; }}
.card {{ background:linear-gradient(180deg,#FFFFFF,#EEF2F7 72%); border:1px solid #AEB8CA;
  border-radius:24px; padding:38px 42px; max-width:480px; width:100%;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.95), 0 22px 48px -16px rgba(90,102,124,.5); }}
.logo {{ height:60px; width:auto; display:block; margin:0 auto 4px;
  filter:drop-shadow(0 4px 9px rgba(90,102,124,.4)); }}
.tag {{ font-size:11px; letter-spacing:3px; text-transform:uppercase; color:#8792A4;
  font-weight:800; text-align:center; margin-bottom:18px; }}
h1 {{ font-size:22px; margin:0 0 6px; }} p {{ color:#586273; }}
.note {{ font-size:12px; color:#8792A4; margin-top:18px; }}
a.fb {{ display:inline-block; background:#1877F2; color:#fff; font-weight:700;
  padding:13px 26px; border-radius:14px; text-decoration:none; font-size:16px; margin-top:6px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.3), 0 6px 14px -4px rgba(24,119,242,.5); }}
button {{ background:linear-gradient(180deg,#8C96AA,#6E7889); color:#fff; border:0; border-radius:14px;
  padding:12px 26px; font-weight:700; font-size:15px; cursor:pointer; margin-top:14px;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.4), 0 6px 14px -5px rgba(90,102,124,.5); }}
label {{ display:flex; gap:10px; align-items:center; background:linear-gradient(180deg,#FFFFFF,#EAEEF4);
  border:1px solid #AEB8CA; border-radius:12px; padding:12px 14px; margin:8px 0; cursor:pointer;
  font-weight:600; color:#262B36; }}
a.open {{ display:inline-block; color:#4C68AE; font-weight:800; text-decoration:none; margin-top:8px; font-size:16px; }}
"""

_PEEK_CSS = """
.stage {{ position:relative; }}
.peeks {{ position:absolute; left:0; right:0; top:-76px; display:flex; justify-content:center;
  gap:24px; z-index:1; pointer-events:none; }}
.peek {{ position:relative; width:90px; height:130px; opacity:0; transform:translateY(66px);
  animation:peekUp .62s cubic-bezier(.2,1.4,.45,1) forwards; animation-delay:var(--d); }}
.peek img {{ position:absolute; bottom:0; left:50%; transform:translateX(-50%); height:130px; width:auto;
  filter:drop-shadow(0 6px 9px rgba(90,102,124,.42)); }}
.peek .pb {{ animation:pkA .52s steps(1,end) infinite; animation-delay:var(--d); }}
.peek .pw {{ opacity:0; animation:pkB .52s steps(1,end) infinite; animation-delay:var(--d); }}
@keyframes peekUp {{ to {{ opacity:1; transform:translateY(0); }} }}
@keyframes pkA {{ 0%,50%{{opacity:1}} 50.01%,100%{{opacity:0}} }}
@keyframes pkB {{ 0%,50%{{opacity:0}} 50.01%,100%{{opacity:1}} }}
.card {{ position:relative; z-index:2; }}
@media (max-width:520px) {{ .peeks {{ gap:8px; top:-62px; }} .peek {{ width:70px; height:104px; }}
  .peek img {{ height:104px; }} }}
"""

CONNECT_PAGE = """<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Join Abra</title><style>""" + _ONB_CSS + _PEEK_CSS + """</style></head>
<body><div class='stage'>
<div class='peeks'>
<div class='peek' style='--d:.30s'><img class='pb' src='/crew/cat.png' alt=''><img class='pw' src='/crew/cat_wave.png' alt=''></div>
<div class='peek' style='--d:.50s'><img class='pb' src='/crew/dog.png' alt=''><img class='pw' src='/crew/dog_wave.png' alt=''></div>
<div class='peek' style='--d:.70s'><img class='pb' src='/crew/koala.png' alt=''><img class='pw' src='/crew/koala_wave.png' alt=''></div>
</div>
<div class='card' style='text-align:center'>
<img class='logo' src='/logo.png' alt='abra'>
<div class='tag'>open and create</div>
<p>You drop a video — Sage turns it into a post, Nico answers comments in your voice, and Koa keeps the books. Your AI crew works on your page while Abra is running, so you can just film.</p>
<a class='fb' href='{oauth_url}'>Continue with Facebook</a>
<div class='note'>You approve exactly which pages to connect on Facebook's own
screen. Your tokens stay on this machine.</div>
</div></div></body></html>"""

PICKER_PAGE = """<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Choose your pages</title><style>""" + _ONB_CSS + """.card {{ max-width:520px; }}</style></head>
<body><div class='card'>
<img class='logo' src='/logo.png' alt='abra'>
<div class='tag'>open and create</div>
<h1>Welcome, {name}</h1>
<p>Which page(s) should the crew take care of?</p>
<form method='POST' action='/connect/finish'>{options}
<button type='submit'>Activate my crew</button></form>
</div></body></html>"""

CONNECT_AI = """<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Connect your AI</title><style>""" + _ONB_CSS + """
.keyfield {{ text-align:left; margin:14px 0; }}
.keyfield label {{ display:block; font-size:12.5px; font-weight:800; margin-bottom:5px; color:#262B36; }}
.keyfield .kh {{ font-size:11px; color:#8792A4; font-weight:600; margin-top:4px; }}
.keyin {{ width:100%; padding:11px 13px; border-radius:11px; border:1px solid #AEB8CA;
  background:#fff; color:#262B36; font:13px/1.4 ui-monospace,Menlo,monospace; }}
.keyin:focus {{ outline:2px solid #8C96AA; outline-offset:1px; }}
form button {{ width:100%; margin-top:10px; }}
.help {{ text-align:left; margin:8px 0 2px; border:1px solid #AEB8CA; border-radius:12px;
  background:#F4F7FB; overflow:hidden; }}
.help summary {{ cursor:pointer; padding:12px 15px; font-weight:800; font-size:13.5px; color:#4C68AE;
  list-style:none; display:flex; align-items:center; }}
.help summary::-webkit-details-marker {{ display:none; }}
.help summary::before {{ content:'?'; display:inline-flex; width:19px; height:19px; border-radius:50%;
  background:#4C68AE; color:#fff; align-items:center; justify-content:center; font-size:12px;
  font-weight:800; margin-right:9px; flex:none; }}
.help[open] summary {{ border-bottom:1px solid #DCE3EC; }}
.helpbody {{ padding:4px 16px 14px; font-size:13px; color:#586273; }}
.hstep {{ margin:13px 0; }} .hstep > b {{ color:#262B36; font-size:13.5px; }}
.helpbody ol {{ margin:6px 0 0; padding-left:20px; line-height:1.65; }}
.helpbody a {{ color:#4C68AE; font-weight:700; }}
.hnote {{ font-size:12px; color:#8792A4; font-style:italic; margin-top:12px; }}
</style></head>
<body><div class='card'>
<img class='logo' src='/logo.png' alt='abra'>
<div class='tag'>connect your AI</div>
<p style='text-align:center; margin-top:2px'>Abra runs on your own API keys. Paste them below —
they're saved only on this machine and never shown again.</p>
<details class='help'>
<summary>Not sure where to find your keys? Tap here for simple steps</summary>
<div class='helpbody'>
<div class='hstep'><b>Claude key (required)</b>
<ol>
<li>Go to <a href='https://platform.claude.com' target='_blank' rel='noopener'>platform.claude.com</a> and sign in (or create a free account).</li>
<li>On the left side, click <b>API keys</b>.</li>
<li>Click <b>Create Key</b>, give it any name, and press Create.</li>
<li>Copy the key (it starts with <b>sk-ant-</b>) and paste it in the Claude box above.</li>
</ol></div>
<div class='hstep'><b>Muse key (required)</b>
<ol>
<li>Go to <a href='https://dev.meta.ai' target='_blank' rel='noopener'>dev.meta.ai</a> and sign in with your Facebook/Meta account.</li>
<li>Open the <b>Model API</b> (or API keys) section.</li>
<li>Create a new key and copy it.</li>
<li>Paste it in the Muse box above.</li>
</ol></div>
<p class='hnote'>Tip: copy each key right after you create it — some sites only show it once.
You can always come back and update a key later from Settings.</p>
</div>
</details>
<form method='POST' action='/connect/ai'>
<div class='keyfield'><label>Claude — Anthropic</label>
<input class='keyin' type='password' name='anthropic' placeholder='sk-ant-...' autocomplete='off' autocapitalize='off' spellcheck='false'>
<div class='kh'>platform.claude.com → API keys. Powers the comment replies.</div></div>
<div class='keyfield'><label>Muse — Meta Model API</label>
<input class='keyin' type='password' name='muse' placeholder='your Meta Muse key' autocomplete='off' autocapitalize='off' spellcheck='false'>
<div class='kh'>dev.meta.ai. Powers transcription, copy, comment screening &amp; Story overlays.</div></div>
<button type='submit'>Save &amp; activate my crew</button>
</form>
<div class='note'>Keys are written to this machine's local config only , and each one is sent only to the provider it belongs to.
Leave a field blank to skip it.</div>
</div></body></html>"""

DONE_PAGE = """<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Crew activated</title><style>""" + _ONB_CSS + """</style></head>
<body><div class='card' style='text-align:center'>
<img class='logo' src='/logo.png' alt='abra'>
<div class='tag'>open and create</div>
<h1>Your crew is on the job</h1>
<p>You're all set — your crew starts now: replies, likes and bookkeeping. Manage the AI keys that power them anytime in Settings.</p>
<a class='open' href='/'>Open Abra →</a>
</div></body></html>"""

_pending_pages: dict = {}


_SMOOTH_SEM = threading.BoundedSemaphore(1)   # one smooth cut at a time (the judge host is small)
_RTC_MAX_SESSIONS = 200                        # cap on open camera-relay sessions


def _private_ip(host: str) -> bool:
    """True only for a real private or loopback address (an exact check, not a string prefix)."""
    import ipaddress as _ipa
    try:
        a = _ipa.ip_address(host)
    except ValueError:
        return False
    return a.is_private or a.is_loopback


def oauth_start_url() -> str:
    state = _secrets.token_hex(12)
    if len(_oauth_states) > 200:   # every /connect adds a state: never let the set grow without limit
        _oauth_states.clear()
    _oauth_states.add(state)
    return "https://www.facebook.com/v23.0/dialog/oauth?" + urlencode({
        "client_id": FB_APP_ID, "redirect_uri": OAUTH_REDIRECT,
        "scope": OAUTH_SCOPES, "state": state, "response_type": "code"})


def _page_capability(pg: dict) -> str:
    """What the crew can do on this page, from the viewer's tasks.
    Admin/Editor → full (reply, like, seed, stories). Moderator → engage
    (reply, like) but no posting. Analyst/Advertiser → none."""
    tasks = set(pg.get("tasks") or [])
    if not pg.get("access_token"):
        return "none"
    if tasks & {"CREATE_CONTENT", "MANAGE"}:
        return "full"
    if tasks & {"MODERATE", "MESSAGING"}:
        return "engage"
    return "none"


def _graph_all(url: str, params: dict) -> list[dict]:
    """Fetch every page of a Graph edge by following paging.next."""
    out, guard = [], 0
    while url and guard < 20:
        guard += 1
        r = _rq.get(url, params=params, timeout=30).json()
        out.extend(r.get("data", []) or [])
        url = (r.get("paging") or {}).get("next")
        params = {} # 'next' already carries everything
    return out


def _profile_db():
    db = db_conn()
    db.execute("""CREATE TABLE IF NOT EXISTS profile (
        fb_id TEXT PRIMARY KEY, name TEXT, email TEXT,
        picture_path TEXT, connected_at TEXT)""")
    return db


def _save_profile(me: dict, token: str) -> None:
    """Create/refresh the creator's Abra profile straight from their Facebook login —
    their real name and profile photo, saved the instant they connect. Keeps the
    original connect date if the profile already exists."""
    fb_id = str(me.get("id") or "")
    if not fb_id:
        return
    name = me.get("name", "") or ""
    email = me.get("email", "") or ""
    pic_path = ""
    try:
        url = (((me.get("picture") or {}).get("data")) or {}).get("url", "")
        if url:
            img = _rq.get(url, timeout=30)
            if img.ok and img.content:
                dest = BASE / "assets" / f"profile_{fb_id}.jpg"
                dest.write_bytes(img.content)
                pic_path = dest.name
    except Exception:
        pass
    db = _profile_db()
    prev = db.execute("SELECT connected_at, picture_path, email FROM profile WHERE fb_id=?",
                      (fb_id,)).fetchone()
    connected_at = prev[0] if prev else datetime.now(timezone.utc).isoformat()
    if not pic_path and prev:
        pic_path = prev[1] or ""
    if not email and prev:
        email = prev[2] or ""
    db.execute("INSERT OR REPLACE INTO profile (fb_id, name, email, picture_path, connected_at) "
               "VALUES (?,?,?,?,?)", (fb_id, name, email, pic_path, connected_at))
    db.commit(); db.close()


def _page_fallback_profile() -> dict:
    """Before a creator does the personal Facebook login, show their name + photo
    from their connected Page (we already hold that token) so the profile isn't
    blank. Upgraded to the personal FB profile the moment they log in."""
    try:
        pid = B.PRIMARY_PAGE_ID
        if not pid:
            return {}
        B.PAGE_ID, B.PAGE_TOKEN = pid, B.PRIMARY_PAGE_TOKEN
        info = B.graph_get(pid, fields="name,picture.width(256).height(256)")
        name = info.get("name", "") or ""
        url = (((info.get("picture") or {}).get("data")) or {}).get("url", "")
        pic_path = ""
        dest = BASE / "assets" / f"profile_page_{pid}.jpg"
        try:
            fresh = dest.exists() and (_time.time() - dest.stat().st_mtime < 86400)
            if url and not fresh:
                img = _rq.get(url, timeout=20)
                if img.ok and img.content:
                    dest.write_bytes(img.content)
            if dest.exists():
                pic_path = dest.name
        except Exception:
            if dest.exists():
                pic_path = dest.name
        if not name and not pic_path:
            return {}
        return {"fb_id": "", "name": name, "email": "", "picture": bool(pic_path),
                "picture_path": pic_path, "since": "", "from_page": True}
    except Exception:
        return {}


def _current_profile() -> dict:
    """The creator shown in the dashboard — their personal Facebook login if they've
    done it, otherwise derived from their connected Page."""
    try:
        db = _profile_db()
        row = db.execute("SELECT fb_id, name, email, picture_path, connected_at "
                         "FROM profile ORDER BY connected_at DESC LIMIT 1").fetchone()
        db.close()
    except Exception:
        row = None
    if row:
        return {"fb_id": row[0], "name": row[1] or "", "email": row[2] or "",
                "picture": bool(row[3]), "picture_path": row[3] or "", "since": row[4] or ""}
    return _page_fallback_profile()


def oauth_exchange(code: str):
    """code -> user token -> long-lived -> ALL the creator's FB pages (every
    source, paginated)."""
    secret = os.environ.get("FB_APP_SECRET", "")
    base = f"https://graph.facebook.com/{B.API_VERSION}"
    tok = _rq.get(f"{base}/oauth/access_token", params={
        "client_id": FB_APP_ID, "redirect_uri": OAUTH_REDIRECT,
        "client_secret": secret, "code": code}, timeout=30).json()["access_token"]
    ll = _rq.get(f"{base}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": FB_APP_ID,
        "client_secret": secret, "fb_exchange_token": tok}, timeout=30).json()["access_token"]
    me = _rq.get(f"{base}/me",
                 params={"fields": "name,picture.width(256).height(256)",
                         "access_token": ll}, timeout=30).json()

    pf = "id,name,access_token,tasks" # tasks → include admin/editor/moderator
    # IG fields re-added when instagram scopes approved
    pages = {}
    def _consider(pg):
        cap = _page_capability(pg)
        if cap != "none" and pg["id"] not in pages:
            pg["_cap"] = cap
            pages[pg["id"]] = pg
    # source 1: directly-managed pages
    for pg in _graph_all(f"{base}/me/accounts",
                         {"fields": pf, "limit": 100, "access_token": ll}):
        _consider(pg)
    # source 2: business-portfolio owned + client pages (paginated per business)
    try:
        for b in _graph_all(f"{base}/me/businesses",
                            {"fields": "id", "limit": 100, "access_token": ll}):
            for edge in ("owned_pages", "client_pages"):
                for pg in _graph_all(f"{base}/{b['id']}/{edge}",
                                     {"fields": pf, "limit": 100, "access_token": ll}):
                    _consider(pg)
    except Exception:
        pass
    if not _manages_connected_page(pages.keys()):
        raise PermissionError("this account does not manage the connected page")
    try:
        _save_profile(me, ll)   # instant Abra profile, saved only after the page-manager check
    except Exception:
        pass
    return me.get("name", "creator"), list(pages.values())


def _deals_db():
    db = db_conn()
    db.execute("""CREATE TABLE IF NOT EXISTS deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT, contact TEXT,
        stage TEXT DEFAULT 'prospect', value TEXT, notes TEXT, updated TEXT)""")
    return db


def deals_list() -> list[dict]:
    db = _deals_db()
    rows = db.execute("SELECT id, brand, contact, stage, value, notes, updated "
                      "FROM deals ORDER BY updated DESC").fetchall()
    db.close()
    keys = ["id", "brand", "contact", "stage", "value", "notes", "updated"]
    return [dict(zip(keys, r)) for r in rows]


def deal_save(d: dict) -> None:
    db = _deals_db()
    now = datetime.now(timezone.utc).isoformat()
    if d.get("id"):
        db.execute("UPDATE deals SET brand=?, contact=?, stage=?, value=?, notes=?, updated=? WHERE id=?",
                   (d.get("brand",""), d.get("contact",""), d.get("stage","prospect"),
                    d.get("value",""), d.get("notes",""), now, d["id"]))
    else:
        db.execute("INSERT INTO deals (brand, contact, stage, value, notes, updated) VALUES (?,?,?,?,?,?)",
                   (d.get("brand",""), d.get("contact",""), d.get("stage","prospect"),
                    d.get("value",""), d.get("notes",""), now))
    db.commit(); db.close()


def draft_pitch(brand: str, notes: str) -> str:
    """A brand pitch is drafted in the creator's voice. The creator reviews it and sends it themselves."""
    persona = (B.PERSONA_PATH.read_text(encoding="utf-8")[:3000] if B.PERSONA_PATH.exists() else "")
    bio = B.CREATOR_BIO or "(none given: write the pitch from the notes only, and do not invent any numbers)"
    sign_off = (f"Sign off with the name '{B.CREATOR_NAME}'." if B.CREATOR_NAME
                else "Do not add a name to the sign-off.")
    resp = B.claude.messages.create(
        model=B.MODEL, max_tokens=700,
        messages=[{"role": "user", "content":
            f"You are drafting a brand-partnership outreach email FROM {B.CREATOR_REF}, "
            f"a content creator. About the creator (use only what is written here, never invent numbers): "
            f"{bio}\n"
            f"Voice notes (how they talk):\n{persona[:1500]}\n\n"
            f"Target brand: {brand}\nAngle/notes from the creator: {notes or '(none)'}\n\n"
            f"Write a short, warm, confident pitch email (under 220 words): "
            f"hook about why this audience fits THIS brand, one proof line using real numbers "
            f"from the bio or notes only (skip the proof line if there are none), "
            f"then name the offer confidently using the offer details in the notes "
            f"(if the notes give none, offer a short call to talk options). "
            f"Close with a low-friction ask (a short call this week). "
            f"{sign_off} Confident, never desperate, no exclamation "
            f"spam. Output ONLY the email (subject first)."}])
    B.record_claude_usage(_deals_db(), "pitch", resp.usage)
    return next((b.text for b in resp.content if b.type == "text"), "").strip()


_fanlove_cache = {"at": 0.0, "data": []}


def fan_love() -> list[dict]:
    """Hottest comment threads on recent posts: fan reply-backs + likes.
    Identities are hidden from apps by Facebook, so each row deep-links to
    the comment — the creator sees names natively and can invite or respond themselves."""
    if _time.time() - _fanlove_cache["at"] < 600:
        return _fanlove_cache["data"]
    toks = _page_tokens()
    db = db_conn()
    out = []
    try:
        for p in fetch_posts("recent")[:15]:
            tok = toks.get((p.get("id") or "").split("_")[0], "")
            if not tok:
                continue
            r = _rq.get(
                f"https://graph.facebook.com/{B.API_VERSION}/{p['id']}/comments",
                params={"fields": "message,like_count,comment_count,permalink_url",
                        "filter": "toplevel", "limit": 50, "access_token": tok},
                timeout=20).json()
            for c in r.get("data", []):
                replied = db.execute(
                    "SELECT 1 FROM handled WHERE comment_id=? AND action='replied'",
                    (c["id"],)).fetchone() is not None
                fan_replies = max(0, (c.get("comment_count") or 0) - (1 if replied else 0))
                likes = c.get("like_count") or 0
                score = fan_replies * 3 + likes
                if score <= 0:
                    continue
                out.append({"comment": (c.get("message") or "")[:160],
                            "likes": likes, "fan_replies": fan_replies,
                            "replied": replied, "score": score,
                            "url": c.get("permalink_url") or "",
                            "post": (p.get("caption") or "")[:80]})
    except Exception:
        pass
    db.close()
    out.sort(key=lambda x: x["score"], reverse=True)
    _fanlove_cache["data"] = out[:25]
    _fanlove_cache["at"] = _time.time()
    return _fanlove_cache["data"]


_insights_state = {"at": 0.0, "busy": False}


def _maybe_refresh_insights() -> None:
    """Pull fresh Facebook Insights in the background (at most once an hour),
    so the Analytics tab shows today's numbers, not just the 9 AM snapshot."""
    if _insights_state["busy"] or _time.time() - _insights_state["at"] < 3600:
        return
    _insights_state["busy"] = True

    def run():
        try:
            import threading # noqa: F401 (kept for symmetry with other workers)
            import analytics
            db = db_conn()
            analytics.snapshot_insights(db)
            db.close()
            _insights_state["at"] = _time.time()
        except Exception:
            pass
        finally:
            _insights_state["busy"] = False

    import threading
    threading.Thread(target=run, daemon=True).start()


def stats() -> dict:
    out = {"service": "stopped", "replies24": 0, "likes24": 0, "skips24": 0, "repliesHour": 0}
    try:
        out["cap"] = B.reply_cap(db_conn())
    except Exception:
        out["cap"] = 30
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
        if f"{B.LAUNCHD_PREFIX}.fbcommentbot" in r.stdout:
            out["service"] = "running"
    except Exception:
        pass
    if out["service"] != "running":   # Windows and Linux have no launchd: the bot's heartbeat file tells us
        try:
            hb = BASE / ".bot_heartbeat"
            if hb.exists() and _time.time() - hb.stat().st_mtime < 900:
                out["service"] = "running"
        except Exception:
            pass
    try:
        db = db_conn()
        day = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        win = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        out["replies24"] = db.execute("SELECT COUNT(*) FROM handled WHERE action='replied' AND handled_at>?", (day,)).fetchone()[0]
        out["repliesHour"] = db.execute("SELECT COUNT(*) FROM handled WHERE action='replied' AND handled_at>?", (win,)).fetchone()[0]
        out["skips24"] = db.execute("SELECT COUNT(*) FROM handled WHERE action='skipped' AND handled_at>?", (day,)).fetchone()[0]
        out["likes24"] = db.execute("SELECT COUNT(*) FROM liked WHERE liked_at>?", (day,)).fetchone()[0]
        try:
            row = db.execute("SELECT paused_until FROM throttle WHERE id=1").fetchone()
            if row and row[0]:
                left = (datetime.fromisoformat(row[0]) - datetime.now(timezone.utc)).total_seconds()
                if left > 0:
                    out["throttleMin"] = round(left / 60)
        except Exception:
            pass
        db.close()
    except Exception:
        pass
    try: # recent Facebook pushback, from the log tail — the flag early-warning
        lines = _tail_lines(BASE / "bot.log")
        out["fbTrips"] = sum(1 for l in lines if "THROTTLE tripped" in l)
        out["fbErrors"] = sum(1 for l in lines if "Facebook error" in l or "Like failed" in l)
    except Exception:
        pass
    try: # Koa's standing watch: earnings collapse = possible monetization strike
        db = db_conn()
        rows = db.execute(
            "SELECT value FROM page_insights WHERE metric="
            "'monetization_approximate_earnings' AND page_id=? "
            "ORDER BY taken_at DESC LIMIT 8", (B.PRIMARY_PAGE_ID,)).fetchall()
        db.close()
        if len(rows) >= 4:
            latest = rows[0][0] or 0
            prior = [r[0] or 0 for r in rows[1:]]
            avg = sum(prior) / len(prior)
            if avg > 15 and latest < avg * 0.1:
                out["monetizationWarn"] = True
    except Exception:
        pass
    return out


PAGE = """<!doctype html><html><head><meta charset="utf-8"><link rel="icon" href="/logo.png">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PAGE_TITLE__</title>
<style>
  :root {
    /* liquid chrome — bright brushed-metal palette */
    color-scheme: light;
    --bg:#E6ECF4; --card:#F8FAFD; --card2:#EDF1F8; --border:#CCD5E2;
    --ink:#262B36; --ink1:#1A1F28; --ink2:#586273; --ink3:#8792A4; --kpinum:#1E85FF;
    --accent:#8C96AA; --accent-soft:#E6EAF2;
    --reply:#4C68AE; --reply-bg:#DEE7F8; --like:#AE6690; --like-bg:#F3E3ED;
    --skip:#8792A4; --skip-bg:#E6EAF1;
    --good:#3E9E66; --warn:#BE962E; --bad:#D5564E; --r:20px;
    --mercury:linear-gradient(135deg,#FBFCFE 0%,#CAD3E1 40%,#9BA7B9 70%,#6C7789 100%);
    --chrome:linear-gradient(135deg,#FFFFFF,#D6DCE7 45%,#9BA5B6 100%);
    /* the real sidebar chrome texture — shared by the desktop rail + mobile bars */
    --chrometex:
      linear-gradient(110deg, rgba(255,255,255,.34) 0%, rgba(255,255,255,0) 22%, rgba(255,255,255,.30) 48%, rgba(255,255,255,0) 70%, rgba(255,255,255,.22) 100%),
      linear-gradient(180deg, rgba(249,251,254,.46), rgba(238,242,249,.36) 50%, rgba(249,251,254,.46)),
      url('/sidebar.png') center/cover no-repeat;
  }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.55 -apple-system,"SF Pro Text","Segoe UI",sans-serif;
         background:#D4DBE6; color:var(--ink);
         background-image:
           radial-gradient(1200px 700px at 78% -8%, rgba(255,255,255,.75), transparent 60%),
           radial-gradient(900px 600px at 12% 108%, rgba(175,190,214,.55), transparent 55%),
           linear-gradient(135deg,#E9EDF3 0%,#D2DAE5 45%,#BAC4D4 100%);
         background-attachment:fixed; padding:26px; }
  /* floating liquid-metal shell (V18 layout) */
  .app { display:flex; height:calc(100vh - 52px); max-width:1260px; margin:0 auto; position:relative;
    border-radius:28px; overflow:hidden; background:var(--card); border:1px solid var(--border);
    box-shadow:var(--rim), var(--e4); }
  /* desktop-only hackathon sticker, pinned to the app's top-right corner */
  .hackstick { display:none; }
  @media (min-width:861px){
    .hackstick { display:inline-flex; position:absolute; top:16px; right:18px; z-index:70; pointer-events:none;
      align-items:center; gap:8px; padding:6px 13px 6px 10px; border-radius:999px; white-space:nowrap;
      font-size:8.5px; letter-spacing:1.1px; text-transform:uppercase; font-weight:700; color:#EAEEF4;
      background:linear-gradient(180deg,#20242D,#0E1116); border:1px solid #2B313C;
      box-shadow:inset 0 1px 0 rgba(255,255,255,.08), 0 7px 18px -9px rgba(10,14,25,.6); }
    .hackstick svg { width:17px; height:9px; flex:none; fill:none; stroke:#0866FF;
      stroke-width:3.6; stroke-linecap:round; stroke-linejoin:round; }
  }

  /* ---------- launch splash ---------- */
  .splash { position:fixed; inset:0; z-index:9999; display:flex; align-items:center; justify-content:center; overflow:hidden;
    background:
      radial-gradient(680px 460px at 50% 40%, rgba(8,102,255,.20), transparent 60%),
      radial-gradient(900px 720px at 50% 122%, rgba(56,182,255,.11), transparent 58%),
      linear-gradient(160deg,#0b0f18 0%,#0e1422 52%,#070a12 100%);
    animation:splashOut .6s ease 2.7s forwards; }
  /* slow-rotating blue aurora behind everything */
  .splash::before { content:""; position:absolute; width:940px; height:940px; left:50%; top:44%;
    transform:translate(-50%,-50%); border-radius:50%; pointer-events:none; opacity:.55;
    background:conic-gradient(from 0deg, transparent 0deg, rgba(8,102,255,.22) 60deg, transparent 150deg, transparent 230deg, rgba(56,182,255,.17) 300deg, transparent 360deg);
    filter:blur(46px); animation:splashSpin 11s linear infinite; }
  .splash-inner { position:relative; display:flex; flex-direction:column; align-items:center; gap:22px;
    animation:splashIn .8s cubic-bezier(.2,.8,.3,1) both; }
  .splash-logowrap { position:relative; }
  .splash-logo { display:block; height:112px; width:auto;
    filter:drop-shadow(0 0 26px rgba(150,180,230,.22)) drop-shadow(0 10px 22px rgba(0,0,0,.55)); }
  /* a bright shine sweeps across the chrome wordmark — masked to the letters so it
     never paints a box over the logo's transparent background */
  .splash-logowrap::after { content:""; position:absolute; inset:0; pointer-events:none;
    background:linear-gradient(100deg, transparent 38%, rgba(255,255,255,.92) 50%, transparent 62%);
    background-size:280% 100%; background-position:120% 0;
    -webkit-mask:url(/logo.png) center/contain no-repeat; mask:url(/logo.png) center/contain no-repeat;
    animation:splashShine 1.9s ease-in-out .3s infinite; }
  .splash-tag { font-size:11.5px; letter-spacing:4px; text-transform:uppercase; color:rgba(210,222,240,.55); font-weight:800; }
  .splash-vp { font-size:15px; font-weight:600; color:rgba(223,233,248,.88); margin-top:13px; max-width:330px; line-height:1.45; text-align:center; }
  /* hackathon hero: a Meta-blue infinity that draws itself + glows, with the event name */
  .splash-hack { display:flex; flex-direction:column; align-items:center; gap:11px; margin-top:4px;
    animation:badgeIn .7s ease .5s both; }
  .splash-inf { width:122px; height:auto; overflow:visible;
    filter:drop-shadow(0 0 9px rgba(8,102,255,.55)); animation:infGlow 2.6s ease-in-out 2s infinite; }
  .splash-inf path { fill:none; stroke:url(#ig); stroke-width:5.5; stroke-linecap:round; stroke-linejoin:round;
    stroke-dasharray:640; stroke-dashoffset:640; animation:infDraw 1.5s cubic-bezier(.4,.1,.2,1) .4s forwards; }
  .splash-hackname { font-size:16px; font-weight:800; letter-spacing:.3px; color:#F2F6FC; text-align:center;
    text-shadow:0 2px 10px rgba(0,0,0,.4); }
  .splash-hacksub { font-size:9.5px; letter-spacing:2.4px; text-transform:uppercase; font-weight:700; color:#5AA8FF; }
  .splash-bar { width:180px; height:4px; border-radius:99px; background:rgba(120,150,210,.16); overflow:hidden; margin-top:6px; }
  .splash-bar i { display:block; height:100%; width:42%; border-radius:99px;
    background:linear-gradient(90deg,rgba(8,102,255,.2),#4FBEFF,rgba(8,102,255,.2));
    animation:splashBar 1.3s ease-in-out infinite; }
  @keyframes splashIn { from{opacity:0; transform:translateY(12px) scale(.96)} to{opacity:1; transform:none} }
  @keyframes splashOut { to{opacity:0; visibility:hidden} }
  /* ---- login gate ---- */
  .logingate { position:fixed; inset:0; z-index:9500; display:none; align-items:center; justify-content:center;
    background:radial-gradient(1100px 760px at 50% 32%, rgba(42,74,136,.5), transparent 62%), linear-gradient(160deg,#141c2e 0%,#0a0f1a 72%); }
  .logingate.show { display:flex; animation:lgFade .55s ease; }
  @keyframes lgFade { from{opacity:0} to{opacity:1} }
  .lg-card { position:relative; display:flex; flex-direction:column; align-items:center; gap:14px; padding:44px 36px; text-align:center;
    animation:lgRise .6s cubic-bezier(.2,.8,.2,1) both; }
  @keyframes lgRise { from{opacity:0; transform:translateY(16px)} to{opacity:1; transform:none} }
  .lg-logo { height:74px; width:auto; filter:drop-shadow(0 6px 22px rgba(120,170,255,.42)); }
  .lg-tag { font-size:11px; letter-spacing:4.5px; text-transform:uppercase; color:rgba(200,216,240,.5); font-weight:800; margin-top:-2px; }
  .lg-vp { font-size:14.5px; font-weight:600; color:rgba(223,233,248,.86); margin-top:14px; max-width:310px; line-height:1.5; text-align:center; }
  .lg-continue { display:flex; align-items:center; gap:12px; margin-top:20px; background:linear-gradient(180deg,#1E85FF,#0866FF);
    color:#fff; border:0; border-radius:15px; padding:14px 24px; font-size:15.5px; font-weight:700; cursor:pointer;
    box-shadow:0 10px 30px -6px rgba(8,102,255,.55), inset 0 1px 0 rgba(255,255,255,.3); transition:transform .14s ease, box-shadow .14s ease; }
  .lg-continue:hover { transform:translateY(-2px); box-shadow:0 16px 40px -6px rgba(8,102,255,.65), inset 0 1px 0 rgba(255,255,255,.3); }
  .lg-continue .lg-av { width:30px; height:30px; border-radius:50%; object-fit:cover; border:2px solid rgba(255,255,255,.4); }
  .lg-continue b { font-weight:800; }
  .lg-remember { display:flex; align-items:center; gap:8px; color:rgba(200,216,240,.74); font-size:13px; cursor:pointer; margin-top:6px; }
  .lg-remember input { width:15px; height:15px; accent-color:#0866FF; cursor:pointer; }
  .lg-alt { color:#5AA8FF; font-size:12.5px; text-decoration:none; margin-top:2px; }
  .lg-alt:hover { text-decoration:underline; }
  .lg-foot { color:rgba(210,222,240,.5); font-size:11px; font-style:italic; letter-spacing:.2px; margin-top:16px; max-width:300px; line-height:1.55; }
  .lg-foot b { font-style:normal; color:rgba(200,216,240,.42); font-weight:700; }
  .lg-models { display:flex; flex-wrap:wrap; align-items:center; justify-content:center; gap:6px; margin-top:22px; max-width:320px; }
  .lg-models-label { width:100%; font-size:9px; letter-spacing:2.2px; text-transform:uppercase; color:rgba(200,216,240,.38); font-weight:800; margin-bottom:4px; }
  .lg-chip { font-size:11px; font-weight:700; color:rgba(214,226,246,.84); background:rgba(120,150,210,.14); border:1px solid rgba(150,180,240,.22); border-radius:999px; padding:3px 10px; }
  /* the crew peek up over the gate and wave hello */
  .lg-crew { position:absolute; top:-58px; left:0; right:0; display:flex; justify-content:center; align-items:flex-end; gap:10px; pointer-events:none; z-index:1; }
  .lg-peek { position:relative; width:90px; height:92px; opacity:0; transform:translateY(42px); animation:lgPeekUp .72s cubic-bezier(.2,.85,.3,1) both; animation-delay:var(--d); }
  .lg-peek img { position:absolute; left:50%; bottom:0; height:82px; width:auto; transform:translateX(-50%); filter:drop-shadow(0 9px 16px rgba(0,0,0,.55)); }
  .lg-peek .pb { animation:lgBob 3s ease-in-out infinite, lgWaveA 3.4s steps(1,end) infinite; animation-delay:var(--d); }
  .lg-peek .pw { opacity:0; animation:lgBob 3s ease-in-out infinite, lgWaveB 3.4s steps(1,end) infinite; animation-delay:var(--d); }
  @keyframes lgPeekUp { to { opacity:1; transform:translateY(0); } }
  @keyframes lgBob { 0%,100%{ transform:translateX(-50%) translateY(0); } 50%{ transform:translateX(-50%) translateY(-5px); } }
  @keyframes lgWaveA { 0%,80%{opacity:1} 80.01%,100%{opacity:0} }
  @keyframes lgWaveB { 0%,80%{opacity:0} 80.01%,100%{opacity:1} }
  @keyframes splashShine { 0%{background-position:120% 0} 55%,100%{background-position:-40% 0} }
  @keyframes splashBar { 0%{transform:translateX(-130%)} 100%{transform:translateX(330%)} }
  @keyframes badgeIn { from{opacity:0; transform:translateY(8px)} to{opacity:1; transform:none} }
  @keyframes splashSpin { to{ transform:translate(-50%,-50%) rotate(360deg); } }
  @keyframes infDraw { to{ stroke-dashoffset:0; } }
  @keyframes infGlow { 0%,100%{filter:drop-shadow(0 0 7px rgba(8,102,255,.5))} 50%{filter:drop-shadow(0 0 17px rgba(79,190,255,.92))} }

  /* ---------- sidebar ---------- */
  /* slim icon rail (V18) */
  aside { width:96px; flex:none; border-right:1px solid var(--border); padding:16px 8px;
          display:flex; flex-direction:column; align-items:center; gap:3px; overflow-y:auto; }
  .brandmark { display:flex; align-items:center; justify-content:center; margin:4px 0 16px; flex:none; }
  .brandmark img { width:72px; height:auto; filter:drop-shadow(0 2px 3px rgba(90,102,124,.38)); }
  /* mobile-only top strip (hidden on desktop; the sidebar carries the brand there) */
  .mtopbar { display:none; }
  /* mobile-only center-orb slot in the bottom tab bar (hidden on the desktop rail) */
  .navorbslot { display:none; }
  .navitem { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px;
             width:76px; flex:none; padding:9px 4px 7px; border-radius:15px; color:var(--ink3);
             font-weight:650; font-size:9.5px; letter-spacing:.2px; cursor:pointer;
             border:1px solid transparent; background:transparent;
             transition:background-color .14s, color .14s, box-shadow .14s; }
  .navitem .ni { width:23px; height:23px; fill:none; stroke:currentColor; stroke-width:1.9;
             stroke-linecap:round; stroke-linejoin:round; }
  .navitem span { white-space:nowrap; }
  .navitem:hover { color:var(--ink2); background:var(--card); border-color:var(--border); box-shadow:var(--rim); }
  .navitem.active { color:var(--accent-deep); background:linear-gradient(180deg,#FFFFFF,var(--accent-soft) 85%);
             border-color:var(--border);
             box-shadow:var(--rim), 0 2px 0 var(--ledge), 0 7px 13px -7px rgba(var(--sh),.5); }
  .navdiv { width:46px; height:1px; background:var(--border); opacity:.65; margin:5px 0; flex:none; }
  /* grouped-nav section labels (desktop rail only; hidden on the mobile tab bar) */
  .navgrp { width:100%; text-align:center; font-size:8.5px; font-weight:800; letter-spacing:.08em;
    text-transform:uppercase; color:var(--ink3); opacity:.7; margin:9px 0 1px; flex:none; user-select:none; }
  .sidefoot { margin-top:auto; display:flex; flex-direction:column; align-items:center; gap:7px;
          padding-top:10px; width:100%; }
  #engBtn { border:0; border-radius:12px; padding:9px 6px; font-size:9px; font-weight:750; line-height:1.2;
            cursor:pointer; width:80px; text-align:center; }
  .pill { display:flex; align-items:center; justify-content:center; gap:5px; padding:6px 6px; border-radius:11px;
          background:var(--card); border:1px solid var(--border); font-size:9px;
          font-weight:650; color:var(--ink2); width:80px; text-align:center; line-height:1.15; }
  .pill .dot { width:8px; height:8px; border-radius:50%; }
  .pill.on .dot { background:var(--good); box-shadow:0 0 8px #5FA86B66; }
  .pill.off .dot { background:var(--bad); }

  /* ---------- content ---------- */
  .content { flex:1; min-width:0; padding:26px 32px 48px; overflow-y:auto; background:var(--face), var(--card); perspective:1600px; }
  .view { display:none; } .view.active { display:block; }
  /* V18 motion — cinematic 3D staggered entrance on view switch + gentle crew float */
  @keyframes riseIn { from { opacity:0; transform:perspective(1300px) translateZ(-55px) translateY(20px) rotateX(5deg); } to { opacity:1; transform:none; } }
  .view.active > h1, .view.active > .vsub, .view.active > p, .view.active > section {
    animation:riseIn .5s cubic-bezier(.2,.75,.3,1) both; }
  .view.active > section:nth-of-type(2) { animation-delay:.06s; }
  .view.active > section:nth-of-type(3) { animation-delay:.12s; }
  .view.active > section:nth-of-type(4) { animation-delay:.18s; }
  .view.active > section:nth-of-type(5) { animation-delay:.24s; }
  .view.active > section:nth-of-type(n+6) { animation-delay:.3s; }
  .view.active .crewcard { animation:riseIn .55s cubic-bezier(.2,.75,.3,1) both; }
  .view.active .crewcard:nth-child(2) { animation-delay:.08s; }
  .view.active .crewcard:nth-child(3) { animation-delay:.16s; }
  .view.active .crewcard:nth-child(4) { animation-delay:.24s; }
  @keyframes crewFloat { 0%,100% { transform:translateY(0) rotate(0deg); } 50% { transform:translateY(-7px) rotate(-1.2deg); } }
  .crewcard .crewimg { animation:crewFloat 4.2s ease-in-out infinite; }
  .crewcard:nth-child(2) .crewimg { animation:crewFloat 4.9s ease-in-out infinite; animation-delay:-1.1s; }
  .crewcard:nth-child(3) .crewimg { animation:crewFloat 3.9s ease-in-out infinite; animation-delay:-2.2s; }
  .crewcard:nth-child(4) .crewimg { animation:crewFloat 4.5s ease-in-out infinite; animation-delay:-3.1s; }
  @media (prefers-reduced-motion: reduce) {
    .view.active > *, .view.active .crewcard, .crewcard .crewimg { animation:none !important; } }
  h1.vtitle { margin:0 0 4px; font-size:20px; font-weight:750; letter-spacing:-.3px; }
  .vsub { color:var(--ink3); font-size:13px; margin-bottom:18px; }
  section { background:var(--card); border:1px solid var(--border); border-radius:var(--r);
            padding:18px; margin-top:16px; }
  section > h2 { margin:0 0 12px; font-size:14.5px; font-weight:700; display:flex;
                 align-items:center; gap:8px; flex-wrap:wrap; }
  .hint { font-weight:500; color:var(--ink3); font-size:12px; }
  .tiles { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }
  .tile { background:var(--card); border:1px solid var(--border); border-radius:var(--r);
          padding:14px 16px 12px; }
  .tile .lab { font-size:11px; font-weight:700; letter-spacing:.8px; text-transform:uppercase; color:var(--ink3); }
  .tile .val { font-size:27px; font-weight:750; letter-spacing:-.5px; margin-top:2px;
               font-variant-numeric:tabular-nums; }
  .tile .cap { font-size:11.5px; color:var(--ink3); margin-top:1px; }
  .meter { height:5px; border-radius:99px; background:var(--card2); margin-top:9px; overflow:hidden; }
  .meter i { display:block; height:100%; border-radius:99px; background:var(--accent); transition:width .6s; }
  .banner { display:none; margin:12px 0 0; padding:11px 16px; border-radius:12px;
            background:#2A2410; border:1px solid #4A3F18; color:var(--warn); font-weight:600; font-size:13px; }
  .slot { border-radius:12px; padding:11px 14px; background:var(--card2); border:1px solid var(--border); }
  .slot .sl { font-size:12px; font-weight:700; } .slot .si { font-size:11.5px; color:var(--ink3); margin-top:3px; }
  .slot.done { border-color:#9CC6A2; background:#E4F3E8; } .slot.done .sl { color:#2E7D4E; }
  .slot.missed { border-color:#E2A89E; background:#FBE8E5; } .slot.missed .sl { color:#B5453C; }
  .slot.upcoming .sl { color:var(--ink2); }
  input[type=text], select { background:var(--card2); color:var(--ink); border:1px solid var(--border);
          border-radius:8px; padding:7px 11px; font-size:12.5px; }
  /* neumorphic recessed form fields */
  input[type=text], input[type=email], input[type=search], input[type=month], select, textarea, .sc-in {
    box-shadow:var(--inset); border-color:var(--border); border-radius:10px; }
  input[type=text]:focus, input[type=email]:focus, select:focus, textarea:focus, .sc-in:focus {
    outline:none; box-shadow:var(--inset), 0 0 0 2px var(--accent-soft); }
  input[type=file] { font-size:11.5px; color:var(--ink2); }
  .btn { background:var(--accent); background:linear-gradient(180deg, color-mix(in srgb,var(--accent) 86%,#fff), var(--accent)); color:white; border:0; border-radius:11px; padding:9px 20px;
         font-size:13px; font-weight:700; cursor:pointer; box-shadow:0 5px 14px -3px rgba(0,0,0,.4), inset 0 1px 0 rgba(255,255,255,.26); transition:transform .13s ease, box-shadow .13s ease, filter .13s ease; }
  .btn:hover { transform:translateY(-1px); filter:brightness(1.07); box-shadow:0 9px 20px -4px rgba(0,0,0,.46), inset 0 1px 0 rgba(255,255,255,.3); }
  .btn:active { transform:translateY(0); box-shadow:inset 0 2px 7px rgba(0,0,0,.32); }
  .btn.green { background:var(--good); background:linear-gradient(180deg, color-mix(in srgb,var(--good) 86%,#fff), var(--good)); color:#FFFFFF; font-weight:800; }
  .btn.ghost { background:var(--card); color:var(--ink2); border:1px solid var(--border); box-shadow:var(--rim), var(--e1); }
  .row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
  .spotrow { display:flex; gap:9px; align-items:center; margin:2px 0; }
  .spotrow input { flex:1; min-width:170px; }
  .spotcard { margin-top:16px; }
  .spothead { font-size:11px; font-weight:800; letter-spacing:1.6px; text-transform:uppercase; color:var(--accent-deep); margin-bottom:9px; }
  .spotpulse { font-size:14px; line-height:1.62; color:var(--ink); background:var(--card2); border-radius:var(--r); padding:15px 17px; box-shadow:var(--inset); margin-bottom:15px; }
  .spotideas { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; margin-bottom:15px; }
  .spotidea { background:var(--card2); border-radius:var(--r); padding:15px; box-shadow:var(--rim), var(--e1); display:flex; flex-direction:column; }
  .si-hook { font-weight:700; font-size:14px; color:var(--ink); line-height:1.35; }
  .si-why { font-size:12.5px; color:var(--ink2); margin-top:6px; line-height:1.48; flex:1; }
  .si-make { margin-top:11px; align-self:flex-start; padding:6px 13px; font-size:12px; }
  .spotfb { display:inline-flex; }
  .adpill { display:inline-block; font-size:10px; font-weight:800; letter-spacing:.05em; text-transform:uppercase; color:var(--accent-deep); background:var(--accent-soft); border-radius:999px; padding:2px 8px; margin-left:6px; }
  .adbtn { border:1px solid var(--border); background:var(--card2); color:var(--ink2); border-radius:999px; padding:2px 9px; font:700 11px/1.4 -apple-system,sans-serif; cursor:pointer; margin-left:6px; box-shadow:var(--rim), var(--e1); }
  .adbtn:hover { color:var(--ink); }
  .thumbopts { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .thumbopt { cursor:pointer; display:flex; flex-direction:column; align-items:center; gap:4px; }
  .thumbopt img { width:72px; height:112px; object-fit:cover; border-radius:10px; box-shadow:var(--rim), var(--e1); border:2px solid transparent; }
  .thumbopt input:checked + img { border-color:var(--accent); }
  .adsnote { font-size:12px; color:var(--ink3); margin:0 0 12px; line-height:1.55; }
  .adrow { display:flex; align-items:center; gap:14px; flex-wrap:wrap; background:var(--card2); border-radius:14px; padding:12px 14px; box-shadow:var(--rim), var(--e1); margin-bottom:10px; }
  .adrow-main { flex:1 1 220px; min-width:0; }
  .adrow-t { font-weight:700; font-size:13px; color:var(--ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .adrow-m { font-size:11px; color:var(--ink3); margin-top:2px; }
  .adrow-s { display:flex; gap:12px; font-size:11.5px; color:var(--ink3); flex-wrap:wrap; }
  .adrow-s b { color:var(--ink); }
  .spottrends { display:flex; flex-wrap:wrap; align-items:center; gap:7px; margin:4px 0 14px; }
  .spottl { font-size:10px; letter-spacing:.05em; text-transform:uppercase; font-weight:700; color:var(--ink3); margin-right:2px; }
  .spottrend { font-size:11.5px; font-weight:600; color:var(--accent-deep); background:var(--accent-soft); border:1px solid var(--border); border-radius:999px; padding:4px 11px; box-shadow:var(--rim); }
  .exp-range { display:flex; gap:6px; margin:4px 0 14px; }
  .exp-rb { background:var(--card2); color:var(--ink3); border:none; border-radius:999px; padding:6px 14px; font-size:12px; font-weight:600; cursor:pointer; box-shadow:var(--rim); }
  .exp-rb.active { background:var(--accent); color:#fff; box-shadow:var(--rim), var(--e1); }
  .exp-cards { display:flex; align-items:stretch; gap:10px; flex-wrap:wrap; }
  .exp-card { flex:1; min-width:120px; background:var(--card2); border-radius:var(--r); padding:16px 14px; box-shadow:var(--rim), var(--e1); text-align:center; }
  .exp-card.net { background:linear-gradient(145deg, var(--card2), var(--card)); }
  .exp-lbl { font-size:10.5px; letter-spacing:.06em; text-transform:uppercase; color:var(--ink3); font-weight:700; }
  .exp-val { font-size:26px; font-weight:800; margin-top:6px; letter-spacing:-.02em; color:var(--ink); }
  .exp-val.good { color:var(--good); }
  .exp-val.bad { color:var(--bad); }
  .exp-sub { font-size:11px; color:var(--ink3); margin-top:3px; }
  .exp-op { align-self:center; font-size:22px; color:var(--ink3); font-weight:400; }
  .exp-bd { margin-top:16px; }
  .exp-bd-h { font-size:12px; font-weight:700; color:var(--ink2); margin-bottom:10px; }
  .exp-bd-empty { font-size:12.5px; color:var(--ink3); background:var(--card2); border-radius:var(--r); padding:14px; box-shadow:var(--rim); }
  .exp-row { display:flex; align-items:center; gap:10px; padding:7px 0; }
  .exp-rl { flex:1.4; font-size:12.5px; color:var(--ink2); display:flex; flex-direction:column; }
  .exp-rc { font-size:10.5px; color:var(--ink3); margin-top:1px; }
  .exp-bar { flex:2; height:8px; background:var(--card2); border-radius:999px; overflow:hidden; box-shadow:var(--inset); }
  .exp-bar span { display:block; height:100%; background:linear-gradient(90deg, var(--accent), var(--good)); border-radius:999px; }
  .exp-rv { flex:none; width:64px; text-align:right; font-size:12.5px; font-weight:700; color:var(--ink); }
  .exp-foot { font-size:11px; color:var(--ink3); margin-top:14px; }
  .exp-foot a { color:var(--accent); text-decoration:none; }
  .bt-head { display:flex; align-items:center; gap:10px; font-size:13.5px; color:var(--ink2); font-weight:600; margin:2px 0 14px; }
  .bt-conf { font-size:10px; letter-spacing:.05em; text-transform:uppercase; font-weight:700; padding:2px 8px; border-radius:999px; background:var(--card2); color:var(--ink3); box-shadow:var(--rim); }
  .bt-conf.high { color:var(--good); } .bt-conf.medium { color:var(--warn); } .bt-conf.low { color:var(--ink3); }
  .bt-gridwrap { overflow-x:auto; }
  .bt-grid { display:grid; grid-template-columns:auto repeat(7, minmax(34px,1fr)); gap:5px; min-width:420px; }
  .bt-cell { border-radius:9px; min-height:34px; display:flex; align-items:center; justify-content:center; font-size:11px; }
  .bt-corner { background:transparent; }
  .bt-dh { color:var(--ink3); font-weight:700; font-size:11px; min-height:20px; }
  .bt-bh { flex-direction:column; align-items:flex-start; justify-content:center; padding:0 8px 0 2px; color:var(--ink2); line-height:1.15; }
  .bt-bh b { font-size:11.5px; font-weight:700; } .bt-bh span { font-size:9.5px; color:var(--ink3); }
  .bt-c { background:var(--card2); box-shadow:var(--inset); color:#fff; font-size:12px; cursor:default; transition:transform .1s ease; }
  .bt-c:hover { transform:scale(1.07); }
  .bt-empty { box-shadow:none; background:var(--card2); opacity:.35; }
  .bt-best { box-shadow:0 0 0 2px var(--good), var(--e1); color:var(--good); font-size:13px; }
  .bt-legend { display:flex; align-items:center; gap:8px; margin-top:12px; font-size:10.5px; color:var(--ink3); }
  .bt-ramp { height:8px; width:120px; border-radius:999px; background:linear-gradient(90deg, rgba(94,144,230,0.1), rgba(94,144,230,0.9)); }
  .bt-foot { font-size:11px; color:var(--ink3); margin-top:10px; }
  @media (max-width:560px){ .exp-op { display:none; } .exp-card { min-width:40%; } }
  .homegrid { display:grid; grid-template-columns:1fr 1fr; gap:15px; margin-top:4px; }
  @media (max-width:720px){ .homegrid { grid-template-columns:1fr; } }
  .hg-col { background:var(--card2); border-radius:var(--r); padding:15px 16px; box-shadow:var(--inset); min-width:0; }
  .hg-title { font-size:11px; font-weight:800; letter-spacing:.6px; text-transform:uppercase; color:var(--ink3); margin-bottom:11px; }
  .hg-title .hint { text-transform:none; letter-spacing:0; font-weight:500; margin-left:5px; }
  .homecal { display:flex; flex-direction:column; }
  .cal-day { display:flex; gap:10px; padding:8px 0; border-bottom:1px solid var(--border); }
  .cal-day:last-child { border-bottom:0; padding-bottom:0; }
  .cal-day.today .cal-dl { color:var(--accent-deep); font-weight:800; }
  .cal-dl { flex:0 0 94px; font-size:12.5px; font-weight:650; color:var(--ink2); padding-top:1px; }
  .cal-de { flex:1; min-width:0; display:flex; flex-direction:column; gap:6px; }
  .cal-none { font-size:12px; color:var(--ink3); opacity:.5; }
  .cal-ev { display:flex; align-items:center; gap:8px; font-size:12.5px; }
  .cal-ev.done { opacity:.62; }
  .cal-dot { flex:none; width:7px; height:7px; border-radius:50%; background:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  .cal-ev.done .cal-dot { background:var(--good); box-shadow:0 0 0 3px rgba(79,192,126,.16); }
  .cal-evt { flex:1; min-width:0; color:var(--ink); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .cal-evtm { flex:none; font-size:11.5px; color:var(--ink3); font-weight:600; }
  .tk-head { font-size:10px; font-weight:800; letter-spacing:1px; text-transform:uppercase; color:var(--ink3); margin:12px 0 7px; }
  .tk-head:first-child { margin-top:0; }
  .cl-item.content { display:flex; align-items:center; gap:9px; cursor:default; padding:6px 2px; }
  .cl-item.content .cl-ci { flex:none; width:7px; height:7px; border-radius:2px; background:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  .cl-item.content.done .cl-ci { background:var(--good); box-shadow:0 0 0 3px rgba(79,192,126,.16); }
  .cl-item.content .cl-text { flex:1; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .cl-item.content.done .cl-text { color:var(--ink2); }
  .cl-when { flex:none; font-size:11px; color:var(--ink3); font-weight:600; }
  #view-home section.hcollap > h2, #view-home section.hcollap > .hs-head { cursor:pointer; user-select:none; }
  .hcollap .hcx { margin-left:auto; padding-left:12px; transition:transform .22s ease; color:var(--ink3); font-size:12px; align-self:center; flex:none; }
  .hcollap.collapsed .hcx { transform:rotate(-90deg); }
  .hcollap.collapsed > *:not(h2):not(.hs-head) { display:none !important; }
  .pagelist { display:flex; flex-direction:column; gap:8px; }
  .pagerow { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;
    padding:11px 14px; border:1px solid var(--border); border-radius:12px; background:var(--card2); }
  .pagerow.off { opacity:.6; }
  .pagename { display:flex; align-items:center; gap:9px; font-size:14px; font-weight:700; color:var(--ink); cursor:pointer; }
  .pagepub { display:flex; align-items:center; gap:8px; }
  .pagepub-l { font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:.05em; color:var(--ink3); }
  .pagepub select { background:var(--card); color:var(--ink); border:1px solid var(--border);
    border-radius:9px; padding:6px 10px; font-size:12.5px; font-weight:600; cursor:pointer; }
  .tabs { display:flex; background:var(--card2); border:1px solid var(--border); padding:4px;
          border-radius:12px; width:fit-content; }
  .tab { border:0; background:transparent; color:var(--ink2); border-radius:9px; padding:7px 16px;
         font-size:13px; font-weight:600; cursor:pointer; }
  .tab.active { background:var(--accent); color:white; }
  /* page filter pills (top of Boost Videos) */
  .pagefilter { display:flex; flex-wrap:wrap; gap:7px; margin-bottom:12px; }
  .pagefilter:empty { display:none; }
  .pgfilt { border:1px solid var(--border); background:var(--card2); color:var(--ink2); border-radius:99px;
            padding:6px 14px; font-size:12px; font-weight:650; cursor:pointer; transition:background-color .14s,color .14s,box-shadow .14s;
            box-shadow:var(--rim); }
  .pgfilt:hover { color:var(--ink); background:var(--card); }
  .pgfilt.active { color:var(--accent-deep); background:linear-gradient(180deg,#FFFFFF,var(--accent-soft) 85%);
            box-shadow:var(--rim), 0 2px 0 var(--ledge), 0 6px 11px -6px rgba(var(--sh),.45); }
  /* V18 video cards */
  .video { display:flex; flex-direction:column; padding:0; margin:0; border-radius:var(--r); overflow:hidden;
           position:relative; background:var(--face),var(--card); border:1px solid var(--border);
           cursor:pointer; box-shadow:var(--rim), var(--e2); transition:transform .18s, box-shadow .18s; }
  .video:hover { transform:translateY(-3px); box-shadow:var(--rim), var(--e3); }
  .video.t { border-color:var(--accent-deep); box-shadow:var(--rim), 0 0 0 2px rgba(120,132,156,.35), var(--e2); }
  .vbanner { position:relative; height:134px; background:var(--card2); overflow:hidden; }
  .video img.th, .video .noth { width:100%; height:134px; object-fit:cover; display:block; background:var(--card2); }
  .video .noth { display:flex; align-items:center; justify-content:center; color:var(--ink3); font-size:22px; }
  .video input { appearance:none; position:absolute; top:9px; right:9px; z-index:3; width:26px; height:26px;
                 border-radius:9px; border:1px solid var(--border); background:rgba(255,255,255,.82);
                 cursor:pointer; display:grid; place-content:center; box-shadow:0 2px 6px -2px rgba(var(--sh),.5); }
  .video input:checked { background:var(--accent-deep); border-color:var(--accent-deep); }
  .video input:checked::after { content:"✓"; color:#fff; font-size:14px; font-weight:800; }
  .vbody { padding:11px 13px 13px; display:flex; flex-direction:column; flex:1; }
  .video .cap { flex:1; min-width:0; }
  .video .t1 { font-weight:650; font-size:12.5px; line-height:1.32; display:-webkit-box; -webkit-line-clamp:2;
               -webkit-box-orient:vertical; overflow:hidden; }
  .video .m { color:var(--ink3); font-size:11px; margin-top:7px; display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  .video .m b { color:var(--ink2); } .video a { color:var(--reply); text-decoration:none; font-weight:600; }
  .grow { color:var(--good); font-weight:600; }
  .chipT { position:absolute; top:9px; left:9px; z-index:2; font-size:9.5px; font-weight:800; color:#fff;
           background:rgba(30,36,46,.70); border-radius:99px; padding:3px 9px; letter-spacing:.3px; }
  .pchip { display:inline-flex; align-items:center; gap:7px; padding:6px 13px; border-radius:99px;
           background:var(--card2); border:1px solid var(--border); font-size:12.5px; font-weight:600;
           color:var(--ink2); cursor:pointer; }
  .pchip.on { background:var(--accent-soft); border-color:var(--accent); color:var(--ink); }
  .pbadge { font-size:10px; font-weight:700; color:var(--ink3); background:var(--card);
            border:1px solid var(--border); border-radius:99px; padding:1px 8px; }
  .vlist { display:grid; grid-template-columns:repeat(auto-fill,minmax(228px,1fr)); gap:16px;
           max-height:62vh; overflow-y:auto; padding:4px; }
  /* Engagement presenter — Nico the cat, zoomed in, writing live */
  .engpresenter { display:flex; align-items:center; gap:4px; margin:6px 0 22px; min-height:128px; }
  .engcatwrap { position:relative; width:122px; height:122px; flex:none; overflow:visible; }
  .engcatwrap::after { content:''; position:absolute; z-index:0; bottom:1px; left:50%; transform:translateX(-50%);
    width:70%; height:13px; border-radius:50%; pointer-events:none;
    background:radial-gradient(50% 50% at 50% 50%, rgba(40,48,66,.40), rgba(40,48,66,0) 72%); filter:blur(2.5px); }
  .engpresenter .engcat { position:absolute; z-index:1; left:0; bottom:0; height:122px; width:auto; }
  .engpresenter .engwalk, .engpresenter .engblink { opacity:0; }
  /* busy = actively working: rock the body like he's typing */
  .engpresenter.busy .engbase { animation:engBob .3s steps(1,end) infinite; }
  .engpresenter.busy .engwalk { animation:engBob2 .3s steps(1,end) infinite; }
  @keyframes engBob { 0%{opacity:1} 50%{opacity:0} 100%{opacity:1} }
  @keyframes engBob2 { 0%{opacity:0} 50%{opacity:1} 100%{opacity:0} }
  /* blink overrides whatever frame is showing */
  .engcatwrap.blink .engblink { opacity:1 !important; }
  .engcatwrap.blink .engbase, .engcatwrap.blink .engwalk { opacity:0 !important; animation:none !important; }
  /* Koa holds still like Sage — base image + a calm balancing sway, never jogging in place */
  #koaPresenter .engwalk { display:none; }
  #koaPresenter.busy .engbase { animation:none; }
  #koaPresenter .engcatwrap { animation:koaIdle 3.8s ease-in-out infinite; transform-origin:50% 92%; }
  #koaPresenter.busy .engcatwrap { animation:koaIdle 2.1s ease-in-out infinite; }
  @keyframes koaIdle { 0%,100%{transform:translateY(0) rotate(-1.3deg)} 50%{transform:translateY(-4px) rotate(1.3deg)} }
  .engheart { position:absolute; left:46%; bottom:78px; transform:translateX(-50%) scale(.4);
    font-size:32px; opacity:0; pointer-events:none;
    filter:drop-shadow(0 2px 4px rgba(0,0,0,.4)); }
  .engheart.pop { animation:heartUp 1.05s ease-out; }
  @keyframes heartUp { 0%{opacity:0; transform:translateX(-50%) translateY(0) scale(.4)}
    22%{opacity:1; transform:translateX(-50%) translateY(-12px) scale(1.15)}
    100%{opacity:0; transform:translateX(-50%) translateY(-58px) scale(.85)} }
  .engbubble { position:relative; max-width:520px; min-width:220px; padding:13px 18px; border-radius:16px;
    background:linear-gradient(180deg,rgba(40,43,51,.97),rgba(22,24,29,.97));
    border:1px solid rgba(255,255,255,.12); box-shadow:0 6px 20px rgba(0,0,0,.52);
    color:#eef1f6; font-size:14.5px; font-weight:600; line-height:1.4;
    display:flex; align-items:center; gap:7px; min-height:24px;
    transition:transform .18s ease; }
  .engbubble .engsay, .engbubble span { color:#eef1f6; }
  .engbubble::before { content:""; position:absolute; left:-8px; top:50%; margin-top:-7px;
    width:14px; height:14px; transform:rotate(45deg); background:rgba(40,43,51,.97);
    border-left:1px solid rgba(255,255,255,.12); border-bottom:1px solid rgba(255,255,255,.12); }
  .engpresenter.pulse .engbubble { transform:scale(1.025); }
  .engcaret { display:none; width:2px; height:16px; background:var(--ink2); margin-left:1px;
    vertical-align:-3px; animation:caretBlink 1s steps(1,end) infinite; }
  .engpresenter.writing .engcaret { display:inline-block; }
  @keyframes caretBlink { 0%,50%{opacity:1} 50.01%,100%{opacity:0} }
  .engtype { display:none; gap:4px; align-items:center; }
  .engpresenter.busy:not(.writing) .engtype { display:inline-flex; }
  .engtype i { width:6px; height:6px; border-radius:50%; background:var(--ink2);
    animation:engDots 1s infinite ease-in-out; }
  .engtype i:nth-child(2){ animation-delay:.2s; } .engtype i:nth-child(3){ animation-delay:.4s; }
  /* Sage (the dog) small presenter in the workspace — SMOOTH eased motion (no frame-swap) */
  .musep { margin:2px 0 12px; }
  .musep .engbubble, .musep .engbubble #museSay { color:#eef1f6; }  /* light text on the dark bubble */
  .musep .engtype i { background:#aab3c2; }
  .musep .engwalk, .musep .engblink { display:none; }           /* base only; motion via transforms */
  .musep .engcatwrap { animation:museSway 3.2s ease-in-out infinite; transform-origin:50% 92%; }
  .musep.busy .engcatwrap { animation:museSway 1.1s ease-in-out infinite; }
  @keyframes museSway { 0%,100%{transform:translateY(0) rotate(-2.2deg)} 50%{transform:translateY(-7px) rotate(2.2deg)} }

  /* Launchpad landing — big Sage intro, all eased transforms so it reads smooth */
  .museland { display:flex; flex-direction:column; align-items:center; justify-content:center;
    text-align:center; padding:26px 16px 44px; gap:16px; min-height:460px; }
  .museland.museinline { min-height:0; padding:34px 0 8px; gap:8px; }  /* inline in Step 1 */
  /* compact HORIZONTAL Step-1 row: character | player | arrow+message */
  .museland.museinline.muserow { flex-direction:row; align-items:center; justify-content:center;
    gap:20px; min-height:0; padding:10px 0; cursor:pointer; flex-wrap:wrap; }
  .mldogbox { position:relative; height:180px; display:flex; align-items:flex-end; flex:none; }
  .muserow .mldog { height:180px; }
  .muserow .mldogimg { height:180px; }
  .mldogbox .floatfx { position:absolute; inset:-16px; }
  .mlplayerbox { position:relative; width:116px; height:150px; display:flex; align-items:center; justify-content:center; flex:none; }
  .muserow .mlplayer { position:static; width:116px; height:auto; margin:0; transform:none; }
  .muserow .mlplayer.show { transform:none; }
  .muserow .mlcd { position:absolute; left:50%; margin-left:-24px; top:4px; width:48px; height:48px; }

  /* --- Sage batch scheduler (drop up to 10) --- */
  .batchwrap { margin-top:10px; }
  .batchhead { display:flex; align-items:center; justify-content:space-between; margin:0 0 12px; font-size:15px; color:var(--ink); }
  .brow { display:flex; align-items:center; gap:10px; padding:9px 12px; border:1px solid var(--border); border-radius:11px; margin-bottom:7px; background:var(--card2); font-size:13px; }
  .brow .bic { width:18px; flex:none; display:flex; align-items:center; justify-content:center; color:var(--ink3); font-weight:800; }
  .brow.ready .bic { color:var(--good); }
  .brow.error { opacity:.55; }
  .brow .bname { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; color:var(--ink2); }
  .brow .bstat { font-size:11.5px; color:var(--ink3); flex:none; }
  .bspin { width:13px; height:13px; border:2px solid var(--border); border-top-color:var(--accent); border-radius:50%; display:inline-block; animation:bspin .7s linear infinite; }
  @keyframes bspin { to{ transform:rotate(360deg); } }
  .bcard { border:1px solid var(--border); border-radius:14px; padding:12px; margin-bottom:10px; background:var(--card); box-shadow:var(--rim); animation:riseIn .45s cubic-bezier(.2,.75,.3,1) both; }
  .bcard-top { display:flex; align-items:flex-start; gap:11px; }
  .bthumb { width:62px; height:62px; border-radius:10px; object-fit:cover; flex:none; border:1px solid var(--border); }
  .bthumb-ph { display:flex; align-items:center; justify-content:center; background:var(--card2); font-size:22px; }
  .bcard-main { flex:1; min-width:0; }
  .btitle { width:100%; font-size:14px; font-weight:700; color:var(--ink); border:1px solid transparent; border-radius:8px; padding:5px 7px; background:transparent; }
  .btitle:hover, .btitle:focus { border-color:var(--border); background:var(--card2); outline:none; }
  .bmeta { font-size:11px; color:var(--ink3); margin-top:2px; padding:0 7px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .bskip { font-size:11px; color:var(--ink3); display:flex; align-items:center; gap:4px; flex:none; cursor:pointer; }
  .bdesc { width:100%; margin-top:8px; font:12.5px/1.5 -apple-system,sans-serif; color:var(--ink2); background:var(--card2); border:1px solid var(--border); border-radius:9px; padding:7px 9px; resize:vertical; }
  .brow2 { display:flex; align-items:center; gap:8px; margin-top:8px; }
  .bwhen { font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:.05em; color:var(--ink3); }
  .bslot { flex:1; border:1px solid var(--border); border-radius:9px; padding:6px 9px; font-size:12.5px; background:var(--card); color:var(--ink); }
  .batchgo { margin-top:4px; }
  .bmore { margin-left:auto; flex:none; background:none; border:none; color:var(--accent); font-weight:700; font-size:11.5px; cursor:pointer; padding:4px 2px; }
  .bmore:hover { text-decoration:underline; }
  .bextra { margin-top:10px; padding-top:10px; border-top:1px solid var(--border); display:grid; gap:3px; animation:riseIn .3s ease both; }
  .bextra .fl { display:block; font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:.05em; color:var(--ink3); margin-top:8px; }
  .bextra .fl .hint { text-transform:none; letter-spacing:0; font-weight:600; }
  .bsel { width:100%; border:1px solid var(--border); border-radius:9px; padding:6px 9px; font-size:12.5px; background:var(--card); color:var(--ink); }

  .muserow .mlcd.dropin { animation:cdDropH .6s cubic-bezier(.35,.1,.6,1) forwards; }
  @keyframes cdDropH { 0%{opacity:0; transform:translateY(-10px) scale(1)} 16%{opacity:1; transform:translateY(0) scale(1)}
    80%{opacity:1; transform:translateY(54px) scale(.5)} 100%{opacity:0; transform:translateY(62px) scale(.42)} }
  .mlprompt { display:flex; align-items:center; gap:10px; flex:none; }
  .muserow .mlarrow { position:static; width:38px; margin:0; transform:rotate(90deg); }
  .muserow .mlarrow.on { animation:arrowBounceH 1s ease-in-out infinite; }
  @keyframes arrowBounceH { 0%,100%{transform:rotate(90deg) translateY(0)} 50%{transform:rotate(90deg) translateY(9px)} }
  .muserow .mlbubble { min-width:0; font-size:18px; padding:11px 18px; }
  .muserow .mlbubble::after { left:-8px; top:50%; margin-left:0; margin-top:-8px;
    border-left:1px solid var(--border); border-top:0; border-bottom:1px solid var(--border); }
  .mlstage { position:relative; width:320px; height:300px; display:flex; align-items:flex-end;
    justify-content:center; cursor:pointer; overflow:visible; }
  .mldog { position:relative; height:350px; transform-origin:50% 94%; overflow:visible; }
  .mldog > * { position:relative; z-index:1; }
  .mldog::after { content:''; position:absolute; z-index:0; bottom:2px; left:50%; transform:translateX(-50%);
    width:60%; height:16px; border-radius:50%; pointer-events:none;
    background:radial-gradient(50% 50% at 50% 50%, rgba(40,48,66,.42), rgba(40,48,66,0) 72%); filter:blur(3px); }
  .mldogimg { height:350px; width:auto; display:block; }
  /* pose crossfade: generated frames share one canvas, so opacity swaps align exactly */
  .mldog .mldogimg { position:absolute; left:0; top:0; opacity:0; transition:opacity .35s ease; }
  .mldog .pose-greet { position:relative; }   /* this one sizes the box */
  .mldog[data-pose="greet"] .pose-greet,
  .mldog[data-pose="beckon"] .pose-beckon,
  .mldog[data-pose="holdcd"] .pose-holdcd,
  .mldog[data-pose="earbuds"] .pose-earbuds,
  .mldog[data-pose="run"] .pose-run { opacity:1; }
  /* transform states (separate from pose so they compose) */
  .mldog[data-state="bob"] { animation:mlBreathe 3.4s ease-in-out infinite; }
  .mldog[data-state="beckon"] { animation:mlBeckon 1.05s ease-in-out infinite; }
  .mldog[data-state="listen"] { animation:mlListen 1.15s ease-in-out infinite; }
  .mldog[data-state="run"] { animation:mlRun .85s ease-in forwards; }
  @keyframes mlBreathe { 0%,100%{transform:translateY(0) scale(1)} 50%{transform:translateY(-7px) scale(1.012)} }
  @keyframes mlBeckon { 0%,100%{transform:translateY(0) rotate(-2deg)} 50%{transform:translateY(-8px) rotate(2deg)} }
  @keyframes mlListen { 0%,100%{transform:scale(1.02) rotate(-1.5deg)} 50%{transform:scale(1.05) rotate(1.5deg)} }
  @keyframes mlRun { 0%{transform:translateX(0) rotate(0)} 100%{transform:translateX(640px) rotate(4deg)} }
  /* cutscene props */
  .mlprop { position:absolute; pointer-events:none; }
  .mlprop svg { width:100%; height:100%; display:block; filter:drop-shadow(0 4px 7px rgba(0,0,0,.35)); }
  /* CD player sits out front from the start (the drop target); the file drops INTO it */
  .mlplayer { width:120px; height:94px; left:50%; margin-left:104px; bottom:60px; opacity:0;
    transform:translateY(16px) scale(.9); z-index:2; }
  .mlplayer.show { opacity:1; transform:translateY(0) scale(1); transition:all .45s cubic-bezier(.2,.8,.3,1.1); }
  .mlplayer .pldisc { transform-box:fill-box; transform-origin:center; }
  .mlplayer.spin .pldisc { animation:discSpin 1.05s linear infinite; }
  @keyframes discSpin { to { transform:rotate(360deg); } }
  .mlcd img { width:100%; height:100%; display:block; filter:drop-shadow(0 3px 6px rgba(0,0,0,.3)); }
  /* bouncing arrow pointing down at the player slot */
  .mlarrow { position:absolute; left:50%; margin-left:147px; top:70px; width:34px; opacity:0; z-index:6; }
  .mlarrow img { width:100%; display:block; filter:drop-shadow(0 3px 5px rgba(0,0,0,.3)); }
  .mlarrow.on { opacity:.92; animation:arrowBounce 1s ease-in-out infinite; }
  @keyframes arrowBounce { 0%,100%{transform:translateY(0)} 50%{transform:translateY(11px)} }
  /* the dropped file falls into the player */
  .mlcd { width:52px; height:52px; left:50%; margin-left:138px; top:44px; opacity:0; z-index:4; }
  .mlcd.dropin { animation:cdDrop .64s cubic-bezier(.35,.1,.6,1) forwards; }
  .mlcd.cdgone { opacity:0; }
  @keyframes cdDrop { 0%{opacity:0; transform:translateY(-10px) scale(1)} 16%{opacity:1; transform:translateY(0) scale(1)}
    80%{opacity:1; transform:translateY(118px) scale(.5)} 100%{opacity:0; transform:translateY(132px) scale(.42)} }
  .mlbubble { position:relative; font-size:22px; font-weight:800; color:var(--ink); background:var(--card);
    border:1px solid var(--border); border-radius:var(--r); padding:14px 26px; box-shadow:var(--rim), var(--e4);
    min-width:190px; transition:transform .25s ease, background .25s ease; }
  .mlbubble::after { content:""; position:absolute; left:50%; top:-9px; margin-left:-8px; width:16px; height:16px;
    transform:rotate(45deg); background:var(--card); border-left:1px solid var(--border); border-top:1px solid var(--border); }
  .mldrop { font-size:14px; color:var(--ink2); border:2px dashed var(--border); border-radius:14px;
    padding:15px 28px; cursor:pointer; transition:border-color .15s, background .15s; }
  .mldrop.drag { border-color:var(--accent); background:var(--accent-soft); transform:scale(1.01); }
  .mlskip { margin-top:2px; }
  .floatfx { position:absolute; inset:-34px; pointer-events:none; overflow:visible; }
  .glyph { position:absolute; font-weight:800; opacity:0; animation:glyphUp 2.3s ease-out forwards;
    text-shadow:0 1px 3px rgba(0,0,0,.25); }
  @keyframes glyphUp { 0%{opacity:0; transform:translateY(10px) scale(.5)}
    18%{opacity:.95} 100%{opacity:0; transform:translateY(-150px) scale(1.15)} }
  @keyframes engDots { 0%,60%,100%{opacity:.25; transform:translateY(0)} 30%{opacity:1; transform:translateY(-3px)} }
  @keyframes popIn { 0%{opacity:0; transform:translateY(-8px) scale(.98)} 100%{opacity:1; transform:translateY(0) scale(1)} }
  .ev.fresh { animation:popIn .4s ease both;
    box-shadow:0 0 0 1px rgba(174,180,194,.4), 0 6px 16px rgba(0,0,0,.4); }
  /* Settings: profile / connections / billing */
  .profile { display:flex; align-items:center; gap:16px; }
  .avatar { width:58px; height:58px; border-radius:50%; flex:none; display:flex; align-items:center;
    justify-content:center; font-weight:800; font-size:20px; color:#3A4252; letter-spacing:.5px;
    background:linear-gradient(145deg,#FFFFFF,#C8D2E0 55%,#9BA7B9);
    box-shadow:inset 0 1px 1px rgba(255,255,255,.9), inset 0 -3px 5px rgba(120,135,160,.4),
      0 3px 8px -3px rgba(90,102,124,.5); }
  .pname { font-size:17px; font-weight:800; }
  .pemail { font-size:13px; color:var(--ink2); }
  .pmeta { font-size:11.5px; color:var(--ink3); margin-top:2px; }
  /* Home welcome header */
  .homehead { display:flex; align-items:center; gap:16px; margin:0 0 18px; }
  .homehead-pic, .homehead-init { width:62px; height:62px; border-radius:50%; flex:none; object-fit:cover;
    box-shadow:inset 0 1px 2px rgba(255,255,255,.9), inset 0 -3px 6px rgba(120,135,160,.35),
      0 5px 13px -5px rgba(90,102,124,.55); border:2px solid rgba(255,255,255,.75); }
  .homehead-init { display:flex; align-items:center; justify-content:center; font-weight:800;
    font-size:23px; color:#3A4252; letter-spacing:.5px;
    background:linear-gradient(145deg,#FFFFFF,#C8D2E0 55%,#9BA7B9); }
  .homehead-hi { font-size:25px; font-weight:800; letter-spacing:-.015em; line-height:1.1; }
  .homehead-status { font-size:13.5px; color:var(--ink2); margin-top:3px; display:flex; align-items:center; gap:7px; }
  /* live pulse dot */
  .livedot { width:8px; height:8px; border-radius:50%; background:var(--good); flex:none;
    box-shadow:0 0 0 0 color-mix(in srgb, var(--good) 60%, transparent); animation:livePulse 2s ease-out infinite; }
  @keyframes livePulse { 0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--good) 55%,transparent)}
    70%{box-shadow:0 0 0 7px color-mix(in srgb,var(--good) 0%,transparent)} 100%{box-shadow:0 0 0 0 transparent} }
  /* KPI cards */
  .kpis { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:0 0 22px; }
  .kpi { position:relative; overflow:hidden; text-align:left; cursor:pointer; display:flex; flex-direction:column;
    gap:2px; padding:16px 16px 14px; border-radius:var(--r); border:1px solid var(--border);
    background:linear-gradient(170deg, color-mix(in srgb,var(--card) 92%,#fff), var(--card2));
    box-shadow:var(--rim), var(--e2); transition:transform .14s ease, box-shadow .14s ease; }
  .kpi:hover { transform:translateY(-3px); box-shadow:var(--rim), var(--e4); }
  .kpi::after { content:''; position:absolute; top:0; left:-120%; width:70%; height:100%;
    background:linear-gradient(100deg, transparent, rgba(255,255,255,.55), transparent); transform:skewX(-18deg);
    transition:left .6s ease; pointer-events:none; }
  .kpi:hover::after { left:160%; }
  .kpi-n { font-size:27px; font-weight:800; letter-spacing:-.02em; line-height:1.05; color:var(--kpinum);
    font-variant-numeric:tabular-nums; }
  .kpi-n.money { color:var(--good); }
  .kpi-l { font-size:12.5px; font-weight:700; color:var(--ink2); margin-top:1px; }
  .kpi-s { font-size:11px; color:var(--ink3); }
  /* live activity feed */
  .seeall { float:right; font-size:12px; font-weight:700; color:var(--accent-deep); background:none;
    border:none; cursor:pointer; padding:2px 4px; }
  .seeall:hover { text-decoration:underline; }
  .homefeed { display:flex; flex-direction:column; gap:8px; }
  .feedrow { display:flex; align-items:center; gap:12px; padding:11px 14px; border-radius:14px;
    background:var(--card2); border:1px solid var(--border); box-shadow:var(--rim);
    animation:feedIn .4s ease both; }
  @keyframes feedIn { from{opacity:0; transform:translateY(6px)} to{opacity:1; transform:none} }
  .feedav { width:38px; height:38px; border-radius:10px; flex:none; object-fit:contain;
    background:linear-gradient(160deg,#fff,var(--accent-soft)); border:1px solid var(--border); padding:2px; }
  .feedmain { flex:1; min-width:0; }
  .feedtop { display:flex; align-items:baseline; gap:8px; }
  .feedwho { font-weight:800; font-size:13px; }
  .feedact { font-size:11.5px; font-weight:700; padding:1px 8px; border-radius:999px; }
  .feedact.reply { color:var(--good); background:color-mix(in srgb,var(--good) 13%,transparent); }
  .feedact.like { color:var(--accent-deep); background:var(--accent-soft); }
  .feedact.skip { color:var(--ink3); background:var(--card); }
  .feedtime { font-size:11px; color:var(--ink3); margin-left:auto; flex:none; }
  .feedtext { font-size:12.5px; color:var(--ink2); margin-top:2px; overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap; }
  .feedtext b { color:var(--ink1); font-weight:700; }
  @media (max-width:880px){ .kpis { grid-template-columns:repeat(2,1fr); } }
  /* Profile tab hero */
  .profhero { display:flex; align-items:center; gap:22px; flex-wrap:wrap; }
  .profhero-pic, .profhero-init { width:96px; height:96px; border-radius:50%; flex:none; object-fit:cover;
    box-shadow:inset 0 1px 2px rgba(255,255,255,.9), inset 0 -4px 7px rgba(120,135,160,.35),
      0 6px 16px -5px rgba(90,102,124,.55); border:2px solid rgba(255,255,255,.75); }
  .profhero-init { display:flex; align-items:center; justify-content:center; font-weight:800;
    font-size:34px; color:#3A4252; letter-spacing:.5px;
    background:linear-gradient(145deg,#FFFFFF,#C8D2E0 55%,#9BA7B9); }
  .profhero-name { font-size:24px; font-weight:800; letter-spacing:-.01em; }
  .profhero-sub { font-size:13.5px; color:var(--ink2); margin-top:2px; }
  .profhero-badges { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
  .pbadge { display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:700;
    color:var(--ink2); background:var(--card2); border:1px solid var(--border);
    border-radius:999px; padding:5px 12px; box-shadow:var(--rim); }
  .pbadge.ok { color:var(--good); }
  .pbdot { width:7px; height:7px; border-radius:50%; background:var(--good);
    box-shadow:0 0 0 3px color-mix(in srgb, var(--good) 22%, transparent); }
  .profpages { display:flex; flex-wrap:wrap; gap:10px; }
  .pagechip { display:inline-flex; align-items:center; gap:8px; font-size:13px; font-weight:700;
    color:var(--ink2); background:var(--card2); border:1px solid var(--border);
    border-radius:12px; padding:9px 14px; box-shadow:var(--rim); }
  .pagechip::before { content:''; width:8px; height:8px; border-radius:50%; background:var(--ink3); }
  .pagechip.on { color:var(--ink1); }
  .pagechip.on::before { background:var(--good); box-shadow:0 0 0 3px color-mix(in srgb, var(--good) 20%, transparent); }
  /* in-page sub-tabs (merged sections) */
  .subtabs { display:flex; gap:8px; margin:-2px 0 20px; flex-wrap:wrap; }
  .acct-div { margin:26px 0 2px; padding-top:18px; border-top:1px solid var(--border); }
  .collap { border:1px solid var(--border); border-radius:14px; background:var(--card); margin:0 0 14px; }
  .collap > summary { list-style:none; cursor:pointer; display:flex; align-items:center; gap:8px;
    padding:14px 16px; font-size:15px; font-weight:800; color:var(--ink); }
  .collap > summary::-webkit-details-marker { display:none; }
  .collap > summary::after { content:'▾'; margin-left:auto; color:var(--ink3); font-size:12px; transition:transform .2s ease; }
  .collap[open] > summary::after { transform:rotate(180deg); }
  .collap > summary:hover { color:var(--accent-deep); }
  .collap > summary .hint { font-weight:600; }
  .collap-body { padding:0 16px 16px; }
  .homesched { margin:16px 0 4px; }
  .hs-head { display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:12px; }
  .hs-date { font-size:19px; font-weight:800; color:var(--ink); letter-spacing:-.01em; }
  .hs-sub { font-size:10.5px; font-weight:800; text-transform:uppercase; letter-spacing:.06em; color:var(--ink3); margin-top:3px; }
  .hs-time { font-size:18px; font-weight:750; color:var(--ink2); font-variant-numeric:tabular-nums; }
  .hs-today { display:flex; flex-direction:column; gap:7px; margin-bottom:12px; }
  .hs-item { display:flex; align-items:center; gap:12px; padding:11px 14px; border:1px solid var(--border); border-radius:12px; background:var(--card2); }
  .hs-t { font-size:13px; font-weight:800; color:var(--accent-deep); min-width:72px; font-variant-numeric:tabular-nums; }
  .hs-ttl { flex:1; min-width:0; font-size:13.5px; font-weight:700; color:var(--ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .hs-badge { font-size:11px; font-weight:700; color:var(--ink3); flex:none; }
  .hs-none { padding:14px; border:1px dashed var(--border); border-radius:12px; color:var(--ink3); font-size:13px; text-align:center; }
  .hs-sched { margin-bottom:12px; }
  .hs-sched:empty { display:none; }
  .hs-schhead, .hs-clhead { font-size:10.5px; font-weight:800; text-transform:uppercase; letter-spacing:.06em; color:var(--ink3); margin:0 0 8px; }
  .hs-clhead { margin-top:2px; }
  .hs-clhead .hint { text-transform:none; letter-spacing:0; font-weight:600; }
  .hs-schrow { display:flex; align-items:center; gap:12px; padding:9px 13px; border:1px solid var(--border); border-radius:11px; background:var(--card2); margin-bottom:6px; }
  .hs-cl { display:flex; flex-direction:column; gap:6px; margin-bottom:11px; }
  .cl-item { display:flex; align-items:center; gap:11px; padding:10px 13px; border:1px solid var(--border); border-radius:11px; background:var(--card2); cursor:pointer; }
  .cl-item input[type=checkbox] { width:17px; height:17px; accent-color:var(--accent); flex:none; cursor:pointer; }
  .cl-text { flex:1; min-width:0; font-size:13.5px; font-weight:600; color:var(--ink); }
  .cl-item.done .cl-text { text-decoration:line-through; color:var(--ink3); }
  .cl-del { flex:none; background:none; border:none; color:var(--ink3); font-size:18px; line-height:1; cursor:pointer; padding:0 2px; opacity:.5; }
  .cl-del:hover { opacity:1; color:var(--bad); }
  .cl-empty { font-size:13px; color:var(--ink3); padding:4px 2px; }
  .hs-add { display:flex; gap:8px; }
  .hs-input { flex:1; min-width:0; border:1px solid var(--border); border-radius:10px; padding:9px 12px; font-size:13.5px; background:var(--card2); color:var(--ink); }
  .hs-input:focus { outline:none; border-color:var(--accent); }
  .hs-addbtn { flex:none; }
  .rxinput, .rxtext { width:100%; border:1px solid var(--border); border-radius:11px; padding:10px 13px; font-size:14px; background:var(--card2); color:var(--ink); margin-bottom:9px; }
  .rxinput:focus, .rxtext:focus { outline:none; border-color:var(--accent); }
  .rxtext { resize:vertical; font:14px/1.5 -apple-system,sans-serif; }
  .rxplan:empty { display:none; }
  .rxplan { margin-top:16px; }
  .rxhook-l { font-size:10.5px; font-weight:800; text-transform:uppercase; letter-spacing:.06em; color:var(--ink3); margin-bottom:6px; }
  .rxhook { font-size:18px; font-weight:800; color:var(--ink); line-height:1.3; margin-bottom:14px; }
  .rxbeats { display:flex; flex-direction:column; gap:9px; }
  .rxbeat { display:flex; gap:11px; padding:12px 14px; border:1px solid var(--border); border-radius:13px; background:var(--card2); }
  .rxbnum { flex:none; width:22px; height:22px; border-radius:50%; background:var(--accent-soft); color:var(--accent-deep); font-weight:800; font-size:12px; display:flex; align-items:center; justify-content:center; }
  .rxbbody { min-width:0; }
  .rxmoment { font-size:12px; font-weight:700; color:var(--ink2); margin-bottom:3px; }
  .rxtake { font-size:14px; font-weight:650; color:var(--ink); line-height:1.4; }
  .rxcompliance { margin-top:12px; padding:10px 13px; border-radius:11px; background:var(--accent-soft); border:1px solid var(--accent); font-size:12.5px; color:var(--ink); line-height:1.5; }
  .rxfstage { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1.1fr); gap:16px; margin:4px 0 12px; }
  @media (max-width:760px){ .rxstage { grid-template-columns:1fr; } }
  .rxcamwrap { position:relative; aspect-ratio:9/16; max-height:420px; border-radius:16px; overflow:hidden; background:#0a0e18; border:1px solid var(--border); }
  .rxcam { width:100%; height:100%; object-fit:cover; transform:scaleX(-1); background:#0a0e18; }
  #rxCamWrap.mirror-off .rxcam, #rxCamWrap.clip-play .rxcam { transform:none; }   /* the preview mirrors like a mirror; a finished take shows as it was filmed */
  .rxdbg { position:absolute; left:8px; top:8px; font:10px ui-monospace,monospace; color:rgba(200,220,255,.65); background:rgba(8,11,20,.45); padding:1px 6px; border-radius:6px; pointer-events:none; z-index:4; }
  .rxcropbox { position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); box-sizing:border-box; border:2px dashed rgba(120,200,255,.92); border-radius:10px; box-shadow:0 0 0 2000px rgba(8,11,20,.42); pointer-events:none; }
  .rxfaceguide { position:absolute; left:50%; top:16%; transform:translateX(-50%); width:52%; aspect-ratio:1; border:2px dashed rgba(255,255,255,.4); border-radius:14px; display:flex; align-items:flex-end; justify-content:center; pointer-events:none; }
  .rxfaceguide span { font-size:10px; color:rgba(255,255,255,.7); margin-bottom:-18px; white-space:nowrap; }
  .rxhud { position:absolute; top:10px; left:10px; right:10px; display:flex; align-items:center; justify-content:space-between; pointer-events:none; }
  .rxmicdot { width:11px; height:11px; border-radius:50%; background:rgba(255,255,255,.3); }
  .rxmicdot.on { background:#4FC07E; box-shadow:0 0 0 4px rgba(79,192,126,.25); }
  .rxtimer { font-size:13px; font-weight:800; color:#fff; background:rgba(0,0,0,.45); border-radius:8px; padding:2px 8px; font-variant-numeric:tabular-nums; }
  .rxcammsg { position:absolute; left:0; right:0; bottom:0; padding:10px; text-align:center; font-size:12px; color:rgba(223,233,248,.8); background:linear-gradient(transparent, rgba(0,0,0,.6)); }
  .rxcammsg:empty { display:none; }
  .rxside { display:flex; flex-direction:column; gap:12px; min-width:0; }
  .rxtalk-l { font-size:12.5px; font-weight:700; color:var(--ink2); display:flex; justify-content:space-between; }
  .rxtalk-l b { color:var(--accent-deep); }
  .rxtalkbar { height:9px; border-radius:99px; background:var(--card2); border:1px solid var(--border); overflow:hidden; margin:7px 0 4px; }
  .rxtalkbar i { display:block; height:100%; width:0%; background:linear-gradient(90deg,var(--accent),var(--accent-deep)); transition:width .2s ease; }
  .rxtalk-goal { font-size:11px; color:var(--ink3); }
  .rxlen { margin:0 0 10px; }
  .rxlen-l { font-size:12px; font-weight:800; color:var(--ink2); margin-bottom:7px; }
  .rxlen-hint { font-weight:500; color:var(--ink3); font-size:11px; margin-left:4px; }
  .rxlen-chips { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .rxlenchip { border:1px solid var(--border); background:var(--card); color:var(--ink2); border-radius:99px; padding:4px 11px; font-size:12px; font-weight:700; cursor:pointer; }
  .rxlenchip:hover { color:var(--ink); border-color:var(--accent-deep); }
  .rxlenchip.on { background:var(--accent-soft); border-color:var(--accent); color:var(--accent-deep); }
  .rxlenchip.mic { margin-left:auto; }
  .rxcaptog { display:flex; align-items:flex-start; gap:8px; font-size:12px; color:var(--ink2); margin:0 0 12px; cursor:pointer; line-height:1.45; }
  .rxcaptog input { margin-top:2px; accent-color:var(--accent); flex:none; }
  .rxcaptog b { color:var(--ink); }
  .rights-add { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }
  .rights-in { flex:1; min-width:180px; background:var(--card2); color:var(--ink); border:1px solid var(--border); border-radius:10px; padding:9px 11px; font:12.5px -apple-system,sans-serif; }
  .rights-list { display:flex; flex-direction:column; gap:7px; margin-bottom:16px; }
  .rights-row { display:flex; align-items:center; gap:10px; padding:9px 12px; border:1px solid var(--border); border-radius:10px; background:var(--card2); flex-wrap:wrap; }
  .rr-t { font-weight:700; color:var(--ink); font-size:13px; }
  .rr-d { font-size:11.5px; color:#4fc07e; font-weight:700; }
  .rr-f { font-size:11px; color:var(--ink3); font-family:ui-monospace,monospace; margin-left:auto; }
  .rights-h3 { font-size:14px; font-weight:800; color:var(--ink); margin:4px 0 8px; }
  .rights-ta { width:100%; min-height:80px; background:var(--card2); color:var(--ink); border:1px solid var(--border); border-radius:10px; padding:10px; font:12.5px/1.5 -apple-system,sans-serif; }
  .rights-out { white-space:pre-wrap; font-size:12.5px; line-height:1.55; margin-top:10px; color:var(--ink); }
  .rxtalk-goal.hit { color:var(--good); font-weight:700; }
  .rxpromptlabel { font-size:10.5px; font-weight:800; text-transform:uppercase; letter-spacing:.06em; color:var(--ink3); }
  .rxpromptlabel .hint { text-transform:none; letter-spacing:0; font-weight:600; }
  .rxprompter { flex:1; min-height:120px; max-height:260px; overflow:auto; display:flex; flex-direction:column; gap:7px; padding:4px 0; }
  .rxpline { padding:10px 13px; border-left:3px solid var(--accent); background:var(--card2); border-radius:0 10px 10px 0; font-size:var(--prompt-size,13.5px); line-height:1.4; color:var(--ink); }
  .rxpline.on { box-shadow:inset 0 0 0 1.5px var(--accent); }
  .rxprompt-tools { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin:6px 0; }
  .rxprompt-v { font-size:12px; color:var(--ink2); }
  .rxprompt-add { margin:6px 0 4px; }
  .rxprompt-add > summary { cursor:pointer; font-size:12px; font-weight:800; color:var(--ink2); padding:4px 0; }
  .rxprompt-sub { font-size:11px; font-weight:800; text-transform:uppercase; letter-spacing:.05em; color:var(--ink3); margin:8px 0 4px; }
  .rxprompt-drop { border:1.5px dashed var(--border); border-radius:11px; padding:10px; font-size:12px; color:var(--ink2); text-align:center; margin:4px 0 8px; }
  .rxprompt-drop.over { border-color:var(--accent); }
  .rxcontrols { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
  #rxRecBtn.on { background:var(--bad, #E0564B); }
  .rxcamrow { align-items:center; gap:9px; margin:2px 0 12px; flex-wrap:wrap; }
  .rxcamrow-l { font-size:11px; font-weight:800; text-transform:uppercase; letter-spacing:.05em; color:var(--ink3); }
  .rxcamsel { border:1px solid var(--border); border-radius:9px; padding:6px 10px; font-size:12.5px; font-weight:600; background:var(--card2); color:var(--ink); max-width:230px; }
  .filmgenres { display:grid; grid-template-columns:repeat(auto-fill,minmax(148px,1fr)); gap:9px; }
  .filmgenre { text-align:left; display:flex; flex-direction:column; gap:3px; padding:12px 13px; border:1px solid var(--border); border-radius:13px; background:var(--card2); color:var(--ink); cursor:pointer; }
  .filmgenre:hover { border-color:var(--accent-deep); }
  .filmgenre.on { border-color:var(--accent); background:var(--accent-soft); box-shadow:var(--rim); }
  .rxplanby { font-size:11px; color:var(--ink2); margin:-1px 0 9px; letter-spacing:.4px; text-transform:uppercase; }
  .fg-label { font-size:14px; font-weight:800; }
  .fg-blurb { font-size:11.5px; color:var(--ink2); line-height:1.35; font-weight:500; }
  .filminputs .fl { display:block; margin:12px 0 5px; }
  .rxfmt { display:flex; align-items:center; gap:7px; flex-wrap:wrap; margin:2px 0 12px; }
  .rxfmt-l { font-size:11px; font-weight:800; text-transform:uppercase; letter-spacing:.05em; color:var(--ink3); }
  .rxfmtbtn { border:1px solid var(--border); background:var(--card2); color:var(--ink2); border-radius:99px; padding:5px 12px; font-size:12px; font-weight:700; cursor:pointer; }
  .rxfmtbtn:hover { color:var(--ink); }
  .rxfmtbtn.on { background:var(--accent-soft); border-color:var(--accent); color:var(--accent-deep); }
  .rxphonebtn { margin-left:auto; border:1px solid var(--border); background:var(--card2); color:var(--ink2); border-radius:99px; padding:5px 13px; font-size:12px; font-weight:700; cursor:pointer; }
  .rxphonebtn:hover { color:var(--ink); border-color:var(--accent-deep); }
  .rxhelpq { margin-left:6px; width:28px; height:28px; border-radius:50%; border:1px solid var(--border); background:var(--card2); color:var(--ink2); font-weight:800; font-size:14px; cursor:pointer; }
  .rxhelpq:hover { color:var(--ink); border-color:var(--accent-deep); }
  .rxhelp { position:fixed; inset:0; z-index:1200; background:rgba(5,9,18,.6); display:flex; align-items:center; justify-content:center; padding:16px; }
  .rxhelp-card { width:min(560px,100%); max-height:86vh; display:flex; flex-direction:column; background:var(--card); border:1px solid var(--border); border-radius:18px; box-shadow:var(--e2); color:var(--ink); }
  .rxhelp-head { display:flex; align-items:center; justify-content:space-between; padding:14px 18px; border-bottom:1px solid var(--border); }
  .rxhelp-head h3 { margin:0; font-size:17px; }
  .rxhelp-x { border:0; background:transparent; color:var(--ink2); font-size:18px; cursor:pointer; }
  .rxhelp-body { overflow:auto; padding:6px 18px 18px; font-size:14px; line-height:1.45; }
  .rxhelp-body h4 { margin:14px 0 6px; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--ink2); }
  .rxhelp-body ul { margin:0; padding-left:18px; }
  .rxhelp-body li { margin:5px 0; }
  .rxhelp-note { color:var(--ink2); margin:0 0 6px; }
  .rxhelp-voice { color:var(--ink2); font-size:13px; line-height:1.5; margin:0; padding-left:18px; }
  .rxphone { display:flex; gap:14px; align-items:center; margin:0 0 14px; padding:14px; border:1px solid var(--border); border-radius:14px; background:var(--card2); flex-wrap:wrap; }
  .rxqr { width:140px; height:140px; border-radius:10px; background:#fff; flex:none; }
  .rxphone-info { flex:1; min-width:200px; }
  .rxphone-t { font-size:14px; font-weight:800; color:var(--ink); margin-bottom:5px; }
  .rxphone-info p { font-size:12.5px; color:var(--ink2); line-height:1.55; margin:0 0 6px; }
  .rxphone-info a { color:var(--accent-deep); font-weight:700; word-break:break-all; }
  .rxphone-note { font-size:11px; color:var(--ink3); }
  .rxphone-status { font-size:12px; font-weight:700; color:var(--ink2); margin:0 0 6px; }
  .rxphone-status.on { color:#4fc07e; }
  .rxphone-status.bad { color:#e0794b; }
  /* --- hands-free gesture control --- */
  .rxhf { display:flex; gap:14px; align-items:center; margin:0 0 14px; padding:14px; border:1px solid var(--border); border-radius:14px; background:var(--card2); flex-wrap:wrap; }
  .rxmonitor { width:200px; height:120px; border-radius:10px; background:#05070d; border:1px solid var(--border); flex:none; }
  .rxhf-info { flex:1; min-width:220px; }
  .rxhf-t { font-size:14px; font-weight:800; color:var(--ink); margin-bottom:7px; }
  .rxhf-legend { display:flex; gap:7px; flex-wrap:wrap; margin-bottom:8px; }
  .rxhf-legend span { font-size:12px; color:var(--ink2); background:var(--card); border:1px solid var(--border); border-radius:99px; padding:3px 9px; }
  .rxhf-legend b { color:var(--accent-deep); text-transform:uppercase; font-size:10.5px; letter-spacing:.03em; margin-left:2px; }
  .rxhf-status { font-size:12.5px; font-weight:700; color:var(--ink2); margin:0 0 8px; }
  .rxhf-status.on { color:#4fc07e; }
  .rxhf-status.bad { color:#e0794b; }
  .rxhf-zoom { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--ink2); margin-bottom:7px; }
  .rxhf-zoom input[type=range] { flex:1; max-width:170px; accent-color:var(--accent); }
  .rxhf-note { font-size:11px; color:var(--ink3); line-height:1.5; }
  #rxHfBtn.on { background:var(--accent-soft); border-color:var(--accent); color:var(--accent-deep); }
  .rxgmsg { position:absolute; left:50%; bottom:96px; transform:translateX(-50%); z-index:8; background:rgba(10,14,24,.78); color:#fff; font-size:13px; font-weight:700; padding:7px 12px; border-radius:12px; white-space:nowrap; pointer-events:none; }
  .rxcountdown { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:84px; font-weight:800;
    color:#fff; background:rgba(8,11,20,.45); z-index:5; pointer-events:none; text-shadow:0 2px 20px rgba(0,0,0,.6); }
  .rxcamwrap.cutting { opacity:.6; }
  .rxcamwrap.cutting::after { content:"Tightening…"; position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
    font-size:15px; font-weight:800; color:#fff; background:rgba(8,11,20,.5); z-index:6; }
  /* --- clips strip (takes saved below) --- */
  .rxclips { margin:6px 0 2px; }
  .rxclips-h { font-size:13px; font-weight:800; color:var(--ink); margin:0 0 9px; }
  .rxclips-h .hint { font-weight:600; }
  .rxclips-strip { display:flex; gap:12px; flex-wrap:wrap; }
  .rxclip { width:150px; border:1px solid var(--border); border-radius:12px; overflow:hidden; background:var(--card2); }
  .rxclip.cut { border-color:var(--accent); }
  .rxclip video { width:100%; height:200px; object-fit:cover; background:#05070d; display:block; }
  .rxclip-meta { display:flex; align-items:center; gap:6px; padding:7px 9px 3px; }
  .rxclip-n { font-size:12px; font-weight:800; color:var(--ink); }
  .rxclip-dur { font-size:11px; color:var(--ink3); margin-left:auto; }
  .rxclip-tag { font-size:10px; font-weight:800; color:var(--accent-deep); background:var(--accent-soft); border-radius:99px; padding:1px 6px; }
  .rxclip-acts { display:flex; gap:4px; padding:4px 7px 8px; }
  .rxclip-b { flex:1; border:1px solid var(--border); background:var(--card); color:var(--ink2); border-radius:7px; padding:5px 0; font-size:13px; cursor:pointer; line-height:1; }
  .rxclip-b:hover { color:var(--ink); border-color:var(--accent-deep); }
  .rxclip-b.del:hover { color:#e0794b; border-color:#e0794b; }
  .rxundo { display:flex; align-items:center; gap:12px; margin:10px 0 2px; padding:9px 14px; border:1px solid var(--border);
    border-radius:10px; background:var(--card2); font-size:13px; color:var(--ink2); }
  .rxundo-t { font-weight:700; }
  .rxundo-b { margin-left:auto; border:1px solid var(--accent); background:var(--accent-soft); color:var(--accent-deep);
    border-radius:99px; padding:5px 16px; font-size:12px; font-weight:800; cursor:pointer; }
  /* --- Record stage: clean, predictable sizing --- */
  .rxfstage { display:flex; gap:16px; align-items:flex-start; flex-wrap:wrap; margin:4px 0 14px; }
  .rxcamwrap { width:248px; max-width:100%; aspect-ratio:9/16; max-height:none; flex:none; }
  .rxside { flex:1 1 240px; min-width:0; }
  .rxfaceguide { display:none; }
  .rxfaceguide span { display:none; }
  .rxcamwrap.cam-on .rxfaceguide { display:none; }
  .rxcammsg { inset:0; left:0; right:0; bottom:0; display:flex; align-items:center; justify-content:center;
    text-align:center; padding:18px; background:rgba(10,14,24,.72); line-height:1.5; }
  .rxcamwrap.cam-on .rxcammsg { display:none; }
  .rxcamwrap { cursor:pointer; }
  .rxcamwrap.cam-on { cursor:default; }
  .rxtalk-l b { color:var(--accent-deep); }
  .rxpromptlabel { margin-bottom:7px; }
  .briefbanner { display:flex; align-items:center; gap:8px; margin:0 0 14px; padding:10px 14px; border-radius:12px;
    background:var(--accent-soft); border:1px solid var(--accent); color:var(--ink); font-size:13px; font-weight:600; }
  .briefdot { width:8px; height:8px; border-radius:50%; background:var(--accent-deep); flex:none; }
  .briefbanner b { font-weight:800; }
  .briefx { margin-left:auto; background:none; border:none; color:var(--ink3); font-size:11.5px; font-weight:700; cursor:pointer; text-decoration:underline; }
  .ideastrip { margin:0 0 18px; }
  .ideahead { font-size:13px; font-weight:800; color:var(--ink); margin:0 0 9px; }
  .ideahead .hint { font-weight:600; }
  .idearow { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
  @media (max-width:760px){ .idearow { grid-template-columns:1fr; } }
  .ideacard { display:flex; flex-direction:column; gap:7px; padding:13px 14px; border:1px solid var(--border);
    border-radius:14px; background:var(--card2); min-width:0; }
  .ideahook { font-size:14px; font-weight:750; color:var(--ink); line-height:1.3; }
  .ideawhy { font-size:12px; color:var(--ink2); line-height:1.45; flex:1; }
  .ideago { align-self:flex-start; margin-top:2px; background:var(--accent-soft); color:var(--accent-deep);
    border:1px solid var(--accent); border-radius:9px; padding:6px 12px; font-size:12.5px; font-weight:700; cursor:pointer; }
  .ideago:hover { background:var(--accent); color:#fff; }
  .subtab { font-size:13px; font-weight:700; color:var(--ink2); background:var(--card2);
    border:1px solid var(--border); border-radius:999px; padding:7px 17px; cursor:pointer;
    box-shadow:var(--rim); transition:color .12s, background .12s, box-shadow .12s, transform .08s; }
  .subtab:hover { color:var(--ink1); }
  .subtab:active { transform:translateY(1px); }
  .subtab.active { color:var(--accent-deep); background:linear-gradient(180deg,#fff,var(--accent-soft) 90%);
    box-shadow:var(--rim), var(--e1); }
  .conns { display:flex; flex-direction:column; gap:10px; }
  .conn { display:flex; align-items:center; justify-content:space-between; gap:12px;
    background:var(--card2); border:1px solid var(--border); border-radius:12px; padding:12px 16px; }
  .conn .cn { font-weight:700; font-size:14px; }
  .conn .cs { font-size:11.5px; color:var(--ink3); font-weight:700; margin-top:1px; }
  .conn .cs.ok { color:var(--good); }
  .billcards { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
  .billcard { background:var(--card2); border:1px solid var(--border); border-radius:14px; padding:14px 16px;
    box-shadow:var(--rim), var(--e1); }
  .blabel { font-size:10.5px; font-weight:800; letter-spacing:.6px; text-transform:uppercase; color:var(--ink3); }
  .bval { font-size:22px; font-weight:800; margin:5px 0 2px; font-variant-numeric:tabular-nums; }
  .bsub { font-size:11.5px; color:var(--ink3); }
  @media (max-width:640px){ .billcards { grid-template-columns:1fr; } }
  /* money KPIs: a tidy 4-up strip, 2x2 on tablet, stacked on phone */
  .billcards.bill4 { grid-template-columns:repeat(4,1fr); }
  @media (max-width:760px){ .billcards.bill4 { grid-template-columns:repeat(2,1fr); } }
  @media (max-width:420px){ .billcards.bill4 { grid-template-columns:1fr; } }
  .adv { border:1px solid var(--border); border-radius:12px; background:var(--card2); overflow:hidden; }
  .adv summary { cursor:pointer; padding:12px 15px; font-weight:700; font-size:13.5px; color:var(--reply);
    list-style:none; display:flex; align-items:center; }
  .adv summary::-webkit-details-marker { display:none; }
  .adv summary::before { content:'+'; font-weight:800; margin-right:9px; color:var(--ink3); }
  .adv[open] summary::before { content:'–'; }
  .adv[open] summary { border-bottom:1px solid var(--border); }
  .advbody { padding:12px 15px 15px; }
  /* Help page */
  .help-p { color:var(--ink2); font-size:14px; line-height:1.6; margin:4px 0 0; }
  .help-crew { display:flex; flex-direction:column; gap:12px; }
  .help-row { display:grid; grid-template-columns:120px 120px 1fr; gap:10px; align-items:baseline;
    padding:9px 0; border-bottom:1px solid var(--border); }
  .help-row:last-child { border-bottom:0; }
  .help-row b { font-size:15px; font-weight:800; }
  .help-row span { font-size:10px; font-weight:800; letter-spacing:.6px; text-transform:uppercase; color:var(--ink3); }
  .help-row p { margin:0; font-size:13px; color:var(--ink2); line-height:1.5; }
  .help-tip { margin:12px 0 0; font-size:12.5px; color:var(--ink3); font-style:italic; }
  .help-faq { padding:9px 0; border-bottom:1px solid var(--border); }
  .help-faq:last-child { border-bottom:0; }
  .help-faq > b { font-size:14px; font-weight:800; display:block; margin-bottom:3px; }
  .help-faq p { margin:0; font-size:13px; color:var(--ink2); line-height:1.55; }
  .help-faq p b { color:var(--ink); font-weight:700; }
  .help-link { color:var(--reply); font-weight:700; text-decoration:none; }
  .help-link:hover { text-decoration:underline; }
  .help-steps { margin:4px 0 0; padding-left:22px; color:var(--ink2); font-size:14px; line-height:1.7; }
  .help-steps li { margin-bottom:5px; } .help-steps b { color:var(--ink); }
  .help-ul { margin:4px 0 0; padding-left:20px; color:var(--ink2); font-size:14px; line-height:1.6; }
  .help-ul li { margin-bottom:6px; } .help-ul b { color:var(--ink); }
  @media (max-width:640px){ .help-row { grid-template-columns:1fr; gap:3px; } }
  /* Home crew grid */
  .crewgrid { display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; }
  .crewcard { display:flex; align-items:center; gap:14px; padding:20px 18px; border-radius:var(--r); min-height:150px; min-width:0;
    background:var(--card2); border:1px solid var(--border); cursor:pointer;
    box-shadow:var(--rim), var(--e2); transition:transform .15s ease, box-shadow .15s ease; }
  .crewcard:hover { transform:translateY(-3px); box-shadow:var(--rim), var(--e3); }
  .crewinfo { min-width:0; }
  .crewimg { position:relative; width:120px; height:138px; flex:none; overflow:visible; }
  .crewimg::after { content:''; position:absolute; z-index:0; bottom:3px; left:50%; transform:translateX(-50%);
    width:76%; height:13px; border-radius:50%; pointer-events:none;
    background:radial-gradient(50% 50% at 50% 50%, rgba(78,92,120,.42), rgba(78,92,120,0) 72%); filter:blur(2.5px); }
  .crewimg img { position:absolute; z-index:1; bottom:0; left:50%; transform:translateX(-50%); height:138px; width:auto; }
  .crewimg img.wave, .crewimg img.blink { opacity:0; }
  /* hover = alternate base(arm down) and wave(arm up) frames -> a real hand wave */
  .crewcard:hover .crewimg img.base, .crewcard.waving .crewimg img.base { animation:crewWaveA .5s steps(1,end) infinite; }
  .crewcard:hover .crewimg img.wave, .crewcard.waving .crewimg img.wave { animation:crewWaveB .5s steps(1,end) infinite; }
  @keyframes crewWaveA { 0%,50%{opacity:1} 50.01%,100%{opacity:0} }
  @keyframes crewWaveB { 0%,50%{opacity:0} 50.01%,100%{opacity:1} }
  /* autonomous blink (director swaps in the blink frame briefly) */
  .crewcard.blinking:not(:hover):not(.waving) .crewimg img.base { opacity:0; }
  .crewcard.blinking:not(:hover):not(.waving) .crewimg img.blink { opacity:1; }
  /* mouse parallax: the metal catches depth as the cursor moves */
  .crewgrid { perspective:900px; }
  .crewcard .crewimg { transform-style:preserve-3d; will-change:transform; }
  .crewcard:hover { z-index:2; }
  .crewcard:hover .crewimg img { filter:brightness(1.06); }
  .crewcard:hover .crewimg::after { width:84%; height:15px;
    background:radial-gradient(50% 50% at 50% 50%, rgba(60,80,130,.5), rgba(60,80,130,0) 72%); }
  html[data-theme="dark"] .crewimg::after,
  html[data-theme="dark"] .mldog::after,
  html[data-theme="dark"] .engcatwrap::after {
    background:radial-gradient(50% 50% at 50% 50%, rgba(0,0,0,.55), rgba(0,0,0,0) 72%) !important; }
  html[data-theme="dark"] .crewcard:hover .crewimg::after {
    background:radial-gradient(50% 50% at 50% 50%, rgba(0,0,0,.6), rgba(0,0,0,0) 72%) !important; }
  /* cursor-driven specular gleam, masked to the character -> metal catches light in 3D */
  .crewglow { position:absolute; left:50%; bottom:0; transform:translateX(-50%); width:180px; height:138px;
    pointer-events:none; mix-blend-mode:screen; opacity:.75; z-index:5;
    -webkit-mask-size:auto 138px; mask-size:auto 138px;
    -webkit-mask-position:center bottom; mask-position:center bottom;
    -webkit-mask-repeat:no-repeat; mask-repeat:no-repeat;
    background:radial-gradient(circle 58px at var(--gx,46%) var(--gy,26%), rgba(255,255,255,.75), rgba(190,215,255,.26) 42%, transparent 72%); }
  /* true 3D model (shown once a GLB is dropped into assets/) */
  .crew3d { display:none; position:absolute; left:50%; bottom:-6px; transform:translateX(-50%);
    width:150px; height:158px; z-index:6; background:transparent; --poster-color:transparent; }
  .crewcard.has3d .crewimg { width:150px; }
  .crewcard.has3d .crewimg img, .crewcard.has3d .crewglow { display:none; }
  .crewcard.has3d .crew3d { display:block; }
  .crewinfo { min-width:0; }
  .crewname { font-weight:800; font-size:21px; line-height:1.05; }
  .crewrole { font-size:11px; color:var(--ink3); text-transform:uppercase; letter-spacing:.8px; font-weight:800;
    margin:4px 0 8px; }
  .crewstat { font-size:14px; color:var(--ink2); line-height:1.4; }
  .crewcta { margin-top:11px; display:inline-flex; align-items:center; gap:5px; font-size:13px; font-weight:800;
    color:var(--accent-deep); letter-spacing:-.01em; }
  .crewcta .crewgo { font-size:18px; font-weight:400; line-height:1; transition:transform .14s ease; }
  .crewcard:hover .crewcta .crewgo { transform:translateX(4px); }
  @media (max-width:860px) { .crewgrid { grid-template-columns:1fr; } }
  /* Sage content scheduler */
  .sstep { display:flex; gap:14px; margin:0 0 16px; }
  #schedCard .sstep { animation:schedReveal .45s cubic-bezier(.2,.8,.2,1) both; }
  #schedCard .sstep:nth-child(1){ animation-delay:.04s } #schedCard .sstep:nth-child(2){ animation-delay:.16s }
  #schedCard .sstep:nth-child(3){ animation-delay:.28s } #schedCard .sstep:nth-child(4){ animation-delay:.40s }
  @keyframes schedReveal { from{opacity:0; transform:translateY(12px)} to{opacity:1; transform:none} }
  #schedCard .sc-thumb { animation:thumbFlip .6s cubic-bezier(.2,.8,.2,1) .3s both; }
  @keyframes thumbFlip { from{opacity:0; transform:perspective(820px) rotateY(-20deg) scale(.95)} to{opacity:1; transform:none} }
  @media (prefers-reduced-motion:reduce){ #schedCard .sstep, #schedCard .sc-thumb { animation:none; } }
  .snum { width:30px; height:30px; border-radius:50%; flex:none; display:flex; align-items:center;
    justify-content:center; font-weight:800; font-size:14px; background:var(--accent-soft);
    color:var(--ink); border:1px solid #ECC9AE; }
  .sstep.done .snum { background:#15271a; color:var(--good); border-color:#2E5B3C; }
  .sstep.pending .snum { opacity:.4; }
  .sstep .sbody { flex:1; min-width:0; }
  .slabel { font-weight:800; font-size:13.5px; margin-bottom:8px; color:var(--ink); }
  .slabel .sub { font-weight:600; color:var(--ink3); font-size:11.5px; margin-left:6px; }
  .museread { background:var(--card2); border:1px solid var(--border); border-radius:11px; padding:12px 14px; }
  .museread .heard { color:var(--ink); font-size:13.5px; font-style:italic; }
  .museread .saw { color:var(--ink2); font-size:12.5px; margin-top:7px; }
  .postingto { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:2px 0 16px;
    padding:11px 15px; background:var(--card2); border:1px solid var(--border); border-radius:12px; }
  .postingto .ptlabel { font-size:13px; font-weight:700; color:var(--ink2); }
  .ptselect { font-size:15px; font-weight:800; color:var(--ink); padding:7px 12px; border-radius:9px;
    border:1px solid var(--border); background:var(--card); cursor:pointer; }
  .dropzone { border:2px dashed var(--border); border-radius:16px; padding:28px 20px; text-align:center;
    background:var(--card2); transition:all .15s; cursor:pointer; }
  .dropzone.drag { border-color:var(--accent); background:var(--accent-soft); transform:scale(1.005); }
  .dzinner { display:flex; flex-direction:column; gap:7px; align-items:center; pointer-events:none; }
  .dzpick { color:var(--reply); font-weight:700; text-decoration:underline; cursor:pointer; pointer-events:auto; }
  .schedcard { margin-top:16px; border:1px solid var(--border); border-radius:14px; overflow:hidden;
    background:var(--card2); animation:popIn .4s ease both; }
  .schedcard .sc-head { display:flex; gap:14px; padding:14px; border-bottom:1px solid var(--border); }
  .schedcard .sc-thumb { width:92px; height:140px; object-fit:cover; border-radius:9px; flex:none;
    background:var(--card); box-shadow:0 3px 10px rgba(0,0,0,.4); }
  .schedcard .sc-body { padding:14px; display:flex; flex-direction:column; gap:12px; }
  .schedcard label.fl { font-size:11px; font-weight:800; letter-spacing:.5px; text-transform:uppercase;
    color:var(--ink3); display:block; margin-bottom:5px; }
  .schedcard .titleopt { display:flex; gap:8px; align-items:flex-start; padding:7px 9px; border-radius:9px;
    background:var(--card); margin-bottom:5px; cursor:pointer; font-size:13.5px; }
  .schedcard .titleopt input { margin-top:3px; accent-color:var(--accent); }
  .schedcard .titleopt span:first-of-type { flex:1; }
  .schedcard .titleopt.winner { background:rgba(90,130,255,.1); box-shadow:inset 0 0 0 1.5px var(--accent); animation:hookWin .5s cubic-bezier(.3,1.3,.5,1); }
  .schedcard .titleopt.scanning { background:rgba(90,130,255,.13); box-shadow:inset 0 0 0 1.5px rgba(90,130,255,.5); transition:background .1s ease, box-shadow .1s ease; }
  @keyframes hookWin { 0%{transform:scale(.97)} 55%{transform:scale(1.035)} 100%{transform:scale(1)} }
  .hookbadge:not(:empty) { font-size:9.5px; font-weight:800; text-transform:uppercase; letter-spacing:.4px;
    color:#fff; background:var(--accent); padding:3px 8px; border-radius:99px; white-space:nowrap; align-self:center; }
  .hookpick { font-size:12.5px; color:var(--ink3); padding:8px 11px; margin-bottom:9px; border-radius:9px;
    background:var(--card2); border:1px dashed var(--border); }
  .hookpick.ready { color:var(--ink2); border-style:solid; border-color:rgba(90,130,255,.4);
    background:rgba(90,130,255,.06); }
  .hookpick .hookwhy { color:var(--ink3); font-style:italic; margin-left:4px; }
  .schedcard textarea, .schedcard input.sc-in { width:100%; background:var(--card); color:var(--ink);
    border:1px solid var(--border); border-radius:9px; padding:9px; font:13px/1.5 -apple-system,sans-serif; }
  .schedcard .tags { display:flex; flex-wrap:wrap; gap:6px; }
  .schedcard .tag { background:var(--card); border:1px solid var(--border); border-radius:99px;
    padding:3px 10px; font-size:12px; color:var(--reply); font-weight:600; }
  .schedcard .sc-foot { display:flex; gap:12px; align-items:center; flex-wrap:wrap;
    padding:13px 14px; border-top:1px solid var(--border); background:var(--card); }
  .schedcard .slotpick { color:var(--good); font-weight:700; }
  /* Story template picker cards */
  .storyopts { display:grid; grid-template-columns:repeat(2,1fr); gap:12px; margin-top:4px; }
  .fxrow { display:flex; flex-wrap:wrap; align-items:center; gap:7px; margin:12px 0 2px; }
  .fxlabel { font-size:12.5px; font-weight:800; color:var(--ink); margin-right:4px; }
  .fxchip { border:1px solid var(--border); background:var(--card2); color:var(--ink2); border-radius:99px;
    padding:5px 12px; font-size:12px; font-weight:700; cursor:pointer; }
  .fxchip:hover { color:var(--ink); }
  .fxchip.on { background:var(--accent-soft); border-color:var(--accent); color:var(--accent-deep); }
  .fxhint { font-size:11px; color:var(--ink3); margin:5px 0 2px; }
  .storyselect { width:100%; margin-top:9px; font-size:12px; padding:6px 8px; }
  .fxtoggle { display:flex; align-items:center; gap:9px; margin-top:12px; padding:10px 14px;
    background:var(--card2); border:1px solid var(--border); border-radius:12px; cursor:pointer;
    font-size:13px; color:var(--ink2); }
  .fxtoggle input { accent-color:var(--accent); width:17px; height:17px; flex:none; }
  .fxtoggle b { color:var(--ink); }
  .fxtoggle.disabled { opacity:.5; }
  /* Reaction stickers: Settings grid + composer picker */
  .rxgrid { display:flex; flex-wrap:wrap; gap:12px; }
  .rxtile { position:relative; width:92px; height:118px; border:1px solid var(--border);
    border-radius:12px; background:var(--card2); overflow:hidden; }
  .rxtile img { width:100%; height:100%; object-fit:contain; }
  .rxdel { position:absolute; top:4px; right:4px; width:22px; height:22px; border:0; border-radius:50%;
    background:rgba(16,18,24,.78); color:#fff; font-size:15px; line-height:1; cursor:pointer; }
  .rxpick { margin-top:12px; }
  .rxlabel { font-size:13px; font-weight:700; color:var(--ink); margin-bottom:8px; }
  .rxlabel .sub { font-weight:400; color:var(--ink2); margin-left:6px; }
  .rxrow { display:flex; flex-wrap:wrap; gap:10px; }
  .rxopt { width:70px; height:90px; border:2px solid var(--border); border-radius:12px; cursor:pointer;
    background:var(--card2); display:flex; align-items:center; justify-content:center; overflow:hidden;
    transition:border-color .12s, transform .12s; }
  .rxopt:hover { border-color:var(--accent); transform:translateY(-1px); }
  .rxopt.selected { border-color:var(--accent); box-shadow:0 0 0 3px rgba(90,130,255,.22); }
  .rxopt img { width:100%; height:100%; object-fit:contain; }
  .rxnone { font-size:12px; color:var(--ink2); }
  .rxeditor { margin-top:12px; }
  .rxedlabel { font-size:12px; color:var(--ink2); margin-bottom:6px; }
  .rxstage { position:relative; width:168px; height:299px; border-radius:14px; overflow:hidden;
    background:#15171e; border:1px solid var(--border); touch-action:none; user-select:none; }
  .rxstage img.bg { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; opacity:.92; }
  .rxsub { position:absolute; cursor:move; }
  .rxsub img { width:100%; height:100%; display:block; pointer-events:none; -webkit-user-drag:none;
    filter:drop-shadow(0 2px 6px rgba(0,0,0,.35)); }
  .rxhandle { position:absolute; right:-9px; bottom:-9px; width:18px; height:18px; border-radius:50%;
    background:var(--accent); border:2px solid #fff; cursor:nwse-resize; box-shadow:0 1px 4px rgba(0,0,0,.4); }
  .storyopt { border:2px solid var(--border); border-radius:14px; padding:11px 10px; cursor:pointer;
    background:var(--card2); transition:border-color .12s, box-shadow .12s, transform .12s; }
  .storyopt:hover { border-color:var(--accent); transform:translateY(-1px); }
  .storyopt.selected { border-color:var(--reply); box-shadow:0 0 0 1px var(--reply); }
  .sopt-thumbs { display:flex; gap:5px; justify-content:center; min-height:84px; align-items:center; }
  .sopt-thumbs img { width:47px; height:84px; object-fit:cover; border-radius:6px;
    box-shadow:0 2px 6px rgba(90,102,124,.4); }
  .sopt-ph { width:47px; height:84px; border-radius:6px; background:var(--card); display:flex;
    align-items:center; justify-content:center; font-size:10px; font-weight:700; color:var(--ink3);
    border:1px dashed var(--border); }
  .sopt-nostory { opacity:.55; }
  .sopt-name { font-weight:800; font-size:13px; margin-top:10px; text-align:center; }
  .sopt-desc { font-size:11px; color:var(--ink3); text-align:center; margin-top:2px; line-height:1.3; }
  @media (max-width:640px){ .storyopts { grid-template-columns:1fr; } }
  .flist { max-height:70vh; overflow-y:auto; padding-right:4px; }
  .ev { padding:11px 14px; margin:8px 0; border-radius:12px; background:var(--card2); }
  .ev .meta { color:var(--ink3); font-size:11.5px; margin-bottom:5px; display:flex; gap:8px; }
  .badge { font-size:10.5px; font-weight:800; padding:2px 9px; border-radius:99px; }
  .b-REPLY { background:var(--reply-bg); color:var(--reply); }
  .b-LIKE { background:var(--like-bg); color:var(--like); }
  .b-SKIP { background:var(--skip-bg); color:var(--skip); }
  .ev .comment { color:var(--ink2); font-size:13px; } .ev .reply { color:var(--ink); margin-top:5px; }
  .ev .why { color:var(--ink3); margin-top:4px; font-size:12.5px; font-style:italic; }
  .ev.SKIP { opacity:.7; }
  /* Brand Deals pipeline cards — dedicated layout so they reflow on mobile (not the live-feed .meta row) */
  .dealcard { padding:14px 16px; margin:0 0 10px; border-radius:14px; background:var(--card2);
    background-image:var(--face-sm); box-shadow:var(--row); }
  .dealhead { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .dealbrand { font-size:16px; font-weight:800; color:var(--ink); line-height:1.2; flex:1 1 auto; min-width:0; overflow-wrap:anywhere; }
  .dealstage { flex:0 0 auto; background:var(--card2); color:var(--ink); border:1px solid var(--border);
    border-radius:9px; padding:6px 10px; font-size:12.5px; font-weight:600; cursor:pointer; box-shadow:var(--inset); }
  .dealpitch { flex:0 0 auto; white-space:nowrap; }
  .dealsub { display:flex; align-items:baseline; gap:4px 12px; flex-wrap:wrap; margin-top:8px; font-size:12.5px; }
  .dealval { color:var(--good); font-weight:800; font-size:13.5px; }
  .dealcontact { color:var(--ink3); overflow-wrap:anywhere; }
  .dealnotes { color:var(--ink2); margin-top:9px; font-size:13px; font-style:italic; line-height:1.5; }
  .empty { color:var(--ink3); padding:24px 0; text-align:center; font-size:13px; }
  /* chrome-orb empty state (V18 3D accent) */
  .orbempty { display:flex; flex-direction:column; align-items:center; gap:13px; padding:30px 20px 24px;
    text-align:center; color:var(--ink3); font-size:13px; }
  .orbempty img { width:88px; height:88px; animation:crewFloat 4.6s ease-in-out infinite;
    filter:drop-shadow(0 13px 18px rgba(70,90,130,.32)); }
  /* floating Abra concierge */
  #assistWrap { position:fixed; right:26px; bottom:26px; z-index:1000; }
  .assist-btn { width:88px; height:88px; border:0; padding:0; background:transparent; cursor:pointer;
    display:grid; place-items:center; position:relative; animation:crewFloat 4.5s ease-in-out infinite; transition:transform .2s; }
  .assist-btn > * { grid-area:1/1; }
  .assist-btn:hover { transform:scale(1.08); }
  /* idle orb attention: a soft radar ping to say "tap me" */
  .assist-btn::after { content:""; grid-area:1/1; place-self:center; width:62px; height:62px; border-radius:50%;
    border:2px solid rgba(90,150,255,.5); opacity:0; pointer-events:none; animation:orbPing 4.6s ease-out infinite; }
  .assist-btn.ischar::after, .assist-btn:hover::after { animation:none; opacity:0; }
  @keyframes orbPing { 0%{ transform:scale(.72); opacity:.7; } 68%{ opacity:0; } 100%{ transform:scale(2); opacity:0; } }
  /* first-run invite bubble, centered above the orb (works desktop + mobile) */
  .assist-hint { position:absolute; bottom:98px; right:-4px; width:max-content; max-width:210px; text-align:left;
    transform:translateY(8px) scale(.95); transform-origin:bottom right;
    background:linear-gradient(180deg, color-mix(in srgb,var(--card) 88%,#fff), var(--card)); color:var(--ink); border:1px solid var(--border);
    border-radius:14px; padding:9px 14px; box-shadow:var(--e3); font-size:12px; line-height:1.3;
    opacity:0; pointer-events:none; transition:opacity .32s ease, transform .34s cubic-bezier(.3,1.3,.5,1); z-index:1001; }
  .assist-hint b { display:block; font-size:13px; font-weight:800; color:var(--ink); }
  .assist-hint span { color:var(--ink3); font-size:10.5px; }
  .assist-hint::after { content:""; position:absolute; right:38px; bottom:-6px; width:12px; height:12px;
    background:var(--card); border-right:1px solid var(--border); border-bottom:1px solid var(--border); transform:rotate(45deg); }
  .assist-hint.show { opacity:1; transform:translateY(0) scale(1); pointer-events:auto; cursor:pointer; }
  .assist-btn img { width:64px; height:64px; border-radius:50%; filter:drop-shadow(0 6px 10px rgba(70,90,130,.4)); }
  /* liquid-chrome WebGL orb + energy halo/ring */
  .orb-canvas { width:64px; height:64px; display:block; z-index:3; filter:drop-shadow(0 7px 14px rgba(40,60,110,.55)); }
  .orb-halo { width:88px; height:88px; border-radius:50%; z-index:0; pointer-events:none;
    background:radial-gradient(circle, color-mix(in srgb,var(--accent) 55%,transparent) 0%, transparent 62%);
    filter:blur(5px); animation:orbHalo 3.2s ease-in-out infinite; }
  .orb-ring { width:78px; height:78px; border-radius:50%; z-index:1; pointer-events:none; opacity:.8;
    background:conic-gradient(from 0deg, transparent 0deg, color-mix(in srgb,var(--accent) 85%,#fff) 60deg, transparent 150deg, transparent 230deg, color-mix(in srgb,var(--accent) 65%,#fff) 300deg, transparent 360deg);
    -webkit-mask:radial-gradient(circle, transparent 0 60%, #000 61%); mask:radial-gradient(circle, transparent 0 60%, #000 61%);
    animation:orbSpin 5.5s linear infinite; }
  @keyframes orbHalo { 0%,100%{ opacity:.5; transform:scale(.95);} 50%{ opacity:.95; transform:scale(1.07);} }
  @keyframes orbSpin { to { transform:rotate(360deg); } }
  .assist-btn:hover .orb-ring { animation-duration:2.6s; opacity:1; }
  .assist-btn:hover .orb-halo { animation-duration:1.8s; }
  #assistBtnImg { display:none; }
  .assist-btn.ischar .orb-canvas, .assist-btn.ischar .orb-ring { display:none; }
  .assist-btn.ischar .orb-halo { opacity:.45; }
  .assist-btn.ischar #assistBtnImg { display:block; }
  .assist-hidden { display:none !important; }
  #assistPanel { position:absolute; right:0; bottom:80px; width:360px; max-width:calc(100vw - 40px);
    height:480px; max-height:calc(100vh - 150px); display:flex; flex-direction:column; overflow:hidden;
    border-radius:22px; background:var(--card); border:1px solid var(--border);
    box-shadow:var(--rim), var(--e4); animation:riseIn .32s cubic-bezier(.2,.75,.3,1) both; }
  .assist-head { display:flex; align-items:center; gap:11px; padding:14px 16px; border-bottom:1px solid var(--border);
    background:var(--face), var(--card); }
  .assist-orb-sm { width:38px; height:38px; border-radius:50%; }
  .assist-htext { flex:1; } .assist-title { font-weight:800; font-size:14.5px; color:var(--ink); }
  .assist-sub { font-size:11px; color:var(--ink3); }
  .assist-x { border:0; background:transparent; color:var(--ink3); font-size:14px; cursor:pointer; padding:4px 7px; border-radius:8px; }
  .assist-x:hover { background:var(--card2); color:var(--ink); }
  .assist-snd { border:0; background:transparent; cursor:pointer; padding:4px 7px; border-radius:8px;
    display:grid; place-items:center; color:var(--accent-deep); }
  .assist-snd:hover { background:var(--card2); }
  .assist-snd svg { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.9; stroke-linecap:round; stroke-linejoin:round; }
  .assist-snd .snd-off { display:none; }
  .assist-snd.muted { color:var(--ink3); }
  .assist-snd.muted .snd-on { display:none; }
  .assist-snd.muted .snd-off { display:block; }
  .assist-msgs { flex:1; overflow-y:auto; padding:14px; display:flex; flex-direction:column; gap:10px; }
  .assist-msg { max-width:84%; padding:9px 13px; border-radius:15px; font-size:13px; line-height:1.45; }
  .assist-msg.me { align-self:flex-end; background:linear-gradient(180deg,#FFFFFF,var(--accent-soft) 90%);
    border:1px solid var(--border); color:var(--ink); box-shadow:var(--rim); }
  .assist-msg.muse { align-self:flex-start; background:var(--card2); border:1px solid var(--border); color:var(--ink2); }
  .assist-msg.typing { color:var(--ink3); font-style:italic; }
  .assist-input { display:flex; align-items:flex-end; gap:7px; padding:11px 12px; border-top:1px solid var(--border); }
  .assist-input textarea { flex:1; resize:none; max-height:100px; border:1px solid var(--border); border-radius:13px;
    padding:9px 11px; font:13px/1.4 -apple-system,sans-serif; background:var(--card2); color:var(--ink); box-shadow:var(--well); }
  .assist-mic, .assist-send { flex:none; width:38px; height:38px; border-radius:12px; border:1px solid var(--border);
    background:var(--card); cursor:pointer; display:grid; place-content:center; box-shadow:var(--rim); color:var(--ink2); }
  .assist-mic svg { width:19px; height:19px; fill:none; stroke:currentColor; stroke-width:1.9; stroke-linecap:round; stroke-linejoin:round; }
  .assist-send { font-size:17px; font-weight:800; color:var(--accent-deep); }
  .assist-mic:hover, .assist-send:hover { background:var(--card2); color:var(--ink); }
  .assist-mic.rec { background:var(--bad); color:#fff; border-color:var(--bad); animation:pulseRec 1s infinite; }
  @keyframes pulseRec { 0%,100%{opacity:1} 50%{opacity:.55} }
  .assist-chips { display:flex; flex-wrap:wrap; gap:6px; padding:0 12px 10px; }
  .assist-chips:empty { display:none; }
  .assist-chip { border:1px solid var(--border); background:var(--card2); color:var(--ink2);
    border-radius:99px; padding:6px 11px; font-size:11.5px; font-weight:600; cursor:pointer; box-shadow:var(--rim); }
  .assist-chip:hover { background:var(--card); color:var(--ink); }
  /* orb morphs into the crew member who owns the answer */
  @keyframes morphPop { 0%{transform:scale(1) rotate(0); opacity:1} 42%{transform:scale(.26) rotate(-28deg); opacity:.12}
    100%{transform:scale(1) rotate(0); opacity:1} }
  #assistOrbSm.morphing, #assistBtnImg.morphing { animation:morphPop .52s cubic-bezier(.3,.8,.3,1); }
  /* a crew member steps OUT of the bubble — full figure, no circle crop */
  .assist-btn.ischar { background:transparent; border:0; box-shadow:none; width:auto; height:90px; border-radius:0; }
  .assist-btn.ischar img { width:auto; height:90px; border-radius:0; filter:drop-shadow(0 9px 13px rgba(70,90,130,.42)); }
  #assistOrbSm.ischar { width:34px; height:46px; margin:-5px 2px -5px 0; border-radius:7px; object-fit:contain;
    object-position:bottom; background:transparent; }
  /* a crew member "talks" — gentle bob/squash in rhythm while their voice plays */
  @keyframes talkBob { 0%,100%{transform:translateY(0) scaleY(1) scaleX(1);} 22%{transform:translateY(-3px) scaleY(1.05) scaleX(.98);}
    48%{transform:translateY(1px) scaleY(.95) scaleX(1.03);} 74%{transform:translateY(-2px) scaleY(1.03) scaleX(.99);} }
  /* character talk-bounce removed per preference — the orb's own speak animation stays */
  #assistBtnImg.speaking, #assistOrbSm.speaking { animation:none; }
  @media (max-width:640px){ #assistPanel { width:calc(100vw - 32px); height:62vh; } }
  pre#sbText { white-space:pre-wrap; font:12px/1.6 ui-monospace,monospace; background:var(--card2);
       border:1px solid var(--border); border-radius:12px; padding:14px 16px; max-height:340px;
       overflow-y:auto; margin:10px 0 0; display:none; }
  .spark { stroke:var(--accent); stroke-width:2; fill:none; }
  .sparkfill { fill:#5B7CFA22; stroke:none; }
  .metricrow { display:flex; align-items:center; gap:14px; padding:10px 0; border-bottom:1px solid var(--border); }
  /* Muse Wonder analyst */
  .analystbox { font-size:13.5px; line-height:1.65; color:var(--ink2); padding:15px 17px;
    background:var(--card2); border:1px solid var(--border); border-radius:12px; }
  .analystbox b { color:var(--ink); }
  /* watch-through insights */
  .watchlead { font-size:13.5px; color:var(--ink2); margin-bottom:12px; padding:10px 14px;
    background:var(--card2); border:1px solid var(--border); border-radius:11px; }
  .watchlead b { color:var(--ink); }
  .watchrow { padding:9px 0; border-bottom:1px solid var(--border); }
  .wtitle { font-size:13px; font-weight:600; color:var(--ink); display:flex; justify-content:space-between;
    gap:12px; flex-wrap:wrap; }
  .wtitle .wmeta { font-size:11px; color:var(--ink3); font-weight:500; }
  .wbarwrap { position:relative; height:17px; background:var(--card2); border:1px solid var(--border);
    border-radius:9px; margin-top:6px; overflow:hidden; }
  .wbar { position:absolute; left:0; top:0; height:100%; border-radius:9px 0 0 9px; transition:width .5s ease; }
  .wpct { position:absolute; right:9px; top:0; line-height:17px; font-size:10.5px; font-weight:700; color:var(--ink2); }
  .watchrow.whidden { display:none; }
  #watchWrap.allshown .watchrow.whidden { display:block; }
  .watchmore { display:block; margin:12px auto 2px; background:var(--card2); border:1px solid var(--border);
    color:var(--ink2); font-size:12px; font-weight:700; padding:7px 18px; border-radius:999px; cursor:pointer;
    transition:border-color .12s, color .12s; }
  .watchmore:hover { border-color:var(--accent); color:var(--ink); }
  /* calendar */
  .calhead { display:flex; align-items:center; gap:12px; margin-bottom:14px; }
  .calhead #calLabel { font-size:17px; font-weight:800; color:var(--ink); min-width:150px; }
  .calhead .btn.ghost { padding:4px 12px; font-size:16px; line-height:1; }
  .callegend { display:flex; align-items:center; gap:7px; font-size:11px; color:var(--ink3); margin-left:8px; }
  .legdot { width:10px; height:10px; border-radius:3px; display:inline-block; margin-left:10px; }
  .legdot.scheduled { background:#5a82ff; } .legdot.published { background:#3a9a5a; } .legdot.story { background:#b06ad0; }
  .calgrid { border:1px solid var(--border); border-radius:12px; overflow:hidden; background:var(--card); }
  .caldows { display:grid; grid-template-columns:repeat(7,1fr); background:var(--card2); border-bottom:1px solid var(--border); }
  .caldow { text-align:center; font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:.5px;
    color:var(--ink3); padding:7px 0; }
  .calcells { display:grid; grid-template-columns:repeat(7,1fr); }
  .calcell { min-height:92px; border-right:1px solid var(--border); border-bottom:1px solid var(--border);
    padding:5px 5px 7px; cursor:pointer; transition:background .15s; position:relative; }
  .calcell:nth-child(7n) { border-right:0; }
  .calcell.blank { background:var(--card2); cursor:default; }
  .calcell.has:hover { background:var(--card2); }
  .calcell.today { background:rgba(90,130,255,.07); }
  .calcell.today .caldnum { background:var(--accent); color:#fff; border-radius:50%; width:20px; height:20px;
    display:inline-flex; align-items:center; justify-content:center; }
  .calcell.sel { box-shadow:inset 0 0 0 2px var(--accent); }
  .caldnum { font-size:11.5px; font-weight:700; color:var(--ink2); margin-bottom:4px; display:inline-block; }
  .calchip { font-size:10px; font-weight:600; padding:2px 6px; border-radius:5px; margin-bottom:3px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#fff; }
  .calchip.scheduled, .calchip.approved, .calchip.dry_scheduled { background:#5a82ff; }
  .calchip.published { background:#3a9a5a; }
  .calchip.story { background:#b06ad0; }
  .calmore { font-size:9.5px; color:var(--ink3); font-weight:600; padding-left:2px; }
  .calday { margin-top:16px; }
  .caldayhd { font-size:14px; font-weight:800; color:var(--ink); margin:0 0 10px; }
  .caldrow { display:flex; gap:12px; align-items:center; padding:9px 0; border-bottom:1px solid var(--border); }
  .caldthumb { width:54px; height:72px; object-fit:cover; border-radius:8px; border:1px solid var(--border); flex:none; }
  .caldthumb.ph { background:var(--card2); }
  .caldmeta { flex:1; min-width:0; }
  .caldtitle { font-size:13px; font-weight:600; color:var(--ink); margin-bottom:5px; }
  .caldsub { display:flex; align-items:center; gap:9px; }
  .caldsub .calchip { display:inline-block; text-transform:capitalize; }
  .caldwhen { font-size:11px; color:var(--ink3); }
  .caldlink { font-size:12px; font-weight:700; color:var(--accent); text-decoration:none; flex:none; }
  /* direct messages */
  .dmmodes { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:12px; }
  .dmmode { font-size:12px; color:var(--ink2); display:flex; align-items:center; gap:7px; }
  .dmmode select { background:var(--card2); color:var(--ink); border:1px solid var(--border);
    border-radius:8px; padding:5px 9px; font-size:12px; font-weight:600; }
  .dmrow { display:flex; gap:10px; padding:10px 0; border-bottom:1px solid var(--border); align-items:flex-start; }
  .dmb { font-size:9.5px; font-weight:800; text-transform:uppercase; letter-spacing:.4px;
    padding:3px 7px; border-radius:6px; flex:none; margin-top:1px; }
  .dmb.rep { background:#dff3e6; color:#1b7a3f; } .dmb.flag { background:#fdecd8; color:#9a6212; }
  .dmbody { flex:1; min-width:0; }
  .dmtop { font-size:13px; color:var(--ink); } .dmcat { font-size:10px; color:var(--ink3);
    text-transform:uppercase; letter-spacing:.4px; margin-left:7px; }
  .dmin { font-size:12.5px; color:var(--ink2); margin-top:2px; }
  .dmrep { font-size:12.5px; color:var(--ink); margin-top:3px; }
  /* monetization cockpit */
  .monprog { padding:12px 15px; border:1px solid var(--border); border-radius:12px; margin-bottom:10px; background:var(--card); }
  .monprog.ok { border-color:#bfe3cb; }
  .monname { font-size:14px; font-weight:700; color:var(--ink); margin-bottom:9px; display:flex; align-items:center; flex-wrap:wrap; }
  .monbadge { font-size:10px; font-weight:800; color:#1b7a3f; background:#dff3e6; padding:2px 8px; border-radius:6px; margin-left:8px; }
  .monnote { font-size:11px; color:var(--ink3); font-weight:500; margin-left:8px; }
  .monreq { margin:8px 0; }
  .monreqtop { display:flex; justify-content:space-between; font-size:12px; color:var(--ink2); margin-bottom:4px; }
  .monbarwrap { height:9px; background:var(--card2); border:1px solid var(--border); border-radius:5px; overflow:hidden; }
  .monbar { height:100%; border-radius:5px; transition:width .5s ease; }
  /* programs you've already qualified for collapse into one tidy chip row */
  .monelig { display:flex; align-items:center; flex-wrap:wrap; gap:8px; padding:11px 15px;
    border:1px solid #bfe3cb; border-radius:12px; background:var(--card); margin-bottom:10px; }
  .moneliglabel { font-size:10px; font-weight:800; letter-spacing:.05em; text-transform:uppercase;
    color:var(--ink3); margin-right:2px; }
  .monchip { font-size:12.5px; font-weight:700; color:#1b7a3f; background:#dff3e6;
    border:1px solid #bfe3cb; padding:4px 11px; border-radius:999px; }
  .monchip b { color:#1b7a3f; font-weight:800; }
  /* per-page monetization filter: collective (All) or a checked-off subset */
  .monpages { display:flex; align-items:center; flex-wrap:wrap; gap:7px; margin:0 0 13px; }
  .monpageslabel { font-size:10px; font-weight:800; letter-spacing:.05em; text-transform:uppercase;
    color:var(--ink3); margin-right:2px; }
  .monpagepill { font-size:12px; font-weight:700; color:var(--ink2); background:var(--card2);
    border:1px solid var(--border); padding:5px 12px; border-radius:999px; cursor:pointer;
    transition:background .12s ease, border-color .12s ease, color .12s ease; }
  .monpagepill:hover { border-color:var(--accent); color:var(--ink); }
  .monpagepill.on { color:#fff; background:var(--accent); border-color:var(--accent); }
  .metricrow:last-child { border-bottom:0; }
  .metricrow .mn { width:180px; font-size:13px; color:var(--ink2); font-weight:600; }
  .metricrow .mv { width:90px; font-size:19px; font-weight:750; font-variant-numeric:tabular-nums; }
  .metricrow .md { width:80px; font-size:12px; font-weight:700; }
  /* Koa's books ledger: ONE grid for every row so the columns line up even when labels wrap */
  #budgetWrap .ledgergrid { display:grid; grid-template-columns:minmax(58px,1fr) auto auto auto; column-gap:10px; }
  #budgetWrap .lgc { display:flex; align-items:center; padding:11px 0; border-bottom:1px solid var(--border); }
  #budgetWrap .ledgergrid > .lgc:nth-last-child(-n+4) { border-bottom:0; }
  #budgetWrap .lgc-mn { font-size:13px; color:var(--ink2); font-weight:600; }
  #budgetWrap .lgc-mv { font-size:19px; font-weight:750; font-variant-numeric:tabular-nums; }
  #budgetWrap .lgc-md { font-size:12px; font-weight:700; white-space:nowrap; }
  #budgetWrap .lgc-30 { font-size:12px; color:var(--ink2); white-space:nowrap; justify-content:flex-end; }
  ::-webkit-scrollbar { width:9px; } ::-webkit-scrollbar-thumb { background:#DCCAB2; border-radius:99px; }
  @media (max-width:860px){
    body { padding:0; }
    .app { flex-direction:column; height:auto; min-height:100vh; max-width:100%; margin:0;
      border-radius:0; border:0; box-shadow:none; }
    .content { padding:66px 13px 96px; perspective:none; overflow-x:hidden; }
    /* --- mobile fit: keep wide rows inside the phone frame --- */
    .metricrow { gap:9px; }
    .metricrow .mn { width:auto; flex:1 1 0; min-width:0; }
    .metricrow .mv, .metricrow .md { width:auto; flex:0 0 auto; }
    .metricrow svg { flex:0 0 88px; width:88px; height:34px; }
    .calhead { flex-wrap:wrap; gap:8px 10px; }
    .calhead #calLabel { min-width:0; font-size:15px; }
    .callegend { margin-left:0; flex-basis:100%; }
    .caldows, .calcells { grid-template-columns:repeat(7, minmax(0,1fr)); }
    .calcell { min-height:62px; padding:4px 2px; }
    .calchip, .calmore { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:100%; display:block; }
    /* top strip: brushed-chrome bar — Settings (left) · abra wordmark (center) · Account (right) */
    .mtopbar { display:flex; position:fixed; top:0; left:0; right:0; height:52px; z-index:600;
      align-items:center; justify-content:flex-start; gap:6px;
      padding:env(safe-area-inset-top) 12px 0;
      background:var(--chrometex);
      border-bottom:1px solid var(--border);
      box-shadow:inset 0 1px 0 rgba(255,255,255,.9), 0 6px 18px -12px rgba(var(--sh),.55); }
    .mtoplogo { height:22px; width:auto; flex:0 0 auto; filter:drop-shadow(0 1px 2px rgba(90,102,124,.4)); }
    .mtoplogo-btn { flex:0 0 auto; border:0; background:transparent; padding:4px 6px; border-radius:10px; cursor:pointer; display:flex; align-items:center; -webkit-tap-highlight-color:transparent; }
    .mtoptab { flex:0 0 auto; border:0; background:transparent; color:var(--ink2); font-size:14px; font-weight:700; padding:7px 13px; border-radius:11px; cursor:pointer; white-space:nowrap; -webkit-tap-highlight-color:transparent; transition:background-color .14s, color .14s; }
    .mtoptab.active { color:var(--accent-deep); background:var(--accent-soft); box-shadow:var(--rim); }
    .mtopspacer { flex:1 1 auto; }
    .mtopbtn { flex:0 0 38px; width:38px; height:38px; display:flex; align-items:center; justify-content:center;
      border:0; background:transparent; border-radius:11px; padding:0; cursor:pointer; color:var(--ink2);
      -webkit-tap-highlight-color:transparent; transition:background-color .14s, color .14s, box-shadow .14s; }
    /* hackathon ∞ mark in the left slot — keeps the wordmark centered opposite the avatar */
    .mtophack { flex:0 0 38px; display:flex; align-items:center; justify-content:flex-start; }
    .mtophack svg { width:25px; height:13px; fill:none; stroke:#0866FF; stroke-width:3.6;
      stroke-linecap:round; stroke-linejoin:round; filter:drop-shadow(0 0 4px rgba(8,102,255,.45)); }
    .mtopbtn .ni { width:21px; height:21px; fill:none; stroke:currentColor; stroke-width:1.9;
      stroke-linecap:round; stroke-linejoin:round; }
    .mtopbtn.active { color:var(--accent-deep); background:linear-gradient(180deg,#FFFFFF,var(--accent-soft) 85%);
      box-shadow:var(--rim), 0 2px 0 var(--ledge); }
    .mtopav img, .mtopav .mtopav-init { width:30px; height:30px; border-radius:50%; object-fit:cover;
      border:1px solid var(--border); box-shadow:var(--rim); }
    .mtopav .mtopav-init { display:none; align-items:center; justify-content:center; font-size:11px;
      font-weight:800; color:var(--accent-deep); background:linear-gradient(180deg,#fff,var(--accent-soft)); }
    .mtopav.active img, .mtopav.active .mtopav-init { border-color:var(--accent);
      box-shadow:0 0 0 2px var(--accent-soft), 0 0 0 3px var(--border); }
    /* the icon rail becomes a fixed bottom tab bar, same brushed-chrome texture */
    aside { display:none; }   /* nav moved to the top bar; no bottom bar on mobile */
    .brandmark, .navdiv, .navgrp, .sidefoot { display:none; }
    /* Account moves up to the top strip, so its bottom tab is hidden on mobile */
    aside .navitem[data-v="settings"] { display:none; }
    .navitem { flex:1 1 0; min-width:0; padding:5px 1px; gap:2px; border-radius:11px; }
    .navitem .ni { width:20px; height:20px; }
    .navitem span { font-size:8px; letter-spacing:-.2px; white-space:nowrap; max-width:100%;
      overflow:hidden; text-overflow:ellipsis; }
    /* shorter mobile-only labels for the two that would truncate beside the orb */
    .navitem span[data-short] { font-size:0; letter-spacing:0; }
    .navitem span[data-short]::after { content:attr(data-short); font-size:8px; letter-spacing:-.2px; }
    /* center gap the raised orb nests into: Home·Launchpad | orb | Money·Deals (Engagement+Analytics are now Launchpad sub-tabs) */
    .navorbslot { display:block; flex:0 0 72px; min-width:72px; height:1px; }
    .tiles { grid-template-columns:repeat(2,1fr); }
    /* dock the liquid-chrome orb into the center of the tab bar, raised FAB-style.
       NB: no transform here — a transform would make the fixed panel anchor to this
       88px wrapper instead of the viewport. The orb button is 88px, so -44px centers it. */
    #assistWrap { left:auto; right:16px; transform:none;
      bottom:calc(env(safe-area-inset-bottom) + 16px); z-index:600; }
    #assistPanel { position:fixed; left:16px; right:16px; width:auto; max-width:none;
      bottom:calc(env(safe-area-inset-bottom) + 94px); z-index:1001; }
  }
  .ev { position:relative; }
  .fanbtn { position:absolute; top:10px; right:12px; text-decoration:none;
    font-size:13px; background:var(--accent-soft); border:1px solid var(--border);
    border-radius:10px; padding:4px 8px; transition:transform .15s; }
  .fanbtn:hover { transform:scale(1.15); }

  /* ---------- living mascots: free-roaming plush critters ---------- */
  /* roaming bottom mascots hidden (distracting). Top-of-page
     presenters (Nico/Koa/Sage) are separate .engpresenter/.musep and stay. */
  .critter, #mascotVoice { display:none !important; }
  .critter { position:fixed; bottom:2px; left:60px; width:92px; z-index:60;
    pointer-events:none; text-align:center; }
  .critstage { --flip:1; position:relative; width:max-content; margin:0 auto;
    transform-origin:bottom center; filter:drop-shadow(0 7px 10px rgba(0,0,0,.6));
    animation:breathe 3.4s ease-in-out infinite; }
  .critstage.flip { --flip:-1; }
  .critter img.metalimg { display:block; height:88px; width:auto; }
  .critter img.blinkimg, .critter img.walkimg, .critter img.walk2img {
    position:absolute; left:50%; bottom:0; transform:translateX(-50%);
    height:88px; width:auto; opacity:0; }
  .critstage.blinking img.blinkimg { opacity:1; }
  .critstage.blinking img.metalimg { opacity:0; }
  .critter.walking .critstage img.metalimg { opacity:0; }
  .critter.walking .critstage img.walkimg { animation:legA .5s steps(1,end) infinite; }
  .critter.walking .critstage img.walk2img { animation:legB .5s steps(1,end) infinite; }
  @keyframes legA { 0%{opacity:1} 50%{opacity:0} 100%{opacity:1} }
  @keyframes legB { 0%{opacity:0} 50%{opacity:1} 100%{opacity:0} }
  @keyframes breathe {
    0%,100%{transform:scaleX(var(--flip)) translateY(0) scale(1)}
    50% {transform:scaleX(var(--flip)) translateY(-1px) scale(1.025)} }
  /* real waddle-walk: rock side to side + bob + step squash */
  .critter.walking .critstage { animation:waddle .5s infinite ease-in-out; }
  @keyframes waddle {
    0% {transform:scaleX(var(--flip)) rotate(-3.5deg) translateY(0)}
    25% {transform:scaleX(var(--flip)) rotate(0deg) translateY(-4px)}
    50% {transform:scaleX(var(--flip)) rotate(3.5deg) translateY(0) scale(1.01,.97)}
    75% {transform:scaleX(var(--flip)) rotate(0deg) translateY(-4px)}
    100%{transform:scaleX(var(--flip)) rotate(-3.5deg) translateY(0)} }
  .critter .critstage.hop { animation:hop .55s cubic-bezier(.3,1.4,.6,1); }
  @keyframes hop {
    0%,100%{transform:scaleX(var(--flip)) translateY(0) scale(1)}
    30% {transform:scaleX(var(--flip)) translateY(-10px) scale(.98,1.04)}
    60% {transform:scaleX(var(--flip)) translateY(0) scale(1.05,.95)} }
  .critter svg { display:block; margin:0 auto;
    filter:drop-shadow(0 5px 8px rgba(75,58,43,.22)); }
  .critter svg.flip { transform:scaleX(-1); }
  .critter .mtag { position:absolute; bottom:94px; left:50%; transform:translate(-50%,4px);
    font-size:9px; color:var(--ink2); font-weight:800; letter-spacing:.6px;
    white-space:nowrap; padding:3px 8px; border-radius:7px;
    background:linear-gradient(180deg,rgba(32,34,40,.96),rgba(18,19,23,.96));
    border:1px solid rgba(255,255,255,.1); box-shadow:0 3px 9px rgba(0,0,0,.5);
    opacity:0; pointer-events:none; transition:opacity .16s ease, transform .16s ease; z-index:70; }
  .critter:hover .mtag { opacity:1; transform:translate(-50%,0); }
  .legA, .legB { transform-box:fill-box; transform-origin:top center; }
  .critter.walking .legA { animation:legswing .38s infinite alternate ease-in-out; }
  .critter.walking .legB { animation:legswing .38s infinite alternate-reverse ease-in-out; }
  .critter.walking .body { animation:trot .38s infinite ease-in-out;
    transform-box:fill-box; transform-origin:center; }
  .tailwag { transform-box:fill-box; transform-origin:bottom left;
    animation:tailwag 2.4s infinite ease-in-out; }
  .critter .earL, .critter .earR { transform-box:fill-box; transform-origin:bottom center; }
  .critter.talking .earL { animation:earflick 1.2s ease-in-out; }
  @keyframes legswing { from{transform:rotate(16deg)} to{transform:rotate(-16deg)} }
  @keyframes trot { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-3px)} }
  @keyframes tailwag { 0%,100%{transform:rotate(0)} 50%{transform:rotate(16deg)} }
  @keyframes earflick { 0%,100%{transform:rotate(0)} 30%{transform:rotate(-12deg)} 60%{transform:rotate(6deg)} }
  .critter .bubble { position:absolute; bottom:102px; left:50%; transform:translate(-50%,6px);
    min-width:140px; max-width:230px; background:#FFFDF8; border:1.5px solid var(--border);
    color:var(--ink); font-size:12px; line-height:1.45; padding:8px 12px;
    border-radius:16px 16px 16px 5px; opacity:0; transition:all .25s;
    box-shadow:0 6px 16px rgba(75,58,43,.12); z-index:61; }
  .critter .bubble.show { opacity:1; transform:translate(-50%,0); }
  .critter .prop { position:absolute; top:-6px; right:-4px; font-size:22px; opacity:0;
    transition:opacity .25s, transform .3s; transform:scale(.4); }
  .critter .prop.show { opacity:1; transform:scale(1); animation:propbob 1.4s infinite ease-in-out; }
  @keyframes propbob { 0%,100%{margin-top:0} 50%{margin-top:-5px} }
  .critter svg { pointer-events:auto; cursor:pointer; }
  /* ---------- money grove: top-earning posts as trees ---------- */
  #grove { display:none; }
  .tree { position:absolute; bottom:0; pointer-events:auto; cursor:pointer;
    text-align:center; transition:transform .2s; }
  .tree:hover { transform:scale(1.07); }
  .tree .rank { font-size:9px; font-weight:800; color:var(--ink3); letter-spacing:.4px; }
  .tree .grow { font-size:9px; color:var(--good); font-weight:700; }
  .tree .canopy-g { transform-box:fill-box; transform-origin:bottom center; }
  .tree.tended .canopy-g { animation:treeshake .9s ease; }
  .tree .spark { position:absolute; top:-12px; left:50%; transform:translateX(-50%);
    font-size:15px; opacity:0; pointer-events:none; }
  .tree.tended .spark { animation:sparkle 1.6s ease; }
  @keyframes treeshake { 0%,100%{transform:rotate(0)} 25%{transform:rotate(-5deg)}
    60%{transform:rotate(4deg)} }
  @keyframes sparkle { 0%{opacity:0; margin-top:8px} 30%{opacity:1} 100%{opacity:0; margin-top:-12px} }
  .mpanel { position:fixed; z-index:70; width:340px;
    background:#FFFDF8; border:1.5px solid var(--border); border-radius:18px;
    box-shadow:0 14px 34px rgba(75,58,43,.18); padding:16px 18px; display:none; }
  .mpanel.show { display:block; animation:ledgerpop .22s ease; }
  .mpanel h3 { margin:0 0 10px; font-size:14px; display:flex; justify-content:space-between; }
  .mpanel h3 button { border:0; background:none; cursor:pointer; color:var(--ink3); font-size:14px; }
  .mpanel .note { color:var(--ink3); font-size:10.5px; margin-top:8px; }
  .mpanel .logrow { font-size:12px; border-top:1px solid var(--card2); padding:6px 0; }
  .mpanel .logrow .c { color:var(--ink2); }
  .mpanel .logrow .r { color:var(--reply); margin-top:2px; }
  .mpanel .bigstat { display:flex; gap:18px; margin-bottom:8px; }
  .mpanel .bigstat b { font-size:20px; display:block; }
  .mpanel .bigstat span { font-size:10.5px; color:var(--ink3); letter-spacing:.4px; text-transform:uppercase; }
  #catPanel { left:240px; bottom:118px; } #dogPanel { right:120px; bottom:118px; }
  #ledgerPanel { position:fixed; right:18px; bottom:118px; z-index:70; width:340px;
    background:#FFFDF8; border:1.5px solid var(--border); border-radius:18px;
    box-shadow:0 14px 34px rgba(75,58,43,.18); padding:16px 18px; display:none; }
  #ledgerPanel.show { display:block; animation:ledgerpop .22s ease; }
  @keyframes ledgerpop { from{opacity:0; transform:translateY(8px)} to{opacity:1} }
  #ledgerPanel h3 { margin:0 0 10px; font-size:14px; display:flex; justify-content:space-between; }
  #ledgerPanel h3 button { border:0; background:none; cursor:pointer; color:var(--ink3); font-size:14px; }
  #ledgerPanel table { width:100%; border-collapse:collapse; font-size:12.5px; }
  #ledgerPanel th { text-align:right; color:var(--ink3); font-size:10.5px; letter-spacing:.4px;
    text-transform:uppercase; padding:4px 0; }
  #ledgerPanel th:first-child, #ledgerPanel td:first-child { text-align:left; }
  #ledgerPanel td { text-align:right; padding:5px 0; border-top:1px solid var(--card2); }
  #ledgerPanel tr.tot td { border-top:1.5px solid var(--border); font-weight:800; }
  #ledgerPanel .note { color:var(--ink3); font-size:10.5px; margin-top:8px; }
  #mascotVoice { position:fixed; right:14px; bottom:12px; z-index:62;
    border:1.5px solid var(--border); background:var(--card); color:var(--ink2);
    border-radius:12px; width:34px; height:34px; cursor:pointer;
    box-shadow:0 3px 8px rgba(75,58,43,.1); }
  /* ================= 3D PLUSH LAYER =================
     One warm light straight overhead (so the mascots' walk-flip never swaps the lit side).
     Pure overrides by source order: nothing above is edited. No backslashes, no triple double-quotes. */
  :root {
    --sh:120,132,156; /* cool steel shadow on silver */
    --ledge:#B2BBCB; /* light metal thickness under raised pieces */
    --edge-top:#FFFFFF; --edge-bot:#AEB8C8; /* bevels: bright chrome top, silver bottom */
    --lip-accent:#AAB3C4; --lip-green:#7FB98C; --lip-ghost:#C4CCD9;
    --accent-deep:#6E7889; --good-deep:#328A54;
    --rim:inset 0 1px 0 rgba(255,255,255,.95);
    --puff:inset 0 10px 16px -10px rgba(255,255,255,.75);
    --tuck:inset 0 -10px 16px -10px rgba(120,135,160,.28);
    /* neumorphic dual shadows — dark bottom-right + light top-left = soft extrusion */
    --e1:3px 3px 7px rgba(var(--sh),.20), -3px -3px 6px rgba(255,255,255,.72);
    --e2:5px 5px 12px rgba(var(--sh),.24), -5px -5px 11px rgba(255,255,255,.80);
    --e3:7px 7px 17px rgba(var(--sh),.28), -6px -6px 14px rgba(255,255,255,.85);
    --e4:11px 11px 26px rgba(var(--sh),.30), -9px -9px 20px rgba(255,255,255,.88);
    --inset:inset 3px 3px 6px rgba(var(--sh),.22), inset -3px -3px 6px rgba(255,255,255,.80);
    --well:inset 0 2px 4px -1px rgba(130,145,170,.45), inset 0 1px 0 rgba(130,145,170,.22), 0 1px 0 rgba(255,255,255,.85);
    --sunk:inset 0 2px 6px -1px rgba(130,145,170,.5), inset 0 1px 0 rgba(130,145,170,.28), 0 1px 0 rgba(255,255,255,.75);
    --row:inset 0 1px 0 rgba(255,255,255,.9), inset 0 -2px 0 rgba(170,182,200,.35), 0 2px 0 #C6CEDA, 0 3px 6px -3px rgba(120,135,160,.40);
    --row-hover:inset 0 1px 0 rgba(255,255,255,.98), inset 0 -2px 0 rgba(170,182,200,.35), 0 3px 0 #C6CEDA, 0 8px 14px -8px rgba(120,135,160,.5);
    --face:linear-gradient(180deg,rgba(255,255,255,.78) 0,rgba(255,255,255,0) 96px);
    --face-sm:linear-gradient(180deg,rgba(255,255,255,.62) 0,rgba(255,255,255,0) 70%);
  }
  /* ================= DARK THEME (deep blue) ================= */
  html[data-theme="dark"]{
    --bg:#0D1526; --card:#1A2740; --card2:#121C30; --border:#243249;
    --ink:#EDF2FB; --ink1:#F7FAFF; --ink2:#B6C3DA; --ink3:#8C9BB7; --kpinum:#6FA8FF;
    --accent:#5E90E6; --accent-soft:#223252; --accent-deep:#9CBEF4;
    --reply:#7FA4E8; --reply-bg:#1E2B45; --like:#D98FC0; --like-bg:#36263A;
    --skip:#6C7C99; --skip-bg:#1A2234;
    --good:#4FC07E; --warn:#DBB34E; --bad:#EA726A; --good-deep:#4FC07E;
    --sh:3,7,16; --ledge:#0A1222;
    --mercury:linear-gradient(135deg,#2A3A58,#131D31 70%);
    --chrome:linear-gradient(135deg,#2A3A58,#131D31);
    --chrometex:linear-gradient(110deg, rgba(255,255,255,.06) 0%, transparent 42%, rgba(255,255,255,.045) 72%, transparent 100%), linear-gradient(180deg, rgba(40,56,88,.55), rgba(16,26,46,.55)), #121C32;
    --rim:inset 0 1px 0 rgba(255,255,255,.085);
    --puff:inset 0 10px 18px -10px rgba(255,255,255,.085);
    --tuck:inset 0 -10px 18px -10px rgba(0,0,0,.45);
    --e1:3px 3px 9px rgba(0,0,0,.52), -2px -2px 7px rgba(255,255,255,.04);
    --e2:6px 6px 16px rgba(0,0,0,.58), -4px -4px 12px rgba(255,255,255,.05);
    --e3:9px 9px 24px rgba(0,0,0,.64), -6px -6px 16px rgba(255,255,255,.055);
    --e4:15px 15px 38px rgba(0,0,0,.72), -9px -9px 24px rgba(255,255,255,.06);
    --inset:inset 4px 4px 11px rgba(0,0,0,.62), inset -3px -3px 8px rgba(255,255,255,.045);
    --well:inset 0 2px 6px -1px rgba(0,0,0,.6), inset 0 1px 0 rgba(255,255,255,.055);
    --sunk:inset 0 3px 8px -1px rgba(0,0,0,.64), inset 0 1px 0 rgba(255,255,255,.055);
    --row:inset 0 1px 0 rgba(255,255,255,.06), 0 2px 7px -3px rgba(0,0,0,.55);
    --row-hover:inset 0 1px 0 rgba(255,255,255,.1), 0 8px 16px -7px rgba(0,0,0,.66);
    --face:linear-gradient(180deg,rgba(255,255,255,.07) 0,rgba(255,255,255,0) 120px);
    --face-sm:linear-gradient(180deg,rgba(255,255,255,.06) 0,rgba(255,255,255,0) 72%);
  }
  html[data-theme="dark"] body{ background:#090E18;
    background-image:
      radial-gradient(1200px 760px at 78% -8%, rgba(40,70,130,.40), transparent 60%),
      radial-gradient(900px 600px at 12% 108%, rgba(20,34,60,.55), transparent 55%),
      linear-gradient(135deg,#0C1424 0%,#0A1120 50%,#080D18 100%); }
  html[data-theme="dark"] .navitem.active,
  html[data-theme="dark"] .pgfilt.active,
  html[data-theme="dark"] .mtopbtn.active,
  html[data-theme="dark"] .subtab.active{ background:linear-gradient(180deg,#26344F,#1A2740) !important; color:var(--accent-deep) !important; }
  html[data-theme="dark"] .mpanel,
  html[data-theme="dark"] #ledgerPanel,
  html[data-theme="dark"] .critter .bubble,
  html[data-theme="dark"] #mascotVoice{ background:linear-gradient(180deg,#1E2C47,#141F36) !important; }
  html[data-theme="dark"] .tile{ background-image:linear-gradient(180deg,rgba(255,255,255,.03) 0,transparent 70%); }
  html[data-theme="dark"] .btn.ghost { background:var(--card2); color:var(--ink2); border:1px solid var(--border); text-shadow:none; box-shadow:var(--rim), 0 2px 6px -3px rgba(0,0,0,.5); }
  html[data-theme="dark"] .btn.ghost:hover { background:var(--card); border-color:var(--accent-deep); box-shadow:var(--rim), 0 4px 10px -4px rgba(0,0,0,.6); }
  html[data-theme="dark"] .btn.ghost:active { box-shadow:var(--inset); }
  html[data-theme="dark"] .btn, html[data-theme="dark"] .btn.green { text-shadow:0 1px 1px rgba(0,0,0,.35); box-shadow:inset 0 1px 0 rgba(255,255,255,.14), 0 2px 0 rgba(0,0,0,.45), 0 7px 14px -6px rgba(0,0,0,.55); }
  html[data-theme="dark"] .btn:active, html[data-theme="dark"] .btn.green:active { box-shadow:inset 0 2px 5px rgba(0,0,0,.5); }
  html[data-theme="dark"] .tab:not(.active):hover { color:var(--ink); background:var(--card); box-shadow:var(--rim); }
  html[data-theme="dark"] .tab.active { text-shadow:0 1px 1px rgba(0,0,0,.35); box-shadow:inset 0 1px 0 rgba(255,255,255,.14), 0 2px 0 rgba(0,0,0,.45), 0 4px 8px -3px rgba(0,0,0,.5); }
  html[data-theme="dark"] #pdfMonth + button { background:var(--card2); color:var(--ink2); border:1px solid var(--border); text-shadow:none; box-shadow:var(--rim), 0 2px 6px -3px rgba(0,0,0,.5); }
  html[data-theme="dark"] #engBtn { text-shadow:none; box-shadow:0 2px 0 rgba(0,0,0,.4), 0 5px 11px -4px rgba(0,0,0,.5); }
  html[data-theme="dark"] .assist-msg.me{ background:linear-gradient(180deg,#26344F,#1A2740); color:var(--ink); }
  html[data-theme="dark"] .slot.missed{ background:#3A2420; border-color:#6B3A33; }
  html[data-theme="dark"] .monchip{ background:#17311F; border-color:#285138; color:#7FD79E; }
  html[data-theme="dark"] .monchip b{ color:#7FD79E; }
  html[data-theme="dark"] .monelig{ border-color:#285138; }
  /* streamline: give the light-mode plush treatments their missing dark overrides so nothing glows bright on #0D1526 */
  html[data-theme="dark"] .meter{ background:var(--card2); box-shadow:inset 0 1px 2px rgba(0,0,0,.45); }
  html[data-theme="dark"] .meter i{ background:var(--accent); box-shadow:none; }
  html[data-theme="dark"] .metricrow{ box-shadow:none; }
  html[data-theme="dark"] .slot.done{ background:#17311F; border-color:#285138; box-shadow:var(--rim), var(--e1); }
  html[data-theme="dark"] .slot.done .sl{ color:var(--good); }
  html[data-theme="dark"] .slot.missed .sl{ color:var(--bad); }
  html[data-theme="dark"] .video input{ background:rgba(16,24,40,.72); }
  html[data-theme="dark"] .dmb.rep{ background:#17311F; color:#7FD79E; }
  html[data-theme="dark"] .dmb.flag{ background:#33280F; color:#E0B457; }
  html[data-theme="dark"] .monbadge{ background:#17311F; color:#7FD79E; }
  html[data-theme="dark"] .monprog.ok{ border-color:#285138; }
  html[data-theme="dark"] ::-webkit-scrollbar-thumb{ background:linear-gradient(180deg,#2C3C5A,#1A2740); box-shadow:inset 0 1px 0 rgba(255,255,255,.06); }
  html[data-theme="dark"] input[type=text], html[data-theme="dark"] input[type=email],
  html[data-theme="dark"] select, html[data-theme="dark"] textarea, html[data-theme="dark"] .sc-in{ color:var(--ink); }
  /* kill the light-theme engraving so dark text stays crisp + legible */
  html[data-theme="dark"] .content, html[data-theme="dark"] .content *,
  html[data-theme="dark"] aside, html[data-theme="dark"] aside *,
  html[data-theme="dark"] .mtopbar, html[data-theme="dark"] .mtopbar *,
  html[data-theme="dark"] .mpanel, html[data-theme="dark"] .mpanel *{ text-shadow:none !important; }
  /* theme segmented toggle */
  .themeseg{ display:inline-flex; gap:4px; padding:4px; background:var(--card2); border:1px solid var(--border); border-radius:12px; box-shadow:var(--inset); }
  .themeopt{ border:0; background:transparent; color:var(--ink2); font-size:13px; font-weight:700; padding:8px 18px; border-radius:9px; cursor:pointer; transition:background .14s ease, color .14s ease; }
  .themeopt.active{ background:var(--card); color:var(--ink); box-shadow:var(--rim), var(--e1); }
  /* custom reaction uploader */
  .rxrow { display:flex; gap:12px; align-items:stretch; flex-wrap:wrap; max-width:540px; }
  .rxupload { flex:1 1 300px; display:flex; align-items:center; gap:13px; padding:14px 16px; border-radius:16px;
    border:1.5px dashed var(--border); background:var(--card2); box-shadow:var(--inset); cursor:pointer;
    transition:border-color .16s ease, background .16s ease, transform .1s ease; }
  .rxupload:hover { border-color:var(--accent); background:var(--card); }
  .rxupload.drag { border-color:var(--accent); background:var(--card); transform:scale(1.012); }
  .rxupload.has { border-style:solid; border-color:var(--accent); }
  .rxup-ic { width:42px; height:42px; flex:none; border-radius:12px; display:flex; align-items:center; justify-content:center;
    background:var(--accent-soft); color:var(--accent-deep); box-shadow:var(--rim); }
  .rxup-ic svg { width:23px; height:23px; fill:none; stroke:currentColor; stroke-width:1.7; stroke-linecap:round; stroke-linejoin:round; }
  .rxup-txt { flex:1; min-width:0; }
  .rxup-txt b { display:block; font-size:14px; color:var(--ink); font-weight:700; }
  .rxup-txt span { font-size:11.5px; color:var(--ink3); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; display:block; }
  .rxup-prev { width:46px; height:46px; border-radius:11px; object-fit:cover; flex:none; display:none; border:1px solid var(--border); box-shadow:var(--rim); }
  .rxup-prev.show { display:block; animation:rxPop .32s cubic-bezier(.3,1.3,.5,1); }
  @keyframes rxPop { from{opacity:0; transform:scale(.7)} to{opacity:1; transform:scale(1)} }
  .rxrow .rxgo { flex:0 0 auto; align-self:center; }
  body { background:
           radial-gradient(1300px 820px at 50% -8%, rgba(255,255,255,.92), transparent 64%),
           radial-gradient(1000px 680px at 86% 112%, rgba(190,204,226,.40), transparent 58%),
           linear-gradient(140deg,#F4F7FC 0%,#E6ECF5 46%,#D6DFEC 100%) fixed; }
  /* living backdrop: a slow flowing liquid-steel field behind the whole app */
  .bg-canvas { position:fixed; inset:0; width:100%; height:100%; z-index:-1; pointer-events:none; display:block; }
  .pl-defs { position:absolute; width:0; height:0; overflow:hidden; pointer-events:none; }

  /* sidebar: a lit slab casting a soft shadow onto the page */
  aside { background:var(--chrometex);
          box-shadow:inset -1px 0 0 rgba(255,255,255,.8), inset 1px 0 0 rgba(255,255,255,.45),
            10px 0 26px -16px rgba(120,135,160,.5); }
  .logo { box-shadow:inset 0 1px 1px rgba(255,255,255,.9), inset 0 -3px 5px rgba(120,135,160,.4),
          0 2px 0 #B6C0D0, 0 7px 14px -5px rgba(120,135,160,.5); }
  .navitem { transition:background-color .12s, color .12s, box-shadow .12s; } /* was transition:all, which flashed a dark outline on every view switch */
  .navitem:hover { box-shadow:var(--rim), var(--e1); }
  .navitem.active { outline:none; background:linear-gradient(180deg,#FFFFFF 0,var(--accent-soft) 70%);
          box-shadow:var(--rim), 0 2px 0 #C6CEDA, 0 6px 12px -6px rgba(120,135,160,.45); }
  .navitem:focus-visible { outline:2px solid var(--lip-accent); outline-offset:1px; }
  /* #engBtn colours are inline (paintEng): shadow only. Fallback first; the color-mix line has no var() so old browsers drop it cleanly */
  #engBtn { text-shadow:0 1px 0 rgba(255,255,255,.7);
          box-shadow:0 3px 0 rgba(112,80,50,.20), 0 6px 10px -5px rgba(112,80,50,.28), inset 0 1px 0 rgba(255,255,255,.8);
          box-shadow:0 3px 0 color-mix(in srgb, currentColor 35%, #EAD9C4), 0 6px 10px -5px rgba(112,80,50,.28), inset 0 1px 0 rgba(255,255,255,.8); }
  #engBtn:active { box-shadow:0 1px 0 rgba(112,80,50,.20), inset 0 2px 3px rgba(112,80,50,.16);
          box-shadow:0 1px 0 color-mix(in srgb, currentColor 35%, #EAD9C4), inset 0 2px 3px rgba(112,80,50,.16); }
  .pill { box-shadow:var(--well); border-color:#E0CCB3 var(--border) var(--edge-top); }
  .pill .dot { width:9px; height:9px; }
  .pill.on .dot { background:radial-gradient(circle at 50% 30%,#D6F2DA 0,#8FD39A 25%,var(--good) 60%,#3F8A4D 100%);
          box-shadow:0 0 8px #5FA86B66, 0 1px 1px rgba(40,90,50,.35); }
  .pill.off .dot { background:radial-gradient(circle at 50% 30%,#FADAD3 0,#E8877B 25%,var(--bad) 60%,#A23A2E 100%);
          box-shadow:0 1px 1px rgba(120,40,30,.35); }

  /* cushions: sections rest on the table (e2), stat tiles stand taller (e3) */
  h1.vtitle { text-shadow:0 1px 0 rgba(255,255,255,.8); }
  section { background-image:var(--face); border-color:var(--edge-top) var(--border) var(--edge-bot);
          box-shadow:var(--rim), var(--puff), var(--tuck), var(--e2); }
  section > h2 { text-shadow:0 1px 0 #fff; }
  .tile { background-image:linear-gradient(180deg,#FFFFFC 0,rgba(255,255,252,0) 70%);
          border-color:var(--edge-top) var(--border) var(--edge-bot);
          box-shadow:var(--rim), var(--puff), var(--tuck), var(--e3); }
  .tile .val, .metricrow .mv, .mpanel .bigstat b { text-shadow:0 1px 0 rgba(255,255,255,.9); }
  .meter { position:relative; height:7px; background:#CDD5E1; box-shadow:0 1px 0 rgba(255,255,255,.9); }
  .meter::after { content:''; position:absolute; inset:0; border-radius:inherit; pointer-events:none;
          box-shadow:inset 0 1px 2px rgba(var(--sh),.30); }
  .meter i { background:linear-gradient(180deg,#EAEFF6 0,var(--accent) 55%,#6E7889 100%);
          box-shadow:inset 0 1px 0 rgba(255,255,255,.6); }
  .banner { background-image:var(--face-sm); box-shadow:var(--rim), 0 2px 0 #C6CEDA, 0 8px 14px -10px rgba(120,135,160,.4); }
  .slot, .slot.done, .slot.missed { background-image:var(--face-sm); }
  .slot { border-bottom-color:var(--edge-bot); box-shadow:var(--rim), var(--e1); }
  .slot.done { border-bottom-color:#9CC6A2; box-shadow:var(--rim), 0 2px 0 #C9E2CB, 0 5px 10px -6px rgba(60,120,70,.32); }
  .slot.missed { border-bottom-color:#E2A89E; box-shadow:var(--rim), 0 2px 0 #EFCCC5, 0 5px 10px -6px rgba(150,60,50,.30); }

  /* wells: anything you type into or choose inside is pressed into the felt */
  input[type=text], select, pre#sbText, #pdfMonth { box-shadow:var(--well); }
  input[type=text], select { border-color:#C4CCD9 var(--border) var(--edge-top); }
  input[type=text]:focus, select:focus { outline:2px solid var(--lip-accent); outline-offset:1px; border-color:#AAB3C4; box-shadow:var(--well); }
  .tabs { box-shadow:var(--well); border-color:#C4CCD9 var(--border) var(--edge-top); }

  /* keycaps: chunky toasted lip; gradients only ever DARKEN below today's fill so white text keeps its contrast */
  .btn { --lip:var(--lip-accent);
         background:linear-gradient(180deg,var(--accent) 0,var(--accent) 45%,var(--accent-deep) 100%);
         box-shadow:inset 0 1px 0 rgba(255,255,255,.38), inset 0 -2px 3px rgba(110,48,18,.16), 0 3px 0 var(--lip), 0 7px 12px -5px rgba(150,80,40,.45);
         text-shadow:0 1px 1px rgba(110,48,18,.30); }
  .btn.green { --lip:var(--lip-green);
         background:linear-gradient(180deg,var(--good) 0,var(--good) 45%,var(--good-deep) 100%);
         box-shadow:inset 0 1px 0 rgba(255,255,255,.38), inset 0 -2px 3px rgba(30,80,40,.16), 0 3px 0 var(--lip), 0 7px 12px -5px rgba(60,120,70,.45);
         text-shadow:0 1px 1px rgba(30,80,40,.30); }
  .btn.ghost { --lip:var(--lip-ghost); background:linear-gradient(180deg,#FFFEFB 0,var(--card2) 100%);
         border-color:var(--edge-top) var(--border) var(--edge-bot); text-shadow:0 1px 0 #fff;
         box-shadow:inset 0 1px 0 #fff, 0 3px 0 var(--lip), 0 7px 12px -6px rgba(var(--sh),.32); }
  .btn:active { box-shadow:inset 0 2px 4px rgba(60,30,10,.28), 0 1px 0 var(--lip); }
  .btn.ghost:active { box-shadow:var(--well), 0 1px 0 var(--lip); }
  .btn:disabled { cursor:default; filter:saturate(.75); box-shadow:inset 0 1px 0 rgba(255,255,255,.25), 0 1px 0 var(--lip); }
  .tab:not(.active):hover { color:var(--ink); background:rgba(255,255,255,.55);
         box-shadow:inset 0 1px 0 #fff, 0 1px 0 rgba(var(--sh),.10); }
  .tab.active { background:linear-gradient(180deg,var(--accent) 0,var(--accent) 45%,var(--accent-deep) 100%);
         text-shadow:0 1px 1px rgba(110,48,18,.30);
         box-shadow:inset 0 1px 0 rgba(255,255,255,.38), 0 2px 0 var(--lip-accent), 0 4px 7px -3px rgba(150,80,40,.40); }

  /* chips: raised when off, pushed in when on */
  .pchip { background-image:var(--face-sm); box-shadow:var(--rim), var(--e1); }
  .pchip.on { box-shadow:var(--sunk); }
  .pbadge { box-shadow:inset 0 1px 0 rgba(255,255,255,.9), 0 1px 0 rgba(var(--sh),.12); }
  #sqList > span:not(.hint) { box-shadow:inset 0 1px 0 rgba(255,255,255,.8), 0 2px 0 var(--lip-ghost), 0 4px 7px -4px rgba(var(--sh),.28); }

  /* long lists (~400 video rows, feed re-rendered every 5s): static cheap shadows, one tiny blur, no animation */
  .vlist, .flist { padding-bottom:4px; }
  .video { box-shadow:var(--rim), var(--e2); }
  .video:hover { box-shadow:var(--rim), var(--e3); }
  .video.t { box-shadow:var(--rim), 0 0 0 2px rgba(120,132,156,.4), var(--e2); }
  .video input:checked { background:var(--accent-deep); border-color:var(--accent-deep);
         box-shadow:inset 0 1px 0 rgba(255,255,255,.3), 0 2px 5px -2px rgba(var(--sh),.5); }
  .video img.th, .video .noth { box-shadow:0 0 0 1px rgba(255,255,255,.6), 0 2px 0 rgba(var(--sh),.16); }
  .ev { background-image:var(--face-sm); box-shadow:var(--row); }
  .ev.SKIP { box-shadow:inset 0 1px 2px rgba(var(--sh),.10); }
  .badge { box-shadow:inset 0 1px 0 rgba(255,255,255,.75), inset 0 -1px 0 rgba(75,58,43,.07), 0 1px 0 rgba(var(--sh),.12); }

  /* analytics: engraved dividers, sparkline ribbon floating a hair above the card */
  .metricrow { box-shadow:0 1px 0 rgba(255,255,255,.85); }
  .metricrow:last-child { box-shadow:none; }
  .metricrow svg { filter:drop-shadow(0 2px 1.5px rgba(176,96,54,.28)); }
  polyline.spark { stroke-width:2.4; stroke-linecap:round; stroke-linejoin:round; }
  polygon.sparkfill { fill:url(#pl-spark) rgba(208,122,79,.15); }

  /* generated photos: raised prints; the picked ones get an accent ring */
  #gpGrid img { box-shadow:0 2px 0 #C6CEDA, 0 8px 14px -8px rgba(var(--sh),.42); }
  #gpGrid input:checked + img { box-shadow:0 0 0 3px var(--accent), 0 2px 0 var(--lip-accent), 0 8px 14px -8px rgba(120,135,160,.5); }
  ::-webkit-scrollbar-thumb { background:linear-gradient(90deg,#D6DCE7,#AEB8CA); box-shadow:inset 0 1px 0 rgba(255,255,255,.6); }

  /* floating layer: panels, bubbles, voice button */
  .mpanel, #ledgerPanel { background:linear-gradient(180deg,#FFFFFF 0,#F2F5FA 64px);
         border-color:#FFFFFF var(--border) #C4CCD9; box-shadow:var(--rim), var(--puff), var(--e4); }
  #pdfMonth + button { text-shadow:0 1px 1px rgba(110,48,18,.30);
         box-shadow:inset 0 1px 0 rgba(255,255,255,.35), 0 3px 0 var(--lip-accent), 0 6px 10px -4px rgba(150,80,40,.40); }
  #pdfMonth + button:active { box-shadow:inset 0 2px 4px rgba(60,30,10,.28), 0 1px 0 var(--lip-accent); }
  .critter .bubble { background:linear-gradient(180deg,#FFFFFF 0,#F2F5FA 70%);
         border-color:#FFFFFF var(--border) var(--edge-bot); box-shadow:var(--rim), var(--e3); }
  .critter .prop { text-shadow:0 2px 1.5px rgba(var(--sh),.28); }
  #mascotVoice { background:linear-gradient(180deg,#FFFEFB 0,var(--card2) 100%);
         border-color:var(--edge-top) var(--border) var(--edge-bot);
         box-shadow:inset 0 1px 0 #fff, 0 3px 0 var(--lip-ghost), 0 6px 10px -4px rgba(var(--sh),.30); }
  #mascotVoice:active { box-shadow:var(--well), 0 1px 0 var(--lip-ghost); }

  /* liquid-metal mascots: specular lighting gives polished-chrome sheen while keeping color */
  .critter svg { filter:url(#brushed) drop-shadow(0 6px 13px rgba(0,0,0,.72)); }
  .critter .pl-cshadow { transform-box:fill-box; transform-origin:center; }
  .critter .mtag { color:var(--ink2);
                   text-shadow:0 0 8px rgba(174,180,194,.5), 0 1px 2px #000; }

  /* the diorama floor: grove + mascots stand on a felt ground plane */
  .content { padding-bottom:150px; } /* lets the last card scroll fully clear of the floor */
  #grove::before { content:''; position:absolute; left:0; right:0; bottom:0; height:56px; z-index:-1; pointer-events:none;
    background:radial-gradient(60% 120% at 50% 100%,rgba(255,250,242,.5) 0,rgba(255,250,242,0) 70%),
               linear-gradient(180deg,rgba(234,220,201,0) 0,rgba(234,220,201,.85) 14px,#E9DAC4 26px,#E0CDB3 100%);
    -webkit-mask-image:linear-gradient(90deg,transparent 0,#000 48px);
            mask-image:linear-gradient(90deg,transparent 0,#000 48px); }
  .tree { transform-origin:50% 100%; } /* the existing hover scale now grows up from the ground */
  .tree .rank { color:#664F3F; text-shadow:0 1px 0 rgba(255,255,255,.6); }
  .tree .grow { color:#2C6239; text-shadow:0 1px 0 rgba(255,255,255,.6); }
  /* smaller trees stand further back; nearer trees paint in front where they overlap */
  #grove .tree:nth-child(1) { z-index:10; }
  #grove .tree:nth-child(2) { bottom:2px; z-index:9; } #grove .tree:nth-child(3) { bottom:4px; z-index:8; }
  #grove .tree:nth-child(4) { bottom:6px; z-index:7; } #grove .tree:nth-child(5) { bottom:8px; z-index:6; }
  #grove .tree:nth-child(6) { bottom:10px; z-index:5; } #grove .tree:nth-child(7) { bottom:10px; z-index:4; }
  #grove .tree:nth-child(8) { bottom:10px; z-index:3; } #grove .tree:nth-child(n+9) { bottom:10px; z-index:2; }

  /* native file pickers become ghost keycaps (own rules so an unknown pseudo-element drops only these) */
  input[type=file]::file-selector-button { font:inherit; font-weight:700; color:var(--ink2); cursor:pointer;
    margin-right:8px; padding:5px 11px; border-radius:8px; border:1px solid var(--border);
    border-color:var(--edge-top) var(--border) var(--edge-bot);
    background:linear-gradient(180deg,#FFFEFB 0,var(--card2) 100%);
    box-shadow:inset 0 1px 0 #fff, 0 2px 0 var(--lip-ghost); }
  input[type=file]::file-selector-button:active { box-shadow:var(--well); }

  @keyframes plShadow { 0%,100%{transform:scaleX(1); opacity:1} 50%{transform:scaleX(.86); opacity:.7} }
  @keyframes pop3d { from{opacity:0; transform:perspective(800px) translateY(12px) rotateX(-10deg) scale(.97)} to{opacity:1} }

  /* ---- motion A: hover lifts (real pointer only, never with reduced motion). Must stay BEFORE motion B so presses win ---- */
  @media (prefers-reduced-motion:no-preference) and (hover:hover){
    .tile:hover { transform:translateY(-3px); box-shadow:var(--rim), var(--puff), var(--tuck), var(--e4); }
    .btn:not(:disabled):hover, #engBtn:hover, #mascotVoice:hover, #pdfMonth + button:hover { transform:translateY(-1px); }
    .video:not(.t):hover { transform:translateY(-1px); } /* targeted rows stay pressed in */
    #gpGrid > label:hover { transform:translateY(-2px); }
    /* invisible skirt under a lifted card, so a pointer resting on its bottom edge keeps hover (no shake) */
    .tile { position:relative; }
    .tile:hover::after { content:''; position:absolute; left:-1px; right:-1px; bottom:-5px; height:5px; }
    #gpGrid > label:hover::after { content:''; position:absolute; left:0; right:0; bottom:-3px; height:3px; }
  }
  /* ---- motion B: presses sink into their lip, panels swing up in 3D, the walking contact shadow breathes ---- */
  @media (prefers-reduced-motion:no-preference){
    .navitem { transition:background-color .12s, color .12s, box-shadow .12s, transform .08s; }
    .btn, .tab, .pchip, #engBtn, #mascotVoice, #pdfMonth + button { transition:transform .08s ease, box-shadow .08s ease; }
    .tile { transition:transform .2s ease, box-shadow .2s ease; }
    .video { transition:transform .15s ease, box-shadow .15s ease, border-color .12s ease; }
    #gpGrid > label { transition:transform .15s ease; }
    .btn:not(:disabled):active, #engBtn:active, #mascotVoice:active, #pdfMonth + button:active { transform:translateY(2px); }
    .tab:active, .pchip:active, .navitem:active { transform:translateY(1px); }
    /* invisible lip above a pressed key, so a press near its top edge still releases on it and the click fires */
    .btn, #engBtn, #pdfMonth + button, .tab, .pchip, .navitem { position:relative; }
    .btn:not(:disabled):active::after, #engBtn:active::after, #mascotVoice:active::after, #pdfMonth + button:active::after {
      content:''; position:absolute; left:0; right:0; top:-5px; height:5px; }
    .tab:active::after, .pchip:active::after, .navitem:active::after { content:''; position:absolute; left:0; right:0; top:-3px; height:3px; }
    .mpanel.show, #ledgerPanel.show { animation:pop3d .28s cubic-bezier(.2,.8,.2,1); transform-origin:50% 100%; }
    .critter.walking .pl-cshadow { animation:plShadow .38s infinite ease-in-out; }
  }
  /* ---- reduced motion: every NEW transition/transform/animation is off; the static depth stays ---- */
  @media (prefers-reduced-motion:reduce){
    .btn, .tab, .pchip, .tile, .video, #engBtn, #mascotVoice, #pdfMonth + button, #gpGrid > label { transition:none; }
    .navitem { transition:background-color .12s, color .12s; }
    .btn:hover, .btn:active, .tab:active, .pchip:active, .navitem:active, .tile:hover, .video:hover,
    #engBtn:hover, #engBtn:active, #mascotVoice:hover, #mascotVoice:active,
    #pdfMonth + button:hover, #pdfMonth + button:active, #gpGrid > label:hover { transform:none; }
    .critter .pl-cshadow { animation:none; }
    /* optional, pre-existing motion: uncomment to also calm the tree hover zoom */
    /* .tree:hover { transform:none; } */
  }

  /* narrow layout: the sidebar becomes a top bar, so its shadow points down; the floor spans the full width */
  @media (max-width:860px){
    aside { background:var(--chrometex); border-right:0; border-bottom:0;
            box-shadow:inset 0 1px 0 rgba(255,255,255,.9), 0 -6px 18px -12px rgba(var(--sh),.5); }
    #grove::before { left:-216px; -webkit-mask-image:none; mask-image:none; }
    .dealbrand { flex:1 1 100%; font-size:17px; }
    .dealpitch { margin-left:auto; }
  }
  /* ---- neumorphic unification: every graphic shares one depth language ----
     raised = resting surfaces (cards, pills, tiles) | pressed = selected | well = inputs */
  .conn, .rights-row, .analystbox, .postingto, .monelig, .collap, .adv,
  .filmgenre, .rxlenchip, .rxfmtbtn, .rxphonebtn, .monchip, .monpagepill, .feedact, .snum,
  .assist-chip, .assist-mic, .assist-send, .spottrend, .exp-rb, .bt-conf {
    box-shadow:var(--rim), var(--e1);
  }
  .conn, .rights-row, .analystbox, .postingto, .monelig, .collap, .adv { border-radius:14px; }
  .rxcamwrap { box-shadow:var(--rim), var(--e2); }
  .filmgenre.on, .rxlenchip.on, .rxfmtbtn.on, .monpagepill.on, .pagerow.on, .exp-rb.active {
    box-shadow:var(--inset);
  }
  #view-deals input, .hs-input, .rights-in { box-shadow:var(--well); }
  /* ---- graphic upgrades v2: heatmap values, expense trend, spotlight states ---- */
  .bt-c { color:var(--ink2); }
  .bt-v { font-size:10.5px; font-weight:700; line-height:1; color:var(--ink2); }
  .bt-hot { color:#fff; }
  .bt-hot .bt-v { color:#fff; }
  .bt-thin { opacity:.62; }
  .bt-best { box-shadow:0 0 0 2px var(--good), var(--e1); }
  .bt-best .bt-v { font-size:11px; }
  .bt-sweet { font-size:10.5px; font-weight:800; color:var(--good); background:var(--card2); border-radius:999px; padding:3px 10px; box-shadow:var(--rim), var(--e1); }
  .exp-trend { margin:16px 0 2px; }
  .exp-svg { width:100%; height:96px; display:block; overflow:visible; }
  .exp-legend { display:flex; gap:14px; align-items:center; font-size:11px; color:var(--ink3); margin-top:6px; flex-wrap:wrap; }
  .exp-legend i { display:inline-block; width:14px; height:3px; border-radius:2px; vertical-align:middle; margin-right:6px; }
  .exp-legend .lg-e { background:var(--good); }
  .exp-legend .lg-s { background:var(--accent); }
  .exp-axis { margin-left:auto; }
  .exp-splitbar { display:flex; height:10px; border-radius:999px; overflow:hidden; box-shadow:var(--inset); margin-top:16px; background:var(--card2); }
  .exp-splitbar span { display:block; height:100%; }
  .exp-splitbar .sb-c { background:var(--accent); }
  .exp-splitbar .sb-m { background:var(--good); }
  .exp-splitlg { display:flex; gap:16px; font-size:11.5px; color:var(--ink2); margin-top:8px; flex-wrap:wrap; }
  .exp-splitlg i { display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:6px; vertical-align:middle; }
  .exp-splitlg .lg-c { background:var(--accent); }
  .exp-splitlg .lg-m { background:var(--good); }
  .spot-presets { display:flex; flex-wrap:wrap; align-items:center; gap:7px; margin:2px 0 4px; }
  .spottrend { font:inherit; cursor:pointer; }
  .spot-skel { display:flex; gap:10px; margin:12px 0 8px; }
  .spot-skel i { flex:1; height:74px; border-radius:var(--r); background:linear-gradient(90deg, var(--card2) 0%, var(--card) 50%, var(--card2) 100%); background-size:200% 100%; animation:spotShim 1.2s linear infinite; box-shadow:var(--rim), var(--e1); }
  @keyframes spotShim { to { background-position:-200% 0; } }
  .spot-skel-cap { font-size:12px; color:var(--ink3); }
  .si-plat { display:inline-block; font-size:10px; font-weight:800; letter-spacing:.05em; text-transform:uppercase; color:var(--accent-deep); background:var(--accent-soft); border-radius:999px; padding:2px 9px; margin-bottom:7px; }
  #rxHfPanel { display:none !important; }   /* hands-free runs off-screen: only the camera shows */
  /* ---- Film: big camera while recording; the camera settings gear holds the wide view ---- */
  .rxcamwrap { width:min(100%, max(248px, calc((100vh - 230px) * 9 / 16))); width:min(100%, max(248px, calc((100dvh - 230px) * 9 / 16))); }
  .rxgear { width:34px; height:34px; border-radius:999px; border:1px solid var(--border); background:var(--card2); color:var(--ink2); display:grid; place-items:center; font-size:16px; cursor:pointer; box-shadow:var(--rim), var(--e1); }
  .rxgear:hover { color:var(--ink); }
  .rxgear.on { box-shadow:var(--inset); color:var(--accent-deep); }
  .rxcamset { flex-direction:column; gap:10px; padding:14px; border:1px solid var(--border); border-radius:14px; background:var(--card2); box-shadow:var(--rim), var(--e1); margin:0 0 14px; }
  .rxcamset-row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  #rxHfStatus { position:absolute; left:10px; top:44px; max-width:72%; padding:6px 11px; border-radius:99px; background:rgba(6,9,16,.66); color:#eef4ff; font-size:12px; font-weight:600; line-height:1.3; pointer-events:none; z-index:3; }
  #rxHfStatus.on { color:#bff5d6; }
  #rxHfStatus.bad { color:#ffc2b0; }
  .rxroom-row input[type=range] { flex:1; min-width:120px; accent-color:var(--accent); }
  .rxbgview { position:absolute; left:0; top:0; width:100%; height:100%; z-index:1; pointer-events:none; }
  .rxbgpanel { margin:10px 0; padding:12px; border-radius:14px; background:var(--card2); border:1px solid var(--border); }
  .rxbg-h { font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.05em; color:var(--ink3); margin-bottom:8px; }
  .rxbg-row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:8px 0; }
  .rxbg-l { font-size:12px; color:var(--ink2); min-width:120px; }
  .rxbg-row input[type=range] { flex:1; min-width:120px; accent-color:var(--accent); }
  .rxroom-v { min-width:3.2em; text-align:right; font-size:12px; color:var(--ink2); font-variant-numeric:tabular-nums; }
  .rxcamset .rxhf-note { font-size:11.5px; color:var(--ink3); line-height:1.5; }
  .rxwidebox { display:flex; flex-direction:column; gap:8px; }
  .rxmonitor { width:100%; height:auto; aspect-ratio:16/9; display:block; border-radius:12px; background:#0a0e18; }
  #rxStudio.rec-focus .rxcamwrap { width:auto; height:calc(100vh - 170px); height:calc(100dvh - 170px); max-width:100%; }
  #rxStudio.rec-focus .rxside, #rxStudio.rec-focus .rxfmt, #rxStudio.rec-focus .rxcamset, #rxStudio.rec-focus #rxPhonePanel, #rxStudio.rec-focus #rxHfPanel { display:none !important; }
  #rxStudio.rec-focus .rxfstage { justify-content:center; }
  @media (max-width:860px){
    .rxfstage { flex-direction:column; align-items:stretch; }
    .rxcamwrap { width:min(100%, 420px); margin:0 auto; }
  }
  /* ---- Film rail: slim icon-only neumorphic dock; each option opens as a compact card beside it ---- */
  .rxside { display:none; }
  .rxfstage { justify-content:center; }
  #rxStudio.cam-off .rxcamwrap, #rxStudio.cam-off .rxrail, #rxStudio.cam-off #rxPhonePanel, #rxStudio.cam-off #rxHfPanel, #rxStudio.cam-off #rxCamRow, #rxStudio.cam-off .rxfly-p[data-fly-panel="len"], #rxStudio.cam-off .rxfly-p[data-fly-panel="cap"], #rxStudio.cam-off .rxfly-p[data-fly-panel="cam"] { display:none !important; }
  #rxStudio.cam-off .rxfstage { display:block; }
  #rxStudio.cam-off .rxfly { display:flex !important; position:static; transform:none; width:auto; max-height:none; overflow:visible; }
  #rxStudio.cam-off .rxfly-p[data-fly-panel="prompt"] { display:flex !important; }
  .voice-switch, #assistSound, #assistMic { display:none !important; }   /* voice is off for the live demo */
  .subtab[data-sv="film"] { display:none !important; }   /* Create is removed for the submission */
  .rxrail { position:absolute; right:10px; top:50%; transform:translateY(-50%); display:flex; flex-direction:column; gap:10px; padding:9px; border-radius:24px; background:var(--card); box-shadow:var(--rim), var(--e2); z-index:5; }
  .rxrail-b { width:40px; height:40px; padding:0; border:0; background:transparent; color:var(--ink2); cursor:pointer; display:block; font:inherit; }
  .rxrail-i { width:40px; height:40px; border-radius:50%; background:var(--card2); display:grid; place-items:center; box-shadow:var(--rim), var(--e1); transition:box-shadow .18s ease, color .18s ease; }
  .rxrail-i svg { width:17px; height:17px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
  .rxrail-b:hover .rxrail-i { color:var(--ink); }
  .rxrail-b.on .rxrail-i, .rxrail-b:active .rxrail-i { box-shadow:var(--inset); color:var(--accent-deep); }
  .rxfly { position:absolute; top:50%; right:66px; transform:translateY(-50%); width:min(240px, calc(100% - 96px)); max-height:calc(100% - 24px); overflow-y:auto; flex-direction:column; gap:12px; padding:16px; border-radius:22px; background:var(--card); box-shadow:var(--rim), var(--e3); z-index:6; }
  .rxfly-p { flex-direction:column; gap:10px; }
  .rxfly-h { font-size:11px; font-weight:800; letter-spacing:.06em; text-transform:uppercase; color:var(--ink2); }
  .rxfly .rxcamset { box-shadow:none; background:transparent; border:0; padding:0; margin:0; }
  .rxfly .rxprompter { max-height:220px; overflow-y:auto; }
  .rxfly .rxpromptlabel { display:none; }
  #rxStudio.rec-focus .rxfmt, #rxStudio.rec-focus #rxPhonePanel, #rxStudio.rec-focus #rxHfPanel { display:none !important; }
</style>
<style>html.gate-on .app{visibility:hidden}  .fbbanner{position:fixed;left:12px;right:132px;bottom:12px;z-index:900;display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;padding:12px 16px;border-radius:14px;background:var(--card);box-shadow:var(--e3);font-size:13px;color:var(--ink2)}  @media (max-width:640px){.fbbanner{left:8px;right:92px;bottom:8px;gap:6px;padding:6px 9px;border-radius:12px;font-size:11px;line-height:1.3;}.fbbanner .btn{padding:5px 9px;font-size:11px;white-space:nowrap;}}  /* ---- Create camera: Start as a popup, Record bottom centre, X to close ---- */
  .rxstart-pop { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; background:rgba(8,12,20,.55); z-index:7; }
  .rxstart-card { background:var(--card); border-radius:20px; box-shadow:var(--rim), var(--e3); padding:20px 22px; display:flex; flex-direction:column; gap:12px; align-items:center; min-width:170px; }
  .rxstart-t { font-size:12px; font-weight:700; color:var(--ink2); }
  .rxcamwrap.cam-on .rxstart-pop { display:none; }
  .rxcamwrap:not(.cam-on) .rxcammsg { display:none; }
  .rxcam-x { position:absolute; top:10px; right:10px; z-index:8; width:34px; height:34px; border-radius:50%; border:0; background:rgba(14,20,32,.62); color:#fff; font-size:15px; cursor:pointer; box-shadow:var(--rim), var(--e1); display:none; place-items:center; }
  .rxcamwrap.cam-on .rxcam-x { display:grid; }
  .rxcamwrap .rxhud { right:52px; }
  #rxRecBtn.rxrec-float { position:absolute; bottom:16px; left:50%; transform:translateX(-50%); z-index:9; width:66px; height:66px; padding:0; border-radius:50%; font-size:0; background:#E0564B; border:4px solid rgba(255,255,255,.9); box-shadow:0 4px 14px rgba(0,0,0,.35); display:flex; align-items:center; justify-content:center; }
  .voice-switch{flex:0 0 auto;position:relative;width:44px;height:24px;padding:0;margin:0 4px;border:0;border-radius:12px;background:rgba(120,130,150,.45);cursor:pointer;transition:background-color .18s;-webkit-tap-highlight-color:transparent}
  .voice-switch .voice-knob{position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.35);transition:transform .18s}
  .voice-switch.on{background:#22c55e}
  .voice-switch.on .voice-knob{transform:translateX(20px)}
  .voice-switch:focus-visible{outline:2px solid #0866FF;outline-offset:2px}
  aside .voice-switch{margin:2px auto 12px}
  #voiceToast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,8px);max-width:min(88vw,460px);background:var(--card);color:var(--ink);border:1px solid var(--border);border-radius:14px;padding:10px 14px;font-size:14px;font-weight:600;line-height:1.35;text-align:center;box-shadow:var(--rim),var(--e2);opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;z-index:960}
  #voiceToast.show{opacity:1;transform:translate(-50%,0)}
  .live-meta{position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:950;display:none;align-items:center;gap:8px;max-width:min(78vw,380px);padding:7px 12px;border:1px solid var(--border);border-radius:999px;background:var(--card);color:var(--ink2);font-size:12px;font-weight:700;box-shadow:var(--rim),var(--e1);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .live-meta.show{display:inline-flex}
  /* phones: a small dot under the top bar instead of a long pill over the logo; tap it for the list */
  @media (max-width:860px){
    .live-meta{top:54px;left:auto;right:10px;transform:none;max-width:none;padding:5px;border:0;background:transparent;box-shadow:none;}
    .live-meta .lm-text{display:none;}
    .live-meta-list{top:88px;left:auto;right:10px;transform:none;width:min(88vw,340px);}
  }
  .live-meta .lm-dot{width:8px;height:8px;border-radius:50%;background:#9ca3af;flex:0 0 auto}
  .live-meta.busy .lm-dot{background:#22c55e;box-shadow:0 0 8px #22c55e;animation:lmpulse 1s ease-in-out infinite}
  @keyframes lmpulse{50%{opacity:.35}}
  .live-meta-list{position:fixed;top:52px;left:50%;transform:translateX(-50%);z-index:949;display:none;width:min(88vw,360px);max-height:50vh;overflow:auto;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:8px 12px;font-size:12px;color:var(--ink2);box-shadow:var(--rim),var(--e2)}
  .live-meta-list.open{display:block}
  .live-meta-list .lr{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid var(--border)}
  @media (max-width:860px){ aside .voice-switch{display:none} }
  #rxRecBtn.rxrec-float.on::after { content:''; width:22px; height:22px; border-radius:5px; background:#fff; }
  #rxNextBtn.rxnext-arrow { display:none; position:absolute; right:14px; bottom:16px; z-index:9; width:54px; height:54px; border-radius:50%; border:0; padding:0; background:#ffffff; color:#111827; border:2px solid rgba(0,0,0,.35); font-size:26px; line-height:1; align-items:center; justify-content:center; cursor:pointer; box-shadow:0 4px 14px rgba(0,0,0,.45); }
  .rxcamwrap.cam-on #rxNextBtn.rxnext-arrow { display:flex; }
  #rxNextBtn.rxnext-arrow:disabled { opacity:.6; cursor:not-allowed; }
</style>
<script>(function(){try{var a=localStorage.getItem('abraAuth')==='1'||sessionStorage.getItem('abraAuth')==='1';if(!a)document.documentElement.classList.add('gate-on');}catch(e){}})();</script>
</head><body>
<canvas id="bgCanvas" class="bg-canvas"></canvas>
<div id="splash" class="splash">
  <div class="splash-inner">
    <div class="splash-logowrap"><img class="splash-logo" src="/logo.png" alt="abra"></div>
    <div class="splash-tag">open and create</div>
    <div class="splash-vp">The AI crew that runs your page — so you just film.</div>
    <div class="splash-hack">
      <svg class="splash-inf" viewBox="0 0 120 56" aria-hidden="true"><defs><linearGradient id="ig" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7CD2FF"/><stop offset="1" stop-color="#0866FF"/></linearGradient></defs><path d="M60 28C52 12 34 10 24 18 14 25 14 31 24 38 34 46 52 44 60 28 68 12 86 10 96 18 106 25 106 31 96 38 86 46 68 44 60 28Z"/></svg>
      <div class="splash-hackname">Meta Global AI Developer Hackathon</div>
      <div class="splash-hacksub">2026 · built on Meta's Muse models and Claude</div>
    </div>
    <div class="splash-bar"><i></i></div>
  </div>
</div>
<svg class="pl-defs" width="0" height="0" aria-hidden="true" focusable="false">
  <defs>
    <!-- 3D plush shading, defined ONCE and shared by every mascot, tree and sparkline (never display:none: Firefox/Safari stop painting it) -->
<radialGradient id="pl-cream" cx=".5" cy=".42" r=".66" fx=".5" fy=".2"><stop offset="0" stop-color="#E2DDD3"/><stop offset=".55" stop-color="#C9C2B4"/><stop offset="1" stop-color="#A69D8A"/></radialGradient>
<radialGradient id="pl-oat" cx=".5" cy=".42" r=".66" fx=".5" fy=".2"><stop offset="0" stop-color="#D9CEB9"/><stop offset=".55" stop-color="#BFB199"/><stop offset="1" stop-color="#9C8C71"/></radialGradient>
<radialGradient id="pl-blue" cx=".5" cy=".42" r=".66" fx=".5" fy=".2"><stop offset="0" stop-color="#B8C4D8"/><stop offset=".55" stop-color="#93A1BA"/><stop offset="1" stop-color="#6C7A95"/></radialGradient>
<radialGradient id="pl-blue-deep" cx=".5" cy=".42" r=".66" fx=".5" fy=".2"><stop offset="0" stop-color="#A6B4CE"/><stop offset=".55" stop-color="#8495B0"/><stop offset="1" stop-color="#62728E"/></radialGradient>
<radialGradient id="pl-grey" cx=".5" cy=".42" r=".66" fx=".5" fy=".2"><stop offset="0" stop-color="#CDD2DA"/><stop offset=".55" stop-color="#AEB4BE"/><stop offset="1" stop-color="#8A909C"/></radialGradient>
<radialGradient id="pl-grey-head" cx=".5" cy=".42" r=".66" fx=".5" fy=".2"><stop offset="0" stop-color="#D6DBE2"/><stop offset=".55" stop-color="#B8BEC8"/><stop offset="1" stop-color="#949BA8"/></radialGradient>
<radialGradient id="pl-belly" cx=".5" cy=".42" r=".66" fx=".5" fy=".2"><stop offset="0" stop-color="#DCE0E6"/><stop offset=".55" stop-color="#C2C8D0"/><stop offset="1" stop-color="#A4ABB5"/></radialGradient><linearGradient id="pl-chrome" x1="0" y1="0" x2="0.1" y2="1"><stop offset="0" stop-color="#FFFFFF"/><stop offset=".28" stop-color="#D8E0EC"/><stop offset=".48" stop-color="#8A95A8"/><stop offset=".55" stop-color="#2E343F"/><stop offset=".68" stop-color="#636D7C"/><stop offset=".84" stop-color="#C6CEDB"/><stop offset="1" stop-color="#FFFFFF"/></linearGradient><linearGradient id="pl-chrome-dark" x1="0" y1="0" x2="0.1" y2="1"><stop offset="0" stop-color="#AAB4C4"/><stop offset=".34" stop-color="#434A55"/><stop offset=".55" stop-color="#0C0F14"/><stop offset=".72" stop-color="#2C323C"/><stop offset="1" stop-color="#6E7686"/></linearGradient>
    <radialGradient id="pl-orange" cx=".5" cy=".42" r=".66" fx=".5" fy=".2"><stop offset="0" stop-color="#D6B99C"/><stop offset=".55" stop-color="#B99B7D"/><stop offset="1" stop-color="#93785C"/></radialGradient>
    <radialGradient id="pl-orange-deep" cx=".5" cy=".42" r=".66" fx=".5" fy=".2"><stop offset="0" stop-color="#CBAB87"/><stop offset=".55" stop-color="#AB8D6C"/><stop offset="1" stop-color="#846B4E"/></radialGradient>
    <radialGradient id="pl-fox-belly" cx=".5" cy=".42" r=".66" fx=".5" fy=".2"><stop offset="0" stop-color="#DCD4C8"/><stop offset=".55" stop-color="#C4BBAC"/><stop offset="1" stop-color="#A69C8A"/></radialGradient>
    <!-- concave parts (inner ears) are dark on top and lit below, so they read as recessed -->
    <linearGradient id="pl-cup-peach" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#D49B7F"/><stop offset=".55" stop-color="#E8B79B"/><stop offset="1" stop-color="#F3CFB9"/>
    </linearGradient>
    <linearGradient id="pl-cup-pink" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#D49AA7"/><stop offset=".55" stop-color="#E8B9C2"/><stop offset="1" stop-color="#F4D6DC"/>
    </linearGradient>
    <!-- legs are cylinders: dark edges, lit middle (symmetric, so the flip is safe) -->
    <linearGradient id="pl-leg-near" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#D6C09E"/><stop offset=".5" stop-color="#F8F0E2"/><stop offset="1" stop-color="#D6C09E"/>
    </linearGradient>
    <linearGradient id="pl-leg-far" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#C8AE88"/><stop offset=".5" stop-color="#EFE3CE"/><stop offset="1" stop-color="#C8AE88"/>
    </linearGradient>
    <linearGradient id="pl-leg-grey-near" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#8F98A5"/><stop offset=".5" stop-color="#C9CFD7"/><stop offset="1" stop-color="#8F98A5"/>
    </linearGradient>
    <linearGradient id="pl-leg-grey-far" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#808998"/><stop offset=".5" stop-color="#B5BCC7"/><stop offset="1" stop-color="#808998"/>
    </linearGradient>
    <linearGradient id="pl-collar" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#EAA680"/><stop offset=".45" stop-color="#D07A4F"/><stop offset="1" stop-color="#AD5C37"/>
    </linearGradient>
    <radialGradient id="pl-blush" cx=".5" cy=".5" r=".5">
      <stop offset="0" stop-color="#EDA78B" stop-opacity=".95"/><stop offset=".6" stop-color="#F0B9A0" stop-opacity=".6"/><stop offset="1" stop-color="#F0B9A0" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="pl-knose" cx=".5" cy=".45" r=".6" fx=".5" fy=".22">
      <stop offset="0" stop-color="#6E6C76"/><stop offset=".5" stop-color="#3C3A40"/><stop offset="1" stop-color="#252329"/>
    </radialGradient>
    <radialGradient id="pl-sheen" cx=".5" cy=".5" r=".5">
      <stop offset="0" stop-color="#FFFFFF" stop-opacity=".7"/><stop offset=".55" stop-color="#FFFFFF" stop-opacity=".22"/><stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="pl-ao" cx=".5" cy=".5" r=".5">
      <stop offset="0" stop-color="#4B3A2B" stop-opacity=".30"/><stop offset=".6" stop-color="#4B3A2B" stop-opacity=".12"/><stop offset="1" stop-color="#4B3A2B" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="pl-contact" cx=".5" cy=".5" r=".5">
      <stop offset="0" stop-color="#4B3A2B" stop-opacity=".34"/><stop offset=".55" stop-color="#4B3A2B" stop-opacity=".14"/><stop offset="1" stop-color="#4B3A2B" stop-opacity="0"/>
    </radialGradient>
    <!-- money grove -->
    <linearGradient id="pl-trunk" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#77502F"/><stop offset=".38" stop-color="#B5855C"/><stop offset=".72" stop-color="#9A6B43"/><stop offset="1" stop-color="#6E492B"/>
    </linearGradient>
    <radialGradient id="pl-leaf" cx=".5" cy=".42" r=".66" fx=".5" fy=".2">
      <stop offset="0" stop-color="#BFE3BC"/><stop offset=".55" stop-color="#7DB97F"/><stop offset="1" stop-color="#56905A"/>
    </radialGradient>
    <radialGradient id="pl-leaf-side" cx=".5" cy=".42" r=".66" fx=".5" fy=".2">
      <stop offset="0" stop-color="#C9E8C7"/><stop offset=".55" stop-color="#8FC791"/><stop offset="1" stop-color="#639F67"/>
    </radialGradient>
    <radialGradient id="pl-coin" cx=".5" cy=".5" r=".55" fx=".5" fy=".28">
      <stop offset="0" stop-color="#FFF6D2"/><stop offset=".5" stop-color="#F2C94C"/><stop offset="1" stop-color="#BD8B16"/>
    </radialGradient>
    <!-- analytics sparkline fill (replaces the off-palette blue) -->
    <linearGradient id="pl-spark" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#D07A4F" stop-opacity=".30"/><stop offset="1" stop-color="#D07A4F" stop-opacity="0"/>
    </linearGradient>
  </defs>
</svg>
<div id="loginGate" class="logingate" aria-hidden="true">
  <div class="lg-card">
    <div class="lg-crew">
      <div class="lg-peek" style="--d:.18s"><img class="pb" src="/crew/cat.png" alt=""><img class="pw" src="/crew/cat_wave.png" alt=""></div>
      <div class="lg-peek" style="--d:.34s"><img class="pb" src="/crew/dog.png" alt=""><img class="pw" src="/crew/dog_wave.png" alt=""></div>
      <div class="lg-peek" style="--d:.50s"><img class="pb" src="/crew/koala.png" alt=""><img class="pw" src="/crew/koala_wave.png" alt=""></div>
    </div>
    <img class="lg-logo" src="/logo.png" alt="abra">
    <div class="lg-tag">open and create</div>
    <div class="lg-vp">Your crew answers comments and turns your videos into posts — so you just film.</div>
    <button class="lg-continue" onclick="doLogin()">
      <img id="lgAvatar" class="lg-av" src="/avatar" alt="" onerror="this.style.display='none'">
      <span class="lg-ctext">Continue<b id="lgName"></b></span>
    </button>
    <label class="lg-remember"><input type="checkbox" id="lgRemember" checked><span>Remember me</span></label>
    <button class="lg-alt" type="button" onclick="exploreWithoutConnecting()">Try Abra now (no sign-in needed)</button>
    <div class="lg-models"><span class="lg-models-label">Built on Meta's AI and Claude</span><span class="lg-chip">Wonder</span><span class="lg-chip">Spark</span><span class="lg-chip">Image</span><span class="lg-chip">Voice</span><span class="lg-chip">SAM 3.1</span></div>
    <div class="lg-foot">Built for the Meta Global AI Developer Hackathon.</div>
  </div>
</div>
<div class="app">
  <div class="hackstick" aria-hidden="true"><svg viewBox="0 0 44 22"><path d="M22 11C19 5 13 4 9 7 5 9 5 13 9 15 13 18 19 17 22 11 25 5 31 4 35 7 39 9 39 13 35 15 31 18 25 17 22 11Z"/></svg><span>Meta Global AI Developer Hackathon</span></div>
  <header class="mtopbar">
    <button class="mtoplogo-btn" data-mv="home" onclick="showView('home')" aria-label="Home"><img class="mtoplogo" src="/logo.png" alt="abra"></button>
    <button class="mtoptab" data-mv="studio" onclick="showView('film')">Launchpad</button>
    <span class="mtopspacer"></span>
    <button class="voice-switch" type="button" role="switch" aria-checked="false" onclick="voiceToggle()" title="Abra mic off" aria-label="Abra mic"><span class="voice-knob"></span></button>
    <button class="mtopbtn mtopav" id="mtopAvatar" onclick="showView('profile')" aria-label="Account"><img src="/avatar" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><span class="mtopav-init">AB</span></button>
  </header>
  <aside>
    <div class="brandmark" onclick="showView('home')" style="cursor:pointer" title="Home"><img src="/logo.png" alt="abra"></div>
    <button class="voice-switch" type="button" role="switch" aria-checked="false" onclick="voiceToggle()" title="Abra mic off" aria-label="Abra mic"><span class="voice-knob"></span></button>
    <button class="navitem" data-v="home" onclick="showView('home')" style="display:none"><svg class="ni" viewBox="0 0 24 24"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h14V9.5"/></svg><span>Home</span></button>
    <div class="navorbslot" aria-hidden="true"></div>
    <div class="navdiv"></div>
    <button class="navitem" data-v="studio" onclick="showView('film')"><svg class="ni" viewBox="0 0 24 24"><path d="M12 3c3 2 5 6 5 10l-5 4-5-4c0-4 2-8 5-10Z"/><circle cx="12" cy="10" r="2"/></svg><span>Launchpad</span></button>
    <button class="navitem" data-v="money" onclick="showView('money')" style="display:none"><svg class="ni" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 8v8M14.5 10.5c0-1.1-1.1-2-2.5-2s-2.5.9-2.5 2 1.1 1.6 2.5 1.6 2.5.6 2.5 1.8-1.1 2-2.5 2-2.5-.9-2.5-2"/></svg><span>Money</span></button>
    <button class="navitem" data-v="deals" onclick="showView('deals')" style="display:none"><svg class="ni" viewBox="0 0 24 24"><path d="M20 12 12 20l-8-8V4h8l8 8Z"/><circle cx="8.5" cy="8.5" r="1.4"/></svg><span data-short="Deals">Brand Deals</span></button>
    <div class="navdiv"></div>
    <button class="navitem" data-v="settings" onclick="showView('profile')"><svg class="ni" viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.6"/><path d="M5.5 20c.6-3.7 3.4-5.8 6.5-5.8s5.9 2.1 6.5 5.8"/></svg><span>Account</span></button>
    <div class="sidefoot">
      <button id="engBtn" onclick="toggleEngagement()">…</button>
      <div class="pill off" id="svc"><span class="dot"></span><span id="svcTxt">checking…</span></div>
    </div>
  </aside>
  <div class="content">

  <div class="view active" id="view-home">
    <div class="homehead">
      <img id="homeAvatar" class="homehead-pic" alt="" style="display:none">
      <div id="homeAvatarInit" class="homehead-init">AB</div>
      <div class="homehead-txt">
        <div class="homehead-hi" id="homeHi">Welcome back</div>
        <div class="homehead-status"><span class="livedot"></span><span id="homeStatus">your crew is on the clock — updates live.</span></div>
      </div>
    </div>
    <div class="kpis" id="homeKpis">
      <button class="kpi" onclick="showView('analytics')">
        <span class="kpi-n" id="kpiFollowers">—</span>
        <span class="kpi-l">Followers</span>
        <span class="kpi-s">across your pages</span>
      </button>
      <button class="kpi" onclick="showView('engage')">
        <span class="kpi-n" id="kpiReplies">—</span>
        <span class="kpi-l">Replies, last 24h</span>
        <span class="kpi-s" id="kpiRepliesSub">by your crew</span>
      </button>
      <button class="kpi" onclick="showView('analytics')">
        <span class="kpi-n" id="kpiReach">—</span>
        <span class="kpi-l">Minutes viewed</span>
        <span class="kpi-s">last 60 days</span>
      </button>
      <button class="kpi" onclick="showView('money')">
        <span class="kpi-n money" id="kpiEarned">—</span>
        <span class="kpi-l">Earned (30 days)</span>
        <span class="kpi-s" id="kpiEarnedSub">from your pages</span>
      </button>
    </div>
    <section>
      <h2> Your crew <span class="hint">tap a card to put them to work</span></h2>
      <div class="crewgrid">
        <div class="crewcard" onclick="showView('engage')">
          <div class="crewimg"><img class="base" src="/crew/cat.png" alt=""><img class="wave" src="/crew/cat_wave.png" alt=""><img class="blink" src="/crew/cat_blink.png" alt=""><span class="crewglow" style="-webkit-mask-image:url(/crew/cat.png);mask-image:url(/crew/cat.png)"></span><model-viewer class="crew3d" data-glb="cat" camera-controls camera-orbit="0deg 82deg 100%" min-camera-orbit="auto 68deg auto" max-camera-orbit="auto 94deg auto" interaction-prompt="none" disable-zoom exposure="1.7" tone-mapping="neutral" shadow-intensity="0.22" environment-image="neutral"></model-viewer></div>
          <div class="crewinfo"><div class="crewname">Nico</div><div class="crewrole">Engagement</div>
            <div class="crewstat" id="crewClaude">watching the comments…</div>
            <div class="crewcta">Answer comments <span class="crewgo">&rsaquo;</span></div></div>
        </div>
        <div class="crewcard" onclick="showView('studio')">
          <div class="crewimg"><img class="base" src="/crew/dog.png" alt=""><img class="wave" src="/crew/dog_wave.png" alt=""><img class="blink" src="/crew/dog_blink.png" alt=""><span class="crewglow" style="-webkit-mask-image:url(/crew/dog.png);mask-image:url(/crew/dog.png)"></span><model-viewer class="crew3d" data-glb="dog" camera-controls camera-orbit="0deg 82deg 100%" min-camera-orbit="auto 68deg auto" max-camera-orbit="auto 94deg auto" interaction-prompt="none" disable-zoom exposure="1.7" tone-mapping="neutral" shadow-intensity="0.22" environment-image="neutral"></model-viewer></div>
          <div class="crewinfo"><div class="crewname">Sage</div><div class="crewrole">Launchpad</div>
            <div class="crewstat" id="crewMuse">ready for your next video</div>
            <div class="crewcta">Drop content <span class="crewgo">&rsaquo;</span></div></div>
        </div>
        <div class="crewcard" onclick="showView('money')">
          <div class="crewimg"><img class="base" src="/crew/koala.png" alt=""><img class="wave" src="/crew/koala_wave.png" alt=""><img class="blink" src="/crew/koala_blink.png" alt=""><span class="crewglow" style="-webkit-mask-image:url(/crew/koala.png);mask-image:url(/crew/koala.png)"></span><model-viewer class="crew3d" data-glb="koala" camera-controls camera-orbit="0deg 82deg 100%" min-camera-orbit="auto 68deg auto" max-camera-orbit="auto 94deg auto" interaction-prompt="none" disable-zoom exposure="1.7" tone-mapping="neutral" shadow-intensity="0.22" environment-image="neutral"></model-viewer></div>
          <div class="crewinfo"><div class="crewname">Koa</div><div class="crewrole">Money &amp; Legal</div>
            <div class="crewstat" id="crewKoa">balancing the books…</div>
            <div class="crewcta">View earnings <span class="crewgo">&rsaquo;</span></div></div>
        </div>
      </div>
    </section>
    <section class="homesched">
      <div class="hs-head">
        <div><div class="hs-date" id="hsDate">—</div><div class="hs-sub">your week at a glance</div></div>
        <div class="hs-time" id="hsTime">—</div>
      </div>
      <div class="homegrid">
        <div class="hg-col">
          <div class="hg-title">Next 7 days</div>
          <div id="homeCal" class="homecal"><div class="empty">loading…</div></div>
        </div>
        <div class="hg-col">
          <div class="hg-title">Tasks <span class="hint">to-dos + scheduled content</span></div>
          <div id="clList" class="hs-cl"><div class="empty">loading…</div></div>
          <form class="hs-add" onsubmit="return addChecklist(event)">
            <input id="clInput" class="hs-input" placeholder="Add a task…" maxlength="200" autocomplete="off">
            <button type="submit" class="btn green hs-addbtn">Add</button>
          </form>
        </div>
      </div>
    </section>
    <section>
      <h2> Live activity <span class="livedot"></span> <span class="hint">your crew, working right now</span>
        <button class="seeall" onclick="showView('engage')">see all</button></h2>
      <div class="homefeed" id="homeFeed"><div class="empty">loading…</div></div>
    </section>
  </div>

  <div class="view" id="view-studio">
    <h1 class="vtitle">Launchpad</h1>
    <div class="vsub">Drop content — Sage writes the post, cuts the Stories, and schedules it all.</div>
    <div class="subtabs">
      <button class="subtab" data-sv="film" onclick="showView('film')">Create</button>
      <button class="subtab" data-sv="studio" onclick="showView('studio')">Load</button>
      <button class="subtab" data-sv="engage" onclick="showView('engage')">Engagement</button>
      <button class="subtab" data-sv="analytics" onclick="showView('analytics')">Insights</button>
    </div>
    <section>
      <h2> Sage Scheduler <span class="hint">Sage writes the post from what it hears, then schedules it</span></h2>
      <div id="briefBanner" class="briefbanner" style="display:none"></div>
      <div class="postingto">
        <span class="ptlabel">Posting to</span>
        <select id="scPage" class="ptselect" onchange="showScMode()"></select>
        <span class="hint" id="scPageMode"></span>
      </div>
      <div class="sstep">
        <div class="snum">1</div>
        <div class="sbody">
          <div class="slabel">Drop content <span class="sub">Sage watches &amp; listens to it</span></div>
          <div id="museLanding" class="museland museinline muserow" onclick="document.getElementById('mlVideo').click()">
            <div class="mldogbox">
              <div class="floatfx" id="floatFx"></div>
              <div class="mldog" id="mlDog" data-pose="greet" data-state="bob">
                <img loading="lazy" decoding="async" class="mldogimg pose-greet" src="/crew/dog_greet.png" alt="Sage">
                <img loading="lazy" decoding="async" class="mldogimg pose-beckon" src="/crew/dog_beckon.png" alt="">
                <img loading="lazy" decoding="async" class="mldogimg pose-holdcd" src="/crew/dog_holdcd.png" alt="">
                <img loading="lazy" decoding="async" class="mldogimg pose-earbuds" src="/crew/dog_earbuds.png" alt="">
                <img loading="lazy" decoding="async" class="mldogimg pose-run" src="/crew/dog_run.png" alt="">
              </div>
            </div>
            <div class="mlplayerbox">
              <div class="mlprop mlplayer" id="mlPlayer"><svg viewBox="0 0 120 92">
                <rect x="4" y="4" width="112" height="84" rx="16" fill="#d2dae7" stroke="#8793a8" stroke-width="2"/>
                <image class="pldisc" href="/crew/cd.png" x="30" y="15" width="60" height="60"/>
                <rect x="86" y="70" width="22" height="7" rx="3" fill="#8793a8"/></svg></div>
              <div class="mlprop mlcd" id="mlCD"><img src="/crew/cd.png" alt=""></div>
            </div>
            <input type="file" id="mlVideo" accept="video/*" multiple hidden onchange="videoDrop(this.files)">
          </div>
        </div>
      </div>
      <div id="schedBusy" class="hint" style="display:none; margin:0 0 14px 44px"></div>
      <div id="schedCard"></div>
    </section>
    <section>
      <h2> Upcoming <span class="hint">scheduled reels &amp; their stories</span></h2>
      <div id="upcomingWrap"><div class="empty">nothing scheduled yet — drop content above</div></div>
    </section>
  </div>

  <div class="view" id="view-film">
    <h1 class="vtitle">Launchpad</h1>
    <div class="vsub">Type a script and let Sage help.</div>
    <div class="subtabs">
      <button class="subtab" data-sv="film" onclick="showView('film')">Create</button>
      <button class="subtab" data-sv="studio" onclick="showView('studio')">Load</button>
      <button class="subtab" data-sv="engage" onclick="showView('engage')">Engagement</button>
      <button class="subtab" data-sv="analytics" onclick="showView('analytics')">Insights</button>
    </div>
    <div id="rxHelp" class="rxhelp" style="display:none" role="dialog" aria-modal="true" aria-labelledby="rxHelpT" onclick="if(event.target===this)rxHelpClose()">
      <div class="rxhelp-card">
        <div class="rxhelp-head"><h3 id="rxHelpT">How recording works</h3><button type="button" class="rxhelp-x" onclick="rxHelpClose()" aria-label="Close">&#10005;</button></div>
        <div class="rxhelp-body">
          <h4>Recording</h4>
          <ul>
            <li><b>Red button</b> starts, pauses or resumes a take. Pausing keeps the same take going.</li>
            <li><b>Resuming after a pause</b> counts down 3 seconds first, so you can catch a breath.</li>
            <li><b>X</b> at the top right ends the take (it stays in the strip) and turns hands-free off. Nothing starts again until you press record or say <i>new take</i>.</li>
            <li><b>Length</b> (right rail): set a time limit, or say <i>Abra set length to thirty seconds</i> or <i>Abra no limit</i>.</li>
            <li><b>Captions, Teleprompter and Camera</b> are on the right rail. <i>Show the sides</i> is in the camera panel.</li>
            <li><b>Smooth cut</b> trims the silent gaps in the latest take.</li>
            <li><b>Talk time (estimated)</b>, in the teleprompter panel, is a guess from the sound in the speaking range. It cannot tell who is speaking. Say <i>talk time</i> to hear it.</li>
            <li><b>Teleprompter</b>: type or paste a script, choose a .txt or .md file, or have Sage write one from a summary and up to three links (Add a script). Your script is saved to Abra, so it follows you to any device. Step with <i>Next line</i>, and set the speed and text size. It scrolls only while the take records.</li>
            <li><b>Arrow</b> (right of the camera) sends the latest take to Load, where Sage writes the post.</li>
          </ul>
          <h4>Hands-free gestures</h4>
          <p class="rxhelp-note">Show each gesture out to the side of the picture, not inside the framed shot.</p>
          <ul>
            <li><b>Index finger up</b> starts a take. It counts down 3 seconds, then records.</li>
            <li><b>Both hands up</b>, held out to the side of the picture, starts a take with a 10-second countdown.</li>
            <li><b>Two fingers up</b> (victory sign) pauses.</li>
            <li><b>Closed fist</b> stops. The take is finished and goes into the strip. It also cancels a countdown that has started.</li>
            <li><b>Wave while paused</b> deletes the previous take. Undo stays on screen for 6 seconds.</li>
            <li><b>Four fingers out</b>, thumb folded, deletes the last take when no take is running. Undo stays on screen for 6 seconds.</li>
            <li><b>After a stop:</b> wipe a flat hand across to trim the silent gaps, or push the hand toward the camera to delete the last take.</li>
          </ul>
          <h4>Say &ldquo;Abra&rdquo; and&hellip;</h4>
          <ul class="rxhelp-voice">
            <li><i>Record</i>, <i>pause</i>, <i>resume</i>, <i>stop</i>, <i>new take</i>, <i>delete last take</i></li>
            <li><i>Create a video that is two minutes</i> (sets the length and records), <i>record in five seconds</i></li>
            <li><i>Set length to thirty seconds</i>, <i>stop after twenty seconds</i>, <i>no limit</i>, <i>how long</i></li>
            <li><i>Send to load</i>, <i>hand to Sage</i>, <i>play it back</i>, <i>how many takes</i></li>
            <li><i>Captions on</i> or <i>off</i>, <i>teleprompter on</i>, <i>hands-free on</i>, <i>show the sides</i>, <i>smooth cut</i></li>
            <li><i>Open Create</i>, <i>Load</i>, <i>Engagement</i>, <i>Insights</i>, <i>Home</i>, <i>Account</i></li>
            <li><i>Approve</i>, then <i>confirm</i> or <i>cancel</i>; <i>scroll down</i>, <i>scroll up</i>, <i>help</i></li>
            <li><i>Turn voice off</i>, <i>hear replies on</i> or <i>off</i>, <i>open the chat</i>, <i>close the chat</i></li>
            <li><i>Pause engagement</i>, <i>resume engagement</i>, <i>undo</i>, <i>talk to Nico</i>, <i>talk to Sage</i>, <i>back to Abra</i></li>
            <li><i>Delete take two</i>, <i>send take one to Sage</i>, <i>send this to load</i>, <i>save take three</i>, <i>cancel the countdown</i></li>
            <li><i>Switch microphone</i>, <i>use the second microphone</i>, <i>mirror on</i> or <i>off</i>, <i>brighter</i> or <i>darker</i>, <i>set brightness to 40</i>, <i>talk time</i></li>
            <li><i>Approve replies on</i> or <i>off</i>, <i>approve the next reply</i> (Abra asks you to confirm), <i>write a script about</i> your topic, <i>make it thirty seconds</i>, <i>use that</i>, <i>undo script</i>; <i>next line</i>, <i>scroll down</i> and <i>scroll up</i> work during a take; <i>faster</i>, <i>slower</i>, <i>bigger text</i>, <i>edit script</i>, <i>save script</i> need <i>Abra</i> first</li>
            <li><i>Show the best time</i>, <i>show the reach trend</i>, <i>show my watch-through</i>, <i>show my spend</i>, <i>show upcoming</i>, <i>show drafts</i></li>
          </ul>
          <p class="rxhelp-note">You can also say the name of any button on screen, for example <i>Abra choose</i> followed by an option.</p>
        </div>
      </div>
    </div>
    <section id="rxStudio" class="cam-off">
      <h2> Record <span class="hint">pick your framing, hit record</span></h2>
      <div class="rxfmt">
        <button class="rxphonebtn" onclick="rxAddCam()">Add a camera from another device</button>
        <button type="button" class="rxhelpq" onclick="rxHelpOpen()" aria-label="How recording works" title="How recording works, including the hand gestures">?</button>
      </div>
      <div id="rxPhonePanel" class="rxphone" style="display:none">
        <img id="rxQr" class="rxqr" alt="QR to connect a phone camera">
        <div class="rxphone-info">
          <div class="rxphone-t">Use your phone as a wireless camera</div>
          <p>On the <b>same wifi</b>, scan this with your phone — or open <a id="rxLanLink" href="#" target="_blank" rel="noopener">the link</a>. Tap through the one-time security notice, then allow the camera. Your phone becomes the lens for <b>this</b> recorder — flip between <b>front and back</b> right on the phone. You still frame, time and hand off here.</p>
          <div class="rxphone-status" id="rxPhoneStatus">Waiting for a phone to connect…</div>
          <span class="rxphone-note">Streams phone → computer over your local wifi. The QR is drawn on this computer. Browsers also contact Google's public STUN server to set up the link.</span>
        </div>
      </div>
      <div id="rxHfPanel" class="rxhf" style="display:none">
        <div class="rxhf-info">
          <div class="rxhf-t">Hands-free — run recording with off-frame gestures</div>
          <div class="rxhf-legend"><span>one finger <b>start</b></span><span>peace <b>pause</b></span><span>fist <b>stop</b></span><span>wipe across <b>trim dead air</b></span><span>push toward cam <b>delete last</b></span></div>
          <span class="rxhf-note">Runs on your device (Google MediaPipe). Hold your hand out to the side of the frame. It controls recording and never shows up in the shot.</span>
        </div>
      </div>
      <div class="rxfstage">
        <div class="rxcamwrap fmt-portrait" id="rxCamWrap" onclick="rxStartCam()">
          <video id="rxCam" class="rxcam" autoplay muted playsinline onloadedmetadata="rxUpdateCropBox()"></video>
          <canvas id="rxBgView" class="rxbgview" style="display:none"></canvas>
          <div class="rxcropbox" id="rxCropBox" style="display:none"></div>
          <div class="rxdbg" id="rxDbg"></div>
          <video id="rxGhost" muted playsinline autoplay style="position:fixed;left:-9999px;top:0;width:2px;height:2px;opacity:0.01;pointer-events:none"></video>
          <div class="rxfaceguide"><span>keep your face about this big</span></div>
          <div class="rxhud"><span class="rxmicdot" id="rxMicDot"></span><span class="rxtimer" id="rxTimer">00:00</span></div>
          <div class="rxcammsg" id="rxCamMsg">Tap to connect your camera</div>
          <div class="rxcountdown" id="rxCountdown" style="display:none">3</div>
          <div class="rxgmsg" id="rxGMsg" style="display:none" aria-live="polite"></div>
          <div class="rxhf-status" id="rxHfStatus" style="display:none" aria-live="polite">Turn on to load the hand model…</div>
          <div class="rxstart-pop"><div class="rxstart-card"><div class="rxstart-t">Your camera is off</div><button class="btn green" id="rxStartBtn" onclick="event.stopPropagation();rxStartCam()">Start camera</button></div></div>
          <button class="rxcam-x" type="button" aria-label="Reset camera" title="Reset camera" onclick="event.stopPropagation();rxResetCamera()">&#10005;</button>
          <button class="btn green rxrec-float" id="rxRecBtn" aria-label="Record" title="Record" onclick="event.stopPropagation();rxToggleRec()"></button>
          <button class="rxnext-arrow" id="rxNextBtn" type="button" onclick="rxNext()" disabled title="Send the latest take to Load" aria-label="Send the latest take to Load">&#8594;</button>
        <div class="rxrail" onclick="event.stopPropagation()">
          <button class="rxrail-b" id="rxHfBtn" type="button" onclick="rxToggleHandsFree()" title="Hands-free" aria-label="Hands-free"><span class="rxrail-i"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="2"/><path d="M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.2 5.2a9.5 9.5 0 0 0 0 13.6M18.8 5.2a9.5 9.5 0 0 1 0 13.6"/></svg></span></button>
          <button class="rxrail-b" data-fly="len" type="button" aria-expanded="false" onclick="rxFly('len')" title="Length" aria-label="Length"><span class="rxrail-i"><svg viewBox="0 0 24 24"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2M9.5 2.5h5"/></svg></span></button>
          <button class="rxrail-b" data-fly="prompt" type="button" aria-expanded="false" onclick="rxFly('prompt')" title="Teleprompter" aria-label="Teleprompter"><span class="rxrail-i"><svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M7 9.5h10M7 13h10M7 16.5h6"/></svg></span></button>
          <button class="rxrail-b" data-fly="cap" type="button" aria-expanded="false" onclick="rxFly('cap')" title="Captions" aria-label="Captions"><span class="rxrail-i"><svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M10.2 10.2a2.2 2.2 0 1 0 0 3.6M16.2 10.2a2.2 2.2 0 1 0 0 3.6"/></svg></span></button>
          <button class="rxrail-b" id="rxGearBtn" data-fly="cam" type="button" aria-expanded="false" aria-controls="rxFly" onclick="rxFly('cam')" title="Camera" aria-label="Camera"><span class="rxrail-i"><svg viewBox="0 0 24 24"><path d="M4 8.5h3l1.8-2.5h6.4L17 8.5h3V19H4z"/><circle cx="12" cy="13.5" r="3.2"/></svg></span></button>
        </div>
        <div class="rxfly" id="rxFly" style="display:none" onclick="event.stopPropagation()">
          <div class="rxfly-p" data-fly-panel="len"><div class="rxfly-h">Length</div>
          <div class="rxlen">
            <div class="rxlen-l">Record for <span class="rxlen-hint" id="rxLenHint">Record as long as you like; Sage takes clips up to 200 MB.</span></div>
            <div class="rxlen-chips">
              <button class="rxlenchip on" data-sec="0" onclick="rxSetTarget(0)">Free</button>
              <button class="rxlenchip" data-sec="15" onclick="rxSetTarget(15)">0:15</button>
              <button class="rxlenchip" data-sec="30" onclick="rxSetTarget(30)">0:30</button>
              <button class="rxlenchip" data-sec="60" onclick="rxSetTarget(60)">1:00</button>
              <button class="rxlenchip" data-sec="90" onclick="rxSetTarget(90)">1:30</button>
              <button class="rxlenchip mic" onclick="rxSayTarget()">Say it</button>
            </div>
          </div>
          </div>
          <div class="rxfly-p" data-fly-panel="prompt"><div class="rxfly-h">Teleprompter &amp; talk time</div>
          <div class="rxtalk"><div class="rxtalk-l">Talk time <span class="hint">(estimated)</span> <b id="rxTalkPct">not measured</b></div>
            <div class="rxtalkbar"><i id="rxTalkBar"></i></div>
            <div class="rxtalk-goal" id="rxTalkGoal">Abra's target: 1/3+ of the clip is you talking. This is estimated from sound, so it cannot tell who is speaking.</div></div>
          <div class="rxpromptlabel">Teleprompter <span class="hint">type or paste your script</span></div>
          <div class="rxprompter" id="rxPrompter"><div class="empty">Type your script with Edit script.</div></div>
          <details class="rxprompt-add" id="rxPromptAdd">
            <summary>Add a script</summary>
            <div class="rxprompt-sub">Read a script you already have</div>
            <div class="rxprompt-tools">
              <button class="rxlenchip" type="button" onclick="rxPromptEdit()">Paste or type it</button>
              <button class="rxlenchip" type="button" onclick="rxPromptPick()">Choose a text file</button>
              <input type="file" id="rxPromptFile" accept=".txt,.md,text/plain,text/markdown" style="display:none" onchange="rxPromptFileIn(this.files[0]); this.value='';">
            </div>
            <div class="rxprompt-drop" id="rxPromptDropZone" ondragover="event.preventDefault()" ondrop="rxPromptDropFile(event)">Or drop a .txt or .md file here</div>
            <div class="rxprompt-sub">Or have Sage write one</div>
            <textarea id="rxPromptSum" class="rxtext" rows="3" aria-label="What the video is about"></textarea>
            <textarea id="rxPromptLinks" class="rxtext" rows="2" aria-label="Links to web pages to read, one per line"></textarea>
            <div class="rxprompt-tools">
              <label class="rxprompt-v">Length <select id="rxPromptSecs" class="rxcamsel"><option value="30">30 seconds</option><option value="60" selected>1 minute</option><option value="90">90 seconds</option><option value="120">2 minutes</option></select></label>
              <button class="btn green" type="button" onclick="rxPromptWrite()">Write it with Sage</button>
            </div>
            <div class="rxhf-note">Links are read as text only. Sage cannot watch a video on another site, so for a video link it works from the page text.</div>
            <div id="rxPromptDraft" style="display:none"></div>
          </details>
          <div class="rxprompt-tools">
            <button class="rxlenchip" type="button" onclick="rxPromptNext()">Next line</button>
            <button class="rxlenchip" type="button" onclick="rxPromptSpeed(-1)">Slower</button>
            <span class="rxprompt-v" id="rxPromptSpeedV">Speed 3</span>
            <button class="rxlenchip" type="button" onclick="rxPromptSpeed(1)">Faster</button>
            <button class="rxlenchip" type="button" onclick="rxPromptSize(-2)" aria-label="Smaller text">A&minus;</button>
            <button class="rxlenchip" type="button" onclick="rxPromptSize(2)" aria-label="Bigger text">A+</button>
            <button class="rxlenchip" type="button" onclick="rxPromptEdit()">Edit script</button>
          </div>
          <div id="rxPromptEditBox" style="display:none">
            <textarea id="rxPromptText" class="rxtext" rows="6" aria-label="Your script, one line for each thing you say"></textarea>
            <div class="rxprompt-tools">
              <button class="btn green" type="button" onclick="rxPromptSave()">Save script</button>
              <button class="rxlenchip" type="button" onclick="rxPromptCancel()">Cancel</button>
            </div>
          </div>
          <div class="rxprompt-tools" id="rxPromptPlanBox" style="display:none">
            <span class="hint">Your saved script stays until you choose Sage's plan.</span>
            <button class="rxlenchip" type="button" onclick="rxPromptUsePlan()">Use Sage's plan instead</button>
            <button class="rxlenchip" type="button" onclick="rxPromptUndo()">Undo</button>
          </div>
          <div class="rxhf-note" id="rxPromptMsg"></div>
</div>
          <div class="rxfly-p" data-fly-panel="cap"><div class="rxfly-h">Captions</div>
          <label class="rxcaptog"><input type="checkbox" id="rxCapChk" checked onchange="rxSetCaptions(this.checked)">
            <span><b>Auto-captions</b> — Muse Voice's transcript is burned onto your Reel and Stories when you schedule, in Studio, on auto-publish pages, and in batches. Reels need portrait 9:16 video.</span></label>
          </div>
          <div id="rxCamSet" class="rxcamset rxfly-p" data-fly-panel="cam" style="display:none" role="region" aria-label="Camera settings">
        <div class="rxcamset-row">
          <span class="rxfmt-l">Camera view</span>
          <button class="rxlenchip" id="rxWideBtn" type="button" aria-pressed="false" onclick="rxToggleWide()">Show the sides</button>
        </div>
        <div class="rxcamset-row rxroom-row">
          <label class="rxfmt-l" for="rxZoomSl">Room for your hand</label>
          <input type="range" id="rxZoomSl" aria-label="Room for your hand" min="100" max="180" value="120" oninput="rxSetZoom(this.value, true)">
          <span id="rxZoomV" class="rxroom-v">1.2×</span>
        </div>
        <div class="rxhf-note" id="rxRoomNote">Room sets how far out your hand can be, and it tightens the picture to match. It stays where you set it. It cannot change while a take is running.</div>
        <div class="rxcamset-row">
          <label class="rxfmt-l" for="rxMirrorChk">Mirror preview</label>
          <input type="checkbox" id="rxMirrorChk" checked onchange="rxSetMirror(this.checked)">
          <span class="hint">Your saved take is never mirrored, so text and logos read correctly.</span>
        </div>
        <div class="rxcamset-row rxroom-row" id="rxBrightRow" style="display:none">
          <label class="rxfmt-l" for="rxBrightSl">Brightness</label>
          <input type="range" id="rxBrightSl" aria-label="Brightness" min="0" max="1" step="0.1" value="0.5" oninput="rxSetBrightness(this.value)">
          <span id="rxBrightV" class="rxroom-v">0.5</span>
        </div>
        <div class="rxhf-note" id="rxCapNote" style="display:none"></div>
        <div id="rxWideBox" class="rxwidebox" style="display:none">
          <canvas id="rxMonitor" class="rxmonitor" width="640" height="360"></canvas>
          <div class="rxhf-note">The whole camera frame. The dashed box is exactly what gets recorded. Hand signals are read from this full frame, so gestures out to the sides still count. Start the camera to see it.</div>
        </div>
        <div class="rxcamset-row">
          <span class="rxfmt-l">Start countdown</span>
          <select id="rxCountSel" class="rxcamsel" onchange="rxSetStartCount(this.value)"><option value="3">3 seconds</option><option value="5">5 seconds</option><option value="10">10 seconds</option></select>
        </div>
        <div class="rxcamrow" id="rxCamRow" style="display:flex">
        <label class="rxcamrow-l" for="rxCamSel">Camera</label>
        <select id="rxCamSel" class="rxcamsel" onchange="rxCamChange()"><option>Looking for cameras…</option></select>
        <span class="hint">use your phone if it is connected as a webcam (Continuity Camera, Camo…)</span>
        </div>
        <div class="rxhf-note">A phone connected as a webcam appears in this list.</div>
        <div class="rxcamrow" id="rxMicRow" style="display:flex">
        <label class="rxcamrow-l" for="rxMicSel">Microphone</label>
        <select id="rxMicSel" class="rxcamsel" onchange="rxMicChange()"><option value="">System default</option></select>
        <span class="hint">Recordings use this microphone. Voice commands always listen on this computer's default microphone.</span>
        </div>
      </div>
        </div>
        </div>
      </div>
      <div class="rxcontrols">
        <button class="btn" onclick="rxFinish()">Done</button>
        <a class="btn ghost" id="rxDownload" download="reaction.webm" style="display:none">Download</a>
        <button class="btn" id="rxCutBtn" onclick="rxSmoothCut()" style="display:none">Smooth cut</button>
        <button class="btn green" id="rxHandBtn" onclick="rxHandToSage()" style="display:none">Hand to Sage →</button>
      </div>
      <div id="rxClips" class="rxclips" style="display:none"></div>
      <div id="rxUndo" class="rxundo" style="display:none"><span class="rxundo-t"></span><button class="rxundo-b" onclick="rxUndoDo()">Undo</button></div>
    </section>
    <section style="display:none">
      <h2> Templates <span class="hint">Sage writes your hook, beats &amp; lines</span></h2>
      <div id="filmGenres" class="filmgenres"><div class="empty">loading templates…</div></div>
      <div id="rxBgPanel" class="rxbgpanel" style="display:none">
        <div class="rxbg-h">Greenscreen background</div>
        <label class="rxcaptog"><input type="checkbox" id="rxBgOn" onchange="rxBgSetOn(this.checked)">
          <span><b>Greenscreen</b> &mdash; you are cut out of your camera and placed in front of a background clip. A plain wall or a green screen gives the cleanest cut-out.</span></label>
        <div class="rxbg-row"><label class="rxbg-l" for="rxBgFile">Background clip (your own)</label>
          <input type="file" id="rxBgFile" accept="video/*" onchange="rxBgPick(this.files[0])"></div>
        <div class="rxbg-row"><label class="rxbg-l" for="rxBgVoiceSl">Your voice</label>
          <input type="range" id="rxBgVoiceSl" min="0" max="150" value="100" oninput="rxBgVol('voice', this.value)">
          <span id="rxBgVoiceV" class="rxroom-v">100%</span></div>
        <div class="rxbg-row"><label class="rxbg-l" for="rxBgBgSl">Background sound</label>
          <input type="range" id="rxBgBgSl" min="0" max="150" value="60" oninput="rxBgVol('bg', this.value)">
          <span id="rxBgBgV" class="rxroom-v">60%</span></div>
        <div class="rxbg-row"><label class="rxbg-l" for="rxBgZoomSl">Zoom</label>
          <input type="range" id="rxBgZoomSl" min="60" max="100" value="100" aria-label="Greenscreen zoom" oninput="rxBgZoom(this.value)">
          <span id="rxBgZoomV" class="rxroom-v">100%</span></div>
        <div class="rxhf-note" id="rxBgNote">Pick a background clip, then record. Your clip plays behind you and is mixed with your voice.</div>
      </div>
      <div id="filmInputs" class="filminputs" style="display:none"></div>
      <div id="rxPlan" class="rxplan"></div>
    </section>
  </div>

  <div class="view" id="view-engage">
    <h1 class="vtitle">Launchpad</h1>
    <div class="vsub">The latest replies and likes as they happen — and boost the posts worth pushing.</div>
    <div class="subtabs">
      <button class="subtab" data-sv="film" onclick="showView('film')">Create</button>
      <button class="subtab" data-sv="studio" onclick="showView('studio')">Load</button>
      <button class="subtab" data-sv="engage" onclick="showView('engage')">Engagement</button>
      <button class="subtab" data-sv="analytics" onclick="showView('analytics')">Insights</button>
    </div>
    <div id="replyQueueCard" style="display:none;margin:8px 0 14px;padding:12px 14px;border-radius:14px;background:var(--card2);border:1px solid var(--border)">
      <div class="lab">Replies waiting for you</div>
      <div class="cap">Abra drafted these. Nothing is posted until you approve it.</div>
      <div id="replyQueueList" style="margin-top:8px"></div>
      <div id="replyQueueMsg" class="hint" style="margin-top:6px"></div>
      <div id="replyApprovals" style="margin-top:8px"></div>
    </div>
    <div class="tiles">
      <div class="tile"><div class="lab">Replies</div><div class="val" id="tReplies">–</div><div class="cap">last 24 hours</div></div>
      <div class="tile"><div class="lab">Likes</div><div class="val" id="tLikes">–</div><div class="cap">last 24 hours</div></div>
      <div class="tile"><div class="lab">Skipped</div><div class="val" id="tSkips">–</div><div class="cap">comments Abra chose not to answer</div></div>
      <div class="tile"><div class="lab">Pace</div><div class="val" id="tPace">–</div>
        <div class="cap" id="paceCap">per 15 min</div><div class="meter"><i id="paceBar" style="width:0%"></i></div></div>
      <div class="tile"><div class="lab">Muse checks</div><div class="val" id="tReview">–</div>
        <div class="cap" id="tReviewCap">drafts checked by Muse before posting (simple praise skips it)</div></div>
      <div class="tile"><div class="lab">FB Health</div><div class="val" id="tHealth">–</div>
        <div class="cap" id="tHealthCap">flags &amp; throttles</div></div>
    </div>
    <div class="banner" id="throttleBanner"></div>
    <div class="engpresenter" id="engPresenter">
      <div class="engcatwrap" id="engCatWrap">
        <img loading="lazy" decoding="async" class="engcat engbase" src="/crew/cat.png" alt="Nico">
        <img loading="lazy" decoding="async" class="engcat engwalk" src="/crew/cat_walk.png" alt="">
        <img loading="lazy" decoding="async" class="engcat engblink" src="/crew/cat_blink.png" alt="">
        <span class="engheart" id="engHeart"></span>
      </div>
      <div class="engbubble" id="engBubble">
        <span class="engsay" id="engSay">watching the comments roll in…</span><span class="engcaret"></span>
        <span class="engtype" id="engType"><i></i><i></i><i></i></span>
      </div>
    </div>
    <details class="collap">
      <summary> Live activity <span class="hint">updates every 5 seconds</span></summary>
      <div class="collap-body">
        <div class="flist" id="feed"><div class="empty">loading…</div></div>
      </div>
    </details>
    <details class="collap">
      <summary> Fan Love <span class="hint">your hottest threads — reloads when you open this tab (cached 10 min)</span></summary>
      <div class="collap-body">
        <p class="sub" style="margin:2px 0 10px">Fans talking back. Tap a thread to open it on Facebook,
        see who they are, and make them feel special — reply as yourself and invite them to follow.</p>
        <div id="fanloveWrap"><div class="empty">loading…</div></div>
      </div>
    </details>
    <section style="display:none">
      <h2> Direct messages <span class="hint">Direct messages are not connected yet</span></h2>
      <div id="dmModes" class="dmmodes"></div>
      <div id="dmWrap"><div class="empty">loading…</div></div>
    </section>
    <section id="adsWrap">
      <h2> Ads <span class="hint">posts running ads: the crew gives their comments priority</span></h2>
      <div class="adsnote">Mark a video as running an ad with "Running an ad?" in Boost videos below. Abra does not launch or detect ads yet, so only the posts you mark are treated as ad posts.</div>
      <div id="adsBody"><div class="empty">loading…</div></div>
    </section>
    <section>
      <h2> Boost videos <span class="hint">prioritize engagement on posts you pick</span></h2>
      <div class="pagefilter" id="pageFilter"></div>
      <div class="tabs" style="margin-bottom:12px;">
        <button class="tab active" id="tab-recent" onclick="setTab('recent')">Most Recent</button>
        <button class="tab" id="tab-viral" onclick="setTab('viral')">Most Viral</button>
        <button class="tab" id="tab-trending" onclick="setTab('trending')">Trending</button>
        <button class="tab" id="tab-fresh" onclick="setTab('fresh')">Past 24h</button>
        <button class="tab" id="tab-earners" onclick="setTab('earners')">Top Earners</button>
      </div>
      <div class="vlist" id="videos"><div class="empty">loading…</div></div>
      <div class="row" style="padding-top:12px; border-top:1px solid var(--border); margin-top:10px;">
        <button class="btn" id="submitBtn" onclick="submitTargets()" style="opacity:.55;">Submit</button>
        <button class="btn ghost" id="clearAllBtn" onclick="clearAllTargets()">Clear</button>
        <span id="submitMsg" class="hint">no changes</span>
      </div>
    </section>
  </div>

  <div class="view" id="view-deals">
    <h1 class="vtitle">Brand Deals</h1>
    <div class="vsub">Draft the pitch here, then copy it. Every email still sends from you. Track each deal from hello to paid.</div>
    <section>
      <h2> Closed this week</h2>
      <div class="bigstat" style="display:flex; gap:18px; align-items:center">
        <div><b id="qClosed" style="font-size:22px">–</b><span style="font-size:10.5px; color:var(--ink3); display:block">CLOSED THIS WEEK</span></div>
        
        
      </div>
    </section>
    <section>
      <h2> Add a brand</h2>
      <div style="display:flex; gap:8px; flex-wrap:wrap">
        <input id="dBrand" placeholder="Brand name" style="flex:2; min-width:140px">
        <input id="dContact" placeholder="Contact / email (optional)" style="flex:2; min-width:140px">
        <input id="dValue" placeholder="Est. value $" style="flex:1; min-width:80px">
        <input id="dNotes" placeholder="Angle / notes for the pitch" style="flex:3; min-width:180px">
        <button class="btn green" onclick="addDeal()">Add</button>
      </div>
    </section>
    <section>
      <h2> Pipeline</h2>
      <div id="dealsWrap"><div class="empty">no deals yet — add your first brand above </div></div>
    </section>
    <section id="pitchBox" style="display:none">
      <h2> Draft <button class="btn" style="float:right" onclick="copyPitch()">Copy</button></h2>
      <textarea id="pitchText" style="width:100%; min-height:220px; background:var(--card2);
        color:var(--ink); border:1px solid var(--border); border-radius:10px; padding:10px;
        font:13px/1.5 -apple-system,sans-serif"></textarea>
      <div class="hint">Review, tweak, and send it yourself from your email — your name, your send.</div>
    </section>
  </div>
  <div class="view" id="view-analytics">
    <h1 class="vtitle">Launchpad</h1>
    <div class="vsub">Daily reach and how long people watch.</div>
    <div class="subtabs">
      <button class="subtab" data-sv="film" onclick="showView('film')">Create</button>
      <button class="subtab" data-sv="studio" onclick="showView('studio')">Load</button>
      <button class="subtab" data-sv="engage" onclick="showView('engage')">Engagement</button>
      <button class="subtab" data-sv="analytics" onclick="showView('analytics')">Insights</button>
    </div>
    <section>
      <h2> Creator Spotlight <span class="hint">type a genre — Sage writes an AI read from general knowledge on short-form formats, plus fresh ideas</span></h2>
      <div class="spotrow">
        <input type="text" id="spotQ" placeholder="e.g. what-if, cooking hacks, nostalgia, car reviews" onkeydown="if(event.key==='Enter'){runSpotlight();}">
        <button class="btn" onclick="runSpotlight()">Spotlight</button>
      </div>
      <div id="spotOut"><div class="spot-presets"><span class="spottl">Try a genre</span><button class="spottrend" onclick="spotPreset('nostalgia')">nostalgia</button><button class="spottrend" onclick="spotPreset('cooking hacks')">cooking hacks</button><button class="spottrend" onclick="spotPreset('what-if')">what-if</button><button class="spottrend" onclick="spotPreset('car reviews')">car reviews</button><button class="spottrend" onclick="spotPreset('DIY fails')">DIY fails</button></div></div>
    </section>
    <section>
      <h2> Best time to post <span class="hint">when your posts actually land — learned from your own history (ET)</span></h2>
      <div class="bt-head"><span id="btHead">reading your history…</span><span class="bt-conf" id="btConf"></span><span class="bt-sweet" id="btSweet"></span></div>
      <div class="bt-gridwrap"><div class="bt-grid" id="btGrid"></div></div>
      <div class="bt-legend"><span>less</span><i class="bt-ramp"></i><span>more engagement / post</span></div>
      <div class="bt-foot" id="btFoot"></div>
    </section>
    <section>
      <h2> Sage's read <span class="hint">Muse Wonder reads your real data and tells you what to do</span>
        <button class="btn ghost" style="float:right; padding:4px 11px; font-size:12px" onclick="loadAnalyst(true)">Refresh</button></h2>
      <div id="analystWrap" class="analystbox"><div class="empty">Sage is reading your numbers…</div></div>
      <div id="analystIdeas"></div>
    </section>
    <section>
      <h2> Watch-through <span class="hint">how far into each reel viewers stay — your real retention</span></h2>
      <div id="watchInsight" class="empty"></div>
      <div id="watchWrap"><div class="empty">loading…</div></div>
    </section>
    <section>
      <h2> Reach trend <span class="hint">daily, per page</span></h2>
      <div id="trendWrap"><div class="empty">loading…</div></div>
    </section>
  </div>

  <div class="view" id="view-profile">
    <h1 class="vtitle">Account</h1>
    <div class="vsub">Your account, pages, money &amp; settings — all in one place.</div>
    <div class="subtabs">
      <button class="subtab" data-sv="profile" onclick="showView('profile')">Profile</button>
      <button class="subtab" data-sv="help" onclick="showView('help')">Help</button>
    </div>
    <section>
      <div class="profhero">
        <img id="profHeroPic" class="profhero-pic" alt="" style="display:none">
        <div id="profHeroInit" class="profhero-init">—</div>
        <div class="profhero-info">
          <div class="profhero-name" id="profHeroName">—</div>
          <div class="profhero-sub" id="profHeroSub"></div>
          <div class="profhero-badges">
            <span class="pbadge ok"><span class="pbdot"></span> Connected via Facebook</span>
          </div>
        </div>
      </div>
    </section>
    <section>
      <h2> At a glance</h2>
      <div class="billcards">
        <div class="billcard"><div class="blabel">Member since</div>
          <div class="bval" id="profSince">—</div><div class="bsub">first connected</div></div>
      </div>
    </section>
    <section>
      <h2> Connected pages <span class="hint">toggle engagement &amp; set how posts publish, per page</span></h2>
      <div id="pages" class="pagelist"></div>
    </section>
    <section>
      <h2> Connect a page</h2>
      <p class="sub" style="margin:2px 0 10px">Log in with Facebook and pick which pages Abra should manage.
      You can connect any page where you're an admin, editor, or moderator.</p>
      <a class="btn green" href="/connect" target="_blank" rel="noopener"> Connect a Facebook page</a>
    </section>
    <h2 class="acct-div"> Money <span class="hint">earnings, AI costs &amp; Koa's desk</span></h2>
    <div class="engpresenter" id="koaPresenter">
      <div class="engcatwrap" id="koaWrap">
        <img loading="lazy" decoding="async" class="engcat engbase" src="/crew/koala.png" alt="Koa">
        <img loading="lazy" decoding="async" class="engcat engwalk" src="/crew/koala_walk.png" alt="">
        <img loading="lazy" decoding="async" class="engcat engblink" src="/crew/koala_blink.png" alt="">
        <span class="engheart" id="koaHeart"></span>
      </div>
      <div class="engbubble">
        <span class="engsay" id="koaSay">balancing the books…</span><span class="engcaret"></span>
        <span class="engtype"><i></i><i></i><i></i></span>
      </div>
    </div>
    <section id="standingWrap">
      <h2> Page standing <span class="hint">monetization &amp; account health</span></h2>
      <div id="standingBody" class="empty">loading…</div>
    </section>
    <section>
      <h2> Monetization <span class="hint">what you earn &amp; what you're eligible for · thresholds are Abra's working numbers; confirm in Meta's monetization settings</span></h2>
      <div id="monPages" class="monpages" style="display:none"></div>
      <div id="monWrap"><div class="empty">loading…</div></div>
    </section>
    <div id="budgetWrap"></div>
    <section>
      <h2> Earnings &amp; spending <span class="hint">what you earn vs. Abra's estimated AI costs</span></h2>
      <div class="exp-range" id="expRange">
        <button class="exp-rb active" data-r="today" onclick="setExpRange('today')">Today</button>
        <button class="exp-rb" data-r="week" onclick="setExpRange('week')">7 days</button>
        <button class="exp-rb" data-r="month" onclick="setExpRange('month')">30 days</button>
      </div>
      <div class="exp-cards">
        <div class="exp-card">
          <div class="exp-lbl">Earned</div>
          <div class="exp-val good" id="expEarn">—</div>
          <div class="exp-sub">FB monetization</div>
        </div>
        <div class="exp-op">−</div>
        <div class="exp-card">
          <div class="exp-lbl">Spent</div>
          <div class="exp-val" id="expSpend">—</div>
          <div class="exp-sub">AI &amp; tools</div>
        </div>
        <div class="exp-op">=</div>
        <div class="exp-card net">
          <div class="exp-lbl">Net</div>
          <div class="exp-val" id="expNet">—</div>
          <div class="exp-sub" id="expNetSub">kept</div>
        </div>
      </div>
      <div class="exp-trend" id="expTrend"></div>
      <div id="expSplit"></div>
      <div class="exp-bd" id="expBreak"></div>
      <div class="exp-foot">Spending is Abra's own AI usage (Claude + Muse), estimated from the calls it logs. <a href="#" onclick="showView('profile');return false;">Full ledger &amp; monthly PDF for your accountant →</a></div>
    </section>
    <section id="rightsWrap">
      <h2> Koa's legal desk <span class="hint">keeps a dated record of what you make &amp; drafts disputes for claims on it</span></h2>
      <p class="help-p" style="margin:2px 0 10px">Koa logs each reel you schedule here as <b>your original work</b>, with a timestamp — your own dated record. And if anyone tries to claim your content, Koa drafts your dispute for you.</p>
      <div class="rights-add">
        <input id="rightsTitle" class="rights-in" placeholder="Name a post to protect…">
        <button class="btn green" onclick="rightsProtect()">Protect it</button>
        <a class="btn ghost" href="https://www.facebook.com/rights_manager" target="_blank" rel="noopener">Meta Rights Manager →</a>
      </div>
      <div id="rightsList" class="rights-list"><div class="empty">loading…</div></div>
      <h3 class="rights-h3">Someone claiming your content? Koa drafts your dispute.</h3>
      <textarea id="claimText" class="rights-ta" placeholder="Paste the copyright claim or takedown notice you received here…"></textarea>
      <button class="btn green" style="margin-top:6px" onclick="draftDispute()"> Draft my dispute</button>
      <div id="disputeOut" class="rights-out"></div>
      <div class="hint">Koa drafts your counter-notification from the title and claim you paste in — he's not a lawyer, and a serious claim may need a real attorney.</div>
    </section>
    <h2 class="acct-div"> Settings <span class="hint">appearance, billing &amp; how Abra runs</span></h2>
    <section>
      <h2> Appearance <span class="hint">light or dark</span></h2>
      <div class="themeseg" id="themeSeg">
        <button class="themeopt" data-theme-set="light" onclick="setTheme('light')">Light</button>
        <button class="themeopt" data-theme-set="dark" onclick="setTheme('dark')">Dark</button>
      </div>
    </section>
    <section>
      <h2> Your custom stickers <span class="hint">photos of you, cut out to use as stickers on your Stories</span></h2>
      <p class="help-p" style="margin:2px 0 12px">Add photos of yourself — surprised, laughing, pointing.
      Abra cuts you out of the background (Meta SAM) and saves you as a reaction you can drop onto any Story.
      Clear, well-lit photos of just you work best.</p>
      <input type="file" id="rxFile" accept="image/*" hidden onchange="rxPicked(this)">
      <div class="rxrow">
        <div class="rxupload" id="rxUpload" onclick="document.getElementById('rxFile').click()">
          <div class="rxup-ic"><svg viewBox="0 0 24 24"><path d="M4 8h3l1.6-2.2h6.8L17 8h3v11H4z"/><circle cx="12" cy="13.2" r="3.4"/></svg></div>
          <div class="rxup-txt"><b>Add a photo of you</b><span id="rxUpName">tap to choose — or drop it here</span></div>
          <img id="rxUpPrev" class="rxup-prev" alt="">
        </div>
        <button class="btn green rxgo" id="rxGo" onclick="uploadReaction()"><span>Cut me out</span></button>
      </div>
      <span class="hint" id="rxMsg" style="display:block; margin-top:9px"></span>
      <div id="rxGrid" class="rxgrid" style="margin-top:14px"><div class="empty">no custom stickers yet</div></div>
    </section>
    <section>
      <h2> Plugins &amp; connections <span class="hint">the tools your crew works with</span></h2>
      <div class="conns">
        <div class="conn">
          <div><div class="cn">AI models</div><div class="cs ok">Using the keys you add</div></div>
          <span class="hint">Claude · Muse</span>
        </div>
        <div class="conn">
          <div><div class="cn">Instagram</div><div class="cs">Coming soon</div></div>
          <button class="btn ghost" disabled>Connect</button>
        </div>
        <div class="conn">
          <div><div class="cn">TikTok</div><div class="cs">Coming soon</div></div>
          <button class="btn ghost" disabled>Connect</button>
        </div>
      </div>
    </section>
    <section>
      <h2> Plan &amp; billing <span class="hint">billing isn't set up yet</span></h2>
      <div class="billcards">
        <div class="billcard"><div class="blabel">Plan</div>
          <div class="bval">Own keys</div><div class="bsub">no managed billing yet</div></div>
      </div>
      <div class="row" style="margin-top:14px; gap:10px; align-items:center; flex-wrap:wrap">
        <button class="btn" onclick="manageBilling()">Add payment method</button>
        <button class="btn ghost" onclick="showView('money')">View usage ledger</button>
        <span class="hint" id="billMsg">Managed billing is coming soon. For now, Abra runs on the Claude and Muse keys you add in Settings.</span>
      </div>
    </section>
    <section>
      <h2> Run status</h2>
      <div id="settingsStatus" class="empty">loading…</div>
    </section>
    <section>
      <h2> Advanced <span class="hint">for power users</span></h2>
      <details class="adv">
        <summary>Use your own API keys instead</summary>
        <div class="advbody">
          <p class="help-p" style="margin:4px 0 12px">Prefer to run on your own Claude and Muse
          keys — connect them and Abra will use the keys you save on this machine.</p>
          <div class="conns">
            <div class="conn"><div><div class="cn">Claude — Anthropic</div><div class="cs" id="connAnthropic">checking…</div></div></div>
            <div class="conn"><div><div class="cn">Muse — Meta Model API</div><div class="cs" id="connMuse">checking…</div></div></div>
          </div>
          <a class="btn ghost" href="/connect/ai" target="_blank" rel="noopener" style="margin-top:12px; display:inline-block">Connect your own keys</a>
        </div>
      </details>
    </section>
    <section>
      <div class="row" style="gap:10px; flex-wrap:wrap">
        <button class="btn ghost" onclick="signOut()">Sign out</button>
      </div>
    </section>
  </div>

  <div class="view" id="view-help">
    <h1 class="vtitle">Account</h1>
    <div class="vsub">New to Abra? Here's how everything works — and what each of your crew does.</div>
    <div class="subtabs">
      <button class="subtab" data-sv="profile" onclick="showView('profile')">Profile</button>
      <button class="subtab" data-sv="help" onclick="showView('help')">Help</button>
    </div>
    <section>
      <h2> What Abra is</h2>
      <p class="help-p">Abra is your AI crew for Facebook. It works on your page while Abra is running, replies to
      and likes comments in your voice, and turns the videos you drop in into ready-to-post Reels and Stories —
      It turns the videos you drop in into ready-to-post Reels and Stories that you approve before they go live, so you can just film.</p>
      <p class="help-p" style="margin-top:12px"><b>Why Abra exists:</b> keeping a creator's page alive — answering every comment, posting every day — is a full-time job. Abra puts an AI crew on that back-office work so the creator can get back to creating.</p>
    </section>
    <section>
      <h2> Meet your crew</h2>
      <div class="help-crew">
        <div class="help-row"><b>Nico</b><span>Engagement</span><p>Reads comments on your posts and writes a warm, on-topic reply in your voice. Likes the good ones, and skips spam and hate.</p></div>
        <div class="help-row"><b>Sage</b><span>Launchpad</span><p>Watches and listens to a video you drop in, then writes the title, caption, hashtags and picks the best time to post.</p></div>
        <div class="help-row"><b>Koa</b><span>Money &amp; Legal</span><p>Keeps the books, tracks spend vs earnings, keeps a dated record of what you earn and drafts disputes, and watches your page's standing.</p></div>
      </div>
      <p class="help-tip">Tip: on the Home page, hover a crew member to say hi, or tap their card to open their desk.</p>
    </section>
    <section>
      <h2> Built on Meta's Muse models and Claude <span class="hint">your crew runs on Meta's Muse models and Claude</span></h2>
      <div class="help-crew">
        <div class="help-row"><b>Muse Wonder</b><span>AI analyst</span><p>Meta's flagship model reads your real numbers and writes your strategic "read" on the Analytics tab.</p></div>
        <div class="help-row"><b>Muse Spark</b><span>Engagement + copy</span><p>Triages comments fast and drafts your titles, captions and hashtags from this video, in a tone matched to what has reached best on your page.</p></div>
        <div class="help-row"><b>Muse Image</b><span>Visuals</span><p>Generates Story FX overlay art. One live test call returned an image (Nov 2).</p></div>
        <div class="help-row"><b>Muse Voice</b><span>Transcription</span><p>Listens to your videos and transcribes them so the copy matches what you actually said.</p></div>
        <div class="help-row"><b>SAM 3.1</b><span>Segmentation</span><p>Cuts you out for custom stickers and keeps you centered with auto-reframe.</p></div>
      </div>
    </section>
    <section>
      <h2> Quick start — post a video</h2>
      <ol class="help-steps">
        <li><b>Open Launchpad</b> and drop a video onto the Sage Scheduler.</li>
        <li><b>Sage studies it</b> — you'll see what it heard and saw.</li>
        <li><b>Pick a title</b> Sage wrote from your video, tweak the caption if you like.</li>
        <li><b>Choose a time</b> (Sage suggests slots from your post history) and press <b>Approve &amp; schedule</b>.</li>
        <li>Abra schedules the Reel, cuts Stories from the same video, and once it's live Nico handles the comments.</li>
      </ol>
    </section>
    <section>
      <h2> The tabs</h2>
      <div class="help-crew">
        <div class="help-row"><b>Home</b><span></span><p>Your whole crew at a glance — live status, and a tap opens their desk.</p></div>
        <div class="help-row"><b>Launchpad</b><span>Load · Engagement · Insights</span><p>Your content, start to finish: drop a video to make a post, see what's coming up, watch replies happen live, read your analytics, and pick videos to prioritize.</p></div>
        <div class="help-row" style="display:none"><b>Brand Deals</b><span></span><p>Your brand list: add brands, track stages, and copy the drafted pitches.</p></div>
        <div class="help-row"><b>Account</b><span>Profile · Help</span><p>Your profile, earnings &amp; AI costs (Koa's books), connected pages, publishing modes, the engagement pause, and this guide.</p></div>
      </div>
    </section>
    <section>
      <h2> FAQ</h2>
      <div class="help-crew">
        <div class="help-faq"><b>Will it post something I don't like?</b><p>In review mode (the default) Sage only drafts: the Reel, caption and time wait for your approval. Comment replies post under Abra's reply limits without a review step. You can switch a page to full auto in Settings if you want.</p></div>
        <div class="help-faq"><b>How do I stop everything fast?</b><p>Press <b>Pause engagement</b> at the bottom of the sidebar. Replies and likes stop on every page at the next comment until you resume.</p></div>
        <div class="help-faq"><b>Will the replies sound like me?</b><p>Nico is set up to write in your voice. It draws on the post's caption, transcript and frames, and its prompt keeps replies to what the video shows, or to the commenter's own words when that is unknown.</p></div>
        <div class="help-faq"><b>Does it get smarter about my page?</b><p>Partly. Titles and posting times draw on your own post history, and fall back to default hours until there is enough of it. Replies follow your persona and real examples of your past replies.</p></div>
        <div class="help-faq"><b>Does it cost anything to run?</b><p>AI usage has an estimated cost. Most calls are logged, but some are not yet, so the estimate can run low. It is shown in <b>Money &amp; Legal</b> against what your page earns.</p></div>
        <div class="help-faq"><b>Can it run more than one page?</b><p>Yes — connect any page you admin, edit or moderate under <b>Pages</b>, and pick which ones Abra manages.</p></div>
        <div class="help-faq"><b>Is my data private?</b><p>Abra keeps its records on this computer. Your videos, posts and comments are sent to Meta's Muse API and Anthropic's Claude. If you set a key, comment text can also go to an open-weight Muse Spark host (Baseten, Fireworks or Modal), and spoken-reply text to ElevenLabs. Otherwise spoken replies use Microsoft's voice service. Each provider's own data-use terms apply.</p></div>
      </div>
    </section>
    <section>
      <h2> Need a hand?</h2>
      <p class="help-p">Stuck or have an idea? Tap <b>Abra Chat</b> — the orb in the corner — any time,
      and it'll help you get the most out of Abra.</p>
    </section>
  </div>

  </div>
</div>

<button id="liveMeta" class="live-meta" type="button" onclick="liveMetaToggle()" title="Meta model calls, live" aria-label="Meta model calls, live"><span class="lm-dot"></span><span class="lm-text"></span></button>
<div id="liveMetaList" class="live-meta-list" aria-label="Recent Meta model calls"></div>
<!-- floating Abra concierge -->
<div id="assistWrap">
  <div id="assistPanel" class="assist-hidden">
    <div class="assist-head">
      <img id="assistOrbSm" src="/crew/orb.png" class="assist-orb-sm" alt="">
      <div class="assist-htext"><div class="assist-title">Chat</div><div class="assist-sub">your crew concierge</div></div>
      <button class="assist-snd" id="assistSound" onclick="toggleSound()" aria-label="Hear replies" title="Hear replies aloud">
        <svg class="snd-on" viewBox="0 0 24 24"><path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="M15.5 8.6a5 5 0 0 1 0 6.8"/><path d="M18.5 6a9 9 0 0 1 0 12"/></svg>
        <svg class="snd-off" viewBox="0 0 24 24"><path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="M22 9.5l-6 5M16 9.5l6 5"/></svg></button>
      <button class="assist-x" onclick="toggleAssist(false)" aria-label="Close">✕</button>
    </div>
    <div id="assistMsgs" class="assist-msgs"></div>
    <div id="assistChips" class="assist-chips"></div>
    <div class="assist-input">
      <textarea id="assistText" rows="1" placeholder="Tell me what you need…" onkeydown="assistKey(event)"></textarea>
      <button id="assistMic" class="assist-mic" onclick="assistVoice()" title="Speak" aria-label="Speak">
        <svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0M12 17v3"/></svg></button>
      <button class="assist-send" onclick="assistSend()" title="Send" aria-label="Send">↑</button>
    </div>
  </div>
  <div id="assistHint" class="assist-hint" onclick="dismissHint();toggleAssist(true)"><b>Ask me anything</b></div>
  <button id="assistBtn" class="assist-btn" onclick="toggleAssist()" aria-label="Abra"><span class="orb-halo"></span><span class="orb-ring"></span><canvas id="orbCanvas" class="orb-canvas"></canvas><img id="assistBtnImg" src="/crew/orb.png" alt=""></button>
</div>
<!-- model-viewer CDN removed: crew3d is off (USE_CREW_3D=false). loadCrew3D() can inject it if 3D is ever enabled. -->
<script>
// remove the launch splash once its fade-out finishes (keeps it from blocking clicks)
// ---- client-side login gate: Continue as <you> + Remember me ----
function isLoggedIn(){ try{ return localStorage.getItem('abraAuth')==='1' || sessionStorage.getItem('abraAuth')==='1'; }catch(e){ return true; } }
function revealDashboard(){ try{ showView('home'); }catch(e){} document.querySelectorAll('#view-home .crewcard').forEach(function(c,i){ setTimeout(function(){ c.classList.add('waving'); setTimeout(function(){ c.classList.remove('waving'); }, 1100); }, 200 + i*240); }); }
function showLoginGate(){ const g=document.getElementById('loginGate'); if(!g) return; g.classList.add('show'); g.setAttribute('aria-hidden','false');
  if(window._forceNew){ window._lgNew=true; const ct=document.querySelector('.lg-ctext'); if(ct&&ct.firstChild) ct.firstChild.textContent='Connect your Facebook'; const av=document.getElementById('lgAvatar'); if(av) av.style.display='none'; const ne=document.getElementById('lgName'); if(ne) ne.textContent=''; return; }
  fetch('/profile').then(function(r){ return r.json(); }).then(function(pr){ const nm=(pr&&pr.name)?pr.name.split(' ').filter(Boolean).slice(0,2).join(' '):''; const ne=document.getElementById('lgName'), av=document.getElementById('lgAvatar');
    if(nm){ window._lgNew=false; if(ne) ne.textContent=' as '+nm; } else { window._lgNew=true; const ct=document.querySelector('.lg-ctext'); if(ct&&ct.firstChild) ct.firstChild.textContent='Connect your Facebook'; if(av) av.style.display='none'; } }).catch(function(){}); }
function hideLoginGate(){ const g=document.getElementById('loginGate'); if(!g) return; g.style.transition='opacity .5s ease'; g.style.opacity='0'; setTimeout(function(){ g.classList.remove('show'); g.style.opacity=''; g.setAttribute('aria-hidden','true'); }, 520); }
function exploreWithoutConnecting(){ try{ sessionStorage.setItem('abraAuth','1'); }catch(e){} document.documentElement.classList.remove('gate-on'); hideLoginGate(); setTimeout(revealDashboard, 320); }
function doLogin(){ if(window._lgNew){ location.href='/connect'; return; } document.documentElement.classList.remove('gate-on'); const rem=!!(document.getElementById('lgRemember')||{}).checked; try{ localStorage.removeItem('abraAuth'); sessionStorage.removeItem('abraAuth'); (rem?localStorage:sessionStorage).setItem('abraAuth','1'); }catch(e){} hideLoginGate(); setTimeout(revealDashboard, 320); }
function signOut(){ try{ localStorage.removeItem('abraAuth'); sessionStorage.removeItem('abraAuth'); }catch(e){} fetch('/session/signout',{cache:'no-store'}).catch(function(){}); document.documentElement.classList.add('gate-on'); window.scrollTo(0,0); showLoginGate(); }
function applyTheme(t){ if(t==='dark'){ document.documentElement.setAttribute('data-theme','dark'); } else { document.documentElement.removeAttribute('data-theme'); } document.querySelectorAll('.themeopt').forEach(function(b){ b.classList.toggle('active', b.getAttribute('data-theme-set')===(t||'light')); }); }
function setTheme(t){ try{ localStorage.setItem('abraTheme',t); }catch(e){} applyTheme(t); }
(function(){ var t='light'; try{ t=localStorage.getItem('abraTheme')||'light'; }catch(e){} applyTheme(t); })();
setTimeout(() => {
  window._forceNew = /[?&]firstrun=1/.test(location.search);   // demo: force the first-run connect flow
  if (window._forceNew || !isLoggedIn()) showLoginGate();   // ready under the splash so there's no dashboard flash
  const s = document.getElementById('splash');
  if (s){ s.style.transition='opacity .6s ease, transform .6s ease'; s.style.opacity='0'; s.style.transform='scale(1.045)'; setTimeout(()=>{ if(s) s.remove(); }, 620); }
  if (!window._forceNew && isLoggedIn()) revealDashboard();
}, 3150);
function esc(s){ return (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
var VIEW_PARENT = { calendar:'studio', engage:'studio', analytics:'studio', videos:'studio', money:'settings', profile:'settings', pages:'settings', help:'settings' };
var _expData=null, _expRange='today';
function fmtUsd(n){ n=n||0; var neg=n<0; n=Math.abs(n); var s; if(n<1000){ s=n.toFixed(2); } else { s=Math.round(n).toLocaleString(); } return (neg?'-$':'$')+s; }
function prettyKind(pr,kind){ var p=pr==='claude'?'Claude':(pr==='muse'?'Muse':(pr||'')); var k=(kind||'').replace(/_/g,' '); return p+' · '+k; }
async function loadExpenses(){
  try{ _expData=await fetch('/expenses').then(function(r){return r.json();}); }
  catch(e){ _expData=null; }
  renderExpenses();
}
function setExpRange(r){
  _expRange=r;
  var bs=document.querySelectorAll('#expRange .exp-rb');
  for(var i=0;i<bs.length;i++){ bs[i].classList.toggle('active', bs[i].getAttribute('data-r')===r); }
  renderExpenses();
}
function renderExpenses(){
  var d=_expData; if(!d) return;
  var r=_expRange;
  var earn=(d.earned&&d.earned[r])||0, spend=(d.spent&&d.spent[r])||0, net=(d.net&&d.net[r])||0;
  var e=document.getElementById('expEarn'); if(e) e.textContent=fmtUsd(earn);
  var sp=document.getElementById('expSpend'); if(sp) sp.textContent=fmtUsd(spend);
  var ne=document.getElementById('expNet'); if(ne){ ne.textContent=fmtUsd(net); ne.className='exp-val '+(net>=0?'good':'bad'); }
  var ns=document.getElementById('expNetSub'); if(ns) ns.textContent=net>=0?'kept':'in the red';
  renderExpTrend(d);
  renderExpSplit(d);
  var box=document.getElementById('expBreak'); if(!box) return;
  var bd=d.breakdown||[];
  if(!bd.length){ box.innerHTML='<div class="exp-bd-empty">No AI spend logged yet — costs show up here as your crew works.</div>'; return; }
  var max=0; for(var i=0;i<bd.length;i++){ if(bd[i].cost>max) max=bd[i].cost; }
  var h='<div class="exp-bd-h">Where your spend went <span class="hint">last 30 days</span></div>';
  for(var j=0;j<bd.length;j++){
    var x=bd[j];
    var pct=max>0?Math.round(x.cost/max*100):0;
    var calls=x.calls+' call'+(x.calls===1?'':'s');
    h+='<div class="exp-row"><div class="exp-rl">'+prettyKind(x.provider,x.kind)+'<span class="exp-rc">'+calls+'</span></div><div class="exp-bar"><span style="width:'+pct+'%"></span></div><div class="exp-rv">'+fmtUsd(x.cost)+'</div></div>';
  }
  box.innerHTML=h;
}
function renderExpTrend(d){
  var box=document.getElementById('expTrend'); if(!box) return;
  var days=d.daily||[];
  if(days.length<2){ box.innerHTML=''; return; }
  var maxV=0; days.forEach(function(x){ maxV=Math.max(maxV, x.earned||0, x.spent||0); });
  if(maxV<=0){ box.innerHTML='<div class="exp-bd-empty">Your 30-day trend fills in as earnings and spend are recorded.</div>'; return; }
  var W=600, H=96, P=6;
  function pts(key){
    return days.map(function(x,i){
      var px=P+(W-2*P)*i/(days.length-1);
      var py=H-P-(H-2*P)*((x[key]||0)/maxV);
      return px.toFixed(1)+','+py.toFixed(1);
    }).join(' ');
  }
  var earnPts=pts('earned'), spendPts=pts('spent');
  var area='M'+earnPts.split(' ').join(' L')+' L'+(W-P)+','+(H-P)+' L'+P+','+(H-P)+' Z';
  box.innerHTML=
    '<svg class="exp-svg" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" role="img" aria-label="Earned and spent over the last 30 days">'
    +'<defs><linearGradient id="expFillE" x1="0" x2="0" y1="0" y2="1">'
    +'<stop offset="0" style="stop-color:var(--good)" stop-opacity=".30"/>'
    +'<stop offset="1" style="stop-color:var(--good)" stop-opacity="0"/></linearGradient></defs>'
    +'<path d="'+area+'" fill="url(#expFillE)"/>'
    +'<polyline vector-effect="non-scaling-stroke" points="'+spendPts+'" fill="none" style="stroke:var(--accent)" stroke-width="2" stroke-dasharray="5 4" stroke-linecap="round" stroke-linejoin="round"/>'
    +'<polyline vector-effect="non-scaling-stroke" points="'+earnPts+'" fill="none" style="stroke:var(--good)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>'
    +'</svg>'
    +'<div class="exp-legend"><span><i class="lg-e"></i>Earned</span><span><i class="lg-s"></i>Spent</span><span class="exp-axis">last 30 days</span></div>';
}
function renderExpSplit(d){
  var box=document.getElementById('expSplit'); if(!box) return;
  var sp=d.split||{};
  var c=sp.claude||0, m=sp.muse||0, tot=c+m;
  if(tot<=0){ box.innerHTML=''; return; }
  var pc=Math.round(c/tot*100), pm=100-pc;
  box.innerHTML='<div class="exp-splitbar"><span class="sb-c" style="width:'+pc+'%"></span><span class="sb-m" style="width:'+pm+'%"></span></div>'
    +'<div class="exp-splitlg"><span><i class="lg-c"></i>Claude '+pc+'% · '+fmtUsd(c)+'</span><span><i class="lg-m"></i>Muse '+pm+'% · '+fmtUsd(m)+'</span></div>';
}
function fmtK(n){ n=Number(n)||0; if(n>=1000) return (Math.round(n/100)/10)+'k'; return String(Math.round(n)); }
async function loadBestTime(){
  var head=document.getElementById('btHead'), grid=document.getElementById('btGrid');
  var conf=document.getElementById('btConf'), foot=document.getElementById('btFoot'), sweet=document.getElementById('btSweet');
  if(!grid) return;
  var d;
  try{ d=await fetch('/besttime').then(function(r){return r.json();}); }
  catch(e){ if(head) head.textContent='Could not read your history right now.'; return; }
  if(!d || !d.posts_analyzed){
    if(head) head.textContent=(d&&d.headline)||'Not enough posts yet to spot a pattern.';
    grid.innerHTML=''; if(conf) conf.textContent=''; if(foot) foot.textContent=''; if(sweet) sweet.textContent=''; return;
  }
  if(head) head.textContent=d.headline||'';
  if(conf){ conf.textContent=d.confidence; conf.className='bt-conf '+d.confidence; }
  var days=d.days||[], blocks=d.blocks||[], g=d.grid||[];
  var max=0;
  for(var dd=0;dd<g.length;dd++){ for(var bb=0;bb<g[dd].length;bb++){ if(g[dd][bb].avg>max) max=g[dd][bb].avg; } }
  var best=d.best||{};
  var html='<div class="bt-cell bt-corner"></div>';
  for(var i=0;i<days.length;i++){ html+='<div class="bt-cell bt-dh">'+days[i]+'</div>'; }
  for(var b=0;b<blocks.length;b++){
    html+='<div class="bt-cell bt-bh"><b>'+blocks[b].name+'</b><span>'+blocks[b].range+'</span></div>';
    for(var day=0;day<days.length;day++){
      var cell=(g[day]&&g[day][b])||{avg:0,n:0};
      var inten=(max>0&&cell.avg>0)?Math.sqrt(cell.avg/max):0;
      var alpha=cell.n>0?(0.12+0.78*inten).toFixed(3):0;
      var isBest=best&&best.day===days[day]&&best.range===blocks[b].range&&cell.n>0;
      var cls='bt-cell bt-c'+(cell.n===0?' bt-empty':'')+(cell.n>0&&cell.n<3?' bt-thin':'')+(inten>0.6?' bt-hot':'')+(isBest?' bt-best':'');
      var style=cell.n>0?'background:rgba(94,144,230,'+alpha+')':'';
      var tip=cell.n>0?(days[day]+' '+blocks[b].name+' · avg '+Math.round(cell.avg)+' · '+cell.n+' post'+(cell.n===1?'':'s')):(days[day]+' '+blocks[b].name+' · no posts yet');
      var label=cell.n>0?'<span class="bt-v">'+fmtK(cell.avg)+'</span>':'';
      html+='<div class="'+cls+'" style="'+style+'" title="'+tip+'">'+label+'</div>';
    }
  }
  grid.innerHTML=html;
  if(sweet){ sweet.textContent=(best&&best.day)?('Sweet spot · '+best.day+' '+best.range+' ET'):''; }
  var ft='Based on '+d.posts_analyzed+' posts over '+d.span_days+' day'+(d.span_days===1?'':'s')+'. Times in ET. Numbers = average engagement per post; faded cells have fewer than 3 posts.';
  if(foot) foot.textContent=ft;
}
function spotPreset(q){ var i=document.getElementById('spotQ'); if(i) i.value=q; runSpotlight(); }
function spotPresetsHtml(){
  var P=['nostalgia','cooking hacks','what-if','car reviews','DIY fails'];
  return '<div class="spot-presets"><span class="spottl">Try a genre</span>'+P.map(function(p){ return '<button class="spottrend" onclick="spotPreset(this.textContent)">'+p+'</button>'; }).join('')+'</div>';
}
async function loadReviewStats(){
  var el=document.getElementById('tReview'); if(!el) return;
  try{ var d=await fetch('/reviewstats').then(function(r){return r.json();});
    el.textContent=d.checks; document.getElementById('tReviewCap').textContent=d.held+' held back before posting'; }catch(e){}
}
function showView(v){
  if (v==='money'||v==='settings'||v==='pages') v='profile';
  if (v==='videos') v='engage';
  if (v==='calendar') v='home';
  if (v==='film') v='studio';   // Create is removed for the submission: anything that opens it lands on Load
  if (v!=='film' && typeof rxStopCam==='function') rxStopCam();
  document.querySelectorAll('.view').forEach(e=>e.classList.remove('active'));
  document.getElementById('view-'+v).classList.add('active');
  var navV = VIEW_PARENT[v] || v;
  document.querySelectorAll('.navitem').forEach(b=>b.classList.toggle('active', b.dataset.v===navV));
  document.querySelectorAll('.mtoplogo-btn,.mtoptab').forEach(b=>b.classList.toggle('active', b.dataset.mv===navV));
  document.querySelectorAll('.subtab').forEach(b=>b.classList.toggle('active', b.dataset.sv===v));
  var _av=document.getElementById('mtopAvatar'); if(_av)_av.classList.toggle('active', navV==='settings');
  if (v==='profile') { startKoaRotate(); loadProfile(); loadPages(); loadBudget().then(loadBilling); loadBilling(); loadStanding(); loadMonetization(); loadExpenses(); rxListCams(); paintEng(); loadSettingsStatus(); loadConns(); loadReactions(); loadRights(); } else { stopKoaRotate(); }
  if (v==='analytics') { loadAnalyst(); loadTrend(); loadWatch(); loadBestTime(); }
  if (v==='engage') { loadFanLove(); loadVideos(true); loadReviewStats(); loadAds(); }
  if (v==='film') loadFilmGenres();
  if (v==='deals') loadDeals();
  if (v==='studio') { loadUpcoming(); showLanding(); showBrief(); }
  if (v==='home') { loadBudget(); updateCrew(); loadProfile(); loadHomeDash(); }
  // no duplicate of the same animal: hide the roaming copy whose twin presents on this
  // page; on Home the whole crew appears in the grid, so hide all roamers there.
  const presenterTwin = { engage:'critCat', profile:'critKoala' }[v];
  const hideAll = (v==='home');
  ['critCat','critDog','critKoala'].forEach(id=>{
    const el = document.getElementById(id);
    if (el) el.style.display = (hideAll || id===presenterTwin) ? 'none' : '';
  });
}
let engPaused = null;
function paintEng(){
  if (engPaused === null) return;
  ['engBtn'].forEach(id=>{
    const b = document.getElementById(id); if (!b) return;
    if (engPaused) { b.textContent=' Resume engagement'; b.style.background='#132117';
      b.style.color='var(--good)'; b.style.border='1px solid #2E5B3C'; }
    else { b.textContent=' Pause engagement'; b.style.background='#FBE7E3';
      b.style.color='var(--bad)'; b.style.border='1px solid #EFC4BC'; }
  });
}
let monSel=null;   // null = All pages (collective); else an array of selected page ids
function renderMonPages(pages, selected){
  const el=document.getElementById('monPages'); if(!el) return;
  if(!pages || pages.length<2){ el.style.display='none'; el.innerHTML=''; return; }
  el.style.display='flex';
  const allOn=!monSel, selSet=new Set(selected||[]);
  el.innerHTML='<span class="monpageslabel">Pages</span>'
    +`<button class="monpagepill ${allOn?'on':''}" onclick="monPick(null)">All pages</button>`
    +pages.map(p=>`<button class="monpagepill ${(!allOn&&selSet.has(p.id))?'on':''}" onclick="monPick('${p.id}')">${esc(p.name)}</button>`).join('');
}
function monPick(pid){
  if(pid===null){ monSel=null; }
  else {
    if(!monSel) monSel=[];
    const i=monSel.indexOf(pid);
    if(i>=0) monSel.splice(i,1); else monSel.push(pid);
    if(!monSel.length) monSel=null;   // deselected everything -> back to All
  }
  loadMonetization();
}
async function loadMonetization(){
  const wrap=document.getElementById('monWrap'); if(!wrap) return;
  try{
    const qs=(monSel&&monSel.length)?('?pages='+encodeURIComponent(monSel.join(','))):'';
    const d=await fetch('/monetization'+qs).then(r=>r.json());
    if(d.error){ wrap.innerHTML='<div class="empty">could not load monetization data</div>'; return; }
    renderMonPages(d.pages, d.selected);
    const cards=`<div class="billcards bill4" style="margin-bottom:14px">
      <div class="billcard"><div class="blabel">Earned today</div><div class="bval" style="color:var(--good)">${d.earn_day!=null?fmt$(d.earn_day):'—'}</div></div>
      <div class="billcard"><div class="blabel">Earned · 30 days</div><div class="bval" style="color:var(--good)">${d.earn_30!=null?fmt$(d.earn_30):'—'}</div></div>
      <div class="billcard"><div class="blabel">Followers</div><div class="bval">${fmtN(d.followers)}</div></div>
      <div class="billcard"><div class="blabel">Minutes viewed · 60d</div><div class="bval">${fmtN(d.mins60)}</div></div>
    </div>`;
    const programs=d.programs||[];
    const elig=programs.filter(p=>p.eligible), pending=programs.filter(p=>!p.eligible);
    let progs='';
    if(elig.length){
      progs+=`<div class="monelig"><span class="moneliglabel">Eligible for</span>`
        +elig.map(p=>`<span class="monchip" title="${esc(p.note)}">${esc(p.name)} <b>✓</b></span>`).join('')
        +`</div>`;
    }
    progs+=pending.map(p=>{
      const reqs=p.reqs.map(r=>`<div class="monreq"><div class="monreqtop"><span>${esc(r.label)}</span>
        <span>${fmtN(r.have)} / ${fmtN(r.need)}</span></div>
        <div class="monbarwrap"><div class="monbar" style="width:${r.pct}%; background:${r.pct>=100?'var(--good)':'var(--accent)'}"></div></div></div>`).join('');
      return `<div class="monprog"><div class="monname">${esc(p.name)}<span class="monnote">${esc(p.note)}</span></div>${reqs}</div>`;
    }).join('');
    wrap.innerHTML=cards+progs;
  }catch(e){ wrap.innerHTML='<div class="empty">could not load monetization data</div>'; }
}
function loadStanding(){
  const el = document.getElementById('standingBody'); if (!el) return;
  const s = lastStats;
  if (!s) { el.textContent = 'loading…'; return; }
  if (s.monetizationWarn) {
    el.innerHTML = '<div style="color:var(--bad); font-weight:700"> Earnings dropped sharply — check facebook.com/accountquality now.</div>';
  } else if (s.throttleMin) {
    el.innerHTML = '<div style="color:var(--warn); font-weight:700"> Facebook asked us to slow down — auto-resuming in '+s.throttleMin+' min. Standing is fine.</div>';
  } else {
    el.innerHTML = '<div style="color:var(--good); font-weight:700"> ✓ Page in good standing — no monetization warning or active throttle right now.</div>'
      + '<div class="hint" style="margin-top:4px">Check facebook.com/accountquality if earnings look off (30 sec).</div>';
  }
}
const PUBMODE_LABELS = {review:'Review — approve each', auto:'Full auto'};
async function loadPubModes(){
  const el = document.getElementById('pubModes'); if (!el) return;
  try {
    const r = await fetch('/pages'); const pages = await r.json();
    el.innerHTML = pages.map(pg => `
      <div class="metricrow" style="align-items:center">
        <div class="mn" style="width:200px">${esc(pg.name)}</div>
        <select onchange="setPubMode('${pg.id}', this.value)"
          style="background:var(--card2); color:var(--ink); border:1px solid var(--border);
                 border-radius:9px; padding:7px 10px; font-size:13px; font-weight:600">
          ${Object.entries(PUBMODE_LABELS).map(([v,l])=>
            `<option value="${v}" ${pg.pub_mode===v?'selected':''}>${l}</option>`).join('')}
        </select>
      </div>`).join('');
  } catch(e) { el.innerHTML = '<div class="empty">could not load pages</div>'; }
}
async function setPubMode(id, mode){
  await fetch('/pubmode', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({page_id:id, mode})});
}
// ---- Sage Content Scheduler ----
(function(){
  const dz = document.getElementById('dropZone');
  if (dz){
    ['dragenter','dragover'].forEach(ev=>dz.addEventListener(ev, e=>{e.preventDefault(); dz.classList.add('drag');}));
    ['dragleave','drop'].forEach(ev=>dz.addEventListener(ev, e=>{e.preventDefault(); dz.classList.remove('drag');}));
    dz.addEventListener('drop', e=>{ const f=(e.dataTransfer.files||[])[0]; if(f) analyzeVideo(f); });
  }
  // landing drop target (Stage A)
  const land = document.getElementById('museLanding'), mld = document.getElementById('mlDrop');
  if (land){
    ['dragenter','dragover'].forEach(ev=>land.addEventListener(ev, e=>{e.preventDefault(); if(mld) mld.classList.add('drag');}));
    ['dragleave','drop'].forEach(ev=>land.addEventListener(ev, e=>{e.preventDefault(); if(mld) mld.classList.remove('drag');}));
    land.addEventListener('drop', e=>{ const fs=e.dataTransfer.files; if(fs&&fs.length) videoDrop(fs); });
  }
  // smooth blink for the big landing Sage (opacity cross-fade, not a frame cut)
  setInterval(()=>{ const dog=document.getElementById('mlDog'), l=document.getElementById('museLanding');
    if(dog && l && l.style.display!=='none'){ dog.classList.add('blink'); setTimeout(()=>dog.classList.remove('blink'), 150); }
  }, 4300);
})();
let schedDraft = null;
function museState(mode, text){
  const p=document.getElementById('musePresenter'); if(!p) return;
  p.classList.toggle('busy', mode==='listening');
  p.classList.toggle('invite', mode!=='listening');
  const s=document.getElementById('museSay'); if(s && text) s.textContent=text;
}
// ---- Launchpad landing cutscene (the big Sage gate) ----
function mlSleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
function mlBubbleSay(t){ const b=document.getElementById('mlBubble'); if(b) b.textContent=t; }
function museSet(pose, state){ const d=document.getElementById('mlDog'); if(!d) return;
  if(pose) d.setAttribute('data-pose', pose); if(state) d.setAttribute('data-state', state); }
function mlResetProps(){ const cd=document.getElementById('mlCD'), pl=document.getElementById('mlPlayer');
  if(cd) cd.className='mlprop mlcd'; if(pl) pl.className='mlprop mlplayer'; }
let landingBusy=false, beckonT=null;
// reset the in-page Step-1 cutscene to its idle/inviting state
function showLanding(){
  landingBusy=false; stopFloat();
  const cd=document.getElementById('mlCD'), pl=document.getElementById('mlPlayer'), ar=document.getElementById('mlArrow');
  if(cd) cd.className='mlprop mlcd';
  if(pl) pl.className='mlprop mlplayer show';     // CD player is out front from the start
  if(ar) ar.className='mlarrow on';               // arrow points the user at the slot
  museSet('greet','bob'); mlBubbleSay('Drop content here');
  clearTimeout(beckonT);
  beckonT=setTimeout(()=>{ if(!landingBusy){ museSet('beckon','beckon'); } }, 1600);
}
let floatTimer=null;
function startFloat(){
  const fx=document.getElementById('floatFx'); if(!fx) return;
  stopFloat();
  const chars='0101101AXML01#01'.split('');
  floatTimer=setInterval(()=>{
    const g=document.createElement('span'); g.className='glyph';
    g.textContent=chars[Math.floor(Math.random()*chars.length)];
    g.style.left=(10+Math.random()*80)+'%';
    g.style.bottom=(22+Math.random()*48)+'%';
    g.style.fontSize=(13+Math.random()*13)+'px';
    g.style.color=Math.random()<0.5?'var(--accent)':'var(--ink2)';
    fx.appendChild(g); setTimeout(()=>{ if(g.parentNode) g.parentNode.removeChild(g); }, 2400);
  }, 170);
}
function stopFloat(){ if(floatTimer){ clearInterval(floatTimer); floatTimer=null; }
  const fx=document.getElementById('floatFx'); if(fx) fx.innerHTML=''; }
// fetch + render only (no presenter choreography) — shared by both stages
async function fetchAnalyze(file){
  const card=document.getElementById('schedCard'); if(card) card.innerHTML='';
  const fd=new FormData(); fd.append('video', file);
  const sel=document.getElementById('scPage')||document.getElementById('phPage');
  fd.append('page_id', sel?sel.value:'');
  try{
    const r=await fetch('/schedule/analyze',{method:'POST',body:fd});
    const d=await r.json();
    if(d.error) return {ok:false, error:d.error};
    schedDraft=d; renderSchedCard(d); return {ok:true};
  }catch(e){ return {ok:false, error:String(e)}; }
}
// Stage B (small presenter) path
async function analyzeVideo(file){
  if(!file) return;
  const busy=document.getElementById('schedBusy'); if(busy) busy.style.display='none';
  museState('listening','Listening… reading every frame');
  const res=await fetchAnalyze(file);
  if(res.ok){ museState('idle','Got it — here is your post below'); setTimeout(()=>museState('idle','Drop another — give it to me!'), 3000); }
  else { museState('idle','Hmm, that one did not work — try another');
    const card=document.getElementById('schedCard'); if(card) card.innerHTML='<div class="empty">Sage hit a snag: '+esc(res.error)+'</div>'; }
}
// Step-1 cutscene (plays in place): drop file -> into player -> earbuds/listen -> card below
async function landingDrop(file){
  if(!file || landingBusy) return; landingBusy=true; clearTimeout(beckonT);
  const analyzeP=fetchAnalyze(file);
  const cd=document.getElementById('mlCD'), pl=document.getElementById('mlPlayer'), ar=document.getElementById('mlArrow');
  if(ar) ar.className='mlarrow';                      // hide the arrow
  mlBubbleSay('Loading it up…');
  if(cd) cd.className='mlprop mlcd dropin';            // the file drops INTO the player
  await mlSleep(560);
  if(cd) cd.className='mlprop mlcd cdgone';
  if(pl) pl.className='mlprop mlplayer show spin';     // and it spins
  museSet('earbuds','listen'); mlBubbleSay('Listening…'); startFloat();   // Sage vibes to it
  const res=await analyzeP;                            // wait for Sage to finish reading
  stopFloat();
  if(pl) pl.className='mlprop mlplayer show';          // stop the spin
  if(!res.ok){ const card=document.getElementById('schedCard'); if(card && res.error) card.innerHTML='<div class="empty">'+esc(res.error)+'</div>';
    mlBubbleSay(res.error ? 'Hmm — see the note below' : 'Hmm — try another file'); await mlSleep(900); showLanding(); return; }
  mlBubbleSay('Got it — your post is below ↓'); museSet('greet','bob');   // card rendered below
  try{ document.getElementById('schedCard').scrollIntoView({behavior:'smooth', block:'nearest'}); }catch(e){}
  clearTimeout(beckonT);
  beckonT=setTimeout(()=>{ landingBusy=false; showLanding(); }, 3500);    // back to inviting for the next drop
}
function fmtSlot(iso){
  if(!iso) return 'no open slot found';
  const d=new Date(iso);
  return d.toLocaleString('en-US',{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',
    timeZone:'America/New_York'})+' ET';
}
function renderSchedCard(d){
  if(pendingBrief && pendingBrief.hook){ const h=pendingBrief.hook;
    d.titles = [h].concat((d.titles||[]).filter(function(t){return t!==h;})); window._briefIdx0 = true; clearBrief(); }
  const titles = (d.titles&&d.titles.length)?d.titles:['(Sage offline — type a title)'];
  const thumb = d.thumbnail ? '/draft/'+d.thumbnail : '';
  const thumbOpts = (d.thumbnail_options && d.thumbnail_options.length)
    ? '<div class="thumbopts"><span class="hint">Cover (pick one)</span>' + d.thumbnail_options.map(function(f,i){ return '<label class="thumbopt"><input type="radio" name="thumbpick" value="'+f+'"'+(i===0?' checked':'')+'><img src="/draft/'+f+'" alt="cover option '+(i+1)+'"></label>'; }).join('') + '</div>'
    : (thumb ? '<img class="sc-thumb" src="'+thumb+'" alt="thumbnail"><div class="hint" style="align-self:center">Sage cover</div>' : '');
  const heard = d.transcript ? ' Heard: &ldquo;'+esc(d.transcript.slice(0,160))+(d.transcript.length>160?'…':'')+'&rdquo;'
                             : ' No speech detected — titles written from what Sage sees.';
  document.getElementById('schedCard').innerHTML = `
    <div class="sstep done"><div class="snum">2</div><div class="sbody">
      <div class="slabel">Sage studied your video <span class="sub">this is what your titles are written from</span></div>
      <div class="museread">
        <div class="heard">${heard}</div>
        <div class="saw"> Saw: ${esc(d.content_desc||'(no visual read)')}</div>
      </div>
    </div></div>
    <div class="sstep done"><div class="snum">3</div><div class="sbody">
      <div class="slabel">Pick a title Sage wrote for this video <span class="sub">generated from the content above</span></div>
      <div id="hookPick" class="hookpick">Muse Wonder is ranking your hooks…</div>
      <div class="schedcard"><div class="sc-head">
        ${thumbOpts}
        <div style="flex:1" id="titleOpts">
          ${titles.map((t,i)=>`<label class="titleopt" id="titleopt-${i}"><input type="radio" name="sctitle" value="${i}" ${i===0?'checked':''}><span>${esc(t)}</span><span class="hookbadge" id="hookbadge-${i}"></span></label>`).join('')}
        </div></div>
      <div class="sc-body">
        <div><label class="fl">Caption</label><textarea id="scDesc" rows="3" aria-label="Caption">${esc(d.description||'')}</textarea></div>
        <div><label class="fl">Hashtags</label>
          <div class="tags">${(d.hashtags||[]).map(h=>`<span class="tag">${esc(h)}</span>`).join('')||'<span class="hint">none</span>'}</div></div>
      </div></div>
    </div></div>
    <div class="sstep done"><div class="snum">4</div><div class="sbody">
      <div class="slabel">Pick your Stories <span class="sub">auto-made from the same video, posted 15 min apart</span></div>
      <input type="hidden" id="scStory" value="smart">
      ${(() => {
        const sp = d.story_previews||{};
        window._storyPrev = sp;
        const img = f => `<img src="/draft/${f}" alt="">`;
        const chop = (sp.chopped||[]).map(img).join('') || '<div class="sopt-ph">clip 1</div><div class="sopt-ph">clip 2</div><div class="sopt-ph">clip 3</div>';
        const under = (d.duration_ms||0) < 90000;
        const mmss = (()=>{const s=Math.round((d.duration_ms||0)/1000);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');})();
        const smartThumbs = under ? chop : teaserThumbsHTML(1);
        const smartNote = (d.duration_ms? `This video (${mmss}) → ${under?'chopped':'teaser'}` : 'Picks by length');
        return `<div class="storyopts">
          <div class="storyopt selected" data-tpl="smart" onclick="pickStory(this)">
            <div class="sopt-thumbs">${smartThumbs}</div>
            <div class="sopt-name">Smart (recommended)</div>
            <div class="sopt-desc">Under 1:30 → chopped · over → teaser<br><b>${smartNote}</b></div>
          </div>
          <div class="storyopt" data-tpl="chopped" onclick="pickStory(this)">
            <div class="sopt-thumbs">${chop}</div>
            <div class="sopt-name">Chopped up, up to three clips</div>
            <div class="sopt-desc">Plays through as consecutive clips</div>
          </div>
          <div class="storyopt" data-teaser="1" data-tpl="teaser1" onclick="pickStory(this)">
            <div class="sopt-thumbs" id="teaserThumbs">${teaserThumbsHTML(1)}</div>
            <div class="sopt-name">Teaser + "Watch now"</div>
            <select id="teaserStyle" class="sc-in storyselect" onclick="event.stopPropagation()" onchange="changeTeaserStyle()">
              <option value="1">Style: Button</option>
              <option value="2">Style: Player</option>
              <option value="3">Style: Banner</option>
            </select>
          </div>
          <div class="storyopt" data-tpl="reaction_teaser" onclick="pickStory(this)">
            <div class="sopt-thumbs">${teaserThumbsHTML(1)}</div>
            <div class="sopt-name">Reaction teaser</div>
            <div class="sopt-desc">Title + Watch Now, with <b>you reacting</b> in the corner<br>pick a reaction below</div>
          </div>
          <div class="storyopt" data-tpl="none" onclick="pickStory(this)">
            <div class="sopt-thumbs sopt-nostory"><div class="sopt-ph">—</div></div>
            <div class="sopt-name">No Stories</div>
            <div class="sopt-desc">Just the Reel</div>
          </div>
        </div>`;
      })()}
      <div class="fxrow"><span class="fxlabel">Story FX</span>
        <button type="button" class="fxchip on" data-fx="" onclick="pickFx(this)">Off</button>
        <button type="button" class="fxchip" data-fx="auto" onclick="pickFx(this)">Auto</button>
        <button type="button" class="fxchip" data-fx="rain" onclick="pickFx(this)">Rain</button>
        <button type="button" class="fxchip" data-fx="embers" onclick="pickFx(this)">Embers</button>
        <button type="button" class="fxchip" data-fx="stars" onclick="pickFx(this)">Stars</button>
        <button type="button" class="fxchip" data-fx="snow" onclick="pickFx(this)">Snow</button>
        <button type="button" class="fxchip" data-fx="sparkle" onclick="pickFx(this)">Sparkle</button></div>
      <div class="fxhint">A cinematic overlay on your Stories — Auto matches your video, or pick your own.</div>
      <label class="fxtoggle"><input type="checkbox" id="scTrack">
        <span><b>Keep me centered</b> — SAM follows you so you stay in frame (great for wide footage)</span></label>
      ${d.transcript ? `<label class="fxtoggle"><input type="checkbox" id="scCaptions" ${window._rxWantCaptions!==false?'checked':''}>
        <span><b>Burn in captions</b> — Muse Voice's transcript as on-screen captions, for viewers watching without sound. Reels need a portrait 9:16 video; Stories are captioned either way.</span></label>`
      : `<label class="fxtoggle disabled"><input type="checkbox" id="scCaptions" disabled>
        <span><b>Burn in captions</b> — no speech detected in this video</span></label>`}
      <input type="hidden" id="scReaction" value="">
      <div class="rxpick" id="scRxPicker"></div>
    </div></div>
    <div class="sstep ${d.auto_result?'done':''}"><div class="snum">5</div><div class="sbody">
      <div class="slabel">Choose the time &amp; schedule <span class="sub">Sage picks slots from your post history, or a default set until there is enough data</span></div>
      <div class="sc-foot" style="border:1px solid var(--border); border-radius:12px">
        <div><select id="scSlot" onchange="onSlotChange()" class="sc-in" style="width:auto; display:inline-block">
          ${(d.slot_options&&d.slot_options.length?d.slot_options:[d.proposed_slot]).map(s=>`<option value="${s}">${fmtSlot(s)}</option>`).join('')}
          <option value="__custom">Custom time…</option>
        </select>
        <input type="datetime-local" id="scCustom" class="sc-in" style="width:auto; display:none; margin-left:6px"></div>
        ${d.auto_result
          ? `<span class="tag" style="color:var(--good)"> ${esc(d.auto_result)}</span>`
          : `<div class="postto"><span class="hint">Post as</span>
              <label><input type="radio" name="postto" value="page" checked> Page post</label>
              <label><input type="radio" name="postto" value="ad"> Ad: Abra gives its comments priority; you launch the ad in Meta Ads Manager (Abra can't launch ads yet)</label></div>
              <button class="btn green" onclick="approveSchedule()">Approve &amp; schedule</button><span id="scMsg" class="hint"></span>`}
      </div>
    </div></div>`;
  loadReactions();   // populates the reaction picker in step 4
  if (d.auto_result) loadUpcoming();
  museRankHooks(d);  // Muse Wonder picks the winning hook
}
async function museRankHooks(d){
  const pick=document.getElementById('hookPick');
  const titles=(d.titles&&d.titles.length)?d.titles:[];
  if(!pick) return;
  if(window._briefIdx0){ window._briefIdx0=false;
    document.querySelectorAll('input[name=sctitle]').forEach(function(inp){ inp.checked=(+inp.value===0); });
    const b0=document.getElementById('hookbadge-0'); if(b0) b0.textContent='\u2605 your idea';
    pick.innerHTML='<b>From your idea:</b> &ldquo;'+esc(titles[0]||'')+'&rdquo;'; pick.classList.add('ready'); return; }
  if(titles.length<2){ pick.style.display='none'; return; }
  const opts=titles.map((t,i)=>document.getElementById('titleopt-'+i)).filter(Boolean);
  // Muse Wonder "considers" each candidate: sweep a scan highlight while /rankhook is in flight
  let si=0, scanning=true;
  const sweep=()=>{ if(!scanning||!opts.length) return; opts.forEach(o=>o.classList.remove('scanning')); opts[si%opts.length].classList.add('scanning'); si++; };
  sweep(); const scanTimer=setInterval(sweep,150);
  const stopScan=()=>{ scanning=false; clearInterval(scanTimer); opts.forEach(o=>o.classList.remove('scanning')); };
  try{
    const r=await fetch('/rankhook',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({titles, content_desc:d.content_desc||'', transcript:d.transcript||''})});
    const j=await r.json();
    stopScan();
    const w=(typeof j.winner==='number')?j.winner:0;
    document.querySelectorAll('input[name=sctitle]').forEach(inp=>{ inp.checked=(+inp.value===w); });
    titles.forEach((t,i)=>{
      const opt=document.getElementById('titleopt-'+i), b=document.getElementById('hookbadge-'+i);
      if(opt) opt.classList.toggle('winner', i===w);
      if(b) b.textContent = (i===w) ? 'Sage’s pick' : '';
    });
    pick.classList.add('ready');
    pick.innerHTML = '<b>Sage’s pick:</b> &ldquo;'+esc(titles[w])+'&rdquo; <span class="hookwhy" id="hookWhy"></span>';
    const why=document.getElementById('hookWhy');
    if(j.reason && why){ const txt=j.reason; let i=0; (function type(){ if(i<=txt.length){ why.textContent=txt.slice(0,i++); setTimeout(type,14); } })(); }
  }catch(e){ stopScan(); pick.style.display='none'; }
}
function isoToETWall(iso){
  const d=new Date(iso);
  const p=new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York',year:'numeric',month:'2-digit',
    day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).formatToParts(d);
  const g=t=>(p.find(x=>x.type===t)||{}).value; let hh=g('hour'); if(hh==='24')hh='00';
  return `${g('year')}-${g('month')}-${g('day')}T${hh}:${g('minute')}`;
}
function etWallToISO(wall){
  if(!wall) return null;
  for(const off of ['-04:00','-05:00']){ const iso=`${wall}:00${off}`; if(isoToETWall(iso)===wall) return iso; }
  return `${wall}:00-04:00`;
}
function onSlotChange(){
  const sel=document.getElementById('scSlot'), cust=document.getElementById('scCustom');
  if(sel.value==='__custom'){ if(!cust.value && schedDraft) cust.value=isoToETWall(schedDraft.proposed_slot); cust.style.display='inline-block'; }
  else cust.style.display='none';
}
function chosenSlot(){
  const sel=document.getElementById('scSlot');
  if(sel && sel.value==='__custom'){ return etWallToISO(document.getElementById('scCustom').value) || schedDraft.proposed_slot; }
  return (sel && sel.value) || schedDraft.proposed_slot;
}
function pickStory(el){
  document.querySelectorAll('.storyopt').forEach(x=>x.classList.remove('selected'));
  el.classList.add('selected');
  const h=document.getElementById('scStory'); if(h) h.value = el.getAttribute('data-tpl');
  if(el.getAttribute('data-tpl')==='reaction_teaser'){   // this template needs your reaction
    const cur=(document.getElementById('scReaction')||{}).value||'';
    const list=window._reactions||[];
    if(!cur && list.length){ const sr=document.getElementById('scReaction'); if(sr) sr.value=list[0]; renderRxPicker(); }
  }
}
function teaserThumbsHTML(s){
  const sp = window._storyPrev||{}; const img=f=>`<img src="/draft/${f}" alt="">`;
  const hook = sp.teaser_hook ? img(sp.teaser_hook) : '<div class="sopt-ph">preview</div>';
  const ctas = sp.teaser_styles||[];
  const cta = ctas[s-1] ? img(ctas[s-1]) : '<div class="sopt-ph">Watch now</div>';
  return cta + hook;   // style slide shows before the video preview
}
function pickFx(el){ const row=el.closest('.fxrow'); if(!row) return;
  row.querySelectorAll('.fxchip').forEach(function(c){ c.classList.remove('on'); }); el.classList.add('on'); }
function changeTeaserStyle(){
  const s = document.getElementById('teaserStyle').value;
  const card = document.querySelector('.storyopt[data-teaser]');
  if(!card) return;
  card.setAttribute('data-tpl', 'teaser'+s);
  const t = document.getElementById('teaserThumbs'); if(t) t.innerHTML = teaserThumbsHTML(s);
  pickStory(card);   // choosing a style selects the teaser option
}
let _approving=false, _approvedKeys=new Set();
// The exact post as it will be sent. Approve and confirm both read it, so a change in between is caught.
function schedPayload(){
  const ti = parseInt((document.querySelector('input[name=sctitle]:checked')||{value:'0'}).value||'0');
  return { title:(schedDraft.titles||[])[ti]||'', description:document.getElementById('scDesc').value,
    hashtags:schedDraft.hashtags||[], slot:chosenSlot(), thumbnail:((document.querySelector('input[name=thumbpick]:checked')||{}).value)||schedDraft.thumbnail,
    video_file:schedDraft.video_file, page_id:((document.getElementById('scPage')||document.getElementById('phPage')||{}).value)||'',
    story_template:(document.getElementById('scStory')||{}).value||'chopped',
    story_fx:(function(){var c=document.querySelector('.fxrow .fxchip.on'); return c?(c.getAttribute('data-fx')||''):'';})(), content_desc:schedDraft.content_desc||'',
    track:!!(document.getElementById('scTrack')||{}).checked,
    captions:!!(document.getElementById('scCaptions')||{}).checked,
    transcript:schedDraft.transcript||'',
    as_ad:((document.querySelector('input[name=postto]:checked')||{}).value==='ad'),
    reaction:(document.getElementById('scReaction')||{}).value||'',
    reaction_x:rxPlace.x, reaction_y:rxPlace.y, reaction_scale:rxPlace.scale };
}
async function approveSchedule(){
  if(!schedDraft) return {ok:false, msg:'There is no post ready to schedule.'};
  if(_approving) return {ok:false, msg:'Already scheduling.'};
  _approving=true;
  const _ab=document.querySelector('button[onclick="approveSchedule()"]'); if(_ab) _ab.disabled=true;
  const msg = document.getElementById('scMsg');
  let ok=false, out='', keep=false, key='';
  try{
    const payload=schedPayload();
    key=[payload.title, payload.slot, payload.video_file, payload.page_id, payload.as_ad].join('|');
    if(_approvedKeys.has(key)){ out='Already scheduled that one. Make a new post to schedule again.'; if(msg) msg.textContent=out; return {ok:false, msg:out}; }
    if(msg) msg.textContent='scheduling…';
    const r = await fetch('/schedule/confirm', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const j = await r.json().catch(function(){ return {}; });
    out = j.msg || (r.ok ? 'done' : 'error: HTTP '+r.status);
    ok = r.ok && !/^error/i.test(out);
    if(ok) _approvedKeys.add(key);
    if(msg) msg.textContent = out;
    loadUpcoming();
  }catch(e){
    // the request may have reached the server: never retry the same post silently
    if(key) _approvedKeys.add(key);
    keep=true;
    out='Connection dropped before the server answered. Check Upcoming before trying again.';
    if(msg) msg.textContent=out;
  }
  finally{
    _approving=false;
    if(!ok && !keep){ const _ab2=document.querySelector('button[onclick="approveSchedule()"]'); if(_ab2) _ab2.disabled=false; }
  }
  return {ok:ok, msg:out};
}

// ---- Sage BATCH scheduler: drop up to 10 videos -> review -> schedule all ----
let batchDrafts = [];
function videoDrop(files){
  files = Array.from(files||[]).slice(0,10);
  if(!files.length) return;
  if(files.length===1){ landingDrop(files[0]); return; }
  batchDrop(files);
}
async function batchDrop(files){
  if(landingBusy) return; landingBusy=true; clearTimeout(beckonT);
  const sel=document.getElementById('scPage')||document.getElementById('phPage');
  const pageId = sel?sel.value:'';
  batchDrafts = files.map((f,i)=>({file:f, name:f.name, status:'queued', idx:i}));
  mlBubbleSay('On it — building '+files.length+' posts…'); museSet('earbuds','listen'); startFloat();
  renderBatchPanel();
  try{ document.getElementById('schedCard').scrollIntoView({behavior:'smooth', block:'start'}); }catch(e){}
  for(const item of batchDrafts){
    item.status='analyzing'; renderBatchPanel();
    try{
      const fd=new FormData(); fd.append('video', item.file); fd.append('page_id', pageId);
      const d=await fetch('/schedule/analyze',{method:'POST',body:fd}).then(r=>r.json());
      if(d.error){ item.status='error'; item.err=d.error; }
      else{
        let winner=0;
        try{ const j=await fetch('/rankhook',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({titles:d.titles||[], content_desc:d.content_desc||'', transcript:d.transcript||''})}).then(r=>r.json());
          if(typeof j.winner==='number') winner=j.winner; }catch(e){}
        item.draft=d; item.winner=winner; item.status='ready';
      }
    }catch(e){ item.status='error'; item.err=String(e); }
    renderBatchPanel();
  }
  stopFloat();
  assignBatchSlots();
  renderBatchPanel();
  const okN=batchDrafts.filter(x=>x.status==='ready').length;
  museSet('greet','bob'); mlBubbleSay(okN+' posts ready — review & schedule ↓');
  landingBusy=false;
}
function assignBatchSlots(){
  const ready=batchDrafts.filter(x=>x.status==='ready');
  const pool=[];
  ready.forEach(x=>{ ((x.draft.slot_options&&x.draft.slot_options.length)?x.draft.slot_options:[x.draft.proposed_slot]).forEach(sl=>{ if(sl&&pool.indexOf(sl)<0) pool.push(sl); }); });
  pool.sort();
  let last = pool.length?pool[pool.length-1]:((ready[0]&&ready[0].draft.proposed_slot)||null);
  ready.forEach((x,i)=>{
    if(i<pool.length){ x.slot=pool[i]; }
    else if(last){ const b=new Date(last); b.setDate(b.getDate()+1); x.slot=b.toISOString(); last=x.slot; }
    else { x.slot=x.draft.proposed_slot; }
  });
}
function renderBatchPanel(){
  const card=document.getElementById('schedCard'); if(!card) return;
  const doneAll = batchDrafts.every(x=>x.status==='ready'||x.status==='error');
  const readyN = batchDrafts.filter(x=>x.status==='ready').length;
  const rows = batchDrafts.map((x,i)=>{
    if(x.status!=='ready'){
      const ic = x.status==='analyzing'?'<span class="bspin"></span>':x.status==='error'?'!':'·';
      const lbl = x.status==='analyzing'?'Sage is reading it…':x.status==='error'?('skipped — '+esc(x.err||'error')):'queued';
      return `<div class="brow ${x.status}"><div class="bic">${ic}</div><div class="bname">${esc(x.name)}</div><div class="bstat">${lbl}</div></div>`;
    }
    const d=x.draft, titles=d.titles||[], w=x.winner||0;
    const thumb=d.thumbnail?'/draft/'+d.thumbnail:'';
    const opts=((d.slot_options&&d.slot_options.length)?d.slot_options:[d.proposed_slot]).slice();
    if(x.slot && opts.indexOf(x.slot)<0) opts.unshift(x.slot);
    return `<div class="bcard" id="bcard-${i}">
      <div class="bcard-top">
        ${thumb?`<img class="bthumb" src="${thumb}" alt="">`:'<div class="bthumb bthumb-ph">🎬</div>'}
        <div class="bcard-main">
          <input class="btitle" id="bTitle-${i}" value="${esc(titles[w]||'')}">
          <div class="bmeta">${d.duration_ms?Math.round(d.duration_ms/1000)+'s · ':''}${esc((d.content_desc||'').slice(0,54))}</div>
        </div>
        <label class="bskip"><input type="checkbox" id="bSkip-${i}"><span>skip</span></label>
      </div>
      <textarea class="bdesc" id="bDesc-${i}" rows="2">${esc(d.description||'')}</textarea>
      <div class="brow2"><span class="bwhen">when</span>
        <select class="bslot" id="bSlot-${i}">${opts.map(sl=>`<option value="${sl}" ${sl===x.slot?'selected':''}>${fmtSlot(sl)}</option>`).join('')}</select>
        <button type="button" class="bmore" id="bMore-${i}" onclick="toggleBcard(${i})">More options ▾</button></div>
      <div class="bextra" id="bExtra-${i}" style="display:none">
        ${titles.length>1?`<label class="fl">Title <span class="hint">— Sage ranked ${titles.length} hooks</span></label>
        <select class="bsel" id="bTitleSel-${i}" onchange="bApplyTitle(${i})">${titles.map((t,ti)=>`<option value="${ti}" ${ti===w?'selected':''}>${esc(t)}${ti===w?" — Sage's pick":''}</option>`).join('')}</select>`:''}
        <label class="fl">Hashtags</label>
        <input class="bsel" id="bTags-${i}" value="${esc((d.hashtags||[]).join(' '))}" placeholder="#your #tags">
        <label class="fl">Stories <span class="hint">— auto-made from this video</span></label>
        <select class="bsel" id="bStory-${i}"><option value="smart">Smart (recommended)</option><option value="chopped">Chopped up, up to three clips</option><option value="teaser1">Teaser + Watch-now</option><option value="reaction_teaser">Reaction teaser</option></select>
      </div>
    </div>`;
  }).join('');
  card.innerHTML = `<div class="batchwrap">
    <div class="batchhead"><b>Sage is building ${batchDrafts.length} posts</b>
      <span class="hint">${readyN} ready${doneAll?'':' · reading…'}</span></div>
    ${rows}
    ${doneAll&&readyN?`<button class="btn green batchgo" onclick="scheduleBatch()">Schedule all ${readyN} post${readyN===1?'':'s'}</button> <span id="batchMsg" class="hint"></span>`:''}
  </div>`;
}
function toggleBcard(i){
  const e=document.getElementById('bExtra-'+i), b=document.getElementById('bMore-'+i);
  if(!e) return; const open=(e.style.display==='none');
  e.style.display=open?'block':'none';
  if(b) b.textContent=open?'Fewer options ▴':'More options ▾';
}
function bApplyTitle(i){
  const x=batchDrafts[i]; if(!x||!x.draft) return;
  const sel=document.getElementById('bTitleSel-'+i), inp=document.getElementById('bTitle-'+i);
  if(sel&&inp){ const t=(x.draft.titles||[])[+sel.value]; if(t!=null) inp.value=t; }
}
let _batching=false;
async function scheduleBatch(){
  if(_batching) return;
  _batching=true;
  const go=document.querySelector('button[onclick="scheduleBatch()"]'); if(go) go.disabled=true;
  const msg=document.getElementById('batchMsg'); if(msg) msg.textContent='scheduling…';
  const sel=document.getElementById('scPage')||document.getElementById('phPage');
  const pageId=sel?sel.value:'';
  let ok=0, fail=0;
  try{
    for(let i=0;i<batchDrafts.length;i++){
      const x=batchDrafts[i];
      if(x.status!=='ready') continue;
      if((document.getElementById('bSkip-'+i)||{}).checked) continue;
      const d=x.draft;
      const tagsEl=document.getElementById('bTags-'+i);
      const hashtags=tagsEl ? tagsEl.value.split(/[\\s,]+/).filter(Boolean).map(h=>h[0]==='#'?h:'#'+h) : (d.hashtags||[]);
      const storyEl=document.getElementById('bStory-'+i);
      const payload={ title:((document.getElementById('bTitle-'+i)||{}).value)||'',
        description:((document.getElementById('bDesc-'+i)||{}).value)||'',
        hashtags:hashtags, slot:((document.getElementById('bSlot-'+i)||{}).value)||x.slot||d.proposed_slot,
        thumbnail:d.thumbnail, video_file:d.video_file, page_id:pageId,
        story_template:(storyEl?storyEl.value:'smart'), content_desc:d.content_desc||'', transcript:d.transcript||'', captions:!!d.transcript };
      try{
        const j=await fetch('/schedule/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());
        const reply=(j&&j.msg)||(j&&j.error)||'';
        if(j&&j.error || /^error/i.test(reply)){ fail++; x.error=reply; }
        else { ok++; x.status='scheduled'; }
      }catch(e){ fail++; x.error=String(e); }
      if(msg) msg.textContent='scheduling… '+ok+' done';
    }
  } finally {
    _batching=false;
    if(go) go.disabled=false;
  }
  if(msg) msg.textContent='✓ scheduled '+ok+' post'+(ok===1?'':'s')+(fail?(', '+fail+' failed'):'');
  loadUpcoming();
  renderBatchPanel();
  museSet('greet','bob'); mlBubbleSay('Done — '+ok+' posts scheduled!');
}
function updateCrewMemory(){
  // cheap, from already-loaded data — safe to call every tick
  if (lastStats){
    const c=document.getElementById('crewClaude');
    if(c) c.textContent = `${lastStats.replies24??0} replies in the last 24 hours`;
  }
  const koa=document.getElementById('crewKoa');
  if(koa){
    if(lastStats && (lastStats.monetizationWarn))
      koa.textContent='earnings dropped — check standing';
    else if(lastBooks){
      const t=lastBooks.today||{}; const tt=Object.values(t).reduce((a,x)=>a+(x.cost||0),0);
      const earn=(lastBooks.earned||{}).today||0;
      koa.textContent = earn>0 ? `net ${fmt$(earn-tt)} today` : `estimated spend ${fmt$(tt)} today`;
    } else koa.textContent='page in good standing';
  }
}
async function updateCrew(){
  updateCrewMemory();
  try{
    const u = await fetch('/schedule/upcoming').then(r=>r.json());
    const m=document.getElementById('crewMuse');
    if(m) m.textContent = (u.reels&&u.reels.length) ? `next reel ${fmtSlot(u.reels[0].scheduled_for)}` : 'ready for your next video';
  }catch(e){}
  try{
    const d = await fetch('/deals').then(r=>r.json());
    const list = Array.isArray(d) ? d : (d.deals||[]);
    const live = list.filter(x=>!['closed-lost','dead','lost'].includes((x.stage||'').toLowerCase()));
  }catch(e){}
}
async function loadUpcoming(){
  const el = document.getElementById('upcomingWrap'); if(!el) return;
  try{
    const d = await fetch('/schedule/upcoming').then(r=>r.json());
    if(!d.reels || !d.reels.length){ el.innerHTML='<div class="empty">nothing scheduled yet — drop content above</div>'; return; }
    el.innerHTML = d.reels.map(r=>`
      <div class="metricrow" style="align-items:center; gap:12px">
        ${r.thumbnail?`<img src="/draft/${r.thumbnail}" style="width:42px;height:64px;object-fit:cover;border-radius:7px">`:''}
        <div style="flex:1">
          <div style="font-weight:700; font-size:13.5px">${esc(r.title||'(untitled reel)')}</div>
          <div class="hint">${r.status==='dry_scheduled'?' dry-run · ':''} ${fmtSlot(r.scheduled_for)}${r.stories?` · ${r.stories} stories 15 min apart`:''}</div>
        </div>
        <span class="tag" style="color:var(--good)">scheduled</span>
      </div>`).join('');
  }catch(e){ el.innerHTML='<div class="empty">could not load schedule</div>'; }
}
function animateCount(el, target, fmt, dur){
  if(!el) return; dur=dur||850; fmt=fmt||(n=>Math.round(n).toLocaleString());
  target=+target||0; const start=performance.now();
  function step(t){ let p=Math.min(1,(t-start)/dur); p=1-Math.pow(1-p,3);
    el.textContent=fmt(target*p); if(p<1) requestAnimationFrame(step); }
  requestAnimationFrame(step);
}
function fmtMoney(n){ return '$'+Math.round(n).toLocaleString(); }
function fmtBig(n){ n=+n||0; if(n>=1e6) return (n/1e6).toFixed(1)+'M'; if(n>=1e3) return Math.round(n/1e3)+'k'; return ''+Math.round(n); }
function hsClock(){
  const d=new Date();
  const dt=document.getElementById('hsDate'); if(dt) dt.textContent=d.toLocaleDateString('en-US',{weekday:'long', month:'long', day:'numeric'});
  const tm=document.getElementById('hsTime'); if(tm) tm.textContent=d.toLocaleTimeString('en-US',{hour:'numeric', minute:'2-digit'});
}
let _hsClockT=null, _hsCalLoaded=false;
function hsStartClock(){ hsClock(); if(!_hsClockT) _hsClockT=setInterval(hsClock, 20000); }
function hsCalOpen(){ if(_hsCalLoaded) return; _hsCalLoaded=true; loadCalendar(); }
function hInit(){
  var secs=document.querySelectorAll('#view-home > section');
  var st={}; try{ st=JSON.parse(localStorage.getItem('hcollapse')||'{}'); }catch(e){}
  secs.forEach(function(s,i){
    var hdr=s.querySelector(':scope > h2'); if(!hdr) hdr=s.querySelector(':scope > .hs-head');
    if(!hdr || hdr.getAttribute('data-hc')) return;
    hdr.setAttribute('data-hc','1');
    var id='hc'+i; s.setAttribute('data-hc',id); s.classList.add('hcollap');
    var cx=document.createElement('span'); cx.className='hcx'; cx.textContent='▾'; hdr.appendChild(cx);
    hdr.addEventListener('click', function(ev){ if(ev.target.closest('a,button,input,select,label,model-viewer')) return; s.classList.toggle('collapsed'); try{ var m=JSON.parse(localStorage.getItem('hcollapse')||'{}'); m[id]=s.classList.contains('collapsed'); localStorage.setItem('hcollapse',JSON.stringify(m)); }catch(e){} });
    if(st[id]) s.classList.add('collapsed');
  });
}
async function loadHomeCal(){
  var el=document.getElementById('homeCal'); if(!el) return;
  var reels=[];
  try{ var d=await fetch('/schedule/upcoming').then(function(r){return r.json();}); reels=d.reels||[]; }catch(e){}
  var now=new Date();
  var byDay={};
  reels.forEach(function(r){ if(!r.scheduled_for) return; var dt=new Date(r.scheduled_for); if(isNaN(dt)) return; var k=dt.getFullYear()+'-'+(dt.getMonth()+1)+'-'+dt.getDate(); (byDay[k]=byDay[k]||[]).push({dt:dt,r:r}); });
  var out='';
  for(var i=0;i<7;i++){
    var dt=new Date(now.getFullYear(),now.getMonth(),now.getDate()+i);
    var k=dt.getFullYear()+'-'+(dt.getMonth()+1)+'-'+dt.getDate();
    var evs=(byDay[k]||[]).sort(function(a,b){return a.dt-b.dt;});
    var label=i===0?'Today':(i===1?'Tomorrow':dt.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'}));
    var inner=evs.length? evs.map(function(x){ var past=x.dt<now; var tm=x.dt.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'}); return '<div class="cal-ev'+(past?' done':'')+'"><span class="cal-dot"></span><span class="cal-evt">'+esc(x.r.title||'Reel')+'</span><span class="cal-evtm">'+tm+'</span></div>'; }).join('') : '<div class="cal-none">open</div>';
    out+='<div class="cal-day'+(i===0?' today':'')+'"><div class="cal-dl">'+label+'</div><div class="cal-de">'+inner+'</div></div>';
  }
  el.innerHTML=out;
}
async function loadChecklist(){
  var el=document.getElementById('clList'); if(!el) return;
  var items=[], reels=[];
  try{ var c=await fetch('/checklist').then(function(r){return r.json();}); items=c.items||[]; }catch(e){}
  try{ var d=await fetch('/schedule/upcoming').then(function(r){return r.json();}); reels=d.reels||[]; }catch(e){}
  var now=new Date();
  var slots=reels.filter(function(r){return r.scheduled_for;}).map(function(r){return {dt:new Date(r.scheduled_for),r:r};}).filter(function(x){return !isNaN(x.dt);}).sort(function(a,b){return a.dt-b.dt;});
  var html='';
  if(slots.length){
    html+='<div class="tk-head">Content</div>'+slots.map(function(x){ var past=x.dt<now; var when=x.dt.toLocaleDateString('en-US',{weekday:'short'})+' '+x.dt.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'}); return '<div class="cl-item content'+(past?' done':'')+'"><span class="cl-ci"></span><span class="cl-text">'+esc(x.r.title||'Reel')+'</span><span class="cl-when">'+(past?'posted ':'')+when+'</span></div>'; }).join('');
  }
  html+='<div class="tk-head">To-do</div>';
  if(items.length){
    html+=items.map(function(it){ return '<label class="cl-item'+(it.done?' done':'')+'"><input type="checkbox" '+(it.done?'checked':'')+' onchange="toggleChecklist('+it.id+', this)"><span class="cl-text">'+esc(it.text)+'</span><button type="button" class="cl-del" onclick="delChecklist('+it.id+')" aria-label="delete">×</button></label>'; }).join('');
  } else { html+='<div class="cl-empty">No to-dos yet — add one below.</div>'; }
  el.innerHTML=html;
}
async function addChecklist(e){
  if(e&&e.preventDefault) e.preventDefault();
  const inp=document.getElementById('clInput'); const text=(inp&&inp.value||'').trim(); if(!text) return false;
  inp.value='';
  try{ await fetch('/checklist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})}); }catch(err){}
  loadChecklist(); return false;
}
async function toggleChecklist(id, cb){
  const done=!!(cb&&cb.checked); const lab=cb?cb.closest('.cl-item'):null; if(lab) lab.classList.toggle('done', done);
  try{ await fetch('/checklist/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id, done:done})}); }catch(err){}
}
async function delChecklist(id){
  try{ await fetch('/checklist/del',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})}); }catch(err){}
  loadChecklist();
}
async function hsToday(){
  const el=document.getElementById('hsToday'); if(!el) return;
  let reels=[];
  try{ const d=await fetch('/schedule/upcoming').then(r=>r.json()); reels=d.reels||[]; }catch(e){}
  const now=new Date();
  const todayStr=now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0')+'-'+String(now.getDate()).padStart(2,'0');
  const today=reels.filter(function(r){return (r.scheduled_for||'').slice(0,10)===todayStr;})
    .sort(function(a,b){return (a.scheduled_for||'').localeCompare(b.scheduled_for||'');});
  if(!today.length){ el.innerHTML=''; return; }
  el.innerHTML = '<div class="hs-schhead">On the schedule today</div>' + today.map(function(r){
    const t = r.scheduled_for ? new Date(r.scheduled_for).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'}) : '';
    return '<div class="hs-schrow"><span class="hs-t">'+t+'</span><span class="hs-ttl">'+esc(r.title||'Scheduled reel')+'</span>'
      + (r.stories?'<span class="hs-badge">+'+r.stories+' stories</span>':'') + '</div>';
  }).join('');
}
async function loadHomeDash(){
  hsStartClock(); loadHomeCal(); loadChecklist(); try{ hInit(); }catch(e){}
  const setKpi=(id,val,fmt)=>{ const el=document.getElementById(id); if(!el) return;
    if(val==null){ el.textContent='—'; } else animateCount(el,val,fmt); };
  try{
    const m = await fetch('/monetization').then(r=>r.json());
    setKpi('kpiFollowers', m.followers, n=>fmtBig(n));
    setKpi('kpiReach', m.mins60, n=>fmtBig(n));
    setKpi('kpiEarned', m.earn_30, n=>fmtMoney(n));
    const es=document.getElementById('kpiEarnedSub');
    if(es && m.earn_day!=null) es.textContent='$'+(+m.earn_day).toFixed(2)+' today';
  }catch(e){}
  try{
    const d = await fetch('/data').then(r=>r.json());
    const st=d.stats||{};
    setKpi('kpiReplies', st.replies24, n=>Math.round(n).toLocaleString());
    const rs=document.getElementById('kpiRepliesSub');
    if(rs) rs.textContent = (st.repliesHour!=null) ? (st.repliesHour+' in the last 15 minutes') : 'by your crew';
    renderHomeFeed(d.events||[]);
  }catch(e){}
}
function renderHomeFeed(events){
  const box=document.getElementById('homeFeed'); if(!box) return;
  const rows = events.slice(0,8).map((e,i)=>{
    const k=(e.kind||'').toUpperCase();
    const act = k==='REPLY'?'replied' : k==='LIKE'?'liked' : k==='SKIP'?'filtered' : (e.kind||'').toLowerCase();
    const cls = k==='REPLY'?'reply' : k==='LIKE'?'like' : 'skip';
    const c = esc((e.comment||'').slice(0,64));
    let text;
    if(k==='REPLY') text = '“<b>'+c+'</b>” → '+esc((e.detail||'').slice(0,82));
    else if(k==='LIKE') text = 'liked “<b>'+c+'</b>”';
    else text = '“<b>'+c+'</b>” — kept the page clean';
    return '<div class="feedrow" style="animation-delay:'+(i*45)+'ms">'
      + '<div class="feedmain"><div class="feedtop">'
      + '<span class="feedact '+cls+'">'+act+'</span><span class="feedtime">'+esc(e.time||'')+'</span></div>'
      + '<div class="feedtext">'+text+'</div></div></div>';
  }).join('');
  box.innerHTML = rows || '<div class="empty">no activity yet today</div>';
}
function fmtMonthYear(iso){ try{ return new Date(iso).toLocaleString(undefined,{month:'short',year:'numeric'}); }catch(e){ return '—'; } }
function loadProfile(){
  fetch('/profile').then(r=>r.json()).then(p=>{
    const has = !!(p && p.name);
    const nm = has ? p.name : 'Your profile';
    const ini = has ? (nm.trim().split(/\\s+/).map(w=>w[0]||'').slice(0,2).join('')||'·').toUpperCase() : 'AB';
    const hasPic = !!(p && p.picture);
    const avUrl = '/avatar?t='+Date.now();
    // --- Profile tab hero ---
    const hName=document.getElementById('profHeroName'); if(hName) hName.textContent=nm;
    const hPic=document.getElementById('profHeroPic'), hInit=document.getElementById('profHeroInit');
    if(hPic&&hInit){
      if(hasPic){ hPic.src=avUrl; hPic.style.display='block'; hInit.style.display='none'; }
      else { hInit.textContent=ini; hInit.style.display='flex'; hPic.style.display='none'; }
    }
    const hSub=document.getElementById('profHeroSub');
    if(hSub){
      if(has){ const bits=[]; if(p.email) bits.push(p.email); bits.push('Facebook account'); hSub.textContent=bits.join('   ·   '); }
      else { hSub.textContent='Log in with Facebook to set up your profile.'; }
    }
    const hSince=document.getElementById('profSince'); if(hSince) hSince.textContent = (p&&p.since)?fmtMonthYear(p.since):'—';
    const hFoll=document.getElementById('profFollowers'); if(hFoll) hFoll.textContent = (p&&p.followers!=null)?fmtN(p.followers):'—';
    const hRep=document.getElementById('profReplies'); if(hRep) hRep.textContent = (p&&p.replies_today!=null)?(''+p.replies_today):'—';
    const pp=document.getElementById('profPages');
    if(pp){
      pp.innerHTML = (p&&p.pages&&p.pages.length)
        ? p.pages.map(x=>`<span class="pagechip ${x.enabled?'on':''}">${esc(x.name)}</span>`).join('')
        : '<div class="empty">no pages connected yet</div>';
    }
    // --- Home welcome header ---
    const hHi=document.getElementById('homeHi');
    if(hHi) hHi.textContent = has ? ('Welcome back, '+nm.split(/\\s+/)[0]) : 'Welcome back';
    const hStat=document.getElementById('homeStatus');
    // followers + replies live in the KPI cards right below — keep this line clean
    if(hStat) hStat.textContent = 'Your crew is on the clock — updates live.';
    const hAv=document.getElementById('homeAvatar'), hAvI=document.getElementById('homeAvatarInit');
    if(hAv&&hAvI){
      if(hasPic){ hAv.src=avUrl; hAv.style.display='block'; hAvI.style.display='none'; }
      else { hAvI.textContent=ini; hAvI.style.display='flex'; hAv.style.display='none'; }
    }
  }).catch(()=>{});
}
function loadConns(){
  fetch('/aistatus').then(r=>r.json()).then(s=>{
    const set=(id,on)=>{ const el=document.getElementById(id); if(!el) return;
      el.textContent = on?'Connected':'Not connected'; el.className = 'cs'+(on?' ok':''); };
    set('connAnthropic', s.anthropic); set('connMuse', s.muse);
  }).catch(()=>{});
}
function loadReactions(){
  fetch('/reactions').then(r=>r.json()).then(d=>{
    window._reactions = d.reactions||[];
    const g=document.getElementById('rxGrid');
    if(g){
      g.innerHTML = window._reactions.length
        ? window._reactions.map(n=>`<div class="rxtile"><img src="/reaction/${n}" alt=""><button class="rxdel" title="Remove" onclick="delReaction('${n}')">&times;</button></div>`).join('')
        : '<div class="empty">no custom stickers yet</div>';
    }
    renderRxPicker();
  }).catch(()=>{});
}
function uploadReaction(){
  const f=document.getElementById('rxFile'), msg=document.getElementById('rxMsg');
  if(!f || !f.files || !f.files[0]){ if(msg) msg.textContent='choose a photo first'; return; }
  const fd=new FormData(); fd.append('file', f.files[0]);
  if(msg) msg.textContent='cutting you out…';
  fetch('/reactions/add',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{
    if(d.ok){ if(msg) msg.textContent='added \u2713'; f.value=''; rxReset(); loadReactions(); }
    else { if(msg) msg.textContent = d.error||'could not process that photo'; }
  }).catch(e=>{ if(msg) msg.textContent='error: '+String(e); });
}
function rxPicked(inp){ const f=inp&&inp.files&&inp.files[0]; const nm=document.getElementById('rxUpName'), pv=document.getElementById('rxUpPrev'), z=document.getElementById('rxUpload');
  if(f){ if(nm) nm.textContent=f.name; if(pv){ try{ pv.src=URL.createObjectURL(f); }catch(e){} pv.classList.add('show'); } if(z) z.classList.add('has'); } }
function rxReset(){ const nm=document.getElementById('rxUpName'), pv=document.getElementById('rxUpPrev'), z=document.getElementById('rxUpload');
  if(nm) nm.textContent='tap to choose — or drop it here'; if(pv){ pv.classList.remove('show'); pv.removeAttribute('src'); } if(z) z.classList.remove('has'); }
(function(){ const z=document.getElementById('rxUpload'); if(!z) return;
  ['dragenter','dragover'].forEach(function(ev){ z.addEventListener(ev,function(e){ e.preventDefault(); z.classList.add('drag'); }); });
  ['dragleave','drop'].forEach(function(ev){ z.addEventListener(ev,function(e){ e.preventDefault(); z.classList.remove('drag'); }); });
  z.addEventListener('drop',function(e){ const f=(e.dataTransfer.files||[])[0]; if(!f) return; const inp=document.getElementById('rxFile');
    try{ const dt=new DataTransfer(); dt.items.add(f); inp.files=dt.files; }catch(err){} rxPicked({files:[f]}); }); })();
function delReaction(name){
  fetch('/reactions/del',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name})})
    .then(r=>r.json()).then(()=>loadReactions()).catch(()=>{});
}
var rxPlace = {x:0.72, y:0.69, scale:0.62};   // sticker CENTER (norm) + height fraction
var rxImgAspect = 0.8;                         // sticker width/height
function renderRxPicker(){
  const host=document.getElementById('scRxPicker'); if(!host) return;
  const list=window._reactions||[];
  const cur=(document.getElementById('scReaction')||{}).value||'';
  const none=`<div class="rxopt ${cur?'':'selected'}" onclick="pickReaction('',this)"><span class="rxnone">None</span></div>`;
  const tiles=list.map(n=>`<div class="rxopt ${cur===n?'selected':''}" onclick="pickReaction('${n}',this)"><img src="/reaction/${n}" alt=""></div>`).join('');
  const hint = list.length ? '' : '<span class="hint">Add reactions in Account → Your custom stickers</span>';
  host.innerHTML = `<div class="rxlabel">Add a custom sticker <span class="sub">a cutout of you for your Stories</span></div><div class="rxrow">${none}${tiles}</div>${hint}<div class="rxeditor" id="rxEditor"></div>`;
  if(cur) showRxEditor(cur);
}
function pickReaction(name,el){
  const h=document.getElementById('scReaction'); if(h) h.value=name;
  document.querySelectorAll('#scRxPicker .rxopt').forEach(x=>x.classList.remove('selected'));
  if(el) el.classList.add('selected');
  if(name) showRxEditor(name); else hideRxEditor();
}
function layoutRx(){
  const stage=document.getElementById('rxStage'), sub=document.getElementById('rxSub');
  if(!stage||!sub) return;
  const SW=stage.clientWidth, SH=stage.clientHeight;
  const hpx=rxPlace.scale*SH, wpx=hpx*rxImgAspect;
  sub.style.width=wpx+'px'; sub.style.height=hpx+'px';
  sub.style.left=(rxPlace.x*SW - wpx/2)+'px';
  sub.style.top=(rxPlace.y*SH - hpx/2)+'px';
}
function attachRx(){
  const stage=document.getElementById('rxStage'), sub=document.getElementById('rxSub'), handle=document.getElementById('rxHandle');
  if(!stage||!sub||!handle) return;
  let mode=null, sx=0, sy=0, start=null;
  function down(e,m){ mode=m; sx=e.clientX; sy=e.clientY; start={x:rxPlace.x,y:rxPlace.y,scale:rxPlace.scale};
    e.preventDefault(); e.stopPropagation();
    window.addEventListener('pointermove',move); window.addEventListener('pointerup',up); }
  function move(e){ if(!mode) return; const r=stage.getBoundingClientRect();
    if(mode==='drag'){ rxPlace.x=Math.min(1,Math.max(0,start.x+(e.clientX-sx)/r.width));
      rxPlace.y=Math.min(1,Math.max(0,start.y+(e.clientY-sy)/r.height)); }
    else { rxPlace.scale=Math.min(0.98,Math.max(0.15,start.scale+(e.clientY-sy)/r.height*2)); }
    layoutRx(); }
  function up(){ mode=null; window.removeEventListener('pointermove',move); window.removeEventListener('pointerup',up); }
  sub.addEventListener('pointerdown',e=>down(e,'drag'));
  handle.addEventListener('pointerdown',e=>down(e,'resize'));
}
function showRxEditor(name){
  const host=document.getElementById('rxEditor'); if(!host) return;
  const bgf=(window._storyPrev&&window._storyPrev.chopped&&window._storyPrev.chopped[0])||'';
  const bg = bgf ? `<img class="bg" src="/draft/${bgf}" alt="">` : '';
  host.innerHTML = `<div class="rxedlabel">Drag to move · drag the dot to resize</div>
    <div class="rxstage" id="rxStage">${bg}
      <div class="rxsub" id="rxSub"><img src="/reaction/${name}" alt=""><div class="rxhandle" id="rxHandle"></div></div>
    </div>`;
  const img=new Image();
  img.onload=function(){ rxImgAspect=(img.width/img.height)||0.8;
    const stage=document.getElementById('rxStage'); if(!stage) return;
    const SW=stage.clientWidth, SH=stage.clientHeight;
    rxPlace.scale=0.62;                                   // default: flush bottom-right
    const hpx=rxPlace.scale*SH, wpx=hpx*rxImgAspect;
    rxPlace.x=1-(wpx/2)/SW; rxPlace.y=1-(hpx/2)/SH;
    layoutRx(); attachRx(); };
  img.src='/reaction/'+name;
}
function hideRxEditor(){ const h=document.getElementById('rxEditor'); if(h) h.innerHTML=''; }
function loadBilling(){
  const u=document.getElementById('billUsage'), e=document.getElementById('billEarned');
  if(!u || !lastBooks) return;
  const mt=Object.values(lastBooks.month||{}).reduce((a,x)=>a+(x.cost||0),0);
  const earnM=(lastBooks.earned||{}).month||0;
  u.textContent=fmt$(mt); if(e) e.textContent=fmt$(earnM);
}
function manageBilling(){
  const m=document.getElementById('billMsg');
  if(m) m.textContent='Managed billing is coming soon. For now, Abra runs on the Claude + Muse keys you add in Settings.';
}
function loadSettingsStatus(){
  const el = document.getElementById('settingsStatus'); if (!el) return;
  const s = lastStats || {};
  const svcTxt = (document.getElementById('svcTxt')||{}).textContent || '—';
  const rows = [
    ['Service', svcTxt],
    ['Reply pace', (s.repliesHour!=null ? s.repliesHour+' this window' : '—') + (s.paceSafe ? ' · safe-mode 30/15min' : '')],
    ['Engagement', engPaused ? 'paused' : 'running'],
  ];
  el.innerHTML = rows.map(([k,v])=>
    `<div class="metricrow"><div class="mn">${k}</div><div class="mv" style="font-size:14px">${esc(String(v))}</div></div>`).join('');
}
// ---- Replies waiting for approval: the creator approves each one before it posts
async function replyQueueLoad(){
  let d={pending:[],pages:[]};
  try{ d=await fetch('/replies',{cache:'no-store'}).then(function(r){ return r.json(); }); }catch(e){ return; }
  const card=document.getElementById('replyQueueCard'), list=document.getElementById('replyQueueList'), ap=document.getElementById('replyApprovals');
  if(!card || !list) return;
  list.innerHTML='';
  (d.pending||[]).forEach(function(it){
    const row=document.createElement('div'); row.style.margin='8px 0';
    const who=document.createElement('div'); who.className='hint'; who.textContent='A comment: '+(it.snippet||'(no text)'); row.appendChild(who);
    const rp=document.createElement('div'); rp.style.fontWeight='700'; rp.textContent='Abra would reply: '+it.reply; row.appendChild(rp);
    const ok=document.createElement('button'); ok.className='btn green'; ok.type='button'; ok.textContent='Approve and post'; ok.onclick=function(){ replyDecide(it.id,'approve'); }; row.appendChild(ok);
    const no=document.createElement('button'); no.className='rxlenchip'; no.type='button'; no.textContent='Reject'; no.onclick=function(){ replyDecide(it.id,'reject'); }; row.appendChild(no);
    list.appendChild(row);
  });
  if(ap){
    ap.innerHTML='';
    (d.pages||[]).forEach(function(p){
      const lab=document.createElement('label'); lab.className='rxcaptog';
      const c=document.createElement('input'); c.type='checkbox'; c.checked=!!p.approval;
      c.onchange=function(){ fetch('/replies/approval',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page_id:p.page_id,on:c.checked})}).then(function(){ replyQueueLoad(); }).catch(function(){}); };
      lab.appendChild(c);
      const sp=document.createElement('span'); sp.textContent=' Approve each reply to '+(p.name||'this page')+' before it posts'; lab.appendChild(sp);
      ap.appendChild(lab);
    });
  }
  card.style.display = ((d.pending||[]).length || (d.pages||[]).length) ? 'block' : 'none';
}
async function replyDecide(id, action){
  const msg=document.getElementById('replyQueueMsg');
  let r={msg:'Could not reach the dashboard.'};
  try{ r=await fetch('/replies/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,action:action})}).then(function(x){ return x.json(); }); }catch(e){}
  if(msg) msg.textContent=r.msg||'';
  replyQueueLoad();
  return !!r.ok;
}
async function loadEngagement(){
  try { const r = await fetch('/engagement'); engPaused = (await r.json()).paused; paintEng(); } catch(e) {}
  replyQueueLoad();
}
async function toggleEngagement(){
  const r = await fetch('/engagement', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({paused: !engPaused})});
  engPaused = (await r.json()).paused; paintEng(); loadPages();
}
let currentTab = 'recent'; let pageFilter = ''; let allPages = []; const staged = new Map(); let lastPosts = [];
function renderPageFilter(){
  const el = document.getElementById('pageFilter'); if(!el) return;
  if (!allPages || allPages.length <= 1){ el.innerHTML=''; return; }
  let html = `<button class="pgfilt ${pageFilter===''?'active':''}" onclick="setPageFilter('')">All pages</button>`;
  allPages.forEach(pg=>{ html += `<button class="pgfilt ${pageFilter===pg.id?'active':''}" onclick="setPageFilter('${pg.id}')">${esc(pg.name)}</button>`; });
  el.innerHTML = html;
}
function setPageFilter(id){ pageFilter = id; loadVideos(true); }
function refreshSubmit(){
  const btn = document.getElementById('submitBtn'), msg = document.getElementById('submitMsg');
  if (staged.size) { btn.style.opacity=1; msg.style.color='var(--warn)';
    msg.textContent = staged.size + ' unsaved change' + (staged.size===1?'':'s') + ' — click Submit'; }
  else { btn.style.opacity=.55; }
}
function setTab(t){
  currentTab = t;
  for (const k of ['recent','viral','trending','fresh','earners'])
    document.getElementById('tab-'+k).classList.toggle('active', t===k);
  loadVideos(true);
}
function onCheck(id, on){ staged.set(id, on); refreshSubmit(); loadVideos(false); }
async function clearAllTargets(){
  const msg = document.getElementById('submitMsg');
  if (!confirm('Clear all selected videos? Auto-boosted videos under 24h stay prioritized.')) return;
  try{
    await fetch('/targets', {method:'POST', headers:{'Content-Type':'application/json'},
                             body: JSON.stringify({clear_all:true})});
    staged.clear();
    msg.style.color='var(--good)'; msg.textContent=' ✓ cleared all selected';
    refreshSubmit(); loadVideos(true);
  }catch(e){ msg.style.color='var(--warn)'; msg.textContent='error: '+String(e); }
}
async function submitTargets(){
  if (!staged.size) return;
  const add = [], remove = [];
  staged.forEach((on, id) => (on ? add : remove).push(id));
  const r = await fetch('/targets', {method:'POST', headers:{'Content-Type':'application/json'},
                                     body: JSON.stringify({add, remove})});
  const d = await r.json(); staged.clear();
  const msg = document.getElementById('submitMsg');
  msg.style.color='var(--good)'; msg.textContent=' ✓ saved — prioritizing '+d.count+' video'+(d.count===1?'':'s');
  refreshSubmit(); loadVideos(true);
}
let adSet=new Set();
async function loadAds(){
  var box=document.getElementById('adsBody'); if(!box) return;
  var d;
  try{ d=await fetch('/ads/summary').then(function(r){return r.json();}); }catch(e){ box.innerHTML='<div class="empty">Could not load ads right now.</div>'; return; }
  var ads=(d&&d.ads)||[];
  if(!ads.length){ box.innerHTML='<div class="empty">No ad posts yet.</div>'; return; }
  box.innerHTML=ads.map(function(a){
    return '<div class="adrow"><div class="adrow-main"><div class="adrow-t">'+esc(a.caption||('Post '+a.id))+'</div>'
      +'<div class="adrow-m">'+esc(a.page||'')+(a.created?' · '+esc(a.created):'')+'</div></div>'
      +'<div class="adrow-s"><span><b>'+a.comments+'</b> comments</span><span><b>'+a.replied+'</b> replied</span>'
      +'<span><b>'+a.liked+'</b> liked</span><span><b>'+a.held+'</b> held</span></div>'
      +'<button class="adbtn" onclick="toggleAd(&quot;'+a.id+'&quot;,false)">stop</button></div>';
  }).join('');
}

async function toggleAd(pid, on){
  try{ const r=await fetch('/ads/flag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({post_id:pid,on:on})});
    const d=await r.json(); adSet=new Set(d.ads||[]); }catch(e){}
  loadVideos(false);
  loadAds();
}
async function loadVideos(refetch){
  try {
    try{ const a=await fetch('/ads/flag').then(function(r){return r.json();}); adSet=new Set(a.ads||[]); }catch(e){}
    if (!allPages.length) { try { allPages = await fetch('/pages').then(r=>r.json()); } catch(e){} }
    if (refetch !== false || !lastPosts.length) {
      const r = await fetch('/posts?sort=' + currentTab + (pageFilter ? '&page=' + pageFilter : ''));
      lastPosts = await r.json();
    }
    const el = document.getElementById('videos');
    renderPageFilter();
    if (!lastPosts.length) { el.innerHTML = '<div class="empty">no videos found for this page yet…</div>'; return; }
    let note = '';
    if (currentTab==='trending' && lastPosts[0] && lastPosts[0].no_history)
      note = '<div class="empty"> Trending needs a day of history — check back tomorrow!</div>';
    const cutoff24 = Date.now() - 24*3600*1000;
    el.innerHTML = note + lastPosts.map((p, i) => {
      const fresh = p.ts && !isNaN(Date.parse(p.ts)) && Date.parse(p.ts) >= cutoff24;
      const checked = fresh || (staged.has(p.id) ? staged.get(p.id) : p.targeted);
      const th = p.thumb ? `<img class="th" src="${esc(p.thumb)}" loading="lazy">` : `<div class="noth"></div>`;
      const overlay = fresh ? 'AUTO 24H' : (currentTab!=='recent' ? '#'+(i+1) : (checked ? 'TARGETED' : ''));
      const chip = overlay ? `<span class="chipT">${overlay}</span>` : '';
      const growth = currentTab==='trending' ? `<span class="grow">+${p.d_comments||0}c +${p.d_reactions||0}r</span>`
        : currentTab==='earners' ? `<span class="grow">$${(p.usd||0).toFixed(2)} earned</span>`
        : fresh ? `<span class="grow">bot priority</span>` : '';
      return `<label class="video ${checked?'t':''}" ${fresh?'title="Auto-boosted: videos under 24h get priority from the bot. Unchecks itself when the video ages out."':''}>
        <div class="vbanner">${th}${chip}
          <input type="checkbox" ${checked?'checked':''} ${fresh?'disabled':''} onchange="onCheck('${p.id}', this.checked)"></div>
        <div class="vbody"><div class="cap"><div class="t1">${esc(p.caption)}</div>
          <div class="m">${p.page?`<span class="pbadge">${esc(p.page)}</span>`:''}<span>${p.created}</span>
            <span><b>${p.comments}</b> c</span><span><b>${p.reactions}</b> r</span><span><b>${p.shares||0}</b> s</span>${growth}
            ${p.url?`<a href="${p.url}" target="_blank" onclick="event.stopPropagation()">open ↗</a>`:''}${adSet.has(p.id)?`<span class="adpill">Ad running</span><button class="adbtn" onclick="event.preventDefault();event.stopPropagation();toggleAd('${p.id}',false)">stop</button>`:`<button class="adbtn" onclick="event.preventDefault();event.stopPropagation();toggleAd('${p.id}',true)">Running an ad?</button>`}</div></div></div></label>`;
    }).join('');
  } catch(e) {}
}
async function loadPages(){
  try {
    const r = await fetch('/pages'); const pages = await r.json();
    window._pages = pages;
    document.getElementById('pages').innerHTML = pages.map(pg => `
      <div class="pagerow ${pg.enabled?'on':'off'}">
        <label class="pagename">
          <input type="checkbox" ${pg.enabled?'checked':''} style="accent-color:var(--accent);"
            onchange="togglePage('${pg.id}', this.checked)"> <span>${esc(pg.name)}</span>
        </label>
        <div class="pagepub">
          <span class="pagepub-l">Publishing</span>
          <select onchange="setPubMode('${pg.id}', this.value)">
            ${Object.entries(PUBMODE_LABELS).map(([v,l])=>`<option value="${v}" ${pg.pub_mode===v?'selected':''}>${l}</option>`).join('')}
          </select>
        </div>
      </div>`).join('') + `<div class="hint" style="margin-top:6px; line-height:1.6">Switch a page off to pause your crew there. Publishing: <b>Review</b> = approve every post (safest) &middot; <b>Full auto</b> = Sage schedules &amp; posts on its own</div>`;
    // the Sage Scheduler lists ALL pages so you can target any of them
    for (const id of ['phPage','sqPage','gpPage']) {
      const sel = document.getElementById(id);
      if (sel) sel.innerHTML = pages.map(pg => `<option value="${pg.id}">${esc(pg.name)}</option>`).join('');
    }
    const scSel = document.getElementById('scPage');
    if (scSel) { const prev=scSel.value;
      scSel.innerHTML = pages.map(pg => `<option value="${pg.id}">${esc(pg.name)}</option>`).join('');
      if (prev && pages.some(p=>p.id===prev)) scSel.value=prev;
      showScMode();
    }
  } catch(e) {}
}
function showScMode(){
  const sel=document.getElementById('scPage'), hint=document.getElementById('scPageMode');
  if(!sel||!hint) return;
  const pg=(window._pages||[]).find(p=>p.id===sel.value);
  if(!pg){ hint.textContent=''; return; }
  const m=pg.pub_mode||'review';
  const txt={review:'· review mode — Reels and Stories wait for your approval',
             auto:'· FULL AUTO — this page schedules on drop, no approval'}[m]||('· '+m);
  hint.textContent=txt;
  hint.style.color=(m==='auto')?'var(--warn)':'var(--ink2)';
}
async function togglePage(id, on){
  await fetch('/pages', {method:'POST', headers:{'Content-Type':'application/json'},
                         body: JSON.stringify({page_id:id, enabled:on})});
  loadPages(); loadVideos(true); loadEngagement();
  // the active-page set changed; the server just cleared its monetization/watch caches, so
  // repaint everything that aggregates across pages (followers, minutes viewed, earnings)
  try { await loadMonetization(); } catch(e){}
  try { loadHomeDash(); } catch(e){}
  const av=document.getElementById('view-analytics');
  if (av && av.classList.contains('active')) { try{ loadWatch(); }catch(e){} try{ loadTrend(); }catch(e){} }
}
function spark(vals){
  if (!vals.length) return '<span class="hint">no data yet</span>';
  const w=220, h=44, max=Math.max(...vals,1), min=Math.min(...vals,0);
  const pts = vals.map((v,i)=>`${(i/(vals.length-1||1)*w).toFixed(1)},${(h-4-((v-min)/(max-min||1))*(h-8)).toFixed(1)}`);
  const area = `0,${h} ` + pts.join(' ') + ` ${w},${h}`;
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <polygon class="sparkfill" points="${area}"/>
    <polyline class="spark" points="${pts.join(' ')}"/></svg>`;
}
const STAGES = ['prospect','pitched','in talks','signed','delivered','paid'];
async function loadDeals(){
  try {
    const r = await fetch('/deals'); const deals = await r.json();
    document.getElementById('dealsWrap').innerHTML = deals.map(d => `
      <div class="dealcard">
        <div class="dealhead">
          <b class="dealbrand">${esc(d.brand)}</b>
          <select class="dealstage" onchange="stageDeal(${d.id}, this.value)">
            ${STAGES.map(st => `<option ${st===d.stage?'selected':''}>${st}</option>`).join('')}
          </select>
          <button class="btn dealpitch" onclick="makePitch(${d.id}, '${esc(d.brand).replace(/'/g,"&#39;")}')">draft pitch</button>
        </div>
        ${(d.value || d.contact) ? `<div class="dealsub">${d.value ? `<span class="dealval">$${esc(d.value)}</span>` : ''}${d.contact ? `<span class="dealcontact">${esc(d.contact)}</span>` : ''}</div>` : ''}
        ${d.notes ? `<div class="dealnotes">${esc(d.notes)}</div>` : ''}
      </div>`).join('')
      || '<div class="empty">no deals yet — add your first brand above </div>';
    window._deals = deals;
    const week = Date.now() - 7*86400e3;
    const closed = deals.filter(d => ['signed','delivered','paid'].includes(d.stage)
        && Date.parse(d.updated) >= week)
      .reduce((a,d) => a + (parseFloat(String(d.value).replace(/[^0-9.]/g,'')) || 0), 0);
    document.getElementById('qClosed').textContent = '$' + closed.toLocaleString();
  } catch(e) {}
}
async function reviewContract(){
  const t = document.getElementById('contractText').value.trim();
  if (!t) return;
  document.getElementById('contractOut').textContent = 'Koa is reading the fine print… ';
  say('koala', 'Reading the fine print — nobody sneaks a net-90 past me ', false, ' ');
  const r = await fetch('/contractreview', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({text: t})});
  const j = await r.json();
  document.getElementById('contractOut').textContent = j.review || ('review failed: ' + (j.error||'unknown'));
}
async function loadRights(){ const el=document.getElementById('rightsList'); if(!el) return;
  try{ const rows=await fetch('/rights').then(function(r){return r.json();});
    if(!rows||!rows.length){ el.innerHTML='<div class="empty">Nothing logged yet — Koa logs each reel you schedule here.</div>'; return; }
    el.innerHTML=rows.map(function(r){ return '<div class="rights-row"><span class="rr-t">'+esc(r.title||'Untitled')+'</span><span class="rr-d">✓ protected '+esc((r.created_at||'').replace('T',' '))+'</span><span class="rr-f">#'+esc(r.fingerprint||'')+'</span></div>'; }).join('');
  }catch(e){ el.innerHTML='<div class="empty">couldn’t load</div>'; }
}
async function rightsProtect(){ const t=document.getElementById('rightsTitle'); if(!t) return; const v=(t.value||'').trim(); if(!v) return;
  try{ await fetch('/rights/protect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:v})}); }catch(e){}
  t.value=''; if(typeof say==='function') say('koala','Logged — timestamped and yours.',false,' '); loadRights();
}
async function draftDispute(){ const ta=document.getElementById('claimText'); const t=((ta&&ta.value)||'').trim(); if(!t) return;
  const title=((document.getElementById('rightsTitle')||{}).value||'').trim();
  const out=document.getElementById('disputeOut'); if(out) out.textContent='Koa is drafting your dispute…';
  if(typeof say==='function') say('koala','Nobody claims your work on my watch.',false,' ');
  try{ const j=await fetch('/rights/dispute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({claim:t,title:title})}).then(function(r){return r.json();});
    if(out) out.textContent=(j&&j.letter)||('draft failed: '+((j&&j.error)||'unknown')); }
  catch(e){ if(out) out.textContent='draft failed — try again'; }
}
async function addDeal(){
  const b = document.getElementById('dBrand').value.trim();
  if (!b) return;
  await fetch('/deals', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({brand: b, contact: document.getElementById('dContact').value.trim(),
      value: document.getElementById('dValue').value.trim(),
      notes: document.getElementById('dNotes').value.trim()})});
  ['dBrand','dContact','dValue','dNotes'].forEach(i => document.getElementById(i).value='');
  say('koala', 'New prospect on the board — on it ', false, ' ');
  loadDeals();
}
async function stageDeal(id, stage){
  const d = (window._deals||[]).find(x => x.id === id) || {id};
  d.stage = stage;
  await fetch('/deals', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(d)});
  if (stage === 'paid') say('koala', 'DEAL MARKED PAID. Koa, log it! ', true, ' ');
  loadDeals();
}
async function makePitch(id, brand){
  const box = document.getElementById('pitchBox'); box.style.display='block';
  document.getElementById('pitchText').value = 'Drafting… ';
  say('koala', 'Drafting the ' + brand + ' pitch — give me a sec ', false, ' ');
  const d = (window._deals||[]).find(x => x.id === id) || {};
  const r = await fetch('/pitch', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({brand, notes: d.notes || ''})});
  const j = await r.json();
  document.getElementById('pitchText').value = j.pitch || ('draft failed: ' + (j.error||'unknown'));
  box.scrollIntoView({behavior:'smooth'});
}
function copyPitch(){
  const t = document.getElementById('pitchText');
  t.select(); try { document.execCommand('copy'); } catch(e) {}
  say('koala', 'Copied — go close it, boss ', false, ' ');
}
function mdLite(t){ const NL=String.fromCharCode(10); return esc(t).replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>').split(NL).join('<br>'); }
// ===== "What to make next" idea engine: top of Create + the Insights loop-back =====
let _ideas = []; let pendingBrief = null;
async function loadIdeas(force){
  try{ const d = await fetch('/ideas'+(force?'?refresh=1':'')).then(r=>r.json()); _ideas = d.ideas||[]; }
  catch(e){ _ideas = []; }
  return _ideas;
}
function ideaCardsHTML(ideas, mode){
  if(!ideas || !ideas.length) return '';
  const label = 'What to film next';
  const cta = (mode==='insights') ? 'Make this \u2192' : 'Start this';
  const fn = (mode==='insights') ? 'makeThisIdea' : 'startIdea';
  return '<div class="ideastrip"><div class="ideahead">'+label+' <span class="hint">\u2014 from your numbers</span></div>'
    + '<div class="idearow">'
    + ideas.map(function(it,i){ return '<div class="ideacard"><div class="ideahook">'+esc(it.hook||'')+'</div>'
        + '<div class="ideawhy">'+esc(it.why||'')+'</div>'
        + '<button class="ideago" onclick="'+fn+'('+i+')">'+cta+'</button></div>'; }).join('')
    + '</div></div>';
}
async function loadCreateIdeas(){
  const el=document.getElementById('createIdeas'); if(!el) return;
  if(!_ideas.length) el.innerHTML='<div class="ideastrip"><div class="ideahead">What to film next <span class="hint">\u2014 reading your numbers\u2026</span></div></div>';
  const ideas = await loadIdeas();
  el.innerHTML = ideaCardsHTML(ideas, 'create');
}
function startIdea(i){ pendingBrief = _ideas[i] || null; showBrief();
  const dz=document.getElementById('museLanding'); if(dz) dz.scrollIntoView({behavior:'smooth', block:'center'}); }
function makeThisIdea(i){ pendingBrief = _ideas[i] || null; showView('studio'); setTimeout(showBrief, 170); }
async function runSpotlight(){
  var q=(document.getElementById('spotQ').value||'').trim(); if(!q) return;
  var out=document.getElementById('spotOut'); if(!out) return;
  out.innerHTML='<div class="spot-skel"><i></i><i></i><i></i></div><div class="spot-skel-cap">Sage is writing an AI read from general knowledge on short-form formats for "'+esc(q)+'"…</div>';
  var d={};
  try{ d=await fetch('/spotlight?q='+encodeURIComponent(q)).then(function(r){return r.json();}); }catch(e){}
  if(d && d.reason){ out.innerHTML='<div class="empty">'+esc(d.reason)+'</div>'+spotPresetsHtml(); return; }
  if(!d || (!d.pulse && !(d.ideas&&d.ideas.length))){ out.innerHTML='<div class="empty">Could not load that one — try another genre.</div>'+spotPresetsHtml(); return; }
  window._spotIdeas=d.ideas||[];
  var fb='https://www.facebook.com/search/videos/?q='+encodeURIComponent(q);
  var h='<div class="spotcard"><div class="spothead">Spotlight · '+esc(d.query||q)+'</div>';
  if(d.pulse){ h+='<div class="spotpulse">'+esc(d.pulse)+'</div>'; }
  if(d.trends && d.trends.length){ h+='<div class="spottrends"><span class="spottl">Short-form patterns (AI read, general knowledge)</span>'+d.trends.map(function(t){ return '<button class="spottrend" onclick="spotPreset(this.textContent)">'+esc(t)+'</button>'; }).join('')+'</div>'; }
  if(window._spotIdeas.length){ h+='<div class="spotideas">'+window._spotIdeas.map(function(it,i){ return '<div class="spotidea">'+(it.platform?'<span class="si-plat">Suggested for '+esc(it.platform)+'</span>':'')+'<div class="si-hook">'+esc(it.hook)+'</div><div class="si-why">'+esc(it.why||'')+'</div><button class="btn ghost si-make" onclick="makeSpotIdea('+i+')">Make this →</button></div>'; }).join('')+'</div>'; }
  h+='<a class="btn ghost spotfb" href="'+fb+'" target="_blank" rel="noopener">Explore real '+esc(q)+' videos on Facebook →</a>';
  h+='</div>';
  out.innerHTML=h;
}
function makeSpotIdea(i){ var it=(window._spotIdeas||[])[i]; if(!it) return; pendingBrief={hook:it.hook}; showView('studio'); setTimeout(function(){ if(typeof showBrief==='function') showBrief(); }, 170); }
function showBrief(){
  const el=document.getElementById('briefBanner'); if(!el||!pendingBrief) return;
  el.innerHTML = '<span class="briefdot"></span> Filming next: <b>'+esc(pendingBrief.hook)+'</b>'
    + '<button class="briefx" onclick="clearBrief()">clear</button>';
  el.style.display='flex';
}
function clearBrief(){ pendingBrief=null; const el=document.getElementById('briefBanner'); if(el){ el.style.display='none'; el.innerHTML=''; } }
// ===== Film studio: pick a genre, Sage writes the plan, you record it =====
let _filmGenres=[], _filmPick='';
async function loadFilmGenres(){
  const el=document.getElementById('filmGenres'); if(!el) return;
  if(_filmGenres.length){ renderFilmGenres(); return; }
  try{ const d=await fetch('/film/genres').then(function(r){return r.json();}); _filmGenres=d.genres||[]; }catch(e){ _filmGenres=[]; }
  renderFilmGenres();
}
function renderFilmGenres(){
  const el=document.getElementById('filmGenres'); if(!el) return;
  rxBgShow(_filmPick==='reaction');
  rxHfAllowed(_filmPick!=='reaction');
  if(!_filmGenres.length){ el.innerHTML='<div class="empty">could not load templates</div>'; return; }
  el.innerHTML=_filmGenres.map(function(g,i){ return '<button class="filmgenre'+(g.id===_filmPick?' on':'')+'" onclick="filmPickGenre('+i+')"><span class="fg-label">'+esc(g.label)+'</span><span class="fg-blurb">'+esc(g.blurb||'')+'</span></button>'; }).join('');
}
function filmPickGenre(ix){
  const g=(typeof ix==='number')?_filmGenres[ix]:_filmGenres.find(function(x){return x.id===ix;}); if(!g) return;
  _filmPick=g.id; renderFilmGenres();
  if(g.id==='reaction'){   // Reaction goes straight to the greenscreen format: cut-out on, background picker open, recorder in view
    const c=document.getElementById('rxBgOn'); if(c && !c.checked){ c.checked=true; rxBgSetOn(true); }
    const st=document.getElementById('rxStudio'); if(st) st.scrollIntoView({behavior:'smooth', block:'start'});
  }
  const wrap=document.getElementById('filmInputs'); if(!wrap) return;
  wrap.innerHTML=(g.inputs||[]).map(function(f){
    const ph=esc(f.placeholder||'');
    if(f.kind==='textarea') return '<label class="fl">'+esc(f.label)+'</label><textarea id="fi_'+f.key+'" class="rxtext" rows="2" aria-label="'+esc(f.label)+'" placeholder="'+ph+'"></textarea>';
    return '<label class="fl">'+esc(f.label)+'</label><input id="fi_'+f.key+'" class="rxinput" aria-label="'+esc(f.label)+'" placeholder="'+ph+'">';
  }).join('')+'<button class="btn green" style="margin-top:8px" onclick="filmPlan()">Write my '+esc((g.label||'').toLowerCase())+' \u2192</button>';
  wrap.style.display='block';
  const out=document.getElementById('rxPlan'); if(out) out.innerHTML='';
}
async function filmPlan(){
  if(!_filmPick) return;
  const g=_filmGenres.find(function(x){return x.id===_filmPick;}); if(!g) return;
  const inputs={}; (g.inputs||[]).forEach(function(f){ const el=document.getElementById('fi_'+f.key); inputs[f.key]=el?el.value:''; });
  let any=false; Object.keys(inputs).forEach(function(k){ if((inputs[k]||'').trim()) any=true; }); if(!any) return;
  const out=document.getElementById('rxPlan'); if(!out) return;
  const low=esc((g.label||'').toLowerCase());
  out.innerHTML='<div class="empty">Sage is writing your '+low+'…</div>';
  let d={};
  try{ d=await fetch('/film/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({genre:_filmPick,inputs:inputs})}).then(function(r){return r.json();}); }catch(e){}
  if(!d.beats||!d.beats.length){ out.innerHTML='<div class="empty">Could not write that one — add a bit more detail and try again.</div>'; return; }
  window._rxHook=d.hook||'';
  out.innerHTML='<div class="rxhook-l">Sage\u2019s '+low+' plan</div>'
    + (d.by?'<div class="rxplanby">'+esc(d.by)+'</div>':'')
    + '<div class="rxhook">'+esc(d.hook||'')+'</div>'
    + '<div class="rxbeats">'+d.beats.map(function(b,i){ return '<div class="rxbeat"><span class="rxbnum">'+(i+1)+'</span><div class="rxbbody"><div class="rxmoment">'+esc(b.cue||'')+'</div><div class="rxtake">'+esc(b.line||'')+'</div></div></div>'; }).join('')+'</div>'
    + (d.compliance?'<div class="rxcompliance"><b>Keep it yours:</b> '+esc(d.compliance)+'</div>':'');
  _prm.plan=prmClean(d.beats.map(function(b){ return b.line||''; }));   // spoken lines only: a stage direction never goes on the prompter
  _prm.cur=0; prmRender();
  const st=document.getElementById('rxStudio'); if(st){ st.scrollIntoView({behavior:'smooth',block:'start'}); }
}
let _rxFormat='portrait';
// Portrait only. The preview shows the centre 9:16 of the camera; hand signals are read from the full
// frame behind it (rxGhost), so gestures out to the sides still count without the wide view being shown.
function rxUpdateCropBox(){
  var box=document.getElementById('rxCropBox'), v=document.getElementById('rxCam');
  var dbg=document.getElementById('rxDbg'); if(dbg) dbg.textContent=(v&&v.videoWidth?(v.videoWidth+'×'+v.videoHeight):'')+'  portrait 9:16';
  if(!box) return;
  var z=_rxZoom||1;
  if(_rxPlaying || z<=1){ box.style.display='none'; return; }
  box.style.display='block';
  box.style.width=(100/z).toFixed(1)+'%'; box.style.height=(100/z).toFixed(1)+'%';
}
const RX_DIMS={portrait:[1080,1920]};   // recordings are always portrait 9:16
let _rxZoom=1, _rxPlaying=false;
let rxCanvas=null, rxCtx=null, rxDrawRAF=null;
function rxStartCanvas(){
  const dims=RX_DIMS[_rxFormat]||RX_DIMS.portrait;
  if(!rxCanvas) rxCanvas=document.createElement('canvas');
  rxCanvas.width=dims[0]; rxCanvas.height=dims[1];
  rxCtx=rxCanvas.getContext('2d');
  const v=document.getElementById('rxCam');
  // the cut-out runs on the GPU only while the model is loaded and WebGL 2 works; otherwise the take is the raw crop
  const glOn=function(){ return !!(_bg.on && rxSegReady() && !_bg.broken && _gs && _gs.ok); };
  rxStopCanvas();   // a loop left by a start that failed part way must not keep drawing next to this one
  const takeGl=glOn();   // the take keeps the mode it started in: a model that arrives mid-take does not switch it
  _bg.takeCtx=takeGl ? rxCtx : null;
  (function draw(){
    try{ if(v && v.videoWidth){ if(takeGl && glOn()){ _bg.takeCtx=rxCtx; rxGsTick(v, performance.now()); } else { _bg.takeCtx=null; rxDrawRawCrop(rxCtx, v); } } }catch(e){}
    rxDrawRAF=requestAnimationFrame(draw);
  })();
  const cs=rxCanvas.captureStream(30);
  try{ const at=_bg.mixTrack || ((rxStream&&rxStream.getAudioTracks)?rxStream.getAudioTracks()[0]:null); if(at) cs.addTrack(at); }catch(e){}
  return cs;
}
// the raw camera crop for the take, with the room zoom: used whenever the GPU cut-out is not running
function rxDrawRawCrop(ctx, v){
  const vw=v.videoWidth, vh=v.videoHeight, cw=rxCanvas.width, ch=rxCanvas.height, ta=cw/ch, va=vw/vh;
  let sw,sh,sx,sy;
  if(va>ta){ sh=vh; sw=vh*ta; sx=(vw-sw)/2; sy=0; } else { sw=vw; sh=vw/ta; sx=0; sy=(vh-sh)/2; }
  var z=_rxZoom||1; if(z>1){ var nsw=sw/z, nsh=sh/z; sx+=(sw-nsw)/2; sy+=(sh-nsh)/2; sw=nsw; sh=nsh; }
  ctx.drawImage(v, sx,sy,sw,sh, 0,0,cw,ch);
}
// ---- Reaction background ("greenscreen"): you are cut out of your camera with a person-segmentation model and placed
// in front of a background clip. Segmentation, not colour keying, so a plain wall works as well as a green screen.
// Your voice and the background's sound are mixed with their own volumes into the take's audio.
const RX_BG_MODEL='https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter/float16/latest/selfie_segmenter.tflite';
let _bg={on:false, url:'', video:null, vsrc:null, vsrcFor:null, seg:null, segFailed:false, segBusy:false, ctxA:null, gVoice:null, gBg:null, mixTrack:null, voice:1, bgVol:0.6, takeCtx:null};
function rxCoverRect(bw,bh,cw,ch){ const ta=cw/ch, va=bw/bh; if(va>ta){ const sw=bh*ta; return {sx:(bw-sw)/2, sy:0, sw:sw, sh:bh}; } const sh=bw/ta; return {sx:0, sy:(bh-sh)/2, sw:bw, sh:sh}; }
// the camera window in the greenscreen frame (portrait 9:16), as fractions of the camera. Zoom 1 is today's centre crop;
// above 1 is a tighter crop; below 1 the window is taller (or wider) than the camera, so the clip shows around the camera.
// The mask lives in camera space, so a zoom change never needs a new segmentation. Pure: no GL.
function rxGsLayout(vw, vh, zoom){ const ta=9/16, va=vw/vh; let sw0, sh0; if(va>ta){ sh0=vh; sw0=vh*ta; } else { sw0=vw; sh0=vw/ta; } const z=zoom||1, Wc=sw0/z, Hc=sh0/z; return { camS:[Wc/vw, Hc/vh], camO:[0.5-Wc/(2*vw), 0.5-Hc/(2*vh)], R:vw/vh, MB:(vw/vh-1)/2 }; }
// the camera asks for full HD while Greenscreen is on (ideal, so a camera that cannot do it still opens); the phone path never asks
function rxGsWant(){ return (_bg.on && !_rxRemote) ? {width:{ideal:1920}, height:{ideal:1080}} : {}; }
// applyConstraints replaces every constraint, so the camera the track was opened on is named again rather than dropped
function rxGsKeepSource(tr){ const cur=(tr && tr.getConstraints && tr.getConstraints()) || {}, k={}; if(cur.deviceId) k.deviceId=cur.deviceId; if(cur.facingMode) k.facingMode=cur.facingMode; return k; }
// one applyConstraints call: the size (full HD while Greenscreen is on, the camera's own size when it is off) and the brightness if one is set.
// Never during a take. Returns the note it showed ('' for none), so the Greenscreen start note can keep it.
async function rxGsApplyWide(){
  const tr=(rxStream && rxStream.getVideoTracks)?rxStream.getVideoTracks()[0]:null;
  if(!tr || !tr.applyConstraints || _rxRemote || rxTakeBusy() || tr.readyState==='ended') return '';
  const c=Object.assign({}, rxGsKeepSource(tr), rxGsWant());
  if(_rxBrightKey && _rxBrightVal!==null) c.advanced=[{[_rxBrightKey]: _rxBrightVal}];
  let ok=true, note='';
  try{ await tr.applyConstraints(c); }catch(e){ ok=false; }
  try{ const st=(tr.getSettings && tr.getSettings()) || {}; const sh=Math.min(st.width||0, st.height||0), lo=Math.max(st.width||0, st.height||0); if(_bg.on && sh && (sh<1080 || lo<1920)) note='Your camera gives '+st.width+'×'+st.height+', so the picture is less sharp than full HD.'; }catch(e){}
  if(!note && _bg.on && !ok) note='Your camera did not accept full HD, so Greenscreen uses the size it has.';
  if(note) rxBgNote(note);
  return note;
}
// the segmenter's input: the centred square of the camera's full height. It is drawn on black first, so a portrait phone frame gets black side bars and is not squashed.
const RX_GS_N=512;
function rxGsSegInput(v, cv){ const vw=v.videoWidth, vh=v.videoHeight, N=RX_GS_N; const x0=vw/2-vh/2, sx=Math.max(0,x0), ex=Math.min(vw,x0+vh), k=N/vh; const c=cv.getContext('2d'); c.fillStyle='#000'; c.fillRect(0,0,N,N); c.drawImage(v, sx,0, ex-sx,vh, (sx-x0)*k,0, (ex-sx)*k,N); return cv; }
// ---- the cut-out on the GPU (WebGL 2). One composite draws the camera window, the background clip and you (cut out by the mask)
// into the 1080 x 1920 canvas #rxBgView. The take copies that canvas before any preview flip, so the take is always raw.
const RX_GS_GATE=30;   // ms between ticks: 30 (not 33) so a 60 Hz screen gives exactly 30 ticks a second
const RX_GS_VS=['#version 300 es','void main(){ vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2)); gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0); }'].join(String.fromCharCode(10));
const RX_GS_FS=['#version 300 es','precision highp float;','uniform sampler2D uCam; uniform sampler2D uBg; uniform sampler2D uMask;','uniform vec2 uCamS; uniform vec2 uCamO; uniform vec2 uBgS; uniform vec2 uBgO;','uniform float uR; uniform float uMB; uniform float uFlip; uniform float uCut; uniform float uBgOn; uniform vec2 uOut;','out vec4 fragColor;','void main(){','  vec2 ou = vec2(gl_FragCoord.x / uOut.x, 1.0 - gl_FragCoord.y / uOut.y);','  vec2 pou = vec2(mix(ou.x, 1.0 - ou.x, uFlip), ou.y);','  vec2 cuv = pou * uCamS + uCamO;','  vec2 muv = vec2(cuv.x * uR - uMB, cuv.y);','  vec2 buv = ou * uBgS + uBgO;','  vec3 bg = uBgOn > 0.5 ? texture(uBg, buv).rgb : vec3(0.043, 0.059, 0.102);','  vec3 cam = texture(uCam, clamp(cuv, 0.0, 1.0)).rgb;','  float inCam = step(0.0, cuv.x) * step(cuv.x, 1.0) * step(0.0, cuv.y) * step(cuv.y, 1.0);','  float inMask = step(0.0, muv.x) * step(muv.x, 1.0);','  float t = clamp((texture(uMask, clamp(muv, 0.0, 1.0)).r - 0.4) / 0.35, 0.0, 1.0);','  float a = uCut > 0.5 ? t * t * (3.0 - 2.0 * t) * inMask * inCam : inCam;','  fragColor = vec4(mix(bg, cam, a), 1.0);','}'].join(String.fromCharCode(10));
const RX_GS_EMA=['#version 300 es','precision highp float;','uniform sampler2D uRaw; uniform sampler2D uPrev; uniform float uK; uniform vec2 uSize;','out vec4 fragColor;','void main(){ vec2 uv = gl_FragCoord.xy / uSize; fragColor = vec4(mix(texture(uPrev, uv).r, texture(uRaw, uv).r, uK)); }'].join(String.fromCharCode(10));
let _gs=null;   // the GL state: null until rxGsInit runs; {ok:false} when WebGL 2 is missing, the setup fails or the context is lost
// compiles and links one program; throws on failure. names are the uniforms to look up.
function rxGsProgram(gl, vs, fs, names){
  const sh=function(type, src){ const s=gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); if(!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error('shader: '+gl.getShaderInfoLog(s)); return s; };
  const p=gl.createProgram(); gl.attachShader(p, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fs)); gl.linkProgram(p);
  if(!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error('link: '+gl.getProgramInfoLog(p));
  const u={}; names.forEach(function(n){ u[n]=gl.getUniformLocation(p, n); });
  return {p:p, u:u};
}
function rxGsTex(gl, filter){
  gl.activeTexture(gl.TEXTURE3);
  const t=gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return t;
}
function rxGsAllocMask(gl, t, w, h){ gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, t); gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null); }
// sets up the context on #rxBgView, the programs, the textures and the mask targets. Returns false when WebGL 2 is missing or the setup fails.
function rxGsInit(){
  if(_gs) return _gs.ok;
  const cv=document.getElementById('rxBgView');
  if(!cv){ _gs={ok:false}; return false; }
  cv.width=1080; cv.height=1920;
  let gl=null;
  try{ gl=cv.getContext('webgl2', {alpha:false, antialias:false, depth:false, stencil:false, premultipliedAlpha:false, preserveDrawingBuffer:false}); }catch(e){ gl=null; }
  if(!gl){ _gs={ok:false}; return false; }
  try{
    const N=RX_GS_N, G={ok:false, cv:cv, gl:gl, cur:0, mw:N, mh:N, haveMask:false, lastTick:-1e9, bgOn:false, pend:null};
    G.comp=rxGsProgram(gl, RX_GS_VS, RX_GS_FS, ['uCam','uBg','uMask','uCamS','uCamO','uBgS','uBgO','uR','uMB','uFlip','uCut','uBgOn','uOut']);
    G.ema=rxGsProgram(gl, RX_GS_VS, RX_GS_EMA, ['uRaw','uPrev','uK','uSize']);
    G.vao=gl.createVertexArray(); gl.bindVertexArray(G.vao);
    G.camTex=rxGsTex(gl, gl.LINEAR); G.bgTex=rxGsTex(gl, gl.LINEAR); G.rawTex=rxGsTex(gl, gl.NEAREST);
    G.maskTex=[rxGsTex(gl, gl.LINEAR), rxGsTex(gl, gl.LINEAR)];
    G.maskTex.forEach(function(t){ rxGsAllocMask(gl, t, N, N); });
    G.maskFbo=[gl.createFramebuffer(), gl.createFramebuffer()];
    G.maskFbo.forEach(function(f, i){ gl.bindFramebuffer(gl.FRAMEBUFFER, f); gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, G.maskTex[i], 0); if(gl.checkFramebufferStatus(gl.FRAMEBUFFER)!==gl.FRAMEBUFFER_COMPLETE) throw new Error('framebuffer'); });
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.useProgram(G.comp.p); gl.uniform1i(G.comp.u.uCam, 0); gl.uniform1i(G.comp.u.uBg, 1); gl.uniform1i(G.comp.u.uMask, 2);
    gl.useProgram(G.ema.p); gl.uniform1i(G.ema.u.uRaw, 0); gl.uniform1i(G.ema.u.uPrev, 1);
    G.segIn=document.createElement('canvas'); G.segIn.width=N; G.segIn.height=N;
    cv.addEventListener('webglcontextlost', function(e){ if(e && e.preventDefault) e.preventDefault(); if(_gs){ _gs.ok=false; _gs.lost=true; } _bg.broken=true; rxBgNote('The browser reset the graphics, so the cut-out stopped. Reload the page to get it back.'); });
    G.ok=true; _gs=G;
    return true;
  }catch(e){ _gs={ok:false}; return false; }
}
// a new mask size: both mask targets are sized again and the next mask is taken as it is (no blend)
function rxGsMaskStore(w, h){ rxGsAllocMask(_gs.gl, _gs.maskTex[0], w, h); rxGsAllocMask(_gs.gl, _gs.maskTex[1], w, h); _gs.mw=w; _gs.mh=h; _gs.haveMask=false; }
// the camera frame, and the background clip when it has data
function rxGsUpload(v, bgV){
  const g=_gs.gl; g.activeTexture(g.TEXTURE3);
  g.bindTexture(g.TEXTURE_2D, _gs.camTex); g.texImage2D(g.TEXTURE_2D, 0, g.RGBA8, g.RGBA, g.UNSIGNED_BYTE, v);
  _gs.bgOn=!!(bgV && bgV.readyState>=2 && bgV.videoWidth);
  if(_gs.bgOn){ g.bindTexture(g.TEXTURE_2D, _gs.bgTex); g.texImage2D(g.TEXTURE_2D, 0, g.RGBA8, g.RGBA, g.UNSIGNED_BYTE, bgV); }
}
// one composite into the 1080 x 1920 canvas. flip 1 mirrors you (camera and cut-out together); the clip is never flipped.
function rxGsDraw(flip, L, bgV){
  const G=_gs, g=G.gl, u=G.comp.u, bgOk=!!(G.bgOn && bgV && bgV.videoWidth);
  const r=bgOk ? rxCoverRect(bgV.videoWidth, bgV.videoHeight, 1080, 1920) : {sx:0, sy:0, sw:1, sh:1};
  const bw=bgOk ? bgV.videoWidth : 1, bh=bgOk ? bgV.videoHeight : 1;
  g.bindFramebuffer(g.FRAMEBUFFER, null); g.viewport(0, 0, 1080, 1920); g.useProgram(G.comp.p);
  g.uniform2f(u.uCamS, L.camS[0], L.camS[1]); g.uniform2f(u.uCamO, L.camO[0], L.camO[1]);
  g.uniform2f(u.uBgS, r.sw/bw, r.sh/bh); g.uniform2f(u.uBgO, r.sx/bw, r.sy/bh);
  g.uniform1f(u.uR, L.R); g.uniform1f(u.uMB, L.MB); g.uniform1f(u.uFlip, flip ? 1 : 0);
  g.uniform1f(u.uCut, G.haveMask ? 1 : 0); g.uniform1f(u.uBgOn, bgOk ? 1 : 0); g.uniform2f(u.uOut, 1080, 1920);
  g.activeTexture(g.TEXTURE0); g.bindTexture(g.TEXTURE_2D, G.camTex);
  g.activeTexture(g.TEXTURE1); g.bindTexture(g.TEXTURE_2D, G.bgTex);
  g.activeTexture(g.TEXTURE2); g.bindTexture(g.TEXTURE_2D, G.maskTex[G.cur]);
  g.drawArrays(g.TRIANGLES, 0, 3);
}
// blends the raw mask into the running mask: k 1 takes it as it is, 0.5 moves halfway (one step per tick)
function rxGsEma(k){
  const G=_gs, g=G.gl, u=G.ema.u;
  g.bindFramebuffer(g.FRAMEBUFFER, G.maskFbo[1-G.cur]); g.viewport(0, 0, G.mw, G.mh); g.useProgram(G.ema.p);
  g.activeTexture(g.TEXTURE0); g.bindTexture(g.TEXTURE_2D, G.rawTex);
  g.activeTexture(g.TEXTURE1); g.bindTexture(g.TEXTURE_2D, G.maskTex[G.cur]);
  g.uniform1f(u.uK, k); g.uniform2f(u.uSize, G.mw, G.mh);
  g.drawArrays(g.TRIANGLES, 0, 3);
  G.cur=1-G.cur; G.haveMask=true;
}
// a segmentation error: twenty in a row break the cut-out (the raw crop goes on)
function rxGsFail(){ _bg.errs=(_bg.errs||0)+1; if(_bg.errs>=20) rxBgBroke(); }
// runs the segmenter on the 512 square and, when it returns a mask, blends it in. Returns true when a mask was used.
function rxGsSegment(v, now){
  if(!_gs || !_gs.ok || !_bg.seg || _bg.broken || _bg.segBusy) return false;
  rxGsSegInput(v, _gs.segIn);   // the 512 square, never the video element
  _bg.segBusy=true;
  let got=false;
  try{
    _bg.seg.segmentForVideo(_gs.segIn, now, function(res){
      const conf=res && res.confidenceMasks;
      try{
        const pm=(conf && conf.length) ? (conf.length===1 ? conf[0] : conf[RX_BG_INVERT ? 0 : 1]) : null;   // one mask is you; two masks: the class the hard mask used
        if(!pm){ rxGsFail(); return; }
        const G=_gs, g=G.gl, w=pm.width, h=pm.height;
        if(w!==G.mw || h!==G.mh) rxGsMaskStore(w, h);
        g.activeTexture(g.TEXTURE3); g.bindTexture(g.TEXTURE_2D, G.rawTex);
        g.texImage2D(g.TEXTURE_2D, 0, g.R32F, w, h, 0, g.RED, g.FLOAT, pm.getAsFloat32Array());   // read here: the data is valid only inside this callback
        _bg.errs=0; got=true;
      }catch(e){ rxGsFail(); }
      finally{ if(conf) conf.forEach(function(m){ if(m && m.close) m.close(); }); }
    });
  }catch(e){ rxGsFail(); }
  finally{ _bg.segBusy=false; }
  if(got) rxGsEma(_gs.haveMask ? 0.5 : 1);
  return got;
}
// one tick: upload, segment, then the draws. The take gets the raw pass, is copied, and only then does the preview pass flip you.
function rxGsTick(v, now){
  if(!_gs || !_gs.ok || !v || !v.videoWidth || v.readyState<2) return false;
  if(now-_gs.lastTick<RX_GS_GATE) return false;
  _gs.lastTick=now;
  rxGsUpload(v, _bg.video);
  if(_gs.pend){ const p=_gs.pend; _gs.pend=null; rxGsUseMask(p.w, p.h, p.data); }   // a worker mask: uploaded and blended here, on the draw's clock
  if(_segW){ rxSegRotate(now); rxSegSend(v, now); } else if(_bg.seg) rxGsSegment(v, now);
  const L=rxGsLayout(v.videoWidth, v.videoHeight, _bg.zoom);
  if(_bg.takeCtx){
    rxGsDraw(0, L, _bg.video);                  // the take: raw, as filmed
    _bg.takeCtx.drawImage(_gs.cv, 0, 0);       // copied in the same task as the draw, so the canvas still holds this frame
    if(_rxMirror) rxGsDraw(1, L, _bg.video);   // the preview only
  } else rxGsDraw(_rxMirror ? 1 : 0, L, _bg.video);
  return true;
}
// ---- step 8: the segmenter in a worker. The worker only segments the 512 square and sends back the raw mask; the main thread keeps the draws, the blend and the take.
const RX_SEG_STALL=1500, RX_SEG_INIT=15000, RX_SEG_ROTATE=300000;   // ms: a frame with no answer, the start-up limit, and how often a fresh worker replaces the old one between takes
let _segW=null, _segWP=null;   // _segW: the live worker {w, ready, busy, id, t0, made, next}; _segWP: the start in progress
// the worker's source as lines (no backslashes): it loads the model, then segments each frame it is sent and posts the raw mask back
function rxSegWorkerSrc(){ return [
  'let seg=null, inv=true, used="";',
  'self.onmessage=async function(e){',
  '  const d=e.data;',
  '  if(d.t==="init"){',
  '    inv=!!d.invert;',
  '    try{',
  '      const mod=await import(d.esm);',
  '      const vision=await mod.FilesetResolver.forVisionTasks(d.wasm);',
  '      const oc=new OffscreenCanvas(512,512);',
  '      const mk=function(delegate){ return mod.ImageSegmenter.createFromOptions(vision,{canvas:oc, baseOptions:{modelAssetPath:d.model, delegate:delegate}, runningMode:"VIDEO", outputCategoryMask:false, outputConfidenceMasks:true}); };',
  '      try{ seg=await mk("GPU"); used="GPU"; }catch(err){ seg=await mk("CPU"); used="CPU"; }',
  '      self.postMessage({t:"ready", delegate:used});',
  '    }catch(err){ self.postMessage({t:"fail"}); }',
  '    return;',
  '  }',
  '  if(d.t!=="frame") return;',
  '  const bmp=d.bmp; let sent=false, t0=performance.now();',
  '  try{',
  '    if(!seg) throw new Error("no segmenter");',
  '    seg.segmentForVideo(bmp, d.ts, function(res){',
  '      const conf=res && res.confidenceMasks;',
  '      try{',
  '        const pm=(conf && conf.length) ? (conf.length===1 ? conf[0] : conf[inv?0:1]) : null;',
  '        if(!pm) throw new Error("no mask");',
  '        const src=pm.getAsFloat32Array(), copy=new Float32Array(src.length);',
  '        copy.set(src);',
  '        sent=true;',
  '        self.postMessage({t:"mask", id:d.id, w:pm.width, h:pm.height, ms:performance.now()-t0, data:copy.buffer}, [copy.buffer]);',
  '      }catch(err){ if(!sent){ sent=true; self.postMessage({t:"err", id:d.id}); } }',
  '      finally{ if(conf) conf.forEach(function(m){ if(m && m.close) m.close(); }); }',
  '    });',
  '  }catch(err){ if(!sent){ sent=true; self.postMessage({t:"err", id:d.id}); } }',
  '  finally{ try{ bmp.close(); }catch(err){} }',
  '};'
].join(String.fromCharCode(10)); }
// starts a worker: resolves to its record once the model has loaded, or to null (no worker, a failed start, or no answer in time)
function rxSegWorkerMake(){
  return new Promise(function(resolve){
    let url='', w=null;
    try{ url=URL.createObjectURL(new Blob([rxSegWorkerSrc()], {type:'text/javascript'})); w=new Worker(url); }catch(e){ resolve(null); return; }
    const W={w:w, url:url, ready:false, busy:false, id:0, t0:0, made:performance.now(), next:false, delegate:''};
    const timer=setTimeout(function(){ try{ w.terminate(); }catch(e){} resolve(null); }, RX_SEG_INIT);
    w.onmessage=function(e){
      const d=(e && e.data)||{};
      if(d.t==='ready'){ if(!W.ready){ W.ready=true; W.delegate=d.delegate||''; clearTimeout(timer); resolve(W); } return; }
      if(d.t==='fail'){ clearTimeout(timer); try{ w.terminate(); }catch(x){} resolve(null); return; }
      if(d.t==='mask'){ if(d.id===W.id && W.busy){ W.busy=false; _bg.errs=0; _gs.pend={w:d.w, h:d.h, data:new Float32Array(d.data)}; } return; }
      if(d.t==='err'){ if(d.id===W.id && W.busy){ W.busy=false; rxGsFail(); } }
    };
    w.onerror=function(){ if(!W.ready){ clearTimeout(timer); resolve(null); return; } rxSegWorkerDown(W); };
    w.postMessage({t:'init', esm:RX_MP_ESM, wasm:RX_MP_WASM, model:RX_BG_MODEL, invert:RX_BG_INVERT});
  });
}
// a worker that dies: the main-thread segmenter is loaded in its place, so the cut-out goes on
function rxSegWorkerDown(W){ try{ W.w.terminate(); }catch(e){} if(_segW===W){ _segW=null; rxBgLoadSeg(); } }
function rxSegReady(){ return !!((_segW && _segW.ready) || _bg.seg); }
// the segmenter for the cut-out: a worker when one can be made, otherwise the main-thread segmenter (loaded once)
function rxSegStart(){
  if(_segW || _bg.seg) return Promise.resolve(true);
  if(!_segWP) _segWP=rxSegStartNow().then(function(ok){ _segWP=null; return ok; });
  return _segWP;
}
async function rxSegStartNow(){
  const W=await rxSegWorkerMake();
  if(W){ _segW=W; return true; }
  return await rxBgLoadSeg();
}
// one frame to the worker when it is free: the 512 square as a bitmap, stamped with the tick's clock. A frame with no answer after 1.5 s is dropped.
function rxSegSend(v, now){
  const W=_segW; if(!W || !W.ready || !_gs || !_gs.ok) return;
  if(W.busy){ if(now-W.t0>RX_SEG_STALL){ W.busy=false; W.id++; rxGsFail(); } return; }
  rxGsSegInput(v, _gs.segIn);
  W.busy=true; W.t0=now; const id=++W.id;
  createImageBitmap(_gs.segIn).then(function(bmp){ W.w.postMessage({t:'frame', id:id, ts:now, bmp:bmp}, [bmp]); }, function(){ if(W.id===id && W.busy){ W.busy=false; rxGsFail(); } });
}
// a worker mask: its raw values go to the texture, then the same blend as the main-thread path
function rxGsUseMask(w, h, data){
  const G=_gs, g=G.gl;
  if(w!==G.mw || h!==G.mh) rxGsMaskStore(w, h);
  g.activeTexture(g.TEXTURE3); g.bindTexture(g.TEXTURE_2D, G.rawTex);
  g.texImage2D(g.TEXTURE_2D, 0, g.R32F, w, h, 0, g.RED, g.FLOAT, data);
  rxGsEma(G.haveMask ? 0.5 : 1);
}
// between takes, every five minutes: a fresh worker takes over and the old one is closed (a long-running segmenter can leak; see plan step 8e)
function rxSegRotate(now){
  const W=_segW; if(!W || W.next || rxTakeBusy() || now-W.made<RX_SEG_ROTATE) return;
  W.next=true;
  rxSegWorkerMake().then(function(N){
    if(!N){ W.next=false; W.made=performance.now(); return; }   // it will try again in five minutes
    if(_segW!==W || rxTakeBusy()){ try{ N.w.terminate(); }catch(e){} W.next=false; return; }
    _segW=N; try{ W.w.terminate(); }catch(e){}
  });
}
// the category mask from the selfie segmenter marks you with 0 on the phone camera, so the cut-out keeps the 0 pixels.
// Set RX_BG_INVERT to false if a device shows the opposite (you would be cut out and the wall would show).
const RX_BG_INVERT = true;
function rxBgGain(v){ return Math.max(0, Math.min(150, +v||0))/100; }
function rxBgVol(which, v){
  const n=Math.max(0, Math.min(150, parseInt(v,10)||0));
  if(which==='voice'){ _bg.voice=n/100; const el=document.getElementById('rxBgVoiceV'); if(el) el.textContent=n+'%'; if(_bg.gVoice) _bg.gVoice.gain.value=_bg.voice; }
  else { _bg.bgVol=n/100; const el=document.getElementById('rxBgBgV'); if(el) el.textContent=n+'%'; if(_bg.gBg) _bg.gBg.gain.value=_bg.bgVol; }
}
function rxBgZoomLabel(z){ return Math.round(z*100)+'%'; }
function rxBgZoom(v){
  const n=Math.max(60, Math.min(100, Math.round(+v)||100));   // greenscreen zoom only goes smaller than the 100% preset
  const sl=document.getElementById('rxBgZoomSl'), el=document.getElementById('rxBgZoomV');
  if(rxTakeBusy()){ if(sl) sl.value=String(Math.round((_bg.zoom||1)*100)); rxFeedback('Finish this take first, then change the zoom.', 3000); return false; }
  _bg.zoom=n/100; if(sl) sl.value=String(n);
  if(el) el.textContent=rxBgZoomLabel(_bg.zoom);
  return true;
}
function rxBgNote(t){ const n=document.getElementById('rxBgNote'); if(n) n.textContent=t; }
function rxBgShow(on){ const p=document.getElementById('rxBgPanel'); if(p) p.style.display=on?'block':'none'; if(!on && _bg.on) rxBgSetOn(false); }
// one load at a time: a second call while the model loads waits for the same load, so no second segmenter is built
let _segP=null;
function rxBgLoadSeg(){
  if(_bg.seg) return Promise.resolve(true); if(_bg.segFailed) return Promise.resolve(false);
  if(!_segP) _segP=rxBgLoadSegNow().then(function(ok){ _segP=null; return ok; });
  return _segP;
}
async function rxBgLoadSegNow(){
  try{
    const mod=await _gTimeout(import(RX_MP_ESM), 9000);
    const vision=await _gTimeout(mod.FilesetResolver.forVisionTasks(RX_MP_WASM), 9000);
    const mk=function(delegate){ return mod.ImageSegmenter.createFromOptions(vision,{baseOptions:{modelAssetPath:RX_BG_MODEL, delegate:delegate}, runningMode:'VIDEO', outputCategoryMask:false, outputConfidenceMasks:true}); };
    try{ _bg.seg=await _gTimeout(mk('GPU'), 15000); }catch(e){ _bg.seg=await _gTimeout(mk('CPU'), 15000); }
    return true;
  }catch(e){ _bg.segFailed=true; return false; }
}
const RX_GREENSCREEN_ON=false;   // Greenscreen is switched off for the hackathon submission; set true to bring it back
async function rxBgSetOn(on){
  if(on && !RX_GREENSCREEN_ON){ const c0=document.getElementById('rxBgOn'); if(c0) c0.checked=false; rxFeedback('Greenscreen is not in this version of Abra.', 3000); return false; }
  // a take that is running or about to start keeps the mode it began with, so Greenscreen waits until it ends
  if(rxTakeBusy()){ const c0=document.getElementById('rxBgOn'); if(c0) c0.checked=_bg.on; rxFeedback('Finish this take first, then change Greenscreen.', 3000); return false; }
  _bg.on=!!on; _bg.broken=false; _bg.errs=0; const c=document.getElementById('rxBgOn'); if(c) c.checked=_bg.on;
  if(_bg.on){
    if(!rxGsInit()){ _bg.on=false; if(c) c.checked=false; _bg.segFailed=true;
      rxBgNote(_gs && _gs.lost ? 'The browser reset the graphics, so the cut-out cannot start. Reload the page to get it back.' : 'This browser cannot draw the cut-out (it needs WebGL 2), so the camera is shown without it.'); return false; }
    rxBgNote('Greenscreen is on. Loading the cut-out model the first time (it needs internet)\u2026');
    const wide=rxGsApplyWide();   // the camera goes to full HD now; the model load does not wait for it
    const ok=await rxSegStart();
    const wn=await wide;
    if(!_bg.on) return;   // switched off while the model loaded: its note stays
    rxBgNote((ok ? 'Greenscreen is on. Pick a background clip, then record.' : 'The cut-out model could not load (it needs internet), so the picture is not cut out.')+(wn?' '+wn:''));
    if(ok) rxBgViewStart();
  } else {
    rxBgNote('Pick a background clip, then record. Your clip plays behind you and is mixed with your voice.');
    const cv=document.getElementById('rxBgView'); if(cv) cv.style.display='none';
    rxGsApplyWide();   // back to the camera's own size (not during a take)
  }
}
function rxBgPick(f){
  if(!f) return;
  if(!/^video\\//.test(f.type||'')){ rxBgNote('That is not a video file. Choose an MP4 or WebM clip.'); return; }
  if(_bg.url){ try{ URL.revokeObjectURL(_bg.url); }catch(e){} }
  _bg.url=URL.createObjectURL(f);
  const v=document.createElement('video'); v.src=_bg.url; v.loop=true; v.playsInline=true; v.preload='auto';
  v.onloadeddata=function(){ rxBgNote('Background ready: '+(f.name||'clip')+'. Greenscreen shows it behind you when you record.'); };
  v.onerror=function(){ rxBgNote('That video could not be played in this browser. Try an MP4.'); };
  v.muted=true; v.play().catch(function(){});
  _bg.video=v;
  rxBgViewStart();
}
function rxBgBroke(){
  _bg.broken=true;
  rxBgNote('The cut-out could not follow you, so the camera is shown without it. Try a plainer background, or turn Greenscreen off.');
}
// the live preview: the same GPU frame the take copies (rxGsTick), shown only while the cut-out runs
function rxBgViewStart(){
  if(!_bg.on || _bg.viewRAF) return;
  const loop=function(){
    const cv=document.getElementById('rxBgView'), v=document.getElementById('rxCam');
    if(!_bg.on || !cv){ _bg.viewRAF=null; if(cv) cv.style.display='none'; return; }
    // not while a finished take plays back: the review shows the file as it was filmed
    const ready = !!(rxStream && rxSegReady() && !_bg.broken && !_rxPlaying && v && v.videoWidth);
    cv.style.display = ready ? 'block' : 'none';
    if(ready){ try{ rxGsTick(v, performance.now()); }catch(e){} }   // the gate inside rxGsTick keeps this at 30 a second, shared with the take; a throw must not stop the loop
    _bg.viewRAF=requestAnimationFrame(loop);
  };
  _bg.viewRAF=requestAnimationFrame(loop);
}
// the take's sound: your voice and the background clip's sound, each through its own gain
function rxBgAudio(){
  _bg.mixTrack=null;
  if(!_bg.on) return null;
  try{
    const AC=window.AudioContext||window.webkitAudioContext;
    if(!_bg.ctxA) _bg.ctxA=new AC();
    const ctx=_bg.ctxA; if(ctx.state==='suspended'){ try{ ctx.resume(); }catch(e){} }
    const dest=ctx.createMediaStreamDestination();
    _bg.gVoice=ctx.createGain(); _bg.gVoice.gain.value=_bg.voice; _bg.gVoice.connect(dest);
    _bg.gBg=ctx.createGain(); _bg.gBg.gain.value=_bg.bgVol; _bg.gBg.connect(dest);
    const at=(rxStream && rxStream.getAudioTracks) ? rxStream.getAudioTracks()[0] : null;
    if(at) ctx.createMediaStreamSource(new MediaStream([at])).connect(_bg.gVoice);
    if(_bg.video){
      _bg.video.muted=false;   // the clip's sound goes to the take now; the preview stays silent
      if(_bg.vsrcFor!==_bg.video){ _bg.vsrc=ctx.createMediaElementSource(_bg.video); _bg.vsrcFor=_bg.video; }   // one source per clip
      _bg.vsrc.connect(_bg.gBg);
      _bg.video.play().catch(function(){});
    }
    _bg.mixTrack=dest.stream.getAudioTracks()[0]||null;
    return _bg.mixTrack;
  }catch(e){ _bg.mixTrack=null; return null; }
}
function rxStopCanvas(){ if(rxDrawRAF) cancelAnimationFrame(rxDrawRAF); rxDrawRAF=null; _bg.takeCtx=null; }
let rxStream=null, rxRec=null, rxChunks=[], rxRecOn=false, rxRecMs=0, rxTalkMs=0, rxLastMs=0, rxActx=null, rxAn=null, rxRAF=null, rxTimerT=null, rxBlob=null;
let _lanUrl='';
let rxPC=null, rxSess='', rxRtcPoll=null, rxRtcOn=false;
async function rxAddCam(){
  const panel=document.getElementById('rxPhonePanel'); if(!panel) return;
  if(panel.style.display!=='none'){ rxRtcOn=false; if(rxRtcPoll){ clearTimeout(rxRtcPoll); rxRtcPoll=null; } panel.style.display='none'; return; }
  panel.style.display='flex';
  // Reopening keeps the same link while the phone is still with us: a connected phone keeps its camera, and a phone
  // that has not been answered yet keeps its queued offer. Only a failed or closed connection gets a new link,
  // because the phone page never learns a new one on its own.
  const cs=rxPC?rxPC.connectionState:'';
  const reuse=!!rxSess && (!rxPC || cs==='new' || cs==='connecting' || cs==='connected' || cs==='disconnected');
  if(!reuse){
    if(rxPC){ try{ rxPC.close(); }catch(e){} rxPC=null; }
    rxSess=Math.random().toString(36).slice(2)+Date.now().toString(36);
  }
  if(!_lanUrl){ try{ var _lj=(await fetch('/lan').then(function(r){return r.json();}))||{}; _lanUrl=_lj.url||''; window._lanTok=_lj.t||''; }catch(e){} }
  let base=_lanUrl||(location.origin+'/'); if(base.slice(-1)!=='/') base+='/';
  const camUrl=base+'cam?s='+encodeURIComponent(rxSess)+'&t='+encodeURIComponent(window._lanTok||'');
  const qr=document.getElementById('rxQr'); if(qr) qr.src='/qr.svg?d='+encodeURIComponent(camUrl);
  const link=document.getElementById('rxLanLink'); if(link){ link.textContent=camUrl; link.href=camUrl; }
  rxRtcState(rxPC?rxPC.connectionState:'wait');
  rxRtcStart();
}
function rxRtcStop(){
  rxRtcOn=false;
  if(rxRtcPoll){ clearTimeout(rxRtcPoll); rxRtcPoll=null; }
  if(rxPC){ try{ rxPC.close(); }catch(e){} rxPC=null; }
  rxSess='';   // a stopped session is dead: the phone page would never answer it, so the next open makes a new link
}
function rxRtcSend(data){
  return fetch('/rtc/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({s:rxSess,from:'app',data:data})}).catch(function(){});
}
let _rtcGen=0;
function rxRtcStart(){
  // each start gets a generation number, so a poll still in flight from before a close cannot start a second loop
  rxRtcOn=true; const gen=++_rtcGen;
  (function poll(){
    if(!rxRtcOn||gen!==_rtcGen) return;
    fetch('/rtc/poll?s='+encodeURIComponent(rxSess)+'&role=app').then(function(r){return r.json();}).then(async function(r){
      const ms=(r&&r.msgs)||[];
      for(let i=0;i<ms.length;i++){ await rxRtcHandle(ms[i]); }
    }).catch(function(){}).finally(function(){ if(rxRtcOn&&gen===_rtcGen) rxRtcPoll=setTimeout(poll, 1000); });
  })();
}
async function rxRtcHandle(m){
  if(!m) return;
  if(m.type==='caps'){ _rxPhoneCaps = m; if(_rxRemote) rxCamCaps(rxStream); return; }
  if(m.type==='offer'){
    if(rxPC){ try{ rxPC.close(); }catch(e){} }
    rxPC=new RTCPeerConnection({iceServers:[{urls:'stun:stun.l.google.com:19302'}]});
    rxPC.onicecandidate=function(e){ if(e.candidate) rxRtcSend({type:'ice',candidate:e.candidate}); };
    rxPC.ontrack=function(e){ rxOnRemoteStream((e.streams&&e.streams[0])||null); };
    rxPC.onconnectionstatechange=function(){ rxRtcState(rxPC.connectionState); };
    try{
      await rxPC.setRemoteDescription(new RTCSessionDescription(m));
      const ans=await rxPC.createAnswer(); await rxPC.setLocalDescription(ans);
      rxRtcSend({type:ans.type, sdp:ans.sdp});
    }catch(e){}
  } else if(m.type==='ice' && m.candidate){
    try{ if(rxPC) await rxPC.addIceCandidate(m.candidate); }catch(e){}
  }
}
function rxRtcState(cs){
  const t=document.getElementById('rxPhoneStatus'); if(!t) return;
  if(cs==='connected'){ t.textContent='Phone connected — it is now your camera '; t.className='rxphone-status on'; }
  else if(cs==='failed'){ t.textContent='Could not connect — are both on the same wifi?'; t.className='rxphone-status bad'; }
  else if(cs==='disconnected'){ t.textContent='Phone disconnected.'; t.className='rxphone-status bad'; }
  else { t.textContent='Waiting for a phone to connect…'; t.className='rxphone-status'; }
}
function rxOnRemoteStream(stream){
  if(!stream) return;
  try{ if(rxStream && rxStream!==stream){ rxStream.getTracks().forEach(function(t){ t.stop(); }); } }catch(e){}
  rxStream=stream;
  const v=document.getElementById('rxCam'); if(v){ v.srcObject=rxStream; v.muted=true; v.play().catch(function(){}); } rxGhostAttach();
  const cw=document.getElementById('rxCamWrap'); if(cw){ cw.classList.add('cam-on'); cw.classList.remove('clip-play'); }
  const sb=document.getElementById('rxStartBtn'); if(sb) sb.style.display='none';
  const msg=document.getElementById('rxCamMsg'); if(msg) msg.textContent='';
  _gZoomAuto=false; _rxRemote=true;
  rxBuildMeter(); rxCamCaps(rxStream);
}
// ---- Camera and microphone choice, saved in this browser. A saved choice is tried first; if it is not connected,
// the default is used and the camera says so. A blocked or missing microphone keeps the camera and records without sound.
function rxStoreGet(k){ try{ return window.localStorage.getItem(k)||''; }catch(e){ return ''; } }
function rxStoreSet(k, v){ try{ if(v) window.localStorage.setItem(k, v); else window.localStorage.removeItem(k); }catch(e){} }
function rxMicPref(){ return rxStoreGet('abraRecMic'); }
let _rxCamId='';
async function rxOpenStream(deviceId){
  const savedMic=rxMicPref();
  const pick=deviceId||'';                            // the preset is the front camera: no saved camera is used to start
  const camWant=pick?{deviceId:{exact:pick}}:{facingMode:'user'};
  const micWant=savedMic?{deviceId:{exact:savedMic}}:true;
  const wide=rxGsWant();   // ideal full HD while Greenscreen is on; {} otherwise, so every request is unchanged when it is off
  const missing=function(e){ return !!e && (e.name==='OverconstrainedError' || e.name==='NotFoundError'); };
  let e1=null;
  // 1. the choices as saved
  try{ return {stream:await navigator.mediaDevices.getUserMedia({video:Object.assign({}, camWant, wide), audio:micWant}), note:'', camId:pick}; }
  catch(err){ e1=err; }
  // 2. a saved microphone that is not connected: keep the saved camera, use the default microphone, say so
  if(savedMic && missing(e1)){
    try{ return {stream:await navigator.mediaDevices.getUserMedia({video:Object.assign({}, camWant, wide), audio:true}), note:'Your saved microphone is not connected, so the default microphone is in use.', camId:pick}; }
    catch(err){ e1=err; }
  }
  // 3. the saved camera is not connected (never for a camera picked from the list): the default camera, and say so
  if(pick && !deviceId && missing(e1)){
    try{ return {stream:await navigator.mediaDevices.getUserMedia({video:Object.assign({facingMode:'user'}, wide), audio:micWant}), note:'Your saved camera is not connected, so the default camera is in use.', camId:''}; }
    catch(err){ e1=err; }
  }
  // 4. the sound is blocked or missing: keep the camera and record without sound. A camera that is busy or
  //    blocked is reported as such and is never swapped for another camera.
  try{ return {stream:await navigator.mediaDevices.getUserMedia({video:Object.assign({}, camWant, wide), audio:false}), note:'Microphone blocked: the camera works, but recordings will have no sound.', camId:pick}; }
  catch(e4){
    if(pick && !deviceId && missing(e4)){
      return {stream:await navigator.mediaDevices.getUserMedia({video:Object.assign({facingMode:'user'}, wide), audio:false}), note:'Your saved camera is not connected, so the default camera is in use. Recordings will have no sound.', camId:''};
    }
    throw e4;
  }
}
let _rxStarting=null;
// one start at a time: a double tap or a voice record during a start must not open a second camera
const RX_CAMERA_ON=false;   // the live camera is switched off for the hackathon submission; set true to bring it back
function rxStartCam(deviceId){
  if(!RX_CAMERA_ON){ rxFeedback('The camera is not in this version of Abra.', 3000); return Promise.resolve(false); }
  if(rxStream) return Promise.resolve(true);
  if(!_rxStarting){ _rxStarting=rxStartCamNow(deviceId).finally(function(){ _rxStarting=null; }); }
  return _rxStarting;
}
async function rxStartCamNow(deviceId){
  if(rxStream) return true;
  const msg=document.getElementById('rxCamMsg');
  try{
    const got=await rxOpenStream(deviceId);   // native orientation per device (desktop=wide, phone=tall) so the preview always FILLS, no letterbox
    rxStream=got.stream; _rxCamId=got.camId||'';
    const v=document.getElementById('rxCam'); if(v){ v.srcObject=rxStream; v.muted=true; v.play().catch(function(){}); } rxGhostAttach();
    const cw=document.getElementById('rxCamWrap'); if(cw){ cw.classList.add('cam-on'); cw.classList.remove('clip-play'); }
    const sb=document.getElementById('rxStartBtn'); if(sb) sb.style.display='none';
    _rxRemote=false; rxBuildMeter(); rxCamCaps(rxStream);
    if(got.note) rxFeedback(got.note, 6000);
    if(msg) msg.textContent='';
    rxWideSync();
    rxListCams(); rxListMics();
    return true;
  }catch(e){
    const why = e && e.name === 'NotFoundError' ? 'No camera found on this computer.'
      : e && e.name === 'NotReadableError' ? 'The camera is in use by another program (Teams, Zoom or another browser tab). Close it and try again.'
      : e && e.name === 'OverconstrainedError' ? 'This computer has no camera that matches the request.'
      : 'Camera blocked: allow camera access in your browser (on a computer, open Abra at http://localhost:8765).';
    if(msg) msg.textContent=why; return false; }
}
async function rxListCams(){
  const sel=document.getElementById('rxCamSel'); if(!sel) return;
  try{
    const devs=await navigator.mediaDevices.enumerateDevices();
    const cams=devs.filter(function(d){ return d.kind==='videoinput'; });
    const row=document.getElementById('rxCamRow');
    if(!cams.length){ sel.innerHTML='<option>No camera found yet</option>'; if(row) row.style.display='flex'; return; }
    const cur=sel.value;
    sel.innerHTML=cams.map(function(c,i){ return '<option value="'+c.deviceId+'">'+esc(c.label||('Camera '+(i+1)))+'</option>'; }).join('');
    const front=cams.find(function(c){ return /facetime|front|built-?in|integrated/i.test(c.label||''); });
    const want=_rxCamId||(front && front.deviceId)||cur;   // preset: the front camera
    if(want && cams.some(function(c){ return c.deviceId===want; })) sel.value=want;
    if(row) row.style.display='flex';
  }catch(e){}
}
async function rxCamChange(){
  const sel=document.getElementById('rxCamSel'); const id=sel?sel.value:'';
  const prev=_rxCamId;
  if(rxTakeBusy()){ if(sel) sel.value=prev; rxFeedback('Finish this take first, then change the camera.', 3500); return; }   // a paused take must not end when the camera changes
  rxStopCam(); rxStream=null;
  const ok=await rxStartCam(id);
  if(ok){ _rxCamId=id; return; }
  // the new camera could not be used: bring the previous one back and keep the saved choice as it was
  if(sel) sel.value=prev;
  rxFeedback('That camera could not be used. Keeping the one before it.', 4000);
  if(prev) await rxStartCam(prev); else await rxStartCam('');
}
async function rxListMics(){
  const sel=document.getElementById('rxMicSel'); if(!sel || !navigator.mediaDevices) return;
  try{
    const devs=await navigator.mediaDevices.enumerateDevices();
    const mics=devs.filter(function(d){ return d.kind==='audioinput' && d.deviceId && d.deviceId!=='default' && d.deviceId!=='communications'; });
    const saved=rxMicPref();
    let html='<option value="">System default</option>';
    mics.forEach(function(d,i){ html+='<option value="'+esc(d.deviceId)+'">'+esc(d.label||('Microphone '+(i+1)))+'</option>'; });
    if(saved && !mics.some(function(d){ return d.deviceId===saved; })) html+='<option value="'+esc(saved)+'" disabled>Saved microphone (not connected)</option>';
    sel.innerHTML=html; sel.value=saved;
  }catch(e){}
}
async function rxMicChange(){
  const sel=document.getElementById('rxMicSel'); if(!sel) return false;
  const id=sel.value||'';
  if(rxTakeBusy()){ sel.value=rxMicPref(); rxFeedback('Finish this take first, then change the microphone.', 3500); return false; }
  if(_rxRemote){ sel.value=rxMicPref(); rxFeedback("The phone's own microphone records the sound. Switch the camera back to this computer to choose one.", 5000); return false; }
  if(!rxStream){ rxStoreSet('abraRecMic', id); return true; }   // camera off: the next start uses it
  let fresh=null;
  try{ fresh=await navigator.mediaDevices.getUserMedia({audio: id?{deviceId:{exact:id}}:true}); }
  catch(e){ sel.value=rxMicPref(); rxFeedback('That microphone could not be used. Your choice is unchanged.', 4000); return false; }
  // the world may have changed while the permission was pending: a take started, the camera stopped, or the phone took over
  if(rxTakeBusy() || !rxStream || _rxRemote){
    fresh.getAudioTracks().forEach(function(t){ t.stop(); });
    sel.value=rxMicPref(); rxFeedback('The camera changed while the microphone was opening, so your microphone choice is unchanged.', 4000);
    return false;
  }
  rxStream.getAudioTracks().forEach(function(t){ rxStream.removeTrack(t); t.stop(); });
  fresh.getAudioTracks().forEach(function(t){ rxStream.addTrack(t); });
  rxStoreSet('abraRecMic', id);
  rxBuildMeter(); rxGhostAttach();
  return true;
}
// ---- Mirror and brightness. The preview mirrors you (camera and cut-out together, never the clip); the saved take is always raw (rxGsTick copies the raw pass before the preview flip).
let _rxMirror=true;   // in memory only: every page load starts mirrored, as the preview always was
let _rxBrightKey='', _rxBrightRange=null, _rxBrightVal=null;
function rxSetMirror(on){
  _rxMirror=!!on;
  const w=document.getElementById('rxCamWrap'); if(w) w.classList.toggle('mirror-off', !_rxMirror);
  const c=document.getElementById('rxMirrorChk'); if(c) c.checked=_rxMirror;
}
function rxBrightLabel(v){
  const el=document.getElementById('rxBrightV'); if(!el) return;
  if(v===null || v===undefined){ el.textContent='\u2014'; return; }
  el.textContent = (_rxBrightKey==='exposureCompensation') ? ((v>0?'+':'')+(Math.round(v*10)/10)) : String(Math.round(v*10)/10);
}
// reads what this camera can do; the brightness row appears only when it can, and says why when it cannot
// what this camera can do: the brightness row appears only when it can, and says why when it cannot.
// A phone reports its own control over the link (type caps), so its row works the same way.
let _rxPhoneCaps=null;
function rxCamCaps(stream){
  const row=document.getElementById('rxBrightRow'), sl=document.getElementById('rxBrightSl'), note=document.getElementById('rxCapNote');
  let tr=null, caps=null, st=null;
  try{ tr=(stream && stream.getVideoTracks)?stream.getVideoTracks()[0]:null; caps=(tr && tr.getCapabilities)?tr.getCapabilities():null; st=(tr && tr.getSettings)?tr.getSettings():null; }catch(e){}
  let key='', range=null, val=null, reported=false;
  if(caps && caps.exposureCompensation){ key='exposureCompensation'; range=caps.exposureCompensation; val=(st && st.exposureCompensation!==undefined)?st.exposureCompensation:null; }
  else if(caps && caps.brightness){ key='brightness'; range=caps.brightness; val=(st && st.brightness!==undefined)?st.brightness:null; }
  else if(stream && _rxRemote && _rxPhoneCaps){
    reported=true;
    if(_rxPhoneCaps.key){ key=_rxPhoneCaps.key; range={min:_rxPhoneCaps.min, max:_rxPhoneCaps.max, step:_rxPhoneCaps.step}; val=(_rxPhoneCaps.value!==undefined && _rxPhoneCaps.value!==null)?_rxPhoneCaps.value:null; }
  }
  _rxBrightKey=key; _rxBrightRange=key?{min:range.min, max:range.max, step:(range.step||1)}:null; _rxBrightVal=val;
  if(key && sl){
    sl.min=String(range.min); sl.max=String(range.max); sl.step=String(range.step||1);
    sl.value=String(val!==null?val:((range.min+range.max)/2)); rxBrightLabel(val);
  }
  if(row) row.style.display=key?'flex':'none';
  if(note){
    let t='';
    if(stream && !key){
      if(_rxRemote) t = reported ? 'This phone camera does not offer a brightness control.' : 'Waiting for the phone to report its brightness control.';
      else t = 'This camera does not offer a brightness control, so Abra cannot change it.';
    }
    note.textContent=t; note.style.display=t?'block':'none';
  }
}
async function rxSetBrightness(v){
  const sl=document.getElementById('rxBrightSl');
  const back=function(){ if(sl && _rxBrightVal!==null) sl.value=String(_rxBrightVal); rxBrightLabel(_rxBrightVal); };
  if(rxTakeBusy()){ back(); rxFeedback('Finish this take first, then change the brightness.', 3500); return false; }
  if(!_rxBrightKey || !_rxBrightRange){ back(); return false; }
  const r=_rxBrightRange, n=Math.min(r.max, Math.max(r.min, Number(v)));
  _rxBrightVal=n; if(sl) sl.value=String(n); rxBrightLabel(n);
  if(_rxRemote){ rxRtcSend({type:'ctl', value:n}); return true; }   // the phone applies it and reports back what it shows
  const tr=(rxStream && rxStream.getVideoTracks)?rxStream.getVideoTracks()[0]:null;
  if(!tr || !tr.applyConstraints){ back(); rxFeedback('The camera is not ready for brightness yet.', 3500); return false; }
  try{
    await tr.applyConstraints({advanced:[{[_rxBrightKey]: n}]});
    const st=tr.getSettings?tr.getSettings():null;   // read back what the camera actually did
    if(st && st[_rxBrightKey]!==undefined){ _rxBrightVal=st[_rxBrightKey]; if(sl) sl.value=String(_rxBrightVal); rxBrightLabel(_rxBrightVal); }
    return true;
  }catch(e){ back(); rxFeedback('This camera did not accept that brightness.', 3500); return false; }
}
try{ navigator.mediaDevices.addEventListener('devicechange', function(){ rxListCams(); rxListMics(); }); }catch(e){}
let _rxTarget=0;  // 0 = free (no limit); else seconds, auto-stops at the target
let _rxCaptions=true;
function rxSetCaptions(on){ _rxCaptions=!!on; }
function rxSetTarget(sec){
  _rxTarget=sec|0;
  var chips=document.querySelectorAll('.rxlenchip');
  for(var i=0;i<chips.length;i++){ var c=chips[i]; if(c.classList.contains('mic') || c.getAttribute('data-sec')===null) continue;
    c.classList.toggle('on', (parseInt(c.getAttribute('data-sec'),10)||0)===_rxTarget); }
  var hint=document.getElementById('rxLenHint');
  if(hint) hint.textContent = _rxTarget>0 ? ('Auto-stops at '+rxFmtDur(_rxTarget)+' — the timer counts you down.') : 'Record as long as you like; Sage takes clips up to 200 MB.';
}
function rxParseDur(txt){
  txt=(txt||'').toLowerCase();
  var units={one:1,two:2,three:3,four:4,five:5,six:6,seven:7,eight:8,nine:9,ten:10,eleven:11,twelve:12,thirteen:13,fourteen:14,fifteen:15,sixteen:16,seventeen:17,eighteen:18,nineteen:19,twenty:20,thirty:30,forty:40,fifty:50,sixty:60,seventy:70,eighty:80,ninety:90};
  var isMin=/minute|mins|(^|[^a-z])min([^a-z]|$)|(^|[^a-z])m([^a-z]|$)/.test(txt);
  var secs=0, m=txt.match(/([0-9]+)/);
  if(m){ secs=parseInt(m[1],10); }
  else { var words=txt.split(/[^a-z]+/), val=0, got=false;
    for(var i=0;i<words.length;i++){ if(units[words[i]]!=null){ val+=units[words[i]]; got=true; } }
    if(got) secs=val; }
  if(secs===0 && isMin) secs=1;
  if(isMin) secs=secs*60;
  if(/half/.test(txt)) secs+=30;
  if(secs<3) return 0;
  if(secs>600) secs=600;
  return Math.round(secs);
}
function rxSayTarget(){
  var SR=window.SpeechRecognition||window.webkitSpeechRecognition, hint=document.getElementById('rxLenHint');
  if(!SR){ if(hint) hint.textContent='Voice isn’t available in this browser — tap a time instead.'; return; }
  try{
    var r=new SR(); r.lang='en-US'; r.interimResults=false; r.maxAlternatives=1;
    if(hint) hint.textContent='Listening… say how long (e.g. “thirty seconds” or “a minute”).';
    r.onresult=function(e){ var txt=(e.results[0][0].transcript)||''; var sec=rxParseDur(txt);
      if(sec>0) rxSetTarget(sec);
      else if(hint) hint.textContent='Didn’t catch a length — heard “'+txt+'”. Try “30 seconds”.'; };
    r.onerror=function(ev){ if(hint) hint.textContent='Couldn’t hear you — tap a time instead.'; };
    r.start();
  }catch(e){ if(hint) hint.textContent='Voice unavailable — tap a time instead.'; }
}
// ---- Talk time, estimated. Speech-band energy (300 to 3400 Hz) against the room's own quiet level, with a short
// hold so a breath does not end a sentence. It is a guess from the sound, not a measure of who is talking, so
// every place that shows it says "estimated". The thresholds were set by reasoning; they are not calibrated yet.
let rxMeterOn=false, _rxMeterGen=0, _rxRemote=false, _rxLastTalk=null;
let _mtr={state:'quiet', noise:-90, onsetMs:0, holdMs:0};
let _mtrBeat=0;
function rxRecording(){ return !!(rxRec && rxRec.state!=='inactive'); }
function rxMtrReset(){ _mtr.state='quiet'; _mtr.onsetMs=0; _mtr.holdMs=0; }
function rxMtrStep(level, dtc){
  const enter=_mtr.noise+12, exit=_mtr.noise+8;
  if(_mtr.state==='quiet'){
    if(level>=enter){ _mtr.state='onset'; _mtr.onsetMs=0; }
    else { _mtr.noise += (level-_mtr.noise)*(level<_mtr.noise ? 0.2 : 0.01); }   // the quiet level follows the room: down fast, up slowly
  } else if(_mtr.state==='onset'){
    if(level>=enter){ _mtr.onsetMs+=dtc; if(_mtr.onsetMs>=150){ _mtr.state='voiced'; _mtr.holdMs=0; } }
    else { _mtr.state='quiet'; }   // too short to count: a click, a cough, a key
  } else {
    if(level>=exit){ _mtr.holdMs=0; }
    else { _mtr.holdMs+=dtc; if(_mtr.holdMs>=300){ _mtr.state='quiet'; } }
  }
}
function rxStopMeter(){
  rxMeterOn=false; _rxMeterGen++;
  { const _d=document.getElementById('rxMicDot'); if(_d) _d.classList.remove('on'); }
  if(rxRAF){ cancelAnimationFrame(rxRAF); rxRAF=null; }
  if(rxActx){ try{ rxActx.close(); }catch(e){} rxActx=null; }
  rxAn=null;
  rxTalkShow();
}
// the one owner of the microphone analyser: every new audio track goes through here, so only one meter runs
function rxBuildMeter(){
  rxStopMeter();
  try{
    const at=(rxStream && rxStream.getAudioTracks)?rxStream.getAudioTracks():[];
    if(!at || !at.length) return false;
    rxActx=new (window.AudioContext||window.webkitAudioContext)();
    const sn=rxActx.createMediaStreamSource(rxStream);
    rxAn=rxActx.createAnalyser(); rxAn.fftSize=2048; sn.connect(rxAn);
    rxMeterOn=true; rxMeter(); return true;
  }catch(e){ rxStopMeter(); return false; }
}
function rxMeter(){
  if(!rxAn) return;
  const my=++_rxMeterGen;
  const bins=new Float32Array(rxAn.frequencyBinCount), sr=(rxActx&&rxActx.sampleRate)||48000;
  const lo=Math.max(1, Math.round(300*rxAn.fftSize/sr)), hi=Math.min(bins.length-1, Math.round(3400*rxAn.fftSize/sr));
  (function tick(){
    if(my!==_rxMeterGen) return;
    const now=performance.now(), dtc=Math.max(0, Math.min(250, now-rxLastMs)); rxLastMs=now; _mtrBeat=now;   // the shared take clock; the timer covers hidden time
    if(rxActx && rxActx.state==='running'){
      rxAn.getFloatFrequencyData(bins);
      let p=0; for(let i=lo;i<=hi;i++){ p+=Math.pow(10, Math.max(-110, bins[i])/10); }
      const level=10*Math.log10(p/(hi-lo+1)+1e-12);
      rxMtrStep(level, dtc);
      if(_mtr.state==='voiced' && level>_mtr.noise){ _mtr.noise += (level-_mtr.noise)*0.002*(dtc/16); }   // a steady rise in the room is absorbed, not counted as speech
    }
    if(rxRecOn){
      rxRecMs+=dtc; if(_mtr.state==='voiced') rxTalkMs+=dtc;
      if(_rxTarget>0 && rxRecMs>=_rxTarget*1000 && rxRec && rxRec.state!=='inactive'){ rxFinish(); }
    }
    const dot=document.getElementById('rxMicDot'); if(dot) dot.classList.toggle('on', _mtr.state==='voiced');
    rxTalkShow();
    rxRAF=requestAnimationFrame(tick);
  })();
}
// the Create panel's figure: "not measured" is said plainly, never shown as 0%
function rxTalkShow(){
  const pct=document.getElementById('rxTalkPct'), bar=document.getElementById('rxTalkBar'), goal=document.getElementById('rxTalkGoal');
  let txt, ratio=0;
  if(!rxMeterOn) txt='not measured';
  else if(!rxRecording()) txt=(_rxLastTalk!==null)?(_rxLastTalk+'% (last take)'):'starts with your take';
  else if(rxActx && rxActx.state!=='running') txt='not measured (the browser paused the sound)';
  else if(rxRecMs<5000) txt='listening…';
  else { ratio=Math.round(rxTalkMs/rxRecMs*100); txt=ratio+'%'; }
  if(pct) pct.textContent=txt;
  if(bar) bar.style.width=Math.min(100, Math.round(ratio/0.33))+'%';
  if(goal) goal.classList.toggle('hit', ratio>=33);
}
async function rxRecBegin(){
  if(!RX_CAMERA_ON){ rxFeedback('The camera is not in this version of Abra.', 3000); return; }
  if(!rxStream){ const ok=await rxStartCam(); if(!ok) return; }
  if(rxRec && rxRec.state!=='inactive') return;
  if(typeof rxCutting!=='undefined' && rxCutting) return;
  rxAttachLive();
  rxChunks=[]; rxRecMs=0; rxTalkMs=0; rxLastMs=performance.now(); rxMtrReset();
  if(rxActx && rxActx.state==='suspended'){ try{ rxActx.resume(); }catch(e){} }   // a take started by voice may find the sound paused
  rxCut=false; { var _cb=document.getElementById('rxCutBtn'); if(_cb) _cb.style.display='none'; }
  let opts={}; try{ if(MediaRecorder.isTypeSupported('video/webm;codecs=vp9,opus')) opts={mimeType:'video/webm;codecs=vp9,opus'}; }catch(e){}
  if(_bg.on) rxBgAudio();
  const recStream=rxStartCanvas();
  try{ rxRec=new MediaRecorder(recStream, opts); }catch(e){ try{ rxRec=new MediaRecorder(recStream); }catch(e2){ rxStopCanvas(); return; } }
  rxRec.ondataavailable=function(e){ if(e.data&&e.data.size) rxChunks.push(e.data); };
  rxRec.start(); rxRecOn=true; rxTimer(true);
  { var _st2=document.getElementById('rxStudio'); if(_st2) _st2.classList.add('rec-focus'); }
  rxWideStop(); rxFlyRec();
  const btn=document.getElementById('rxRecBtn'); if(btn){ btn.classList.add('on'); btn.setAttribute('aria-label','Pause'); }
  const dl=document.getElementById('rxDownload'); if(dl) dl.style.display='none';
  const h=document.getElementById('rxHandBtn'); if(h) h.style.display='none';
}
function rxRecPause(){
  if(!rxRec || rxRec.state!=='recording') return;
  rxRec.pause(); rxRecOn=false;
  const btn=document.getElementById('rxRecBtn'); if(btn){ btn.classList.remove('on'); btn.setAttribute('aria-label','Resume'); }
}
function rxRecResume(){
  if(!rxRec || rxRec.state!=='paused') return;
  rxRec.resume(); rxRecOn=true; rxLastMs=performance.now();
  const btn=document.getElementById('rxRecBtn'); if(btn){ btn.classList.add('on'); btn.setAttribute('aria-label','Pause'); }
}
async function rxToggleRec(){
  if(!rxRec || rxRec.state==='inactive'){ await rxRecBegin(); }
  else if(rxRec.state==='recording'){ rxRecPause(); }
  else if(rxRec.state==='paused'){ rxResumeAfterBreath(null); }
}
/* ===== Hands-free: off-frame hand gestures drive the recorder (Google MediaPipe GestureRecognizer, on-device) ===== */
const RX_MP_ESM='https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.2.0/vision_bundle.mjs';
const RX_MP_WASM='https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.2.0/wasm';
const RX_MP_MODEL='https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task';
const G_MAP={Pointing_Up:'start', Victory:'pause', Closed_Fist:'stop'};
const G_HOLD=4, G_MINSCORE=0.5, G_INTERVAL=80;
let _grec=null, _grecFailed=false, _gOn=false, _gLoopRAF=null, _gLastRun=0, _gLastTs=0;
let _gRaw='', _gStableN=0, _gStable='', _gLastFired='', _gCountT=null, _gMsgUntil=0, _gZoomAuto=false;
let rxCut=false, rxCutting=false, rxCutUrl='', rxClips=[], _rxUndo=null, _rxUndoT=null;
/* swipe gestures (post-recording): flat hand wipe ACROSS = smooth-cut; push TOWARD camera = erase last */
const SW_BUFMS=650, SW_WINMS=500, SW_MINMS=160, SW_DX_MIN=0.33, SW_DY_MAX=0.18,
      SW_HV_RATIO=2.5, SW_STRAIGHT=0.75, SW_DIRFRAC=0.80, SW_STEP_EPS=0.005,
      SW_VPEAK=0.040, SW_OPEN_SCORE=0.40, SW_OPEN_FRAC=0.60, SW_ARM_V=0.020,
      SW_ARM_FR=3, SW_LEAVE_FR=3, SW_GUARD_MS=700, SW_COOL_MS=800,
      SW_SCALE_MIN=1.22, SW_TOWARD_TXY=0.26, SW_TOWARDMS=800;
let _swBuf=[], _swState='DISARMED', _swSettleN=0, _swGapN=0, _swGuardUntil=0, _swCoolUntil=0;
function swPalmX(lm){ return (lm[0].x+lm[5].x+lm[9].x+lm[13].x+lm[17].x)/5; }
function swPalmY(lm){ return (lm[0].y+lm[5].y+lm[9].y+lm[13].y+lm[17].y)/5; }
function swPalmSc(lm){ var mnx=1,mny=1,mxx=0,mxy=0; for(var i=0;i<lm.length;i++){ var p=lm[i]; if(p.x<mnx)mnx=p.x; if(p.x>mxx)mxx=p.x; if(p.y<mny)mny=p.y; if(p.y>mxy)mxy=p.y; } return Math.hypot(mxx-mnx, mxy-mny); }
function swEligible(){ return rxClips.length>0 && !rxCutting && (!rxRec || rxRec.state==='inactive'); }
function rxGhostAttach(){ try{ var g=document.getElementById('rxGhost'); if(g && rxStream){ g.srcObject=rxStream; g.play().catch(function(){}); }
  _rxPlaying=false; rxUpdateCropBox(); }catch(e){} }
function _gTimeout(pr, ms){ return Promise.race([pr, new Promise(function(_,rej){ setTimeout(function(){ rej(new Error('timeout')); }, ms); })]); }
async function rxLoadGesture(){
  if(_grec) return _grec; if(_grecFailed) return null;
  try{
    const mod=await _gTimeout(import(RX_MP_ESM), 9000);
    const vision=await _gTimeout(mod.FilesetResolver.forVisionTasks(RX_MP_WASM), 9000);
    const mk=function(delegate){ return mod.GestureRecognizer.createFromOptions(vision,{baseOptions:{modelAssetPath:RX_MP_MODEL, delegate:delegate}, runningMode:'VIDEO', numHands:2}); };
    try{ _grec=await _gTimeout(mk('GPU'), 15000); }catch(e){ _grec=await _gTimeout(mk('CPU'), 15000); }
    return _grec;
  }catch(e){ _grecFailed=true; return null; }
}
async function rxToggleHandsFree(){
  if(_gOn){ rxHandsFreeOff(); return; }
  if(_filmPick==='reaction'){ rxFeedback('Greenscreen records standard portrait, so it has no hands-free option.', 3500); return; }
  const panel=document.getElementById('rxHfPanel'), btn=document.getElementById('rxHfBtn'), st=document.getElementById('rxHfStatus');
  _gOn=true; _grecFailed=false; _gZoomAuto=false; if(panel) panel.style.display='flex'; if(btn) btn.classList.add('on');
  { const _hs=document.getElementById('rxHfStatus'); if(_hs) _hs.style.display='block'; }   // the status line shows on the camera while hands-free is on
  if(!rxStream){ const ok=await rxStartCam(); if(!ok){ _gOn=false; if(btn) btn.classList.remove('on');
    rxFeedback('Camera blocked. Allow camera access, then turn hands-free on again.', 4000); if(st){ st.textContent='Camera blocked: allow camera access in your browser, then turn Hands-free on again.'; st.className='rxhf-status bad'; } return; } }
  if(st && !_grec){ st.textContent='Warming up the hand model\u2026'; st.className='rxhf-status'; }
  const rec=await rxLoadGesture();
  if(!_gOn) return;
  if(!rec){ if(st){ st.textContent='Couldn\u2019t load the hand model (needs internet) \u2014 manual buttons still work.'; st.className='rxhf-status bad'; } return; }
  if(st){ st.textContent='Hands-free on \u2014 hold a gesture off to the side.'; st.className='rxhf-status on'; }
  _gLastRun=0; _gRaw=''; _gStableN=0; _gStable=''; _gLastFired='';
  if(!_gLoopRAF) rxGestureLoop();
}
function rxHandsFreeOff(){
  _gOn=false;
  if(_gLoopRAF){ cancelAnimationFrame(_gLoopRAF); _gLoopRAF=null; }
  if(_gCountT){ clearInterval(_gCountT); _gCountT=null; }
  const ov=document.getElementById('rxCountdown'); if(ov) ov.style.display='none';
  if(!rxTakeBusy() && !_rxRoomUser) rxSetZoom(100); _gZoomAuto=false;
  const _cam=document.getElementById('rxCam'); if(_cam) _cam.style.transform='';
  const panel=document.getElementById('rxHfPanel'), btn=document.getElementById('rxHfBtn');
  if(panel) panel.style.display='none'; if(btn) btn.classList.remove('on');
  { const _hs=document.getElementById('rxHfStatus'); if(_hs) _hs.style.display='none'; }
}
function rxGestureLoop(){
  if(!_gOn){ _gLoopRAF=null; return; }
  const v=document.getElementById('rxGhost'), now=performance.now();
  if(v && v.paused) v.play().catch(function(){});
  const studio=document.getElementById('rxStudio');
  const hidden=document.hidden || (studio && studio.offsetParent===null);
  if(_grec && v && v.srcObject && v.videoWidth && v.readyState>=2 && !hidden && (now-_gLastRun)>=G_INTERVAL){
    _gLastRun=now;
    if(!_gZoomAuto && rxAutoZoom()) _gZoomAuto=true;
    let ts=now; if(ts<=_gLastTs) ts=_gLastTs+1; _gLastTs=ts;
    try{
      const res=_grec.recognizeForVideo(v, ts);
      // with two hands in view, the gesture comes from the hand that is making one
      const hands=(res&&res.landmarks)||[], names=(res&&res.gestures)||[];
      let pi=-1, name='', score=0;
      for(let i=0;i<names.length;i++){
        const c=names[i]&&names[i][0]; if(!c) continue;
        if(pi<0 || (G_MAP[c.categoryName] && (!G_MAP[names[pi][0].categoryName] || c.score>names[pi][0].score))) pi=i;
      }
      if(pi>=0){ name=names[pi][0].categoryName||''; score=names[pi][0].score||0; }
      rxGestureTick(name, score, (pi>=0 && hands[pi])||null);
      rxTwoHandsTick(hands);
      rxWaveTick(hands, names);
    }catch(e){}
  }
  _gLoopRAF=requestAnimationFrame(rxGestureLoop);
}
// the bottom band of the frame: a hand held here is counted even inside the recorded frame (0 = top, 1 = bottom)
const RX_BOTTOM_HOLD=0.72;
// only count a gesture when the hand is OUT in the side gutter (not inside the recorded frame)
function rxHandGutter(lm){
  if(!lm || !lm[0]) return true;
  if(lm[0].y >= RX_BOTTOM_HOLD) return true;   // a hand held low, at the bottom of the screen, counts too
  const v=document.getElementById('rxGhost'); if(!v || !v.videoWidth) return true;
  const vw=v.videoWidth, vh=v.videoHeight, va=vw/vh;
  const dims=RX_DIMS[_rxFormat]||RX_DIMS.portrait, ta=dims[0]/dims[1];
  let swN=(va>ta)?((vh*ta)/vw):1;
  const z=_rxZoom||1; if(z>1) swN/=z;
  const sxN=(1-swN)/2, x=lm[0].x;     // centered crop -> symmetric, mirror doesn't matter
  return (x < sxN) || (x > sxN+swN);
}
// zoom in just enough that the hand-gutter is usable on ANY camera: a wide webcam already has big
// side margins (stays ~1x); a tall phone/portrait camera has none, so we crop in (up to 1.8x).
// returns true once it has set the room, so the caller stops asking; a room the creator set by hand, or a change during a take, is left alone
function rxAutoZoom(){
  const v=document.getElementById('rxGhost'); if(!v || !v.videoWidth) return false;
  if(_rxRoomUser || rxTakeBusy()) return false;
  const va=v.videoWidth/v.videoHeight;
  const dims=RX_DIMS[_rxFormat]||RX_DIMS.portrait, ta=dims[0]/dims[1];
  const natSw=(va>ta)?(ta/va):1;                 // crop width fraction before zoom
  let z=natSw/0.6;                                                // only zoom when the camera lacks a natural side gutter (phones); a wide webcam stays 1.0x
  z=Math.max(1, Math.min(1.8, z));
  rxSetZoom(Math.round(z*100));
  return true;
}
function rxHfLive(txt, cls){
  const st=document.getElementById('rxHfStatus'); if(!st) return;
  if(st._live===txt) return; st._live=txt;
  st.textContent=txt; st.className='rxhf-status'+(cls?(' '+cls):'');
}
// four fingers out (index, middle, ring, pinky) with the thumb folded: read from the hand landmarks, not the model's label
function rxFourFingers(lm){
  if(!lm || lm.length<21) return false;
  const d=function(a,b){ return Math.hypot(a.x-b.x, a.y-b.y); };
  const w=lm[0];
  const ext=function(tip,pip){ return d(tip,w) > d(pip,w)*1.15; };
  const thumbOut = d(lm[4],w) > d(lm[3],w)*1.1;
  return ext(lm[8],lm[6]) && ext(lm[12],lm[10]) && ext(lm[16],lm[14]) && ext(lm[20],lm[18]) && !thumbOut;
}
function rxGestureTick(name, score, lm){
  let act=(score>=G_MINSCORE)?(G_MAP[name]||''):'';
  if(!act && (score>=G_MINSCORE) && rxFourFingers(lm)) act='erase';   // four fingers out = delete the last take
  let warn=false;
  if(act && !rxHandGutter(lm)){ warn=true; act=''; if(rxNoRoomGutter()) rxRoomFloorNote(); }   // gesture seen but in-frame -> nudge, don't fire; with no room at all, say why
  rxDrawMonitor(lm, act, warn);
  swTick(name, score, lm, performance.now());
  if(performance.now()>_gMsgUntil){
    if(warn) rxHfLive('Seen — move your hand further out to the side ↔','bad');
    else if(act) rxHfLive(({start:'Start',pause:'Pause',stop:'Stop',erase:'Delete last take'}[act])+' — hold it','on');
    else if(lm) rxHfLive('Hand seen — show a signal out to the side');
    else rxHfLive('Hands-free on — hold a gesture off to the side');
  }
  if(act===_gRaw){ _gStableN++; } else { _gRaw=act; _gStableN=1; }
  if(_gStableN===G_HOLD && _gStable!==act){
    _gStable=act;
    if(act){ if(act!==_gLastFired){ rxFireGesture(act); _gLastFired=act; } }
    else { _gLastFired=''; }
  }
}
// the X at the top right: finish any take that is running (it is kept in the strip), then start a fresh recording
function rxNewTake(){
  var busy = rxRec && (rxRec.state==='recording' || rxRec.state==='paused');
  if(busy){ rxFinish(); setTimeout(function(){ rxCountdownThen(rxRecBegin); }, 500); }
  else { rxCountdownThen(rxRecBegin); }
}
// Reaction records standard portrait: no hands-free button, and hands-free is turned off if it was on
function rxHfAllowed(on){
  const b=document.getElementById('rxHfBtn'); if(b) b.style.display = on ? '' : 'none';
  if(!on && _gOn) rxHandsFreeOff();
}
// the X: end the take (it stays in the strip), cancel any countdown, and turn hands-free off so no gesture starts another
// the X at the top right: a running take is kept, then the camera is switched off and started again fresh, ready to film more
function rxResetCamera(){
  rxCancelCountdown();
  rxStopCam();
  rxFeedback('Camera reset. Starting it again.', 2500);
  rxStartCam(_rxCamId||'');
}
function rxEndTake(){
  rxCancelCountdown();
  if(_gOn) rxHandsFreeOff();
  if(rxRec && (rxRec.state==='recording' || rxRec.state==='paused')){ rxFinish(); rxFeedback('Take ended. Press the red button when you want another.', 3000); }
  else { rxFeedback('Nothing is recording. Press the red button to start.', 2500); }
}
function rxFireGesture(act){
  const st=document.getElementById('rxHfStatus'); _gMsgUntil=performance.now()+1300; if(st) st._live=null;
  if(act==='start'){
    if(!rxRec || rxRec.state==='inactive'){ if(st){ st.textContent='Starting\u2026'; st.className='rxhf-status on'; } rxCountdownThen(rxRecBegin); }
    else if(rxRec.state==='paused'){ rxResumeAfterBreath(st); }
  } else if(act==='pause'){
    if(rxRec && rxRec.state==='recording'){ rxRecPause(); if(st) st.textContent=' Paused'; }
  } else if(act==='stop'){
    rxCancelCountdown();
    if(rxRec && (rxRec.state==='recording'||rxRec.state==='paused')){ rxFinish();
      _swGuardUntil=performance.now()+SW_GUARD_MS; _swState='DISARMED'; _swBuf.length=0; _swSettleN=0;
      if(st) st.textContent='✊ Stopped — wipe a flat hand across to trim, or toward you to erase'; }
  } else if(act==='erase'){
    if(!rxRec || rxRec.state==='inactive'){
      if(rxClips.length){ rxClipErase(rxClips.length-1); if(st){ st.textContent='Deleted the last take (undo is on screen)'; st.className='rxhf-status on'; } }
      else if(st){ st.textContent='No take to delete yet'; st.className='rxhf-status'; }
    }
  }
}
let _rxStartSec=3;
function rxSetStartCount(v){ _rxStartSec=parseInt(v,10)||3; }
function rxCountdownThen(fn, seconds){
  rxAttachLive();
  if(_gCountT) return;
  const ov=document.getElementById('rxCountdown'); let n=seconds||(fn===rxRecBegin?_rxStartSec:3);
  if(ov){ ov.textContent=n; ov.style.display='flex'; }
  _gCountT=setInterval(function(){
    n--;
    if(n<=0){ clearInterval(_gCountT); _gCountT=null; if(ov) ov.style.display='none'; try{ fn(); }catch(e){} }
    else if(ov){ ov.textContent=n; }
  }, 1000);
}

function rxCancelCountdown(){
  if(_gCountT){ clearInterval(_gCountT); _gCountT=null; }
  const ov=document.getElementById('rxCountdown'); if(ov) ov.style.display='none';
}
// a short message on the camera (for example "Resuming in 3 seconds")
let _gMsgT=null;
function rxFeedback(text, ms){
  const el=document.getElementById('rxGMsg'); if(!el) return;
  el.textContent=text; el.style.display='block';
  clearTimeout(_gMsgT); _gMsgT=setTimeout(function(){ el.style.display='none'; }, ms||2000);
}
// every resume after a pause waits 3 seconds, so the creator can catch a breath first
function rxResumeAfterBreath(st){
  if(_gCountT) return;
  rxFeedback('Resuming in 3 seconds. Take a breath.', 3200);
  rxCountdownThen(rxRecResume, 3);
  if(st){ st.textContent='Resuming in 3\u2026'; st.className='rxhf-status on'; }
}
// both hands held up, out in the side gutter, while idle: a 10-second countdown instead of 3
let _twoUp=0, _twoLatched=false;
function rxTwoHandsTick(hands){
  const idle = !rxRec || rxRec.state==='inactive';
  const up = hands.length>=2 && hands[0] && hands[1] && hands[0][0].y<0.42 && hands[1][0].y<0.42
    && rxHandGutter(hands[0]) && rxHandGutter(hands[1]);
  if(!idle || _gCountT || rxCutting || !up){ _twoUp=0; _twoLatched=false; return; }
  _twoUp++;
  if(_twoUp>=G_HOLD*2 && !_twoLatched){            // about half a second with both hands up
    _twoLatched=true;
    rxFeedback('Both hands up: starting in 10 seconds', 2500);
    rxCountdownThen(rxRecBegin, 10);
  }
}
// a wave while paused deletes the previous take. Turns of the open palm are counted over 1.6 seconds.
const WAVE_WINDOW=1600, WAVE_SWING=0.07;
let _wv={buf:[], cool:0};
function rxWaveTurns(buf){
  let turns=0, hi=buf[0].x, lo=buf[0].x, dir=0;      // dir: +1 last stroke went right, -1 went left
  for(let i=1;i<buf.length;i++){
    const x=buf[i].x;
    if(dir>=0 && x<hi-WAVE_SWING){ if(dir===1) turns++; dir=-1; lo=x; hi=x; }
    else if(dir<=0 && x>lo+WAVE_SWING){ if(dir===-1) turns++; dir=1; hi=x; lo=x; }
    else { if(dir>=0 && x>hi) hi=x; if(dir<=0 && x<lo) lo=x; }
  }
  return turns;
}
function rxWaveTick(hands, names){
  const now=performance.now();
  let lm=null;
  for(let i=0;i<hands.length;i++){
    const c=names[i]&&names[i][0];
    if(c && c.categoryName!=='Closed_Fist' && c.categoryName!=='Pointing_Up' && c.categoryName!=='Victory'){ lm=hands[i]; break; }
  }
  if(!rxRec || rxRec.state!=='paused' || _gCountT || now<_wv.cool || !lm){ _wv.buf.length=0; return; }
  _wv.buf.push({t:now, x:swPalmX(lm)});
  while(_wv.buf.length && now-_wv.buf[0].t>WAVE_WINDOW) _wv.buf.shift();
  if(_wv.buf.length>=6 && rxWaveTurns(_wv.buf)>=2){
    _wv.buf.length=0; _wv.cool=now+2500;
    rxWaveDelete();
  }
}
function rxWaveDelete(){
  if(rxClips.length){
    rxClipErase(rxClips.length-1);                    // the previous finished take; undo stays on screen for 6 seconds
    rxFeedback('Waved away the last take. Undo is on screen for 6 seconds.', 3200);
  } else rxFeedback('No earlier take to delete.', 2200);
}
let _rxWide=false, _rxWideT=null;
// Film side rail: one options panel open at a time, beside the camera (TikTok-style)
let _rxFlyKey=null;
function rxFly(key){
  var fly=document.getElementById('rxFly'); if(!fly) return;
  if(key && key!=='prompt' && _rxFlyKey!==key && rxRecording()){ rxFeedback('Finish the take first to change that setting.', 3000); return; }
  var open=(key && _rxFlyKey!==key)?key:null;
  _rxFlyKey=open;
  fly.style.display=open?'flex':'none';
  fly.querySelectorAll('[data-fly-panel]').forEach(function(p){ p.style.display=(p.getAttribute('data-fly-panel')===open)?'block':'none'; });
  document.querySelectorAll('.rxrail-b[data-fly]').forEach(function(b){ var on=(b.getAttribute('data-fly')===open); b.classList.toggle('on',on); b.setAttribute('aria-expanded',on?'true':'false'); });
  rxWideSync();
  if(open==='cam') rxListMics();   // labels appear once the camera has permission, so refresh each time the panel opens
  if(open==='prompt') prmRender();
}
function rxToggleCamSet(){ rxFly('cam'); }
// during a take only the teleprompter (script + talk time) stays open; everything else closes
function rxFlyRec(){ if(_rxFlyKey && _rxFlyKey!=='prompt') rxFly(_rxFlyKey); }
// ---- Teleprompter: your own script, saved on this device; Sage's plan fills it when there is no saved script.
// Auto-scroll moves only while a take is recording (never during a countdown or a pause). Scrolling by hand pauses it for 3 s.
const PRM_KEY='abraPrompter';
let _prm={saved:'', plan:[], lines:[], cur:0, speed:3, size:14, engine:false, holdUntil:0, last:0, y:0};
function prmClean(list){ return (list||[]).map(function(s){ return String(s||'').trim().slice(0,280); }).filter(Boolean); }
function prmLoad(){
  try{
    const raw=window.localStorage.getItem(PRM_KEY); const d=raw?JSON.parse(raw):null;
    if(d && typeof d==='object'){
      _prm.saved=String(d.script||'').slice(0,20000);
      _prm.speed=Math.max(1, Math.min(5, parseInt(d.speed,10)||3));
      _prm.size=Math.max(12, Math.min(22, parseInt(d.size,10)||14));
    }
  }catch(e){ /* blocked storage or bad data: the defaults stay */ }
}
function prmSave(){
  let ok=false;
  try{ window.localStorage.setItem(PRM_KEY, JSON.stringify({script:_prm.saved, speed:_prm.speed, size:_prm.size})); ok=true; }catch(e){ ok=false; }
  try{ fetch('/prompter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({script:_prm.saved, speed:_prm.speed, size:_prm.size})}).catch(function(){}); }catch(e){}
  return ok;
}
function prmPullServer(){
  fetch('/prompter',{cache:'no-store'}).then(function(r){ return r.json(); }).then(function(d){
    if(!d) return;
    if(d.saved){
      _prm.saved=String(d.script||''); _prm.speed=Math.max(1, Math.min(5, parseInt(d.speed,10)||3)); _prm.size=Math.max(12, Math.min(22, parseInt(d.size,10)||14));
      try{ window.localStorage.setItem(PRM_KEY, JSON.stringify({script:_prm.saved, speed:_prm.speed, size:_prm.size})); }catch(e){}
      prmRender();
    } else if(_prm.saved.trim()){ prmSave(); }   // first time on the server: this device's script is copied over
  }).catch(function(){});
}
function prmRender(){
  const tp=document.getElementById('rxPrompter'); if(!tp) return;
  const y=_prm.y;
  _prm.lines = _prm.saved.trim() ? prmClean(_prm.saved.split('\\n')) : prmClean(_prm.plan);
  { const _raw=_prm.saved.trim() ? _prm.saved.split(String.fromCharCode(10)) : _prm.plan, _m=document.getElementById('rxPromptMsg');
    if(_m && _raw.some(function(l){ return String(l||'').trim().length>280; })) _m.textContent='Lines longer than 280 characters are shortened to fit the teleprompter.'; }
  if(_prm.cur>=_prm.lines.length) _prm.cur=0;
  tp.style.setProperty('--prompt-size', _prm.size+'px');
  tp.innerHTML='';
  if(!_prm.lines.length){ tp.innerHTML='<div class="empty">Type your script with Edit script.</div>'; }
  _prm.lines.forEach(function(s,i){ const d=document.createElement('div'); d.className='rxpline'+(i===_prm.cur?' on':''); d.textContent=(i+1)+'. '+s; tp.appendChild(d); });
  if(tp.clientHeight>0){ tp.scrollTop=y; _prm.y=tp.scrollTop; }
  const sv=document.getElementById('rxPromptSpeedV'); if(sv) sv.textContent='Speed '+_prm.speed;
  const pb=document.getElementById('rxPromptPlanBox'); if(pb) pb.style.display=(_prm.saved.trim() && _prm.plan.length)?'flex':'none';
}
function prmActive(){ return !!(rxRec && rxRec.state==='recording' && !_gCountT); }
function prmTick(now){
  const tp=document.getElementById('rxPrompter');
  const dt=_prm.last?Math.min(100, now-_prm.last):0; _prm.last=now;
  if(tp){
    if(!prmActive()){ if(tp.clientHeight>0) _prm.y=tp.scrollTop; }
    else if(now>=_prm.holdUntil && tp.scrollHeight>tp.clientHeight){
      _prm.y=Math.min(tp.scrollHeight, _prm.y+(6*_prm.speed+8)*dt/1000);   // 14 to 38 px a second, speed 1 to 5
      _prm.engine=true; tp.scrollTop=_prm.y; _prm.engine=false;
    }
  }
  requestAnimationFrame(prmTick);
}
(function(){
  const tp=document.getElementById('rxPrompter');
  if(tp) tp.addEventListener('scroll', function(){ if(!_prm.engine && Math.abs(tp.scrollTop-_prm.y)>2){ _prm.holdUntil=performance.now()+3000; _prm.y=tp.scrollTop; } });
})();
try{ prmLoad(); prmRender(); prmPullServer(); }catch(e){}
requestAnimationFrame(prmTick);
function rxPromptNext(){
  const tp=document.getElementById('rxPrompter'); if(!tp || !_prm.lines.length) return false;
  _prm.cur=Math.min(_prm.lines.length-1, _prm.cur+1);
  const el=tp.children[_prm.cur], first=tp.children[0];
  if(el && first){ _prm.y=Math.max(0, el.offsetTop-first.offsetTop-4); tp.scrollTop=_prm.y; }
  prmRender();
  return true;
}
function rxPromptSpeed(d){ _prm.speed=Math.max(1, Math.min(5, _prm.speed+d)); prmSave(); prmRender(); return _prm.speed; }
function rxPromptSize(d){ _prm.size=Math.max(12, Math.min(22, _prm.size+d)); prmSave(); prmRender(); return _prm.size; }
function rxPromptEdit(){
  if(rxTakeBusy()){ rxFeedback('Stop the take first, then edit the script.', 3500); return false; }
  const box=document.getElementById('rxPromptEditBox'), ta=document.getElementById('rxPromptText'); if(!box || !ta) return false;
  ta.value = _prm.saved.trim() ? _prm.saved : _prm.plan.join('\\n');
  box.style.display='block'; ta.focus(); return true;
}
function rxPromptCancel(){ const box=document.getElementById('rxPromptEditBox'); if(box) box.style.display='none'; }
function rxPromptSave(){
  const box=document.getElementById('rxPromptEditBox'), ta=document.getElementById('rxPromptText'), m=document.getElementById('rxPromptMsg');
  if(!ta) return false;
  if(!box || box.style.display!=='block'){ if(m) m.textContent='Open Edit script first, then save.'; return false; }
  const raw=(ta.value||'');
  if(!raw.trim()){ if(m) m.textContent='Type at least one line before you save. Cancel leaves the script as it was.'; return false; }
  const text=raw.slice(0,20000);
  _prm.prev=_prm.saved; _prm.saved=text;
  const ok=prmSave();
  rxPromptCancel(); prmRender();
  if(m) m.textContent = raw.length>20000 ? 'Scripts are kept up to 20,000 characters, so the end was not saved.' : (ok ? 'Saved on this device.' : 'Could not save on this device, so the script will be lost when you close this page.');
  return ok;
}
function rxPromptUsePlan(){
  const m=document.getElementById('rxPromptMsg');
  if(rxTakeBusy()){ rxFeedback('Stop the take first, then use the plan.', 3500); return false; }
  if(!_prm.plan.length){ if(m) m.textContent='Sage has not written a plan yet.'; return false; }
  if(_prm.saved.trim() && !_prm.confirmUse){
    _prm.confirmUse=true;
    if(m) m.textContent='Tap \u201cUse Sage\u2019s plan instead\u201d again to replace your saved script. Undo brings it back.';
    return false;
  }
  _prm.confirmUse=false;
  _prm.prev=_prm.saved;
  _prm.saved=_prm.plan.join('\\n'); const ok=prmSave(); prmRender();
  if(m) m.textContent = ok ? 'Sage\u2019s plan is now your script. Undo brings back the one before.' : 'Could not save on this device.';
  return ok;
}
function rxPromptUndo(){
  const m=document.getElementById('rxPromptMsg');
  if(typeof _prm.prev!=='string'){ if(m) m.textContent='There is nothing to undo.'; return false; }
  _prm.saved=_prm.prev; _prm.prev=null; _prm.confirmUse=false; const ok=prmSave(); prmRender();
  if(m) m.textContent='Put back your previous script.';
  return ok;
}
function rxToggleWide(){
  _rxWide=!_rxWide;
  var b=document.getElementById('rxWideBtn');
  if(b){ b.classList.toggle('on',_rxWide); b.setAttribute('aria-pressed',_rxWide?'true':'false'); b.textContent=_rxWide?'Showing the sides':'Show the sides'; }
  var box=document.getElementById('rxWideBox'); if(box) box.style.display=_rxWide?'block':'none';
  rxWideSync();
}
// the wide view only draws while its panel is open; hands-free already redraws it at detection rate
function rxWideSync(){
  rxWideStop();
  if(!_rxWide) return;
  var p=document.getElementById('rxCamSet'); if(!p || p.style.display==='none') return;
  rxDrawMonitor(null,'',false);
  _rxWideT=setInterval(function(){ if(!_gLoopRAF) rxDrawMonitor(null,'',false); }, 90);
}
function rxWideStop(){ if(_rxWideT){ clearInterval(_rxWideT); _rxWideT=null; } }
// a new take must show the LIVE camera, not the previous clip
function rxAttachLive(){
  { var _cwl=document.getElementById('rxCamWrap'); if(_cwl) _cwl.classList.remove('clip-play'); }
  var v=document.getElementById('rxCam'); if(!v || !rxStream) return;
  try{ v.pause(); }catch(e){}
  v.removeAttribute('src'); v.srcObject=rxStream; v.muted=true; v.loop=false; v.style.transform='';
  v.play().catch(function(){});
  _rxPlaying=false; rxUpdateCropBox();
}
// Room for your hand: the slider, the voice commands and the auto value all go through here, so they cannot drift.
// byUser marks a value the creator set themselves: hands-free no longer overrides it, and it cannot change during a take.
let _rxRoomUser=false;
// a take is running, paused, or about to start (countdown): the camera's picture and sound must not change now
function rxTakeBusy(){ return !!(_gCountT || rxRecording()); }
function rxSetZoom(v, byUser){
  if(byUser){ if(rxTakeBusy()){ const _s=document.getElementById('rxZoomSl'); if(_s) _s.value=String(Math.round(_rxZoom*100)); return false; } _rxRoomUser=true; }
  const n=Math.max(100, Math.min(180, Math.round(+v)));
  _rxZoom=n/100;
  const cam=document.getElementById('rxCam'); if(cam) cam.style.transform='';
  rxUpdateCropBox();
  const el=document.getElementById('rxZoomV'); if(el) el.textContent=(Math.round(_rxZoom*10)/10)+'\u00d7';
  const sl=document.getElementById('rxZoomSl'); if(sl && (+sl.value)!==n) sl.value=String(n);
  return true;
}
// true when the recorded frame already fills the whole camera, so there is no side gutter for a hand at this room
function rxNoRoomGutter(){
  const v=document.getElementById('rxGhost'); if(!v || !v.videoWidth) return false;
  const dims=RX_DIMS[_rxFormat]||RX_DIMS.portrait, ta=dims[0]/dims[1], va=v.videoWidth/v.videoHeight;
  const swN=(va>ta)?((v.videoHeight*ta)/v.videoWidth):1;
  return swN/(_rxZoom||1) >= 0.999;
}
let _rxRoomNoteAt=0;
function rxRoomFloorNote(){
  const t=performance.now(); if(t-_rxRoomNoteAt<8000) return; _rxRoomNoteAt=t;
  rxFeedback('No room for your hand at this zoom. Raise Room for your hand in Camera settings.', 5000);
}
function rxDrawMonitor(lm, act, warn){
  const cv=document.getElementById('rxMonitor'); if(!cv) return;
  const v=document.getElementById('rxGhost'), ctx=cv.getContext('2d'), mw=cv.width, mh=cv.height;
  ctx.clearRect(0,0,mw,mh);
  if(!v || !v.videoWidth) return;
  const vw=v.videoWidth, vh=v.videoHeight, va=vw/vh, ma=mw/mh;
  let dw,dh,dx,dy;
  if(va>ma){ dw=mw; dh=mw/va; dx=0; dy=(mh-dh)/2; } else { dh=mh; dw=mh*va; dy=0; dx=(mw-dw)/2; }
  ctx.save(); if(_rxMirror){ ctx.translate(mw,0); ctx.scale(-1,1); } ctx.drawImage(v, 0,0,vw,vh, dx,dy,dw,dh); ctx.restore();
  const dims=RX_DIMS[_rxFormat]||RX_DIMS.portrait, ta=dims[0]/dims[1];
  let sw,sh,sx,sy;
  if(va>ta){ sh=vh; sw=vh*ta; sx=(vw-sw)/2; sy=0; } else { sw=vw; sh=vw/ta; sx=0; sy=(vh-sh)/2; }
  const z=_rxZoom||1; if(z>1){ const nsw=sw/z, nsh=sh/z; sx+=(sw-nsw)/2; sy+=(sh-nsh)/2; sw=nsw; sh=nsh; }
  const ox=dx+(sx/vw)*dw, oy=dy+(sy/vh)*dh, ow=(sw/vw)*dw, oh=(sh/vh)*dh;
  ctx.fillStyle='rgba(6,9,16,.52)';
  ctx.fillRect(dx,dy,dw,oy-dy); ctx.fillRect(dx,oy+oh,dw,dy+dh-(oy+oh));
  ctx.fillRect(dx,oy,ox-dx,oh); ctx.fillRect(ox+ow,oy,dx+dw-(ox+ow),oh);
  const col = act? '#4fc07e' : (warn? '#e0794b' : '#8aa0c6');
  ctx.strokeStyle=col; ctx.lineWidth=2; ctx.setLineDash([6,4]); ctx.strokeRect(ox+1,oy+1,Math.max(0,ow-2),Math.max(0,oh-2)); ctx.setLineDash([]);
  if(lm && lm[8]){ const hx=dx+(_rxMirror?(1-lm[8].x):lm[8].x)*dw, hy=dy+lm[8].y*dh; ctx.fillStyle=col; ctx.beginPath(); ctx.arc(hx,hy,4,0,6.283); ctx.fill(); }
  let lbl='';
  if(act){ lbl=act==='start'?'\u25cf START':(act==='pause'?'\u275a\u275a PAUSE':'\u25a0 STOP'); }
  else if(warn){ lbl='hold out to the side'; }
  if(lbl){ ctx.fillStyle=col; ctx.font='bold 11px -apple-system,system-ui,sans-serif'; ctx.textBaseline='top'; ctx.fillText(lbl, dx+6, dy+5); }
}
function swTick(name, score, lm, now){
  if(!swEligible()){ _swBuf.length=0; _swState='DISARMED'; return; }
  if(now < _swGuardUntil){ _swBuf.length=0; _swState='DISARMED'; return; }   // fist-retreat guard
  if(!lm || !lm[0]){
    _swGapN++; _swSettleN=0;
    if(_swGapN>=SW_LEAVE_FR){ _swBuf.length=0; if(_swState==='SPENT') _swState='DISARMED'; }
    return;
  }
  _swGapN=0;
  var x=swPalmX(lm), y=swPalmY(lm), sc=swPalmSc(lm), open=(name==='Open_Palm' && score>=SW_OPEN_SCORE);
  var last=_swBuf.length?_swBuf[_swBuf.length-1]:null;
  var step=last?Math.abs(x-last.x):1;
  if(_swState!=='ARMED'){                                                    // ARM on a settled flat hand
    _swSettleN=(open && step<SW_ARM_V)?_swSettleN+1:0;
    if(_swState==='DISARMED' && _swSettleN>=SW_ARM_FR) _swState='ARMED';
    if(_swState==='SPENT' && now>=_swCoolUntil && _swSettleN>=SW_ARM_FR) _swState='ARMED';
  }
  _swBuf.push({t:now, x:x, y:y, sc:sc, open:open});
  while(_swBuf.length && now-_swBuf[0].t>SW_BUFMS) _swBuf.shift();
  // --- push the open hand IN toward the camera (it grows, stays roughly centered) -> erase last clip.
  //     Fires without a settle step; distinct from the sideways wipe (which has big lateral travel). ---
  if(now>=_swCoolUntil){
    var rc=[]; for(var jt=0;jt<_swBuf.length;jt++){ if(now-_swBuf[jt].t<=SW_TOWARDMS) rc.push(_swBuf[jt]); }
    if(rc.length>=4){
      var hh=Math.floor(rc.length/2), scA=9, scB=0, kt;
      for(kt=0;kt<hh;kt++){ if(rc[kt].sc<scA) scA=rc[kt].sc; }                 // smallest at the start
      for(kt=rc.length-hh;kt<rc.length;kt++){ if(rc[kt].sc>scB) scB=rc[kt].sc; }  // biggest at the end
      var tRatio=(scA>0)?(scB/scA):1, ta0=rc[0], tz0=rc[rc.length-1];
      var tdx=Math.abs(tz0.x-ta0.x), tdy=Math.abs(tz0.y-ta0.y), tdt=tz0.t-ta0.t, topen=0, wt;
      for(wt=0;wt<rc.length;wt++) if(rc[wt].open) topen++; topen/=rc.length;
      if(tdt>=SW_MINMS && tdt<=SW_TOWARDMS && tRatio>=SW_SCALE_MIN && tdx<SW_TOWARD_TXY && tdy<SW_TOWARD_TXY && topen>=0.5){
        _swState='SPENT'; _swCoolUntil=now+SW_COOL_MS; _swBuf.length=0; _swSettleN=0;
        rxFireToward(); return;
      }
    }
  }
  if(_swState!=='ARMED') return;
  var n=_swBuf.length; if(n<3) return;
  // --- horizontal wipe -> smooth cut (maximal monotonic x-run ending at newest) ---
  var dirSign=Math.sign(_swBuf[n-1].x-_swBuf[n-2].x);
  if(dirSign!==0){
    var i=n-1, k;
    while(i>0){ var d=_swBuf[i].x-_swBuf[i-1].x; if(!(Math.abs(d)>SW_STEP_EPS && Math.sign(d)===dirSign)) break; i--; }   // run = only real moves in the swipe direction (excludes the settle)
    var run=_swBuf.slice(i), m=run.length;
    if(m>=3){
      var dt=run[m-1].t-run[0].t, dx=run[m-1].x-run[0].x;
      if(dt>=SW_MINMS && dt<=SW_WINMS && Math.abs(dx)>=SW_DX_MIN){
        var minY=run[0].y, maxY=run[0].y;
        for(k=1;k<m;k++){ if(run[k].y<minY)minY=run[k].y; if(run[k].y>maxY)maxY=run[k].y; }
        var dY=maxY-minY;
        if(dY<=SW_DY_MAX && Math.abs(dx)>=SW_HV_RATIO*dY){
          var gross=0, agree=0, counted=0, peak=0;
          for(k=1;k<m;k++){ var sx2=run[k].x-run[k-1].x, ax2=Math.abs(sx2); gross+=ax2; if(ax2>peak)peak=ax2;
            if(ax2>SW_STEP_EPS){ counted++; if(Math.sign(sx2)===Math.sign(dx)) agree++; } }
          var opn=0; for(k=0;k<m;k++) if(run[k].open) opn++;
          if(gross>0 && Math.abs(dx)/gross>=SW_STRAIGHT && counted>0 && agree/counted>=SW_DIRFRAC
             && peak>=SW_VPEAK && opn/m>=SW_OPEN_FRAC){
            _swState='SPENT'; _swCoolUntil=now+SW_COOL_MS; _swBuf.length=0; _swSettleN=0;
            rxFireSwipe(); return;
          }
        }
      }
    }
  }
}
function rxFireSwipe(){
  var st=document.getElementById('rxHfStatus');
  if(st){ st.textContent='Swipe — trimming the dead space…'; st.className='rxhf-status on'; }
  if(typeof rxSmoothCut==='function') rxSmoothCut();
}
function rxFireToward(){
  var st=document.getElementById('rxHfStatus');
  if(!rxClips.length){ if(st){ st.textContent='Nothing to erase yet'; } return; }
  rxClipErase(rxClips.length-1);
  if(st){ st.textContent='↩ Erased the last take — undo below'; st.className='rxhf-status bad'; }
}
async function rxSmoothCut(){
  if(!rxBlob || rxCutting || rxCut) return;
  rxCutting=true;
  const srcBlob=rxBlob;
  const btn=document.getElementById('rxCutBtn'), st=document.getElementById('rxHfStatus'), wrap=document.getElementById('rxCamWrap');
  if(btn){ btn.disabled=true; btn.textContent='Tightening up…'; }
  if(wrap) wrap.classList.add('cutting');
  if(st){ st.textContent=' Tightening up… (on your machine)'; st.className='rxhf-status on'; }
  const ctl=new AbortController(); const to=setTimeout(function(){ ctl.abort(); }, 180000);
  try{
    const r=await fetch('/smoothcut', {method:'POST', headers:{'Content-Type':'video/webm'}, body:rxBlob, signal:ctl.signal});
    if(r.status===422){
      const why=r.headers.get('X-Reason')||'';
      const said = why==='no_audio' ? 'no sound in this take, so there was nothing to trim'
        : why==='too_short' ? 'the take is too short to trim'
        : why==='all_silent' ? 'the take is silent, so there was nothing to trim'
        : 'already tight — nothing to trim';
      if(st){ st.textContent=said; st.className='rxhf-status on'; }
      rxCut=true; if(btn){ btn.textContent='Tightened'; btn.disabled=true; }
      return;
    }
    if(!r.ok) throw new Error('http '+r.status);
    const orig=parseFloat(r.headers.get('X-Orig-Dur')||'0'), neu=parseFloat(r.headers.get('X-New-Dur')||'0');
    const mp4=await r.blob();
    if(rxCutUrl) URL.revokeObjectURL(rxCutUrl);
    rxBlob=new Blob([mp4], {type:'video/mp4'});
    const url=URL.createObjectURL(rxBlob); rxCutUrl=url;
    const v=document.getElementById('rxCam');
    if(v){ v.pause(); v.srcObject=null; v.src=url; v.muted=false; v.loop=false; v.style.transform=''; v.load(); v.play().catch(function(){}); }
    if(wrap) wrap.classList.add('clip-play');   // review shows the file as it was filmed, not mirrored
    const dl=document.getElementById('rxDownload'); if(dl){ dl.href=url; dl.setAttribute('download','reaction.mp4'); }
    const ci=rxClips.findIndex(function(c){ return c.blob===srcBlob; });
    if(ci>=0){ try{ if(rxClips[ci].url) URL.revokeObjectURL(rxClips[ci].url); }catch(e){} rxClips[ci]={blob:rxBlob, url:url, dur:neu, mp4:true, cut:true}; }
    else { rxClips.push({blob:rxBlob, url:url, dur:neu, mp4:true, cut:true}); }
    rxRenderClips();
    const removed=Math.max(0, orig-neu);
    if(st){ st.textContent = removed>=0.3 ? ('removed '+removed.toFixed(1)+'s of dead space') : 'already tight — nothing to trim'; st.className='rxhf-status on'; }
    const tl=document.getElementById('rxTimer');
    if(tl && neu>0){ const sec=Math.floor(neu); tl.textContent=String(Math.floor(sec/60)).padStart(2,'0')+':'+String(sec%60).padStart(2,'0'); }
    rxCut=true; if(btn){ btn.textContent='Tightened'; btn.disabled=true; }
  }catch(e){
    if(st){ st.textContent='Couldn’t tighten — kept your original.'; st.className='rxhf-status bad'; }
    if(btn){ btn.disabled=false; btn.textContent='✂️ Smooth cut'; }
  }finally{
    clearTimeout(to); rxCutting=false;
    if(wrap) wrap.classList.remove('cutting');
  }
}
function rxFmtDur(d){ d=Math.max(0, Math.round(d||0)); return Math.floor(d/60)+':'+String(d%60).padStart(2,'0'); }
function rxRenderClips(){ rxSyncNext();
  const wrap=document.getElementById('rxClips'); if(!wrap) return;
  if(!rxClips.length){ wrap.style.display='none'; wrap.innerHTML=''; return; }
  wrap.style.display='block';
  var h='<div class="rxclips-h">Your takes <span class="hint">'+rxClips.length+' clip'+(rxClips.length>1?'s':'')+' · kept in this tab until you erase them or reload the page</span></div><div class="rxclips-strip">';
  for(var i=0;i<rxClips.length;i++){ var c=rxClips[i];
    h+='<div class="rxclip'+(c.cut?' cut':'')+'">'
      +'<video src="'+c.url+'" playsinline preload="metadata" controls onloadedmetadata="rxClipMeta(this,'+i+')"></video>'
      +'<div class="rxclip-meta"><span class="rxclip-n">Take '+(i+1)+'</span>'+(c.cut?'<span class="rxclip-tag">tight</span>':'')+'<span class="rxclip-dur" id="rxcd'+i+'">'+(c.dur?rxFmtDur(c.dur):'')+'</span></div>'
      +'<div class="rxclip-acts">'
      +'<button class="rxclip-b" title="Save this take" onclick="rxClipDownload('+i+')">Save</button>'
      +'<button class="rxclip-b" title="Hand to Sage" onclick="rxClipSage('+i+')">→</button>'
      +'<button class="rxclip-b del" title="Erase" onclick="rxClipErase('+i+')">Erase</button>'
      +'</div></div>';
  }
  h+='</div>';
  wrap.innerHTML=h;
}
function rxClipMeta(el,i){ if(rxClips[i] && el && el.duration && isFinite(el.duration)){ rxClips[i].dur=el.duration; var d=document.getElementById('rxcd'+i); if(d) d.textContent=rxFmtDur(el.duration); } }
function rxClipDownload(i){ var c=rxClips[i]; if(!c) return; var a=document.createElement('a'); a.href=c.url; a.download='take-'+(i+1)+'.'+(c.mp4?'mp4':'webm'); document.body.appendChild(a); a.click(); a.remove(); }
function rxNext(){ if(!rxClips.length) return; rxClipSage(rxClips.length-1); }
function rxSyncNext(){ var b=document.getElementById('rxNextBtn'), h=document.getElementById('rxNextHint'); if(!b) return; var has=rxClips.length>0; var rec=!!(rxRec && rxRec.state!=='inactive'); b.disabled=!has||rec; if(h) h.textContent=has ? 'Your latest take goes to Load, where Sage writes the post' : 'Record a take first'; }
function rxClipSage(i){ var c=rxClips[i]; if(!c) return; try{ var ext=c.mp4?'mp4':'webm'; var f=new File([c.blob], 'reaction.'+ext, {type:c.blob.type||'video/webm'}); window._rxWantCaptions=_rxCaptions && !c.captioned; rxStopCam(); showView('studio'); setTimeout(function(){ if(typeof analyzeVideo==='function') analyzeVideo(f); else if(typeof videoDrop==='function') videoDrop([f]); }, 250); }catch(e){} }
function rxClipErase(i){
  if(i<0 || i>=rxClips.length) return;
  rxUndoFinalize();
  var c=rxClips.splice(i,1)[0];
  _rxUndo={clip:c, i:i};
  rxRenderClips();
  rxShowUndo('Erased Take '+(i+1));
}
function rxShowUndo(msg){
  var u=document.getElementById('rxUndo'); if(!u) return;
  var t=u.querySelector('.rxundo-t'); if(t) t.textContent=msg;
  u.style.display='flex';
  if(_rxUndoT) clearTimeout(_rxUndoT);
  _rxUndoT=setTimeout(function(){ rxUndoFinalize(); }, 6000);
}
function rxUndoDo(){
  if(!_rxUndo) return;
  var at=Math.min(_rxUndo.i, rxClips.length);
  rxClips.splice(at, 0, _rxUndo.clip);
  _rxUndo=null; if(_rxUndoT){ clearTimeout(_rxUndoT); _rxUndoT=null; }
  var u=document.getElementById('rxUndo'); if(u) u.style.display='none';
  rxRenderClips();
}
function rxUndoFinalize(){
  if(_rxUndo){ try{ URL.revokeObjectURL(_rxUndo.clip.url); }catch(e){} _rxUndo=null; }
  var u=document.getElementById('rxUndo'); if(u) u.style.display='none';
  if(_rxUndoT){ clearTimeout(_rxUndoT); _rxUndoT=null; }
}
function rxFinish(){
  if(!rxRec || rxRec.state==='inactive') return;
  { var _st0=document.getElementById('rxStudio'); if(_st0) _st0.classList.remove('rec-focus'); }
  rxRec.onstop=function(){
    rxBlob=new Blob(rxChunks, {type:'video/webm'});
    const url=URL.createObjectURL(rxBlob);
    const dl=document.getElementById('rxDownload'); if(dl){ dl.href=url; dl.style.display='inline-flex'; }
    const h=document.getElementById('rxHandBtn'); if(h) h.style.display='inline-flex';
    const v=document.getElementById('rxCam'); if(v){ v.srcObject=null; v.src=url; v.muted=false; v.loop=false; v.style.transform=''; v.play().catch(function(){}); }
    { var _cwp=document.getElementById('rxCamWrap'); if(_cwp) _cwp.classList.add('clip-play'); }   // review shows the file as it was filmed, not mirrored
    rxCut=false; { var _cb=document.getElementById('rxCutBtn'); if(_cb){ _cb.style.display='inline-flex'; _cb.disabled=false; _cb.textContent=' Smooth cut'; } }
    rxClips.push({blob:rxBlob, url:url, dur:0, mp4:false, cut:false}); rxRenderClips();
    if(_rxCaptions) rxCaptionTake(rxBlob);   // auto captions: the take is captioned here, so the review and the saved file show them
    _rxPlaying=true; rxUpdateCropBox();
  };
  rxRecOn=false; rxTimer(false); rxStopCanvas();
  _rxLastTalk = (rxMeterOn && rxRecMs>=5000) ? Math.round(rxTalkMs/rxRecMs*100) : null;   // the estimate for this take, for the voice reply; never a figure from an unmeasured take
  try{ rxRec.stop(); }catch(e){}
  const btn=document.getElementById('rxRecBtn'); if(btn){ btn.classList.remove('on'); btn.setAttribute('aria-label','Record again'); }
}
function rxTimer(on){
  if(rxTimerT){ clearInterval(rxTimerT); rxTimerT=null; }
  if(!on) return;
  rxTimerT=setInterval(function(){
    if(rxRecOn && (!rxMeterOn || performance.now()-_mtrBeat>600)){ const _now=performance.now(); if(rxLastMs) rxRecMs += Math.min(_now-rxLastMs, 60000); rxLastMs=_now;
      if(_rxTarget>0 && rxRecMs>=_rxTarget*1000 && rxRec && rxRec.state!=='inactive') rxFinish(); }
    const el=document.getElementById('rxTimer'); if(!el) return;
    const sec=Math.floor(rxRecMs/1000);
    if(_rxTarget>0){ var rem=Math.max(0,_rxTarget-sec); el.textContent='-'+String(Math.floor(rem/60)).padStart(2,'0')+':'+String(rem%60).padStart(2,'0'); }
    else { el.textContent=String(Math.floor(sec/60)).padStart(2,'0')+':'+String(sec%60).padStart(2,'0'); } }, 400);
}
function rxCloseCam(){ rxStopCam(); }
function rxStopCam(){
  if(rxRec && rxRec.state!=='inactive'){ try{ rxFinish(); }catch(e){} }
  try{ rxWideStop(); { var _st1=document.getElementById('rxStudio'); if(_st1) _st1.classList.remove('rec-focus'); } rxRtcStop(); rxHandsFreeOff(); rxStopCanvas(); const cw=document.getElementById('rxCamWrap'); if(cw) cw.classList.remove('cam-on');
    const sb=document.getElementById('rxStartBtn'); if(sb) sb.style.display='';
    rxStopMeter(); rxCamCaps(null); _rxRemote=false; _rxPhoneCaps=null;
    if(rxStream){ rxStream.getTracks().forEach(function(t){ t.stop(); }); rxStream=null; }
    { var _g=document.getElementById('rxGhost'); if(_g) _g.srcObject=null; } }catch(e){}
}
// auto captions for a finished take: the server transcribes it and burns the captions in; the review and the clip show the captioned copy
async function rxCaptionTake(srcBlob){
  const st=document.getElementById('rxHfStatus');
  if(st){ st.textContent=' Adding captions\u2026'; st.className='rxhf-status on'; }
  const ctl=new AbortController(); const to=setTimeout(function(){ ctl.abort(); }, 240000);
  try{
    const r=await fetch('/captions/take', {method:'POST', headers:{'Content-Type':'video/webm'}, body:srcBlob, signal:ctl.signal});
    if(r.status===422){ const why=r.headers.get('X-Reason')||''; if(st){ st.textContent = why==='no_speech' ? ' No speech found, so this take has no captions.' : ' Captions need a portrait take.'; st.className='rxhf-status on'; } return; }
    if(!r.ok) throw new Error('http '+r.status);
    const mp4=await r.blob();
    if(rxBlob!==srcBlob) return;   // another take has started since: leave that one alone
    const capBlob=new Blob([mp4], {type:'video/mp4'}); const url=URL.createObjectURL(capBlob);
    const ci=rxClips.findIndex(function(c){ return c.blob===srcBlob; });
    if(ci>=0){ try{ if(rxClips[ci].url) URL.revokeObjectURL(rxClips[ci].url); }catch(e){} rxClips[ci]={blob:capBlob, url:url, dur:rxClips[ci].dur, mp4:true, cut:rxClips[ci].cut, captioned:true}; }
    rxBlob=capBlob;
    const v=document.getElementById('rxCam');
    if(v && !rxRecOn){ v.pause(); v.srcObject=null; v.src=url; v.muted=false; v.loop=false; v.load(); v.play().catch(function(){}); }
    const dl=document.getElementById('rxDownload'); if(dl){ dl.href=url; dl.setAttribute('download','reaction.mp4'); }
    rxRenderClips();
    if(st){ st.textContent=' Captions added'; st.className='rxhf-status on'; }
  }catch(e){ if(st){ st.textContent=' Could not add captions; the take is kept without them.'; st.className='rxhf-status on'; } }
  finally{ clearTimeout(to); }
}
function rxHandToSage(){
  const lastKept=rxClips[rxClips.length-1]; if(!lastKept) return; const srcBlob=lastKept.blob;
  try{ var _ext=(srcBlob&&srcBlob.type&&srcBlob.type.indexOf('mp4')>=0)?'mp4':'webm'; const f=new File([srcBlob], 'reaction.'+_ext, {type:(srcBlob&&srcBlob.type)||'video/webm'});
    if(window._rxHook){ pendingBrief={hook:window._rxHook}; }
    window._rxWantCaptions=_rxCaptions && !(lastKept && lastKept.captioned);
    rxStopCam(); showView('studio');
    setTimeout(function(){ if(typeof analyzeVideo==='function') analyzeVideo(f); else if(typeof videoDrop==='function') videoDrop([f]); }, 250);
  }catch(e){}
}
document.addEventListener('keydown', function(e){
  if(e.code!=='Space') return;
  const rv=document.getElementById('view-film'); if(!rv||!rv.classList.contains('active')) return;
  const st=document.getElementById('rxStudio'); if(!st||st.style.display==='none') return;
  const tag=(e.target&&e.target.tagName)||''; if(tag==='INPUT'||tag==='TEXTAREA'||tag==='A'||tag==='BUTTON') return;
  e.preventDefault(); rxToggleRec();
});
async function loadAnalyst(force){
  const el=document.getElementById('analystWrap'); if(!el) return;
  if(force) el.innerHTML='<div class="empty">Sage is reading your numbers…</div>';
  try{
    const d=await fetch('/analyst'+(force?'?refresh=1':'')).then(r=>r.json());
    el.innerHTML = d.text ? mdLite(d.text) : '<div class="empty">Sage could not generate a read right now</div>';
    const ie=document.getElementById('analystIdeas');
    if(ie){ try{ ie.innerHTML = ideaCardsHTML(await loadIdeas(force), 'insights'); }catch(e){ ie.innerHTML=''; } }
  }catch(e){ el.innerHTML='<div class="empty">could not load Sage read</div>'; }
}
const DM_LABELS={auto:'Auto-reply safe + flag', flag:'Flag only (no auto-send)', off:'Off'};
async function loadDMs(){
  const wrap=document.getElementById('dmWrap'), modesEl=document.getElementById('dmModes');
  if(!wrap) return;
  try{
    const d=await fetch('/dms').then(r=>r.json());
    if(modesEl && d.modes && d.modes.length){
      modesEl.innerHTML=d.modes.map(p=>`<label class="dmmode"><span>${esc(p.name)}</span>
        <select onchange="setDmMode('${p.id}',this.value)">${Object.entries(DM_LABELS).map(([v,l])=>`<option value="${v}" ${p.mode===v?'selected':''}>${l}</option>`).join('')}</select></label>`).join('');
    }
    if((!d.log || !d.log.length)){
      wrap.innerHTML = d.error
        ? '<div class="empty">Direct messages are not connected yet, so there are no messages to show.</div>'
        : '<div class="empty">No DMs yet — when they arrive, Direct messages are not connected yet, so none show here.</div>';
      return;
    }
    const head=`<div class="sub" style="margin-bottom:8px"><b>${d.replied}</b> auto-replied · <b>${d.flagged}</b> flagged for you</div>`;
    wrap.innerHTML=head+d.log.map(m=>{
      const badge=m.action==='replied'?'<span class="dmb rep">replied</span>':'<span class="dmb flag">for you</span>';
      return `<div class="dmrow">${badge}<div class="dmbody">
        <div class="dmtop"><b>${esc(m.sender)}</b><span class="dmcat">${esc(m.category)}</span></div>
        <div class="dmin">${esc(m.incoming)}</div>${m.reply?`<div class="dmrep">↳ ${esc(m.reply)}</div>`:''}</div></div>`;
    }).join('');
  }catch(e){ wrap.innerHTML='<div class="empty">could not load messages</div>'; }
}
async function setDmMode(id,mode){
  try{ await fetch('/dmmode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page_id:id,mode})}); }catch(e){}
  loadDMs();
}
async function loadFanLove(){
  try {
    const r = await fetch('/fanlove'); const rows = await r.json();
    document.getElementById('fanloveWrap').innerHTML = rows.map((t, i) => `
      <div class="ev REPLY" style="margin-bottom:10px">
        <div class="meta"><span class="badge b-LIKE">#${i+1}</span>
          <span><b>${t.fan_replies}</b> fan replies</span><span>·</span>
          <span><b>${t.likes}</b></span><span>·</span>
          <span>${t.replied ? ' crew replied' : ' not replied yet'}</span></div>
        <div class="comment"> ${esc(t.comment)}</div>
        <div class="why">on: ${esc(t.post)}</div>
        ${t.url ? `<a href="${esc(t.url)}" target="_blank" class="fanbtn" title="Open on Facebook — see who & invite"> ↗</a>` : ''}
      </div>`).join('')
      || '<div class="empty">no hot threads yet — they bloom as fans reply back </div>';
  } catch(e) {}
}
function fmtN(n){ if(n==null) return '0'; n=+n; const s=n<0?'-':'', a=Math.abs(n);
  if(a>=1e9) return s+(a/1e9).toFixed(a>=1e10?0:1)+'B';
  if(a>=1e6) return s+(a/1e6).toFixed(a>=1e7?0:1)+'M';
  if(a>=1e3) return s+(a/1e3).toFixed(a>=1e4?0:1)+'k';
  return s+a; }
async function loadWatch(){
  const wrap=document.getElementById('watchWrap'), ins=document.getElementById('watchInsight');
  if(!wrap) return;
  try{
    const r=await fetch('/watchinsights'); const d=await r.json();
    const rows=(Array.isArray(d)?d:[]).filter(x=>x.watched_pct!=null);   // real retention only — skip reels with no data
    if(!rows.length){ wrap.innerHTML='<div class="empty">no reel watch data yet</div>'; if(ins) ins.innerHTML=''; return; }
    const clean=t=>{ const c=(t||'').replace(/#[^\\s#]+/g,'').replace(/\\s{2,}/g,' ').replace(/[\\s·\\-–—]+$/,'').trim(); return c||(t||'(reel)'); };
    const best=rows[0];
    const avg=Math.round(rows.reduce((a,x)=>a+x.watched_pct,0)/rows.length*10)/10;
    ins.innerHTML='Best hold: <b>'+best.watched_pct+'%</b> on &ldquo;'+esc(clean(best.title))+'&rdquo; &nbsp;·&nbsp; average hold <b>'+avg+'%</b> across '+rows.length+' reels';
    ins.className='watchlead';
    const SHOWN=8;
    wrap.innerHTML=rows.map((x,i)=>{
      const pct=x.watched_pct, barw=Math.min(100,pct);
      const col=pct>=20?'var(--good)':pct>=12?'var(--accent)':'var(--warn)';
      return `<div class="watchrow${i>=SHOWN?' whidden':''}">
        <div class="wtitle"><span>${esc(clean(x.title))}</span>
          <span class="wmeta">${x.len}s · ${fmtN(x.views)} views · ${fmtN(x.plays)} plays${x.view_min?' · '+fmtN(x.view_min)+' min watched':''}</span></div>
        <div class="wbarwrap"><div class="wbar" style="width:${barw}%; background:${col}"></div>
          <span class="wpct">${pct}% watched${x.avg_watch?' ('+x.avg_watch+'s)':''}</span></div>
      </div>`;
    }).join('') + (rows.length>SHOWN?`<button class="watchmore" onclick="var w=this.parentElement,on=w.classList.toggle('allshown');this.textContent=on?'Show less':'Show all ${rows.length} reels';">Show all ${rows.length} reels</button>`:'');
  }catch(e){ wrap.innerHTML='<div class="empty">could not load watch data</div>'; }
}
let calItems=[], calOffset=0, calSelDate=null;
async function loadCalendar(){
  const grid=document.getElementById('calGrid'); if(!grid) return;
  try{
    const r=await fetch('/calendar'); const d=await r.json();
    calItems = (d&&d.items)||[];
  }catch(e){ calItems=[]; }
  calRender();
}
function calMonth(delta){ calOffset+=delta; calSelDate=null; document.getElementById('calDay').innerHTML=''; calRender(); }
function calRender(){
  const grid=document.getElementById('calGrid'), label=document.getElementById('calLabel');
  if(!grid) return;
  const base=new Date(); base.setDate(1); base.setMonth(base.getMonth()+calOffset);
  const y=base.getFullYear(), m=base.getMonth();
  label.textContent = base.toLocaleString('en-US',{month:'long', year:'numeric'});
  const firstDow=new Date(y,m,1).getDay();
  const days=new Date(y,m+1,0).getDate();
  const byDate={};
  calItems.forEach(it=>{ if(!it.date) return; (byDate[it.date]=byDate[it.date]||[]).push(it); });
  const now=new Date();
  const todayStr=`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
  const dows=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].map(d=>`<div class="caldow">${d}</div>`).join('');
  let cells='';
  for(let i=0;i<firstDow;i++) cells+='<div class="calcell blank"></div>';
  for(let d=1; d<=days; d++){
    const ds=`${y}-${String(m+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const its=byDate[ds]||[];
    const chips=its.slice(0,3).map(it=>{
      const cls = it.type==='story' ? 'story' : (it.status||'scheduled');
      return `<div class="calchip ${cls}" title="${esc(it.title)}">${esc((it.title||'').slice(0,20))}</div>`;
    }).join('') + (its.length>3?`<div class="calmore">+${its.length-3} more</div>`:'');
    cells+=`<div class="calcell${ds===todayStr?' today':''}${its.length?' has':''}${ds===calSelDate?' sel':''}" onclick="calPick('${ds}')">
      <div class="caldnum">${d}</div>${chips}</div>`;
  }
  grid.innerHTML = `<div class="caldows">${dows}</div><div class="calcells">${cells}</div>`;
}
function calPick(ds){
  calSelDate=ds; calRender();
  const its=calItems.filter(x=>x.date===ds);
  const box=document.getElementById('calDay');
  if(!its.length){ box.innerHTML=`<div class="empty">Nothing on ${ds}.</div>`; return; }
  const dt=new Date(ds+'T00:00:00');
  const head=dt.toLocaleDateString('en-US',{weekday:'long', month:'long', day:'numeric'});
  box.innerHTML = `<h3 class="caldayhd">${head}</h3>` + its.map(it=>{
    const cls = it.type==='story' ? 'story' : (it.status||'scheduled');
    const thumb = it.thumb ? `<img class="caldthumb" src="${esc(it.thumb)}" alt="">` : `<div class="caldthumb ph"></div>`;
    const link = it.url ? `<a href="${esc(it.url)}" target="_blank" class="caldlink">open ›</a>` : '';
    return `<div class="caldrow">${thumb}
      <div class="caldmeta"><div class="caldtitle">${esc(it.title||'(untitled)')}</div>
      <div class="caldsub"><span class="calchip ${cls}">${esc(it.status||it.type||'')}</span>
      ${it.when?'<span class="caldwhen">'+esc(it.when)+'</span>':''}</div></div>${link}</div>`;
  }).join('');
}
async function loadTrend(){
  try {
    const r = await fetch('/trend'); const pages = await r.json();
    const label = {monetization_approximate_earnings:' Earnings / day (USD)', page_views_total:'Page views / day', page_video_views:'Video views / day',
                   page_post_engagements:'Post engagements / day'};
    document.getElementById('trendWrap').innerHTML = pages.map(pg => `
      <section><h2>${esc(pg.name)}</h2>
        ${Object.entries(pg.metrics).map(([m, rows]) => {
          const vals = rows.map(x=>x.v);
          const last = vals.length ? vals[vals.length-1] : '–';
          const prev = vals.length > 1 ? vals[vals.length-2] : null;
          const delta = (prev !== null && prev !== 0) ? Math.round((last-prev)/prev*100) : null;
          const dtxt = delta === null ? '' : (delta >= 0 ? `↑ ${delta}%` : `↓ ${Math.abs(delta)}%`);
          const dcol = delta === null ? '' : (delta >= 0 ? 'var(--good)' : 'var(--bad)');
          return `<div class="metricrow"><div class="mn">${label[m]||m}</div>
            <div class="mv">${last}</div><div class="md" style="color:${dcol}">${dtxt}</div>
            <div>${spark(vals)}</div></div>`;
        }).join('')}
      </section>`).join('') || '<div class="empty">no analytics yet — they collect daily</div>';
  } catch(e) {}
}
async function tick(){
  try {
    const r = await fetch('/data'); const d = await r.json();
    const s = d.stats;
    const svc = document.getElementById('svc');
    svc.className = 'pill ' + (s.service==='running'?'on':'off');
    document.getElementById('svcTxt').textContent = s.service==='running' ? 'System running' : 'System stopped';
    document.getElementById('tReplies').textContent = s.replies24;
    document.getElementById('tLikes').textContent = s.likes24;
    document.getElementById('tSkips').textContent = s.skips24;
    document.getElementById('tPace').textContent = s.repliesHour;
    const cap = s.cap || 30;
    document.getElementById('paceCap').textContent = 'of ' + cap + ' per 15 min';
    document.getElementById('paceBar').style.width = Math.min(100, s.repliesHour/cap*100) + '%';
    const h = document.getElementById('tHealth'), hc = document.getElementById('tHealthCap');
    const trips = s.fbTrips || 0, ferrs = s.fbErrors || 0;
    if (s.monetizationWarn) { h.textContent = ' $'; h.style.color = 'var(--bad)';
      hc.textContent = 'earnings collapsed — check facebook.com/accountquality NOW'; }
    else if (trips > 0) { h.textContent = ' ' + trips; h.style.color = 'var(--warn)';
      hc.textContent = 'throttle warnings — backing off'; }
    else if (ferrs > 3) { h.textContent = ferrs; h.style.color = 'var(--warn)';
      hc.textContent = 'recent FB errors — watching'; }
    else { h.textContent = ' ✓ '; h.style.color = 'var(--good)';
      hc.textContent = 'no flags — all clear'; }
    const b = document.getElementById('throttleBanner');
    if (s.throttleMin) { b.style.display='block';
      b.textContent = ' Facebook asked us to slow down — auto-resuming in ' + s.throttleMin + ' min.';
    } else b.style.display='none';
    const feed = document.getElementById('feed');
    if (d.events.length) {
      const rev = d.events.slice().reverse();
      const topKey = rev[0].time + '|' + rev[0].kind + '|' + (rev[0].comment||'').slice(0,30);
      const topIsNew = (lastFeedTop !== null && topKey !== lastFeedTop);
      feed.innerHTML = rev.map((e, idx) => {
        const kind = e.kind.replace('DRYRUN','REPLY').replace('DRYLIKE','LIKE');
        const fresh = (idx===0 && topIsNew) ? ' fresh' : '';
        let body = `<div class="comment"> ${esc(e.comment)}</div>`;
        if (e.kind==='REPLY'||e.kind==='DRYRUN') body += `<div class="reply">↳ ${esc(e.detail)}</div>`;
        if (e.kind==='SKIP') body += `<div class="why">skipped: ${esc(e.detail)}</div>`;
        return `<div class="ev ${kind}${fresh}"><div class="meta"><span class="badge b-${kind}">${kind}</span>
                <span>${e.time}</span><span>·</span><span>${e.source}</span></div>${body}</div>`;
      }).join('');
      lastFeedTop = topKey;
    }
    lastStats = s; lastEvents = d.events;
    mascotFeed(d.events);
    if (document.getElementById('view-home').classList.contains('active')) updateCrewMemory();
    if (document.getElementById('view-profile').classList.contains('active')) { loadStanding(); loadSettingsStatus(); }
  } catch(e) {}
}

/* ---------- living mascots: Nico the cat & Sage the dog, free-roaming ---------- */
const CAT_SVG = `
<svg viewBox="0 0 110 112" width="84" height="86">
  <ellipse class="pl-cshadow" cx="55.5" cy="108.4" rx="26" ry="3.4" fill="url(#pl-contact) none"/>
  <path class="tailwag" d="M88 86 Q104 80 100 62" stroke="#E6D5BC" stroke-width="9" fill="none" stroke-linecap="round"/>
  <path class="tailwag" d="M86.6 84.4 Q102.6 78.4 98.6 60.4" stroke="#FFFAF1" stroke-opacity=".75" stroke-width="2.6" fill="none" stroke-linecap="round"/>
  <g class="legs">
    <rect class="legA" x="40" y="88" width="11" height="20" rx="5.5" fill="url(#pl-leg-far) #EADBC2"/>
    <rect class="legB" x="60" y="88" width="11" height="20" rx="5.5" fill="url(#pl-leg-near) #F2E6D0"/>
  </g>
  <g class="body">
    <ellipse cx="56" cy="74" rx="27" ry="23" fill="url(#pl-cream) #F2E6D0"/>
    <path class="earL" d="M35 22 L40 2 L52 15 Z" fill="url(#pl-cream) #F2E6D0"/>
    <path class="earR" d="M62 14 L74 0 L80 20 Z" fill="url(#pl-cream) #F2E6D0"/>
    <path d="M39 19 L41.5 9 L48 16 Z" fill="url(#pl-chrome)"/>
    <path d="M65 12 L71 5 L74 16 Z" fill="url(#pl-chrome)"/>
    <circle cx="57" cy="36" r="25" fill="url(#pl-cream) #F2E6D0"/>
    <ellipse cx="57" cy="20" rx="11" ry="5.5" fill="url(#pl-sheen) none" opacity=".6"/>
    <ellipse cx="57" cy="66.5" rx="21" ry="5" fill="url(#pl-ao) none"/>
    <rect x="34" y="52" width="46" height="13" rx="6.5" fill="url(#pl-collar) #D07A4F"/>
    <path d="M41 55 H73" stroke="#FFFFFF" stroke-opacity=".35" stroke-width="1.6" stroke-linecap="round"/>
    <circle cx="47" cy="33" r="3.2" fill="url(#pl-chrome-dark)"/>
    <circle cx="68" cy="33" r="3.2" fill="url(#pl-chrome-dark)"/>
    <circle cx="48.1" cy="31.8" r="1.5" fill="#FFFFFF"/>
    <circle cx="69.1" cy="31.8" r="1.5" fill="#FFFFFF"/>
    <ellipse cx="41" cy="41" rx="5" ry="3.1" fill="url(#pl-blush) #F0B9A0"/>
    <ellipse cx="73" cy="41" rx="5" ry="3.1" fill="url(#pl-blush) #F0B9A0"/>
    <path d="M55 40 Q57.5 43 60 40" stroke="#4A3425" stroke-width="1.8" fill="none" stroke-linecap="round"/>
    <ellipse cx="57.5" cy="38.5" rx="2.6" ry="1.9" fill="url(#pl-chrome-dark)"/>
    <ellipse cx="56.9" cy="37.8" rx="1.1" ry=".6" fill="#FFFFFF"/>
    <path d="M28 36 L40 38 M28 43 L40 43" stroke="#C9B394" stroke-width="1.4" stroke-linecap="round"/>
    <path d="M86 36 L74 38 M86 43 L74 43" stroke="#C9B394" stroke-width="1.4" stroke-linecap="round"/>
  </g>
</svg>`;
const DOG_SVG = `
<svg viewBox="0 0 110 112" width="84" height="86">
  <ellipse class="pl-cshadow" cx="55.5" cy="108.4" rx="27" ry="3.4" fill="url(#pl-contact) none"/>
  <path class="tailwag" d="M86 82 Q98 76 96 64" stroke="#EADBC2" stroke-width="10" fill="none" stroke-linecap="round"/>
  <path class="tailwag" d="M84.6 80.4 Q96.6 74.4 94.6 62.4" stroke="#FFFAF1" stroke-opacity=".75" stroke-width="3" fill="none" stroke-linecap="round"/>
  <g class="legs">
    <rect class="legA" x="40" y="88" width="11" height="20" rx="5.5" fill="url(#pl-leg-far) #EADBC2"/>
    <rect class="legB" x="60" y="88" width="11" height="20" rx="5.5" fill="url(#pl-leg-near) #F2E6D0"/>
  </g>
  <g class="body">
    <ellipse cx="56" cy="74" rx="28" ry="24" fill="url(#pl-blue) #6B9BE8"/>
    <path d="M44 58 Q56 66 68 58 L68 74 Q56 80 44 74 Z" fill="url(#pl-blue-deep) #5E8CD6"/>
    <ellipse cx="57" cy="63.5" rx="20" ry="5" fill="url(#pl-ao) none"/>
    <circle cx="57" cy="36" r="26" fill="url(#pl-cream) #F2E6D0"/>
    <ellipse cx="57" cy="19" rx="12" ry="6" fill="url(#pl-sheen) none" opacity=".6"/>
    <ellipse cx="41.5" cy="33" rx="4.5" ry="12" fill="url(#pl-ao) none" opacity=".7"/>
    <ellipse cx="72.5" cy="33" rx="4.5" ry="12" fill="url(#pl-ao) none" opacity=".7"/>
    <path class="earL" d="M33 18 Q22 24 26 44 Q34 48 40 40 Q38 24 33 18 Z" fill="url(#pl-oat) #E3CFAE"/>
    <path class="earR" d="M81 18 Q92 24 88 44 Q80 48 74 40 Q76 24 81 18 Z" fill="url(#pl-oat) #E3CFAE"/>
    <circle cx="47" cy="33" r="3.4" fill="url(#pl-chrome-dark)"/>
    <circle cx="68" cy="33" r="3.4" fill="url(#pl-chrome-dark)"/>
    <circle cx="48.2" cy="31.7" r="1.6" fill="#FFFFFF"/>
    <circle cx="69.2" cy="31.7" r="1.6" fill="#FFFFFF"/>
    <ellipse cx="41" cy="41" rx="5" ry="3.1" fill="url(#pl-blush) #F0B9A0"/>
    <ellipse cx="73" cy="41" rx="5" ry="3.1" fill="url(#pl-blush) #F0B9A0"/>
    <ellipse cx="57.5" cy="41" rx="4.7" ry="3.6" fill="url(#pl-chrome-dark)"/>
    <ellipse cx="56.6" cy="39.4" rx="1.9" ry="1" fill="#FFFFFF"/>
    <path d="M57.5 44 Q57.5 48 53 48 M57.5 44 Q57.5 48 62 48" stroke="#4A3425" stroke-width="1.8" fill="none" stroke-linecap="round"/>
  </g>
</svg>`;
const KOALA_SVG = `
<svg viewBox="0 0 110 112" width="84" height="86">
  <ellipse class="pl-cshadow" cx="55.5" cy="108.4" rx="25" ry="3.4" fill="url(#pl-contact) none"/>
  <g class="legs">
    <rect class="legA" x="40" y="88" width="11" height="20" rx="5.5" fill="url(#pl-leg-grey-far) #A8B0BC"/>
    <rect class="legB" x="60" y="88" width="11" height="20" rx="5.5" fill="url(#pl-leg-grey-near) #B9C0C9"/>
  </g>
  <g class="body">
    <ellipse cx="56" cy="76" rx="26" ry="22" fill="url(#pl-grey) #B9C0C9"/>
    <ellipse cx="56" cy="80" rx="15" ry="13" fill="url(#pl-belly) #D8DDE3"/>
    <ellipse cx="57" cy="65.5" rx="20" ry="5" fill="url(#pl-ao) none"/>
    <circle class="earL" cx="30" cy="20" r="14" fill="url(#pl-grey) #B9C0C9"/>
    <circle class="earR" cx="84" cy="20" r="14" fill="url(#pl-grey) #B9C0C9"/>
    <circle cx="30" cy="20" r="7.5" fill="url(#pl-chrome)"/>
    <circle cx="84" cy="20" r="7.5" fill="url(#pl-chrome)"/>
    <circle cx="57" cy="38" r="26" fill="url(#pl-grey-head) #C7CDD5"/>
    <ellipse cx="57" cy="21" rx="12" ry="6" fill="url(#pl-sheen) none" opacity=".55"/>
    <circle cx="47" cy="33" r="3.2" fill="url(#pl-chrome-dark)"/>
    <circle cx="68" cy="33" r="3.2" fill="url(#pl-chrome-dark)"/>
    <circle cx="48.1" cy="31.8" r="1.5" fill="#FFFFFF"/>
    <circle cx="69.1" cy="31.8" r="1.5" fill="#FFFFFF"/>
    <ellipse cx="57.5" cy="43" rx="7" ry="9" fill="url(#pl-chrome-dark)"/>
    <ellipse cx="55" cy="39.5" rx="2.2" ry="3.1" fill="#FFFFFF" opacity=".92"/>
    <path d="M44 55 Q57 60 70 55" stroke="#8D95A1" stroke-width="1.6" fill="none" stroke-linecap="round"/>
  </g>
</svg>`;
const FOX_SVG = `
<svg viewBox="0 0 110 112" width="84" height="86">
  <path class="tailwag" d="M84 84 Q102 78 98 58 L90 66 Q92 76 82 80 Z" fill="url(#pl-orange)"/>
  <g class="legs">
    <rect class="legA" x="40" y="88" width="11" height="20" rx="5.5" fill="url(#pl-orange-deep)"/>
    <rect class="legB" x="60" y="88" width="11" height="20" rx="5.5" fill="url(#pl-orange)"/>
  </g>
  <g class="body">
    <ellipse cx="56" cy="75" rx="26" ry="22" fill="url(#pl-orange)"/>
    <ellipse cx="56" cy="80" rx="14" ry="12" fill="url(#pl-fox-belly)"/>
    <path class="earL" d="M34 22 L40 2 L52 16 Z" fill="url(#pl-orange)"/>
    <path class="earR" d="M62 15 L76 0 L82 20 Z" fill="url(#pl-orange)"/>
    <path d="M38 19 L41.5 9 L47 15 Z" fill="url(#pl-chrome)"/>
    <path d="M66 13 L73 6 L76 16 Z" fill="url(#pl-chrome)"/>
    <circle cx="57" cy="36" r="25" fill="url(#pl-orange)"/>
    <path d="M38 42 Q57 62 76 42 Q70 54 57 56 Q44 54 38 42 Z" fill="url(#pl-fox-belly)"/>
    <circle cx="47" cy="32" r="3.2" fill="url(#pl-chrome-dark)"/>
    <circle cx="68" cy="32" r="3.2" fill="url(#pl-chrome-dark)"/>
    <ellipse cx="57.5" cy="44" rx="3.8" ry="3" fill="url(#pl-chrome-dark)"/>
    <ellipse cx="56.7" cy="43" rx="1.3" ry=".7" fill="#FFFFFF"/>
    <path d="M51 50 Q57.5 54 64 50" stroke="#4A3425" stroke-width="1.6" fill="none" stroke-linecap="round"/>
    <rect x="70" y="64" width="16" height="12" rx="2.5" fill="#9A6B43"/>
    <rect x="76" y="62" width="4" height="3" rx="1.5" fill="#7A5433"/>
  </g>
</svg>`;
document.body.insertAdjacentHTML('beforeend', `
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
  <filter id="brushed" x="-20%" y="-20%" width="140%" height="140%"
          color-interpolation-filters="sRGB">
    <!-- fine horizontal brushing: high-x / low-y turbulence, desaturated, subtle -->
    <feTurbulence type="fractalNoise" baseFrequency="0.012 0.9" numOctaves="2"
        seed="4" result="n"/>
    <feColorMatrix in="n" type="matrix"
        values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0.5 0" result="na"/>
    <feComponentTransfer in="na" result="grain">
      <feFuncA type="linear" slope="0.1"/>
    </feComponentTransfer>
    <feComposite in="grain" in2="SourceAlpha" operator="in" result="gm"/>
    <feMerge><feMergeNode in="SourceGraphic"/><feMergeNode in="gm"/></feMerge>
  </filter>
</defs></svg>`);
document.body.insertAdjacentHTML('beforeend', `
<div class="critter" id="critCat">
  <div class="bubble" id="catBubble"></div>
  <div class="prop" id="catProp"></div>
  <div class="critstage"><img class="metalimg" src="/crew/cat.png" alt=""><img class="metalimg blinkimg" src="/crew/cat_blink.png" alt=""><img class="metalimg walkimg" src="/crew/cat_walk.png" alt=""><img class="metalimg walk2img" src="/crew/cat_walk2.png" alt=""></div>
  <div class="mtag">NICO · ENGAGEMENT</div>
</div>
<div class="critter" id="critDog">
  <div class="bubble" id="dogBubble"></div>
  <div class="prop" id="dogProp"></div>
  <div class="critstage"><img class="metalimg" src="/crew/dog.png" alt=""><img class="metalimg blinkimg" src="/crew/dog_blink.png" alt=""><img class="metalimg walkimg" src="/crew/dog_walk.png" alt=""><img class="metalimg walk2img" src="/crew/dog_walk2.png" alt=""></div>
  <div class="mtag">SAGE · LAUNCHPAD</div>
</div>
<div class="critter" id="critKoala">
  <div class="bubble" id="koalaBubble"></div>
  <div class="prop" id="koalaProp"></div>
  <div class="critstage"><img class="metalimg" src="/crew/koala.png" alt=""><img class="metalimg blinkimg" src="/crew/koala_blink.png" alt=""><img class="metalimg walkimg" src="/crew/koala_walk.png" alt=""><img class="metalimg walk2img" src="/crew/koala_walk2.png" alt=""></div>
  <div class="mtag">KOA · BANKER & LEGAL</div>
</div>
<button id="mascotVoice" title="mascot voices on/off"></button>
<div id="catPanel" class="mpanel">
  <h3> Nico&rsquo;s reply desk <button onclick="this.closest('.mpanel').classList.remove('show')"> ✕ </button></h3>
  <div class="bigstat"><div><b id="cpReplies">–</b><span>replies 24h</span></div></div>
  <div id="cpLog"></div>
  <div class="note">The freshest replies, straight from the desk.</div>
</div>
<div id="dogPanel" class="mpanel">
  <h3> Sage&rsquo;s patrol log <button onclick="this.closest('.mpanel').classList.remove('show')"> ✕ </button></h3>
  <div class="bigstat"><div><b id="dpBlocked">–</b><span>blocked by Sage</span></div>
    <div><b id="dpSkips">–</b><span>skipped 24h total</span></div></div>
  <div id="dpLog"></div>
  <div class="note">Spam, hate &amp; junk is screened out before any reply is drafted, when Muse is available.</div>
</div>
<div id="ledgerPanel" class="mpanel">
  <h3> Koa&rsquo;s ledger <button onclick="document.getElementById('ledgerPanel').classList.remove('show')"> ✕ </button></h3>
  <table><thead><tr><th>Who</th><th>Today</th><th>7 days</th><th>30 days</th></tr></thead>
  <tbody id="ledgerRows"></tbody></table>
  <div class="note">Estimated from logged usage: token counts for Claude and the Muse chat calls that report them, flat per-call or per-hour rates for the rest — tracking since Oct 17. Billing dashboards are the source of truth.</div>
  <div style="display:flex; gap:8px; margin-top:10px; align-items:center">
    <input type="month" id="pdfMonth" style="flex:1; border:1.5px solid var(--border);
      border-radius:10px; padding:5px 8px; font-size:12px; background:var(--card); color:var(--ink)">
    <button onclick="window.open('/ledger.pdf?month='+(document.getElementById('pdfMonth').value||new Date().toISOString().slice(0,7)),'_blank')"
      style="border:0; background:var(--accent); color:#fff; font-weight:700; font-size:12px;
      border-radius:10px; padding:7px 12px; cursor:pointer"> PDF for my accountant</button>
  </div>
</div>`);
document.getElementById('pdfMonth').value = new Date().toISOString().slice(0,7);

function wander(id, speed, startX){
  const el = document.getElementById(id), svg = el.querySelector('.critstage') || el.querySelector('svg');
  let x = startX, dir = 1, mode = 'walk';
  el.style.left = x + 'px';
  svg.addEventListener('mouseenter', () => { el.dataset.hover = '1'; });
  svg.addEventListener('mouseleave', () => { el.dataset.hover = '0'; });
  setInterval(() => {
    if (el.dataset.talking === '1' || el.dataset.hover === '1') { el.classList.remove('walking'); return; }
    const seek = parseFloat(el.dataset.seek || 'NaN');
    if (!isNaN(seek)) {
      dir = seek > x ? 1 : -1;
      x += dir * speed * 2.4;
      el.style.left = x + 'px';
      svg.classList.toggle('flip', dir < 0);
      el.classList.add('walking');
      if (Math.abs(x - seek) < 12) { delete el.dataset.seek;
        if (el.id === 'critCat' && window._catArrive) window._catArrive(); }
      return;
    }
    if (mode !== 'walk') { el.classList.remove('walking'); return; }
    x += dir * speed;
    const max = Math.max(200, window.innerWidth - 140);
    if (x >= max) dir = -1;
    if (x <= 230) dir = 1; // keep clear of the sidebar
    el.style.left = x + 'px';
    svg.classList.toggle('flip', dir < 0);
    el.classList.add('walking');
  }, 40);
  (function mood(){ mode = Math.random() < 0.35 ? 'rest' : 'walk';
     setTimeout(mood, 2500 + Math.random() * 5000); })();
}
function liveLoop(id, blinkMs, hopChance){
  const stage = () => document.querySelector('#'+id+' .critstage');
  function blink(){
    const st = stage(); if (!st) return;
    st.classList.add('blinking');
    setTimeout(()=>st.classList.remove('blinking'), 150);
    // occasional double-blink
    if (Math.random()<0.3) setTimeout(()=>{ st.classList.add('blinking');
      setTimeout(()=>st.classList.remove('blinking'),150); }, 320);
  }
  (function loop(){ setTimeout(()=>{ blink(); loop(); }, blinkMs + Math.random()*2600); })();
  setInterval(() => {
    const el = document.getElementById(id), st = stage();
    if (!st || el.dataset.talking==='1' || Math.random()>hopChance) return;
    st.classList.remove('hop'); void st.offsetWidth; st.classList.add('hop');
  }, 5000 + Math.random()*4000);
}
liveLoop('critCat', 3200, .5); liveLoop('critDog', 4100, .5);
liveLoop('critKoala', 3600, .45);
wander('critCat', 1.1, 300);
wander('critDog', 1.45, 700);
wander('critKoala', 0.8, 500);

/* Koa the banker: reads the real spend ledger */
let lastBooks = null;
function fmt$(v){ return '$' + (v||0).toFixed(2); }
async function loadBudget(){
  try {
    const r = await fetch('/budget'); const b = await r.json();
    lastBooks = b;
    const w = document.getElementById('budgetWrap');
    if (w) {
      const rows = ['claude','muse'].map(pr => {
        const t = (b.today||{})[pr] || {cost:0, calls:0};
        const m = (b.month||{})[pr] || {cost:0, calls:0};
        const nm = {claude:' Claude (replies & vision)', muse:' Muse (logged calls)'}[pr];
        return `<div class="lgc lgc-mn">${nm}</div><div class="lgc lgc-mv">${fmt$(t.cost)}</div><div class="lgc lgc-md">today · ${t.calls} calls</div><div class="lgc lgc-30">${fmt$(m.cost)} / 30 days</div>`;
      }).join('');
      const tt = Object.values(b.today||{}).reduce((a,x)=>a+(x.cost||0),0);
      const mt = Object.values(b.month||{}).reduce((a,x)=>a+(x.cost||0),0);
      const earn = b.earned || {today:0, month:0};
      w.innerHTML = `<section><h2> Koa's books <span class="hint">what your AI crew costs</span></h2>
        <div class="ledgergrid">${rows}<div class="lgc lgc-mn"><b>Total</b></div><div class="lgc lgc-mv"><b>${fmt$(tt)}</b></div><div class="lgc lgc-md">today</div><div class="lgc lgc-30"><b>${fmt$(mt)}</b> / 30 days</div></div>
        <div class="empty" style="padding:6px 0 0">estimates from the ledger, tracked since Oct 17, 2026; Anthropic & Meta dashboards stay the source of truth</div></section>`;
    }
  } catch(e) {}
}
function koalaReport(){
  if (!lastBooks) return;
  const t = lastBooks.today || {};
  const tt = Object.values(t).reduce((a,x)=>a+(x.cost||0),0);
  const bits = ['claude','muse'].filter(p=>t[p]).map(p=>({claude:'Claude',muse:'Muse'}[p]) + ' ' + fmt$(t[p].cost));
  const earned = (lastBooks.earned || {}).today || 0;
  const line = earned > 0
    ? 'Books: earned ' + fmt$(earned) + ' today, estimated spend ' + fmt$(tt) + ' on the crew — estimated profit ' + fmt$(earned - tt) + ' '
    : (tt > 0 ? 'Books today: ' + bits.join(' · ') + ' — ' + fmt$(tt) + ' total ' : 'Books are clean — nothing spent yet today ');
  say('koala', line, false, ' ');
}
function renderLedger(){
  if (!lastBooks) return;
  const names = {claude:' Claude', muse:' Muse'};
  const tot = {today:0, week:0, month:0};
  const rows = ['claude','muse'].map(pr => {
    const cells = ['today','week','month'].map(k => {
      const v = ((lastBooks[k]||{})[pr]||{}).cost || 0; tot[k] += v; return v;
    });
    return `<tr><td>${names[pr]}</td>` + cells.map(v=>`<td>${fmt$(v)}</td>`).join('') + '</tr>';
  }).join('');
  const earn = lastBooks.earned || {today:0, week:0, month:0};
  document.getElementById('ledgerRows').innerHTML = rows +
    `<tr class="tot"><td>AI spend</td><td>${fmt$(tot.today)}</td><td>${fmt$(tot.week)}</td><td>${fmt$(tot.month)}</td></tr>` +
    `<tr><td> Earned (FB)</td><td>${fmt$(earn.today)}</td><td>${fmt$(earn.week)}</td><td>${fmt$(earn.month)}</td></tr>` +
    `<tr class="tot" style="color:var(--good)"><td>Profit</td><td>${fmt$(earn.today-tot.today)}</td><td>${fmt$(earn.week-tot.week)}</td><td>${fmt$(earn.month-tot.month)}</td></tr>`;
}
/* ---------- money grove ---------- */
/* money grove removed — keep catTend as a no-op so mascotFeed stays safe */
let treeByPost = {};
function catTend(){}
window._catArrive = function(){};

let lastStats = null, lastEvents = [];
let lastFeedTop = null;
function esc2(t){ return String(t||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function openPanel(id){
  document.querySelectorAll('.mpanel').forEach(x => { if (x.id !== id) x.classList.remove('show'); });
  const pnl = document.getElementById(id);
  const was = pnl.classList.contains('show');
  pnl.classList.toggle('show', !was);
  return !was;
}
function onCrit(id, fn){
  const el = document.querySelector('#'+id+' .critstage');
  if (el){ el.style.cursor='pointer'; el.addEventListener('click', fn); }
}
onCrit('critKoala', async () => {
  if (!openPanel('ledgerPanel')) return;
  await loadBudget(); renderLedger();
  say('koala', 'Here are the books! ', false, ' ');
});
onCrit('critCat', () => {
  if (!openPanel('catPanel')) return;
  if (lastStats) { document.getElementById('cpReplies').textContent = lastStats.replies24; }
  const replies = lastEvents.filter(e => e.kind==='REPLY').slice(-4).reverse();
  document.getElementById('cpLog').innerHTML = replies.map(e =>
    `<div class="logrow"><div class="c"> ${esc2(e.comment).slice(0,70)}</div>
     <div class="r">↳ ${esc2(e.detail).slice(0,90)}</div></div>`).join('')
    || '<div class="logrow">no replies yet in this window</div>';
  say('cat', 'My latest work — fresh off the paws ', false, ' ');
});
onCrit('critDog', () => {
  if (!openPanel('dogPanel')) return;
  const blocked = lastEvents.filter(e => e.kind==='SKIP' && (e.detail||'').includes('muse'));
  document.getElementById('dpBlocked').textContent = blocked.length;
  if (lastStats) document.getElementById('dpSkips').textContent = lastStats.skips24;
  document.getElementById('dpLog').innerHTML = blocked.slice(-4).reverse().map(e =>
    `<div class="logrow"><div class="c"> ${esc2(e.comment).slice(0,80)}</div></div>`).join('')
    || '<div class="logrow">nothing blocked recently — clean comment section </div>';
  say('dog', 'My patrol log — junk stops here ', false, ' ');
});
loadBudget(); setInterval(loadBudget, 60000);
setInterval(koalaReport, 180000); setTimeout(koalaReport, 15000);

let voiceOn = false;
try { voiceOn = localStorage.getItem('mascotVoice')==='1'; } catch(e) {}
const vBtn = document.getElementById('mascotVoice');
function setVoice(on){ voiceOn=on; vBtn.textContent = on?' ':' ';
  try{ localStorage.setItem('mascotVoice', on?'1':'0'); }catch(e){} }
setVoice(voiceOn);
vBtn.onclick = () => setVoice(!voiceOn);
const bubbleT = {};
function say(who, text, speak, prop){
  const b = document.getElementById(who+'Bubble');
  const p = document.getElementById(who+'Prop');
  const m = document.getElementById({cat:'critCat',dog:'critDog',koala:'critKoala'}[who]);
  b.textContent = text; b.classList.add('show');
  m.dataset.talking = '1'; // stop walking while talking
  m.classList.remove('talking'); void m.offsetWidth; m.classList.add('talking');
  if (prop) { p.textContent = prop; p.classList.add('show'); }
  clearTimeout(bubbleT[who]);
  bubbleT[who] = setTimeout(()=>{
    b.classList.remove('show'); p.classList.remove('show');
    m.dataset.talking = '0';
  }, 6000);
  if (voiceOn && speak && window.speechSynthesis) {
    const u = new SpeechSynthesisUtterance(text.replace(/[^\\p{L}\\p{N}\\s.,!?'—-]/gu,''));
    u.rate = 1.05; u.pitch = {cat:1.25, dog:0.8, koala:1.0, fox:1.1}[who];
    speechSynthesis.speak(u);
  }
}
let lastEvKey = null, mascotIdle;
function mascotFeed(events){
  if (!events.length) return;
  const e = events[events.length-1];
  const key = e.time + '|' + e.kind + '|' + (e.comment||'').slice(0,30);
  if (key === lastEvKey) return;
  const first = lastEvKey === null;
  lastEvKey = key;
  if (first) return; // don't replay history on page load
  engPresent(e.kind, e.detail);
  const postPart = (e.cid||'').split('_')[0];
  if ((e.kind==='REPLY' || e.kind==='LIKE') && treeByPost[postPart]) {
    catTend(treeByPost[postPart]);
    resetIdle();
    return;
  }
  if (e.kind==='REPLY' || e.kind==='DRYRUN')
    say('cat', 'Just replied to a fan: "' + (e.detail||'').slice(0,70) + '…"', true, ' ');
  else if (e.kind==='LIKE' || e.kind==='DRYLIKE')
    say('cat', 'Liked a sweet comment ', false, ' ');
  else if (e.kind==='SKIP' && (e.detail||'').includes('muse'))
    say('dog', 'Sniffed out junk and skipped it ', true, ' ');
  else if (e.kind==='STORY')
    say('dog', 'Posted a story from the queue ', true, ' ');
  resetIdle();
}
/* Engagement-page presenter: Nico writes the reply in real time, then it appears below */
let engTyper=null, engThinkT=null, engSettleT=null;
function engTypeOut(text, done){
  const pr = document.querySelector('.engpresenter');
  const sayEl = document.getElementById('engSay');
  clearInterval(engTyper); sayEl.textContent='';
  pr.classList.add('writing'); pr.classList.remove('busy'); // stop dots; caret + motion on
  pr.classList.add('busy'); // keep body rocking while typing
  let i=0;
  engTyper = setInterval(()=>{
    sayEl.textContent = text.slice(0, ++i);
    if (i >= text.length){ clearInterval(engTyper); pr.classList.remove('writing','busy'); if(done) done(); }
  }, 30);
}
function engHeartPop(){
  const h = document.getElementById('engHeart'); if(!h) return;
  h.classList.remove('pop'); void h.offsetWidth; h.classList.add('pop');
}
function engPresent(kind, detail){
  const pr = document.querySelector('.engpresenter'); if (!pr) return;
  const sayEl = document.getElementById('engSay');
  clearTimeout(engSettleT); clearTimeout(engThinkT); clearInterval(engTyper);
  pr.classList.remove('writing');
  if (kind==='REPLY' || kind==='DRYRUN'){
    pr.classList.add('busy','pulse'); sayEl.textContent=' writing a reply';
    setTimeout(()=>pr.classList.remove('pulse'), 220);
    engThinkT = setTimeout(()=>{
      const txt = ' "' + (detail||'').replace(/\\s+/g,' ').trim() + '"';
      engTypeOut(txt);
    }, 650);
  } else if (kind==='LIKE' || kind==='DRYLIKE'){
    pr.classList.add('busy','pulse'); sayEl.textContent=' liking a sweet comment';
    engHeartPop(); setTimeout(()=>pr.classList.remove('pulse'), 220);
    engSettleT = setTimeout(()=>pr.classList.remove('busy'), 1300);
  } else if (kind==='SKIP'){
    pr.classList.remove('busy'); sayEl.textContent=' skipped one — kept the page clean';
  } else if (kind==='STORY'){
    pr.classList.remove('busy'); sayEl.textContent=' posted a story';
  }
}
/* idle blink for every zoomed-in presenter (Nico, Koa, …) */
setInterval(()=>{
  document.querySelectorAll('.engcatwrap').forEach(w=>{
    if (Math.random() < 0.5){ w.classList.add('blink'); setTimeout(()=>w.classList.remove('blink'), 150); }
  });
}, 3600);

/* ---- Koa the banker narrates the Analytics page ---- */
let koaTyper=null, koaThinkT=null, koaRotate=null;
function koaTypeOut(text){
  const pr=document.getElementById('koaPresenter'), sayEl=document.getElementById('koaSay');
  if(!pr||!sayEl) return;
  clearInterval(koaTyper); sayEl.textContent=''; pr.classList.add('writing','busy');
  let i=0;
  koaTyper=setInterval(()=>{ sayEl.textContent=text.slice(0,++i);
    if(i>=text.length){ clearInterval(koaTyper); pr.classList.remove('writing','busy'); } }, 28);
}
function koaPresent(text){
  const pr=document.getElementById('koaPresenter'), sayEl=document.getElementById('koaSay');
  if(!pr||!sayEl) return;
  clearTimeout(koaThinkT); clearInterval(koaTyper);
  pr.classList.remove('writing'); pr.classList.add('busy','pulse');
  sayEl.textContent=' checking the books';
  setTimeout(()=>pr.classList.remove('pulse'),220);
  koaThinkT=setTimeout(()=>koaTypeOut(text), 520);
}
function koaBookLine(){
  if (!lastBooks) return ' Pulling the ledger together…';
  const t=lastBooks.today||{}; const tt=Object.values(t).reduce((a,x)=>a+(x.cost||0),0);
  const mt=Object.values(lastBooks.month||{}).reduce((a,x)=>a+(x.cost||0),0);
  const earnT=(lastBooks.earned||{}).today||0, earnM=(lastBooks.earned||{}).month||0;
  const lines=[];
  if (earnT>0 && tt>0) lines.push(' Today: earned '+fmt$(earnT)+', estimated spend '+fmt$(tt)+' on the crew — estimated net '+fmt$(earnT-tt));
  else if (tt>0) lines.push(' Spent an estimated '+fmt$(tt)+' running the crew today');
  else lines.push(' No AI usage logged yet today');
  if (earnM>0) lines.push(' '+fmt$(earnM)+' earned over 30 days vs '+fmt$(mt)+' in estimated AI costs');
  return lines[Math.floor(Math.random()*lines.length)];
}
const koaIdleLines=[' crunching the reach numbers…',' balancing the books',' reading the fine print',
  ' quick eucalyptus break',' keeping the page in good standing','never sign perpetual usage rights, boss'];
function startKoaRotate(){
  clearInterval(koaRotate);
  koaPresent(koaBookLine());
  koaRotate=setInterval(()=>{
    koaPresent(Math.random()<0.6 ? koaBookLine() : koaIdleLines[Math.floor(Math.random()*koaIdleLines.length)]);
  }, 13000);
}
function stopKoaRotate(){ clearInterval(koaRotate); }
const catIdle = ['watching the comments roll in ','paws ready on the keyboard',
  'rereading your voice notes ','thinking up the perfect reply…'];
const dogIdle = ['sniffing for spam…','tail wagging — all clear ',
  'guarding the comment section ','nose down, on patrol'];
const koalaIdle = ['counting coins…','balancing the books ','quick eucalyptus break ',
  'auditing the pennies','reading fine print ','never sign perpetual usage rights, boss'];
function resetIdle(){
  clearInterval(mascotIdle);
  mascotIdle = setInterval(()=>{
    const roll = Math.random();
    if (roll < 0.4) say('cat', catIdle[Math.floor(Math.random()*catIdle.length)], false);
    else if (roll < 0.7) say('dog', dogIdle[Math.floor(Math.random()*dogIdle.length)], false);
    else say('koala', koalaIdle[Math.floor(Math.random()*koalaIdle.length)], false);
    const pr = document.querySelector('.engpresenter');
    if (pr && !pr.classList.contains('busy'))
      document.getElementById('engSay').textContent = catIdle[Math.floor(Math.random()*catIdle.length)];
  }, 45000);
}
resetIdle();
tick(); loadPages(); loadEngagement(); loadVideos(true); loadProfile(); loadHomeDash(); loadCrew3D();
showView('home');  // apply Home's crew-grid + hide roaming duplicates on first load
setTimeout(updateCrew, 1400);
fetch('/session',{cache:'no-store'}).then(function(r){return r.json();}).then(renderBanner).catch(function(){});
function renderBanner(s){
  var old=document.getElementById('fbBanner'); if(old) old.remove();
  var needFb = !s.signed_in && !s.judge;
  var needKeys = s.judge ? !(s.visitor_muse && s.visitor_claude) : !s.keys_saved;
  if(!needFb && !needKeys) return;
  var b=document.createElement('div'); b.id='fbBanner'; b.className='fbbanner';
  var t=document.createElement('span');
  t.textContent = s.judge ? 'Sample data for demonstration: the numbers are invented.'
    : (needFb ? 'Your stats are private. Sign in with Facebook to see them (refresh after you sign in).'
              : 'Connect your AI keys to try the plans and Hand to Sage.');
  b.appendChild(t);
  if(needFb){
    var fb=document.createElement('a'); fb.className='btn green'; fb.href='/connect'; fb.target='_blank'; fb.rel='noopener';
    fb.textContent='Sign in with Facebook'; b.appendChild(fb);
  }
  if(needKeys){
    var kr=document.createElement('div'); kr.style.display='flex'; kr.style.gap='8px'; kr.style.alignItems='center'; kr.style.flexWrap='wrap';
    if(!s.judge){
      var ck=document.createElement('a'); ck.className='btn green'; ck.href='/connect/ai'; ck.target='_blank'; ck.rel='noopener';
      ck.textContent='Connect your AI keys (saved on this computer)'; kr.appendChild(ck);
    } else {
      var kt=document.createElement('span');
      kt.textContent='Try the plans and Hand to Sage with your own keys (kept only in this browser session, never saved):';
      kr.appendChild(kt);
      var inpM=document.createElement('input'); inpM.type='password'; inpM.placeholder=s.visitor_muse ? 'Muse key saved for this session' : 'Muse API key'; inpM.autocomplete='off'; inpM.style.minWidth='200px';
      var inpC=document.createElement('input'); inpC.type='password'; inpC.placeholder=s.visitor_claude ? 'Claude key saved for this session' : 'Claude API key (Hand to Sage)'; inpC.autocomplete='off'; inpC.style.minWidth='200px';
      var go=document.createElement('button'); go.className='btn'; go.textContent='Save keys';
      var msg=document.createElement('span'); msg.style.color='var(--ink3)';
      go.onclick=function(){
        var body={};
        if(inpM.value.trim()) body.muse=inpM.value.trim();
        if(inpC.value.trim()) body.claude=inpC.value.trim();
        if(!body.muse && !body.claude){ msg.textContent='Paste at least one key.'; return; }
        go.disabled=true; msg.textContent='Checking the keys...';
        fetch('/visitor/keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),cache:'no-store'})
          .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); })
          .then(function(x){ inpM.value=''; inpC.value=''; go.disabled=false; if(x.ok){ refreshFbBanner(); } else { msg.textContent=(x.j && x.j.error) || 'Those keys were not accepted.'; } })
          .catch(function(){ inpM.value=''; inpC.value=''; go.disabled=false; msg.textContent='Could not reach Abra.'; });
      };
      kr.appendChild(inpM); kr.appendChild(inpC); kr.appendChild(go);
      if(s.visitor_muse || s.visitor_claude){
        var clr=document.createElement('button'); clr.className='btn'; clr.textContent='Remove keys';
        clr.onclick=function(){ fetch('/visitor/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',cache:'no-store'}).then(refreshFbBanner).catch(function(){}); };
        kr.appendChild(clr);
      }
      kr.appendChild(msg);
    }
    b.appendChild(kr);
  }
  document.body.appendChild(b);
}
function refreshFbBanner(){
  fetch('/session',{cache:'no-store'}).then(function(r){return r.json();}).then(renderBanner).catch(function(){});
}
setInterval(tick, 5000);
function dismissHint(){ const h=document.getElementById('assistHint'); if(h) h.classList.remove('show'); try{sessionStorage.setItem('abraHintSeen','1');}catch(e){} }
(function(){
  try{ if(sessionStorage.getItem('abraHintSeen')) return; }catch(e){}
  setTimeout(function(){ const h=document.getElementById('assistHint'), pn=document.getElementById('assistPanel');
    if(h && pn && pn.classList.contains('assist-hidden')){ h.classList.add('show'); setTimeout(dismissHint, 7500); } }, 2800);
})();
setInterval(loadEngagement, 30000);
setInterval(() => { const v = document.getElementById('view-analytics');
  if (v && v.classList.contains('active')) { loadTrend(); loadBudget(); } }, 30000);
setInterval(() => { if (!staged.size) loadVideos(true); }, 120000);

// ---- floating Abra concierge ----
let assistHistory = []; let assistBusy = false;
const ASSIST_CHIPS = ['How does Abra work?','What should I post next?','How is my reach?','Handle my comments','Show my schedule'];
// ===== living backdrop: a slow flowing liquid-steel field behind the whole app =====
(function initBg(){
  var cv=document.getElementById('bgCanvas'); if(!cv) return;
  var gl=null; try{ gl=cv.getContext('webgl',{alpha:false,antialias:false,powerPreference:'low-power'}); }catch(e){}
  if(!gl){ cv.style.display='none'; return; }
  var vs='attribute vec2 p;void main(){gl_Position=vec4(p,0.0,1.0);}';
  var fs=[
  'precision mediump float;',
  'uniform vec2 u_res; uniform float u_time; uniform vec2 u_mouse;',
  'float hash(vec3 p){p=fract(p*0.3183099+0.1);p*=17.0;return fract(p.x*p.y*p.z*(p.x+p.y+p.z));}',
  'float noise(vec3 x){vec3 i=floor(x),f=fract(x);f=f*f*(3.0-2.0*f);',
  ' return mix(mix(mix(hash(i+vec3(0.,0.,0.)),hash(i+vec3(1.,0.,0.)),f.x),mix(hash(i+vec3(0.,1.,0.)),hash(i+vec3(1.,1.,0.)),f.x),f.y),',
  '            mix(mix(hash(i+vec3(0.,0.,1.)),hash(i+vec3(1.,0.,1.)),f.x),mix(hash(i+vec3(0.,1.,1.)),hash(i+vec3(1.,1.,1.)),f.x),f.y),f.z);}',
  'float fbm(vec3 p){float a=0.5,s=0.0;for(int i=0;i<4;i++){s+=a*noise(p);p*=2.02;a*=0.5;}return s;}',
  'void main(){vec2 uv=gl_FragCoord.xy/u_res; vec2 p=uv*2.0-1.0; p.x*=u_res.x/u_res.y;',
  ' float t=u_time*0.028;',
  ' vec2 q=vec2(fbm(vec3(p*1.1,t)),fbm(vec3(p*1.1+5.0,t)));',
  ' float f=fbm(vec3(p*1.5+q*1.25+u_mouse*0.12,t*1.1));',
  ' vec3 c1=vec3(0.955,0.972,1.0),c2=vec3(0.84,0.88,0.95),c3=vec3(0.76,0.81,0.91);',
  ' vec3 col=mix(c2,c1,smoothstep(0.28,0.72,f));',
  ' col=mix(col,c3,smoothstep(0.58,0.96,fbm(vec3(p*0.75-q,t))));',
  ' float acc=smoothstep(0.7,0.0,length(p-vec2(sin(t*1.9)*0.6,cos(t*1.6)*0.42)));',
  ' col=mix(col,vec3(0.80,0.86,1.0),acc*0.22);',
  ' gl_FragColor=vec4(col,1.0);}'].join(' ');
  function sh(ty,src){var s=gl.createShader(ty);gl.shaderSource(s,src);gl.compileShader(s);return s;}
  var pr=gl.createProgram(); gl.attachShader(pr,sh(gl.VERTEX_SHADER,vs)); gl.attachShader(pr,sh(gl.FRAGMENT_SHADER,fs));
  gl.linkProgram(pr); gl.useProgram(pr);
  var bf=gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER,bf);
  gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
  var lo=gl.getAttribLocation(pr,'p'); gl.enableVertexAttribArray(lo); gl.vertexAttribPointer(lo,2,gl.FLOAT,false,0,0);
  var uR=gl.getUniformLocation(pr,'u_res'),uT=gl.getUniformLocation(pr,'u_time'),uM=gl.getUniformLocation(pr,'u_mouse');
  var mx=0,my=0,tmx=0,tmy=0,st=performance.now(),W=2,H=2;
  function resize(){ var s=Math.min(window.devicePixelRatio||1,1.3);
    W=Math.max(2,Math.round(Math.min(window.innerWidth,1200)*s));
    H=Math.max(2,Math.round(window.innerHeight/Math.max(1,window.innerWidth)*W));
    if(cv.width!==W||cv.height!==H){ cv.width=W; cv.height=H; } gl.viewport(0,0,W,H); }
  window.addEventListener('mousemove',function(e){ tmx=(e.clientX/window.innerWidth*2-1); tmy=-(e.clientY/window.innerHeight*2-1); },{passive:true});
  window.addEventListener('resize',resize);
  function frame(){ if(document.hidden){ requestAnimationFrame(frame); return; }
    resize(); mx+=(tmx-mx)*0.03; my+=(tmy-my)*0.03;
    gl.uniform2f(uR,W,H); gl.uniform1f(uT,(performance.now()-st)/1000); gl.uniform2f(uM,mx,my);
    gl.drawArrays(gl.TRIANGLES,0,3); requestAnimationFrame(frame); }
  resize(); requestAnimationFrame(frame);
})();
// ===== liquid-chrome WebGL orb =====
(function initOrb(){
  var canvas=document.getElementById('orbCanvas'); if(!canvas) return;
  var gl=null; try{ gl=canvas.getContext('webgl',{premultipliedAlpha:true,alpha:true,antialias:true})||canvas.getContext('experimental-webgl'); }catch(e){}
  if(!gl){ canvas.style.display='none'; var im0=document.getElementById('assistBtnImg'); if(im0){ im0.style.display='block'; im0.src='/crew/orb.png'; } return; }
  var vsrc='attribute vec2 p;void main(){gl_Position=vec4(p,0.0,1.0);}';
  var fsrc=[
  'precision highp float;',
  'uniform vec2 u_res; uniform float u_time; uniform float u_speak; uniform vec2 u_point; uniform sampler2D u_tex;',
  'float hash(vec3 p){p=fract(p*0.3183099+0.1);p*=17.0;return fract(p.x*p.y*p.z*(p.x+p.y+p.z));}',
  'float noise(vec3 x){vec3 i=floor(x),f=fract(x);f=f*f*(3.0-2.0*f);',
  ' return mix(mix(mix(hash(i+vec3(0.,0.,0.)),hash(i+vec3(1.,0.,0.)),f.x),mix(hash(i+vec3(0.,1.,0.)),hash(i+vec3(1.,1.,0.)),f.x),f.y),',
  '            mix(mix(hash(i+vec3(0.,0.,1.)),hash(i+vec3(1.,0.,1.)),f.x),mix(hash(i+vec3(0.,1.,1.)),hash(i+vec3(1.,1.,1.)),f.x),f.y),f.z);}',
  'float fbm(vec3 p){float a=0.5,s=0.0;for(int i=0;i<4;i++){s+=a*noise(p);p*=2.02;a*=0.5;}return s;}',
  'void main(){vec2 uv=(gl_FragCoord.xy*2.0-u_res)/min(u_res.x,u_res.y);',
  ' vec3 ro=vec3(u_point*0.12,2.6); vec3 rd=normalize(vec3(uv,-1.8));',
  ' float b=dot(ro,rd),c=dot(ro,ro)-1.0,disc=b*b-c; if(disc<0.0){gl_FragColor=vec4(0.0);return;}',
  ' float hh=sqrt(disc); vec3 pos=ro+rd*(-b-hh); vec3 n=normalize(pos);',
  // liquid ripple: perturb the surface normal so the reflection flows like metal
  ' float amp=0.05+0.22*u_speak+0.015*sin(u_time*1.6); float tt=u_time*(0.3+0.8*u_speak);',
  ' vec3 q=pos*2.4+vec3(0.0,0.0,tt);',
  ' float d1=fbm(q),d2=fbm(q+vec3(5.2,1.3,2.7)),d3=fbm(q+2.0);',
  ' n=normalize(n+amp*vec3(d1-0.5,d2-0.5,d3-0.5));',
  // sample the orb render as a matcap (gentle sway keeps it alive), so it matches the art exactly
  ' float ra=0.05*sin(u_time*0.4)+u_point.x*0.05; float cs=cos(ra),sn=sin(ra);',
  ' vec2 nn=mat2(cs,-sn,sn,cs)*n.xy; vec2 muv=nn*0.5+0.5;',
  ' vec3 col=texture2D(u_tex,muv).rgb;',
  ' float fres=pow(1.0-max(dot(n,-rd),0.0),4.0); col+=fres*vec3(0.5,0.62,0.95)*0.22;',
  ' float alpha=smoothstep(0.0,0.03,disc); gl_FragColor=vec4(col*alpha,alpha);}'].join(' ');
  function sh(ty,src){var s=gl.createShader(ty);gl.shaderSource(s,src);gl.compileShader(s);
    if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)) console.warn('orb',gl.getShaderInfoLog(s)); return s;}
  var prog=gl.createProgram(); gl.attachShader(prog,sh(gl.VERTEX_SHADER,vsrc)); gl.attachShader(prog,sh(gl.FRAGMENT_SHADER,fsrc));
  gl.linkProgram(prog); gl.useProgram(prog);
  var buf=gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER,buf);
  gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
  var loc=gl.getAttribLocation(prog,'p'); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc,2,gl.FLOAT,false,0,0);
  var uRes=gl.getUniformLocation(prog,'u_res'),uTime=gl.getUniformLocation(prog,'u_time'),
      uSpeak=gl.getUniformLocation(prog,'u_speak'),uPoint=gl.getUniformLocation(prog,'u_point'),
      uTex=gl.getUniformLocation(prog,'u_tex');
  // the orb render itself, used as the sphere's material (matcap) -> matches the art exactly
  var tex=gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D,tex);
  gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,1,1,0,gl.RGBA,gl.UNSIGNED_BYTE,new Uint8Array([150,165,190,255]));
  gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
  var oimg=new Image(); oimg.onload=function(){ gl.bindTexture(gl.TEXTURE_2D,tex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL,true);
    gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,oimg); };
  oimg.src='/crew/orb.png';
  gl.enable(gl.BLEND); gl.blendFunc(gl.ONE,gl.ONE_MINUS_SRC_ALPHA);
  var dpr=Math.min(window.devicePixelRatio||1,2)*1.4, S=64, W=Math.round(S*dpr);
  canvas.width=W; canvas.height=W; gl.viewport(0,0,W,W);
  var speakCur=0,speakTarget=0,px=0,py=0,ptx=0,pty=0,start=performance.now();
  window.orbSpeak=function(on){ speakTarget=on?1.0:0.0; };
  window.addEventListener('mousemove',function(e){ var r=canvas.getBoundingClientRect(); if(!r.width) return;
    var cx=r.left+r.width/2,cy=r.top+r.height/2;
    ptx=Math.max(-1,Math.min(1,(e.clientX-cx)/240)); pty=Math.max(-1,Math.min(1,-(e.clientY-cy)/240)); },{passive:true});
  function frame(){ speakCur+=(speakTarget-speakCur)*0.08; px+=(ptx-px)*0.06; py+=(pty-py)*0.06;
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D,tex); gl.uniform1i(uTex,0);
    gl.uniform2f(uRes,W,W); gl.uniform1f(uTime,(performance.now()-start)/1000);
    gl.uniform1f(uSpeak,speakCur); gl.uniform2f(uPoint,px,py);
    gl.drawArrays(gl.TRIANGLES,0,3); requestAnimationFrame(frame); }
  requestAnimationFrame(frame);
})();
// speaking state → orb ripples harder + character bobs while talking
var _talkTimer=null;
function setSpeaking(on){
  if(window.orbSpeak) window.orbSpeak(!!on);
  ['assistBtnImg','assistOrbSm'].forEach(function(id){ var im=document.getElementById(id);
    if(im) im.classList.toggle('speaking', !!on && im.classList.contains('ischar')); });
}
function talkBurst(ms){ setSpeaking(true); if(_talkTimer) clearTimeout(_talkTimer);
  _talkTimer=setTimeout(function(){ setSpeaking(false); }, ms||1600); }
// swap in true 3D models for any crew member whose GLB has been added to assets/
// Flip to true to swap the Home crew cards to the 3D Meshy models. Off for now —
// the 2D renders (crew_*.png) have the exact texture; the 3D pipeline (GLBs in
// assets/, /crew3d routes, <model-viewer> tags) all stays wired, ready to re-enable.
var USE_CREW_3D = false;
function loadCrew3D(){
  if(!USE_CREW_3D) return;
  fetch('/crew3d/list').then(function(r){return r.json();}).then(function(have){
    var any=false;
    document.querySelectorAll('.crew3d').forEach(function(mv){ var k=mv.dataset.glb;
      if(have[k]){ mv.setAttribute('src','/crew3d/crew_'+k+'.glb');
        // all four keep Meshy's native muted chrome fur (no mirror override)
        var card=mv.closest('.crewcard'); if(card) card.classList.add('has3d'); any=true; } });
    return any;
  }).catch(function(){});
}
// ===== crew come alive: autonomous blink/wave + 3D cursor parallax =====
(function crewLife(){
  var cards=[].slice.call(document.querySelectorAll('.crewgrid .crewcard')); if(!cards.length) return;
  function blink(card){ card.classList.add('blinking'); setTimeout(function(){ card.classList.remove('blinking'); }, 150); }
  function wave(card){ if(card.matches(':hover')) return; card.classList.add('waving'); setTimeout(function(){ card.classList.remove('waving'); }, 1600); }
  setInterval(function(){ if(document.hidden) return;
    var card=cards[Math.floor(Math.random()*cards.length)];
    if(card.classList.contains('waving')||card.classList.contains('blinking')) return;
    if(Math.random()<0.25) wave(card); else blink(card);
  }, 2100);
})();
(function crewParallax(){
  var grid=document.querySelector('.crewgrid'); if(!grid) return;
  var cards=[].slice.call(grid.querySelectorAll('.crewcard'));
  grid.addEventListener('mousemove', function(e){
    for(var i=0;i<cards.length;i++){ var card=cards[i];
      if(card.classList.contains('has3d')) continue;   // real 3D model handles its own motion
      var r=card.getBoundingClientRect();
      var dx=(e.clientX-(r.left+r.width/2))/r.width, dy=(e.clientY-(r.top+r.height/2))/r.height;
      var hov=card.matches(':hover');
      card.style.transform='rotateX('+(-dy*7).toFixed(2)+'deg) rotateY('+(dx*9).toFixed(2)+'deg) translateY('+(hov?-5:0)+'px) translateZ('+(hov?22:0)+'px)';
      var g=card.querySelector('.crewglow');
      if(g){ var gr=g.getBoundingClientRect();
        var gx=Math.max(-20,Math.min(120,(e.clientX-gr.left)/gr.width*100));
        var gy=Math.max(-20,Math.min(120,(e.clientY-gr.top)/gr.height*100));
        g.style.setProperty('--gx',gx.toFixed(1)+'%'); g.style.setProperty('--gy',gy.toFixed(1)+'%'); } }
  }, {passive:true});
  grid.addEventListener('mouseleave', function(){ for(var i=0;i<cards.length;i++){ cards[i].style.transform='';
    var g=cards[i].querySelector('.crewglow'); if(g){ g.style.setProperty('--gx','46%'); g.style.setProperty('--gy','26%'); } } }, {passive:true});
})();
(function kpiTilt(){
  var row=document.getElementById('homeKpis'); if(!row) return;
  var cards=[].slice.call(row.querySelectorAll('.kpi'));
  row.addEventListener('mousemove',function(e){
    for(var i=0;i<cards.length;i++){ var c=cards[i], r=c.getBoundingClientRect();
      var dx=(e.clientX-(r.left+r.width/2))/r.width, dy=(e.clientY-(r.top+r.height/2))/r.height;
      c.style.transform='perspective(680px) rotateX('+(-dy*8).toFixed(2)+'deg) rotateY('+(dx*10).toFixed(2)+'deg) translateY(-2px)'; }
  },{passive:true});
  row.addEventListener('mouseleave',function(){ for(var i=0;i<cards.length;i++) cards[i].style.transform=''; },{passive:true});
})();
function toggleAssist(force){
  const p = document.getElementById('assistPanel');
  const show = (force===undefined) ? p.classList.contains('assist-hidden') : force;
  p.classList.toggle('assist-hidden', !show);
  if (show){
    const _h=document.getElementById('assistHint'); if(_h) _h.classList.remove('show');
    updateSoundUI();
    if (!assistHistory.length){
      assistAppend('muse', 'Hi! I am Abra, your crew concierge. Tell me what you need — type it and I will answer out loud.');
      renderChips();
    }
    setTimeout(()=>{ const t=document.getElementById('assistText'); if(t) t.focus(); }, 60);
  } else { stopVoiceChat(); }
}
function renderChips(){
  const c=document.getElementById('assistChips'); if(!c) return;
  c.innerHTML = ASSIST_CHIPS.map(function(t){ return '<button class="assist-chip" onclick="assistChip(this.textContent)">'+t+'</button>'; }).join('');
}
function assistChip(t){ warmupSpeech(); const c=document.getElementById('assistChips'); if(c) c.innerHTML=''; sendAssist(t); }
function assistAppend(who, text){
  const m = document.getElementById('assistMsgs'); const d = document.createElement('div');
  d.className = 'assist-msg ' + (who==='me'?'me':'muse'); d.textContent = text;
  m.appendChild(d); m.scrollTop = m.scrollHeight; return d;
}
function assistKey(e){ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); assistSend(); } }
// ---- Voice control: say "Abra" and a command to run it in the app. Off until the Voice button is turned on. ----
// Commands run existing functions and call no AI. Anything else after "Abra" is a question for the concierge.
var VOICE={on:false, rec:null, pending:null, restartT:null, speaking:false, speakT:null, lastQ:0, armedUntil:0, errs:0};
// Wake word "abra" (also heard as "a bra", "ebra", "obra", "sabra"). Text is lowercased, punctuation becomes spaces.
var VOICE_FILLER=/(^|[^a-z'])(please|now|the|my|me|just|okay|ok|hey|can you|could you|would you|for me)(?![a-z'])/g;
function voiceNorm(raw){ return (raw||'').toLowerCase().replace(/[^a-z0-9:' -]/g,' ').replace(/ +/g,' ').trim(); }
function voiceStrip(s){ return s.replace(VOICE_FILLER,'$1 ').replace(/ +/g,' ').trim(); }
// Returns {cmd, wake} for a command, {cmd:'', wake:true} for a bare wake word, or null to ignore the speech.
function voiceRawTail(raw){
  var r=(raw||'').match(/(^|[^a-zA-Z])(abra|a bra|ebra|obra|sabra)(?![a-zA-Z])/i);
  return r ? raw.slice(r.index+r[0].length).replace(/^[ ,.:;!?-]+/,'').trim() : '';
}
// Returns {cmd, wake, raw} for a command, {cmd:'', wake:true} for a bare wake word, or null to ignore the speech.
function voiceParse(raw){
  var t=voiceNorm(raw); if(!t) return null;
  var m=t.match(/(^| )(abra|a bra|ebra|obra|sabra)( |$)/);
  if(m){
    var tail=voiceRawTail(raw), strong=(m[2]==='abra');
    var cmd=voiceStrip(t.slice(m.index+m[0].length));
    if(!cmd){ VOICE.armedUntil=Date.now()+8000; return {cmd:'', wake:true, strong:strong, raw:tail}; }
    VOICE.armedUntil=0;
    return {cmd:cmd, wake:true, strong:strong, raw:tail};
  }
  // after a bare "Abra", the next phrase within 8 seconds is the command (never a confirm)
  if(VOICE.armedUntil && Date.now()<VOICE.armedUntil){
    VOICE.armedUntil=0;
    var c2=voiceStrip(t);
    return c2 ? {cmd:c2, wake:false, strong:false, raw:(raw||'').replace(/^[ ,.:;!?-]+/,'').trim()} : null;
  }
  // while a take is recording, the strong recording phrases work without "Abra"
  if(voiceRecordingNow()){
    var c4=voiceStrip(t);
    if(c4 && VOICE_OPEN_RECORDING.test(c4)) return {cmd:c4, wake:false, strong:false, raw:(raw||'').trim()};
  }
  return null;
}
function voiceMatch(cmd){
  for(var i=0;i<VOICE_CMDS.length;i++){ var c=VOICE_CMDS[i]; if(c.test ? c.test(cmd) : c.re.test(cmd)) return c; }
  return null;
}
function voiceIsQuestion(s){ return /^(what|how|why|when|where|who|which|can|could|should|would|is|are|do|does|did|tell|explain|suggest|recommend|write|draft|summarize|give|find)( |$)/.test(s); }
function voiceHandle(raw){
  var p=voiceParse(raw); if(!p) return;
  if(!p.cmd){ voiceAck('Yes?'); return; }
  var hit=voiceMatch(p.cmd);
  if(hit){
    if(hit.wake && !(p.wake && p.strong)){ voiceShow('Say "Abra" and the command together, for example "Abra '+hit.label+'".'); return; }
    voiceShow('Heard: '+p.cmd);
    Promise.resolve().then(function(){ return hit.run(p.cmd); }).then(function(msg){ voiceAck(msg||'Done.'); },
      function(e){ voiceAck('That did not work. '+String((e&&e.message)||e).slice(0,80)); });
    return;
  }
  if(voiceIsQuestion(p.cmd)){
    if(assistBusy){ voiceShow('Still answering the last question.'); return; }
    var now=Date.now();
    if(now-VOICE.lastQ<10000){ voiceShow('One question at a time. Try again in a moment.'); return; }
    VOICE.lastQ=now; voiceShow('Asking Abra: '+p.cmd); sendAssist(p.cmd, {speak:true});
    return;
  }
  // anything else: a button, tab, switch, option or field on the current screen
  voiceShow('Heard: '+p.cmd);
  Promise.resolve().then(function(){ return voiceDoByName(p.cmd, p.raw); }).then(function(msg){
    voiceAck(msg===null ? 'I did not find that on screen. Say help for the commands.' : msg);
  }, function(e){ voiceAck('That did not work. '+String((e&&e.message)||e).slice(0,80)); });
}
// Visible feedback: a short toast (not part of the top bar, so the switch stays a plain toggle).
function voiceShow(text){
  var el=document.getElementById('voiceToast');
  if(!el){ el=document.createElement('div'); el.id='voiceToast'; el.setAttribute('role','status'); el.setAttribute('aria-live','polite'); document.body.appendChild(el); }
  el.textContent=text; el.classList.add('show'); clearTimeout(el._t);
  el._t=setTimeout(function(){ el.classList.remove('show'); }, 4200);
}
function voiceUi(on){ document.querySelectorAll('.voice-switch').forEach(function(b){ b.classList.toggle('on',!!on); b.setAttribute('aria-checked', on?'true':'false'); b.title=on?'Abra mic on':'Abra mic off'; }); }
function voiceAck(text){
  voiceShow(text);
  noteSpoken(text);
  try{
    if(!window.speechSynthesis){ return; }
    voiceSpeakStart();
    speechSynthesis.cancel();
    var u=new SpeechSynthesisUtterance(text); u.rate=1.05;
    u.onend=u.onerror=function(){ voiceSpeakEnd(); };
    speechSynthesis.speak(u);
  }catch(e){ voiceSpeakEnd(); }
}
// words Abra just said, so the same words coming back through the microphone are not treated as a command
var _echoWords={}, _echoAt=0;
function noteSpoken(text){
  _echoWords={}; _echoAt=Date.now();
  voiceNorm(text).split(' ').forEach(function(w){ if(w.length>2 && w!=='abra') _echoWords[w]=1; });
}
function isEcho(text){
  if(Date.now()-_echoAt>15000) return false;
  var ws=voiceNorm(text).split(' ').filter(function(w){ return w.length>2 && w!=='abra'; });
  if(!ws.length) return false;
  var n=ws.filter(function(w){ return _echoWords[w]; }).length;
  return n/ws.length>=0.6;
}
function voiceSpeakStart(){
  VOICE.speaking=true;
  clearTimeout(VOICE.speakT);
  VOICE.speakT=setTimeout(voiceSpeakEnd, 12000);                 // watchdog: some browsers never fire onend
}
function voiceSpeakEnd(){
  clearTimeout(VOICE.speakT); VOICE.speaking=false;
  if(VOICE.on && !VOICE.rec) voiceStart();
}
function voiceOnError(err){
  if(err==='service-not-allowed'){ voiceStop(); voiceShow('Speech recognition is not available in this browser. Type instead.'); return; }
  if(err==='not-allowed'){ voiceStop(); voiceShow('Microphone blocked. Allow it in your browser to use voice.'); return; }
  if(err==='audio-capture'){ voiceStop(); voiceShow('No microphone found.'); return; }
  if(err==='no-speech' || err==='aborted') return;               // normal: nothing was said, or we stopped it
  VOICE.errs++;
  if(VOICE.errs>=4){ voiceStop(); voiceShow('Voice stopped: the speech service keeps failing ('+err+'). Turn it on to try again.'); }
}
function voiceStart(){
  if(!ABRA_VOICE_ON) return;
  var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){ VOICE.on=false; voiceUi(false); voiceShow('Voice needs Chrome or Edge on a computer.'); return false; }
  if(!window.isSecureContext){ VOICE.on=false; voiceUi(false); voiceShow('Voice needs a secure (https) link on phones. This link is plain http.'); return false; }
  if(VOICE.rec) return true;
  if(voiceChatOn){ VOICE.on=false; voiceUi(false); voiceShow('Turn off the chat mic first, then turn voice on.'); return false; }
  var r=new SR(); r.continuous=true; r.interimResults=false; r.lang='en-US';
  r.onresult=function(e){
    VOICE.errs=0;
    for(var i=e.resultIndex;i<e.results.length;i++){
      if(!e.results[i].isFinal) continue;
      var t=e.results[i][0].transcript;
      if(isEcho(t)) continue;                                                   // Abra's own voice coming back through the microphone
      // while an answer is being read out, only a clear "Abra" starts a command
      if((speaking || assistBusy) && !/(^| )(abra|a bra|ebra|obra|sabra)( |$)/.test(voiceNorm(t))) continue;
      voiceHandle(t);
    }
  };
  r.onerror=function(e){ voiceOnError(e.error); };
  r.onend=function(){
    VOICE.rec=null;
    if(VOICE.on){
      clearTimeout(VOICE.restartT);
      var wait = VOICE.errs ? Math.min(8000, 400*VOICE.errs*VOICE.errs) : 400;
      VOICE.restartT=setTimeout(function(){ if(VOICE.on && !VOICE.rec && !VOICE.speaking) voiceStart(); }, wait);
    }
  };
  VOICE.rec=r;
  try{ r.start(); }catch(e){ VOICE.rec=null; return false; }
  voiceUi(true); return true;
}
function voiceStop(){
  VOICE.on=false; clearTimeout(VOICE.restartT); clearTimeout(VOICE.speakT);
  VOICE.pending=null; VOICE.armedUntil=0; VOICE.errs=0;
  var r=VOICE.rec; VOICE.rec=null; if(r){ try{ r.onend=null; r.stop(); }catch(e){} }
  voiceUi(false);
}
const ABRA_VOICE_ON=false;   // Abra voice (the mic switch, spoken replies and voice commands) is switched off for the live demo; set true to bring it back
function voiceToggle(){
  if(!ABRA_VOICE_ON){ rxFeedback('Voice is switched off in this version of Abra.', 3000); return; }
  if(VOICE.on){ voiceStop(); voiceShow('Voice off.'); return; }
  VOICE.on=true;
  if(voiceStart()) voiceAck('Voice on. Say Abra and a command.'); else VOICE.on=false;
}
// ---- what the commands do. Each reply is only spoken after the action really ran. ----
function voiceEnsureCreate(){ var v=document.getElementById('view-film'); if(!v || !v.classList.contains('active')) showView('film'); }
function voiceRecord(){
  if(rxRec && rxRec.state==='paused'){ rxResumeAfterBreath(null); return 'Resuming in three.'; }
  if(rxRec && rxRec.state==='recording') return 'Already recording.';
  voiceEnsureCreate();
  rxFireGesture('start');                                         // 3-second countdown, then the recorder starts
  setTimeout(function(){ if(!(rxRec && rxRec.state==='recording')) voiceAck('I could not start recording. Allow camera access, then try again.'); }, 3900);
  return 'Recording in three.';
}
function voicePause(){ if(rxRec && rxRec.state==='recording'){ rxRecPause(); return 'Paused.'; } return 'Not recording.'; }
function voiceStopRec(){ if(_gCountT){ rxCancelCountdown(); return 'Cancelled the countdown.'; } if(rxRec && (rxRec.state==='recording'||rxRec.state==='paused')){ rxFireGesture('stop'); return 'Stopped.'; } return 'Not recording.'; }
function voiceNewTake(){
  voiceEnsureCreate(); rxNewTake();
  setTimeout(function(){ if(!(rxRec && rxRec.state==='recording')) voiceAck('I could not start the new take. Allow camera access, then try again.'); }, 4600);
  return 'New take.';
}
function voiceDelete(){
  if(rxRec && (rxRec.state==='recording'||rxRec.state==='paused')) return 'Stop the recording first, then delete.';
  if(!rxClips.length) return 'There is no take to delete.';
  rxClipErase(rxClips.length-1); return 'Deleted the last take.';
}
function voiceSend(){
  if(!(rxRec && (rxRec.state==='recording'||rxRec.state==='paused'))){
    if(!rxClips.length) return 'There is no take to send yet. Record one first.';
    rxNext(); return 'Sent to Load.';
  }
  var before=rxClips.length, tries=0;
  rxFireGesture('stop');                                          // the take joins the list only when the recorder stops
  var t=setInterval(function(){
    tries++;
    if(rxClips.length>before){ clearInterval(t); rxNext(); voiceAck('Sent to Load.'); }
    else if(tries>40){ clearInterval(t); voiceAck('The take did not save. Stop the recording, then say send to load.'); }
  }, 100);
  return 'Finishing the take.';
}
function voiceCloseCam(){ rxCloseCam(); return 'Camera closed.'; }
function voiceOpenCam(){
  voiceEnsureCreate();
  if(rxStream) return 'The camera is already on.';
  return Promise.resolve(rxStartCam()).then(function(ok){ return ok ? 'Camera on.' : 'The camera is not available. Allow camera access, then try again.'; });
}
function voiceHandsFree(cmd){
  if(_filmPick==='reaction' && (/(^| )on( |$)/).test(cmd)) return 'Greenscreen records standard portrait, so it has no hands-free option.';
  if((/(^| )on( |$)/).test(cmd)){
    if(_gOn) return 'Hands-free is already on.';
    rxToggleHandsFree();
    setTimeout(function(){ if(!_gOn) voiceAck('Hands-free did not start. The camera may be blocked.'); }, 2500);
    return 'Turning hands-free on.';
  }
  if(!_gOn) return 'Hands-free is already off.';
  rxHandsFreeOff(); return 'Hands-free off.';
}
function voiceGo(v,msg){ showView(v); return msg; }
function voiceDraftTitle(){
  var ti=parseInt((document.querySelector('input[name=sctitle]:checked')||{value:'0'}).value||'0',10)||0;
  return ((schedDraft&&schedDraft.titles)||[])[ti]||'(no title)';
}
function voiceApprove(){
  if(!schedDraft) return 'There is no post ready to schedule. Make one in Load first.';
  if(schedDraft.auto_result) return 'Full auto already published this video when you dropped it, so there is nothing to approve.';
  var p;
  try{ p=schedPayload(); }catch(e){ return 'Open a video in Load first, then say approve.'; }
  var fp=JSON.stringify(p), title=voiceDraftTitle();
  var pageSel=document.getElementById('scPage')||document.getElementById('phPage'), slotSel=document.getElementById('scSlot');
  var page=(pageSel && pageSel.selectedIndex>=0) ? pageSel.options[pageSel.selectedIndex].textContent.trim() : 'the selected page';
  var when=(slotSel && slotSel.selectedIndex>=0) ? slotSel.options[slotSel.selectedIndex].textContent.trim() : 'the proposed time';
  VOICE.pending={title:title, expires:Date.now()+15000,
    check:function(){
      if(!schedDraft) return 'The post is gone. Say approve again.';
      var now; try{ now=JSON.stringify(schedPayload()); }catch(e){ return 'The post changed. Say approve again to check.'; }
      return now===fp ? null : 'The post changed since you approved it. Say approve again to check.';
    },
    run:function(){ return approveSchedule(); }};
  return 'Schedule "'+title.slice(0,60)+'" on '+page+', '+when+'? Say Abra confirm within fifteen seconds.';
}
function voiceConfirm(){
  var pd=VOICE.pending;
  if(!pd) return 'Nothing to confirm.';
  VOICE.pending=null;
  if(Date.now()>pd.expires) return 'That expired. Say the command again.';
  var refusal=pd.check ? pd.check() : null;
  if(refusal) return refusal;
  return Promise.resolve(pd.run()).then(function(res){ return (typeof res==='string') ? res : ((res && res.msg) ? res.msg : 'Done.'); });
}
function voiceCancel(){ if(_gCountT){ rxCancelCountdown(); return 'Cancelled the countdown.'; } if(!VOICE.pending) return 'Nothing to cancel.'; VOICE.pending=null; return 'Cancelled.'; }
function voiceHelp(){ if(voiceInCreate() && typeof rxHelpOpen==='function'){ rxHelpOpen(); return 'Opened how recording works.'; } var seen={}, out=[]; VOICE_CMDS.forEach(function(c){ if(!seen[c.label]){ seen[c.label]=1; out.push(c.label); } }); return 'Say Abra and a command, or the name of any button, tab, option or field on screen. Commands: '+out.join(', ')+'.'; }
// ---- Create features by voice: the same controls the buttons use, so the screen shows the change ----
function voiceCaptions(cmd){
  var on=/(^| )on( |$)/.test(cmd);
  // with a post open in the Studio, the switch that decides its captions is the one that changes (and the Studio stays open)
  var sc=document.getElementById('scCaptions'), st=document.getElementById('view-studio');
  if(sc && st && st.classList.contains('active')){
    if(sc.disabled) return 'This video has no speech to caption.';
    sc.checked=on; return 'Captions '+(on?'on':'off')+' for this post.';
  }
  voiceEnsureCreate();
  var chk=document.getElementById('rxCapChk'); if(chk) chk.checked=on;
  rxSetCaptions(on);
  return 'Captions '+(on?'on':'off')+'.';
}
function voiceTeleprompter(cmd){
  voiceEnsureCreate();
  var on=/(^| )(on|show)( |$)/.test(cmd);
  if(on!==(_rxFlyKey==='prompt')) rxFly('prompt');
  return 'Teleprompter '+(on?'on':'off')+'.';
}
function voiceSides(cmd){
  voiceEnsureCreate();
  var on=/(^| )(on|show)( |$)/.test(cmd);
  if(on!==_rxWide) rxToggleWide();
  return 'Sides '+(on?'shown':'hidden')+'.';
}
function voiceSmoothCut(){
  if(!rxBlob) return 'Record a take first, then say smooth cut.';
  if(rxCutting) return 'Still tightening up the last take.';
  if(rxCut) return 'That take is already smooth.';
  voiceEnsureCreate();
  rxSmoothCut();
  return 'Smoothing the take on your computer. This takes a little while.';
}
function voiceCameraPanel(){ voiceEnsureCreate(); if(_rxFlyKey!=='cam') rxFly('cam'); return 'Camera settings open.'; }
// ---- Room for your hand by voice: the same value and rules as the slider in Camera settings ----
var VOICE_ROOM_UP=voicePhrases(['room for my hand bigger','room for my hand more','room for my hand larger','room for your hand bigger','room for your hand more','more room for my hand','more room for your hand','more room','bigger room','room bigger','room more','hand room bigger','more hand room']);
var VOICE_ROOM_DOWN=voicePhrases(['room for my hand smaller','room for my hand less','room for your hand smaller','room for your hand less','less room for my hand','less room for your hand','less room','smaller room','room smaller','room less','hand room smaller','less hand room']);
// "set room to 150" (a percent) or "set room to 2" (up to 3 reads as a multiple): the speech recognizer never gives a dot, so decimals are not offered
function voiceRoomCmd(c){
  if(VOICE_ROOM_UP.test(c)) return {step:10};
  if(VOICE_ROOM_DOWN.test(c)) return {step:-10};
  var m=/^(?:set|make|change|put) (?:the )?room(?: for hand)?(?: to| at)? ([0-9]+)(?: times| x)?$/.exec(c);
  return m ? {set:parseFloat(m[1])} : null;
}
function voiceRoomAct(c){
  var r=voiceRoomCmd(c); if(!r) return null;
  if(!voiceInCreate()) return 'Open Create to change the room for your hand.';
  if(rxTakeBusy()) return 'Finish this take first, then change the room for your hand.';
  var want=(r.set!==undefined) ? (r.set<=3 ? Math.round(r.set*100) : Math.round(r.set)) : Math.round(_rxZoom*100)+r.step;
  want=Math.max(100,Math.min(180,want));
  rxSetZoom(want, true);
  return 'Room for your hand is '+(want/100).toFixed(1)+' times'+(want===100?'. A tall camera needs more room than that to see your hand, so say room bigger.':'.');
}
// ---- microphone by voice: "switch microphone", "use the second microphone", "use the usb mic" ----
function voiceMicCmd(c){
  if(/^(switch|change|next|cycle) (the |my )?(microphone|mic)s?$/.test(c)) return {cycle:true};
  var m=/^(use|pick|choose|select|set|switch to|switch|change to) (?:the |my |a )*(.+?) ?(microphone|mic)s?$/.exec(c);
  if(m && m[2]) return {name:m[2]};
  m=/^(use|pick|choose|select|set|switch to|switch|change to) (?:the |my |a )*(microphone|mic)s? (.+)$/.exec(c);
  if(m) return {name:m[3]};
  m=/^(microphone|mic) (first|second|third|fourth|one|two|three|four|1|2|3|4)$/.exec(c);
  return m ? {name:m[2]} : null;
}
function voiceSwitchMic(p){
  if(!voiceInCreate()) return 'Open Create to choose a microphone.';
  if(rxTakeBusy()) return 'Finish this take first, then change the microphone.';
  if(_rxRemote) return "The phone's own microphone records the sound. Switch the camera back to this computer to choose one.";
  const sel=document.getElementById('rxMicSel'); if(!sel) return 'The microphone list is not ready yet.';
  const all=[].slice.call(sel.options).filter(function(o){ return !o.disabled; });
  const mics=all.filter(function(o){ return o.value!==''; });
  const names=mics.map(function(o){ return o.textContent.trim(); });
  let target=null;
  if(p.cycle){
    const cur=Math.max(0, all.findIndex(function(o){ return o.value===sel.value; }));
    target=all[(cur+1)%all.length];
  } else {
    const pos={first:0, one:0, '1':0, second:1, two:1, '2':1, third:2, three:2, '3':2, fourth:3, four:3, '4':3};
    const toks=(p.name||'').split(' ').filter(function(x){ return x && ['the','to','my','a','an','please','microphone','mic','mics','input','use'].indexOf(x)<0; });
    if(/default/.test(p.name||'')) target=all[0];
    else if(toks.length===1 && pos[toks[0]]!==undefined){
      target=mics[pos[toks[0]]]||null;
      if(!target) return 'There is no '+toks[0]+' microphone. The microphones are: '+(names.join(', ')||'only the system default')+'.';
    } else {
      const hits=mics.filter(function(o){ const l=o.textContent.toLowerCase(); return toks.length>0 && toks.every(function(t){ return l.indexOf(t)>=0; }); });
      if(hits.length===1) target=hits[0];
      else if(hits.length>1) return 'More than one microphone matches. Say the position: '+hits.map(function(o){ return (mics.indexOf(o)===0?'first':mics.indexOf(o)===1?'second':mics.indexOf(o)===2?'third':'number '+(mics.indexOf(o)+1))+' is '+o.textContent.trim(); }).join(', ')+'.';
      else return 'I did not find that microphone. The microphones are: '+(names.join(', ')||'only the system default')+'.';
    }
  }
  sel.value=target.value; const name=target.textContent.trim();
  if(!rxStream){ rxMicChange(); return name+' will be used when the camera starts.'; }   // saves the choice for the next start
  return rxMicChange().then(function(ok){ return ok ? ('Using '+name+'.') : 'The microphone did not change.'; });
}
// ---- mirror and brightness by voice (the same switches as the Camera panel) ----
function voiceMirror(c){
  if(!voiceInCreate()) return 'Open Create to change the mirror.';
  const on = /(^| )on( |$)/.test(c) ? true : ((/(^| )off( |$)/.test(c) || /unmirror/.test(c)) ? false : !_rxMirror);
  rxSetMirror(on);
  return 'Mirror preview '+(on?'on':'off')+'. Your saved take is never mirrored.';
}
function voiceBrightBlock(){
  if(!voiceInCreate()) return 'Open Create to change the brightness.';
  if(rxTakeBusy()) return 'Finish this take first, then change the brightness.';
  if(!rxStream) return 'Turn on the camera first, then I can change the brightness.';
  if(!_rxBrightKey || !_rxBrightRange) return 'This camera does not offer a brightness control, so I cannot change it.';
  return '';
}
function voiceBrighter(c){
  const block=voiceBrightBlock(); if(block) return block;
  const r=_rxBrightRange, step=r.step||((r.max-r.min)/10), cur=(_rxBrightVal!==null)?_rxBrightVal:((r.min+r.max)/2);
  const down=/dark|dim/.test(c);
  return rxSetBrightness(cur+(down?-step:step)).then(function(ok){ return ok ? ('Brightness set '+(down?'darker':'brighter')+'.') : 'The camera did not change its brightness.'; });
}
// "set brightness to 40": 0 to 100 across the camera's own range
function voiceSetBright(c){
  const block=voiceBrightBlock(); if(block) return block;
  const m=/([0-9]+)/.exec(c); const pct=Math.max(0, Math.min(100, parseInt(m[1],10)));
  const r=_rxBrightRange;
  return rxSetBrightness(r.min+(r.max-r.min)*pct/100).then(function(ok){ return ok ? ('Brightness set to '+pct+' percent of the range.') : 'The camera did not change its brightness.'; });
}
// ---- Script intake: a text file read into the editor, or a script Sage writes from a summary and links. A draft is
// never saved until it is accepted, and an accepted script keeps the one before it for undo.
function rxPromptPick(){ const f=document.getElementById('rxPromptFile'); if(f) f.click(); }
function rxPromptFileIn(f){
  const m=document.getElementById('rxPromptMsg');
  if(!f) return;
  if(f.size>200000){ if(m) m.textContent='That file is over 200 KB. Paste the part you need instead.'; return; }
  if(!/\\.(txt|md)$/i.test(f.name||'') && !/^text\\//.test(f.type||'')){ if(m) m.textContent='Only .txt and .md files can be read here.'; return; }
  const r=new FileReader();
  r.onload=function(){
    let t=String(r.result||'');
    if(/\\.md$/i.test(f.name||'')){ t=t.replace(/^#+[ ]*/gm,'').replace(/^[ ]*[-*][ ]+/gm,''); }   // headings and bullets become plain lines
    rxPromptEdit(); const ta=document.getElementById('rxPromptText'); if(ta) ta.value=t;
    if(m) m.textContent='Read '+f.name+'. Check it, then Save script.';
  };
  r.onerror=function(){ if(m) m.textContent='That file could not be read.'; };
  r.readAsText(f);
}
function rxPromptDropFile(ev){
  ev.preventDefault();
  const z=document.getElementById('rxPromptDropZone'); if(z) z.classList.remove('over');
  const f=ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
  rxPromptFileIn(f);
}
async function rxPromptWrite(){
  const m=document.getElementById('rxPromptMsg');
  const sum=document.getElementById('rxPromptSum'), secsEl=document.getElementById('rxPromptSecs'), linksEl=document.getElementById('rxPromptLinks');
  const summary=((sum && sum.value) || _prm.summary || '').trim();
  const secs=parseInt((secsEl && secsEl.value) || '60', 10) || 60;
  const links=((linksEl && linksEl.value) || '').split(String.fromCharCode(10)).map(function(x){ return x.trim(); }).filter(Boolean).slice(0,3);
  if(!summary && !links.length){ if(m) m.textContent='Tell Sage what the video is about, or add a link to read.'; return false; }
  if(m) m.textContent='Sage is writing your script\u2026';
  let d={};
  try{ d=await fetch('/prompter/write',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({summary:summary,links:links,seconds:secs})}).then(function(r){ return r.json(); }); }
  catch(e){ d={error:'Sage could not be reached. Try again.'}; }
  _prm.summary=summary;
  if(!d.lines || !d.lines.length){ _prmDraft=null; prmShowDraft(); if(m) m.textContent=d.error||'Sage could not write that one.'; return false; }
  _prmDraft=d; prmShowDraft();
  if(m) m.textContent='Sage wrote '+d.lines.length+' lines. Read them, then choose Use this script.';
  return true;
}
function prmShowDraft(){
  const box=document.getElementById('rxPromptDraft'); if(!box) return;
  box.innerHTML='';
  if(!_prmDraft){ box.style.display='none'; return; }
  box.style.display='block';
  const h=document.createElement('div'); h.className='hint'; h.textContent='Draft from Sage, not saved yet'+(_prmDraft.title?': '+_prmDraft.title:''); box.appendChild(h);
  _prmDraft.lines.forEach(function(t,i){ const d=document.createElement('div'); d.className='rxpline'; d.textContent=(i+1)+'. '+t; box.appendChild(d); });
  (_prmDraft.notes||[]).forEach(function(n){ const d=document.createElement('div'); d.className='hint'; d.textContent=n; box.appendChild(d); });
  const row=document.createElement('div'); row.className='rxprompt-tools';
  const use=document.createElement('button'); use.className='btn green'; use.type='button'; use.textContent='Use this script'; use.onclick=function(){ rxPromptUseDraft(); }; row.appendChild(use);
  const drop=document.createElement('button'); drop.className='rxlenchip'; drop.type='button'; drop.textContent='Discard'; drop.onclick=function(){ _prmDraft=null; prmShowDraft(); }; row.appendChild(drop);
  box.appendChild(row);
}
function rxPromptUseDraft(){
  const m=document.getElementById('rxPromptMsg');
  if(!_prmDraft){ if(m) m.textContent='There is no draft to use yet.'; return false; }
  if(rxTakeBusy()){ rxFeedback('Stop the take first, then use the script.', 3500); return false; }
  if(_prm.saved.trim() && !_prm.confirmUse){ _prm.confirmUse=true; if(m) m.textContent='Use this script again to replace your saved script. Undo brings it back.'; return false; }
  _prm.confirmUse=false;
  _prm.prev=_prm.saved;
  _prm.saved=_prmDraft.lines.join(String.fromCharCode(10));
  _prmDraft=null; const ok=prmSave(); prmShowDraft(); prmRender();
  if(m) m.textContent = ok ? 'Sage\u2019s script is now your script. Undo brings back the one before.' : 'Could not save on this device.';
  return ok;
}
// voice: next line, faster or slower, bigger or smaller text, edit or save the script (typing stays on the keyboard)
function voicePrompt(cmd){
  if(!voiceInCreate()) return 'Open Create to use the teleprompter.';
  if(/^next/.test(cmd)){ if(!_prm.lines.length) return 'There is no script yet. Say edit script.'; rxPromptNext(); return 'Next line.'; }
  if(/faster|speed up|quicker/.test(cmd)){ rxPromptSpeed(1); return 'Teleprompter speed '+_prm.speed+' of 5.'; }
  if(/slower|slow down/.test(cmd)){ rxPromptSpeed(-1); return 'Teleprompter speed '+_prm.speed+' of 5.'; }
  if(/bigger|larger/.test(cmd)){ rxPromptSize(2); return 'Teleprompter text size '+_prm.size+'.'; }
  if(/smaller/.test(cmd)){ rxPromptSize(-2); return 'Teleprompter text size '+_prm.size+'.'; }
  if(/edit/.test(cmd)){ if(_rxFlyKey!=='prompt') rxFly('prompt'); return rxPromptEdit() ? 'Editing your script. Type it on the keyboard, because voice cannot dictate it.' : 'Stop the take first, then edit the script.'; }
  if(/save/.test(cmd)){ const box=document.getElementById('rxPromptEditBox'); if(!box || box.style.display!=='block') return 'Open Edit script first, then say save script.'; return rxPromptSave() ? 'Script saved on this device.' : 'The script was not saved.'; }
  return null;
}
let _prmDraft=null;   // Sage's draft script, not saved until it is accepted
// voice: "write a script about my morning", "make it thirty seconds", "use that", "undo script"
function voicePromptWrite(c){
  const m=/^(?:write|make|draft|create|give me|start) (?:me )?(?:a |an )?script (?:about|on|for) (.+)$/.exec(c) || /^(?:talk|speak) about (.+)$/.exec(c);
  if(_rxFlyKey!=='prompt') rxFly('prompt');
  const sum=document.getElementById('rxPromptSum'); if(sum && m) sum.value=m[1];
  if(!m && !(sum && sum.value) && !_prm.summary) return 'Tell me what the video is about first, for example: write a script about my morning routine.';
  return rxPromptWrite().then(function(ok){
    if(ok) return 'Sage wrote your script. Say use that to read it, or make it thirty seconds.';
    const mm=document.getElementById('rxPromptMsg'); return (mm && mm.textContent) || 'Sage could not write that one.';
  });
}
function voicePromptLength(c){
  const m=/^make it (.+?) (seconds?|minutes?)$/.exec(c); if(!m) return null;
  const nums={zero:0, one:1, two:2, three:3, four:4, five:5, six:6, seven:7, eight:8, nine:9, ten:10, eleven:11, twelve:12,
    fifteen:15, twenty:20, thirty:30, forty:40, fifty:50, sixty:60, seventy:70, eighty:80, ninety:90};
  let n=0, ok=false;
  m[1].split(' ').forEach(function(t){ if(nums[t]!==undefined){ n+=nums[t]; ok=true; } else if(/^[0-9]+$/.test(t)){ n+=parseInt(t,10); ok=true; } });
  if(!ok || !n) return 'I did not catch the length. Try thirty seconds or one minute.';
  n = /minute/.test(m[2]) ? n*60 : n;
  const opts=[30,60,90,120]; let best=opts[0]; opts.forEach(function(o){ if(Math.abs(o-n)<Math.abs(best-n)) best=o; });
  const sel=document.getElementById('rxPromptSecs'); if(sel) sel.value=String(best);
  if(!_prm.summary && !((document.getElementById('rxPromptSum')||{}).value||'').trim()) return 'Tell me what the video is about first.';
  return rxPromptWrite().then(function(ok){ return ok ? ('Rewritten for about '+(best>=60?(best/60)+' minute':best+' seconds')+'.') : 'Sage could not rewrite it.'; });
}
function voicePromptUseDraft(){
  if(!_prmDraft) return 'Ask me to write a script first, for example: write a script about your morning.';
  const ok=rxPromptUseDraft();
  if(_prm.confirmUse) return 'Say use that again to replace your saved script.';
  return ok ? 'Using Sage\u2019s script now.' : 'The script was not changed.';
}
// ---- replies by voice: approval on or off for every page, and approve the next reply (confirmed first)
function voiceReplyApproval(c){
  const on=/(^| )on( |$)/.test(c);
  return fetch('/replies',{cache:'no-store'}).then(function(r){ return r.json(); }).then(function(d){
    return Promise.all((d.pages||[]).map(function(p){ return fetch('/replies/approval',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page_id:p.page_id,on:on})}); }));
  }).then(function(){ replyQueueLoad(); return on ? 'Replies now wait for your approval before they post.' : 'Replies post as before.'; })
    .catch(function(){ return 'I could not change reply approval right now.'; });
}
function voiceApproveNext(){
  return fetch('/replies',{cache:'no-store'}).then(function(r){ return r.json(); }).then(function(d){
    const it=(d.pending||[])[0];
    if(!it) return 'No replies are waiting for you.';
    return voiceAsk('Post this reply to a comment that says "'+(it.snippet||'(no text)')+'": "'+it.reply+'".', null, function(){
      return replyDecide(it.id,'approve').then(function(ok){ return ok ? 'Posted.' : 'That reply was not posted.'; });
    });
  }).catch(function(){ return 'I could not reach the replies right now.'; });
}
// ---- direct messages by voice: off, automatic, or ask me first (the same setting as the Engagement page)
function voiceDmMode(c){
  let mode='';
  if(/off|stop|turn off|no/.test(c)) mode='off';
  else if(/ask|flag|review|check/.test(c)) mode='flag';
  else if(/auto|on|automatic/.test(c)) mode='auto';
  if(!mode) return 'Say DMs off, DMs automatic, or DMs ask me.';
  return fetch('/pages',{cache:'no-store'}).then(function(r){ return r.json(); }).then(function(d){
    const pages=(d&&d.pages)||d||[];
    const pid=pages.length? (pages[0].page_id||pages[0].id) : '';
    if(!pid) return 'No page is connected yet.';
    return fetch('/dmmode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page_id:String(pid),mode:mode})})
      .then(function(){ return mode==='off' ? 'Direct messages are off.' : (mode==='flag' ? 'I will ask you before I answer direct messages.' : 'Direct messages are automatic.'); });
  }).catch(function(){ return 'I could not change direct messages right now.'; });
}
// zoom on the greenscreen framing (60% to 200%), in steps of ten
function voiceBgZoomAct(d){
  if(!voiceInCreate()) return 'Open Create to change the zoom.';
  const cur=Math.round((_bg.zoom||1)*100);
  if(!rxBgZoom(cur+d)) return 'Finish this take first, then change the zoom.';
  const now=Math.round((_bg.zoom||1)*100);
  if(now===cur) return 'Greenscreen zoom is already at its '+(d<0?'smallest, 60 percent.':'largest, 100 percent.');
  return 'Greenscreen zoom is '+now+' percent.';
}
// "talk time": the meter's figure, always said as an estimate
function voiceTalkTime(){
  if(!voiceInCreate()) return 'Open Create to check your talk time.';
  if(!rxStream) return 'Turn on the camera first. Talk time needs its microphone.';
  if(!rxMeterOn) return 'I cannot measure talk time without a microphone.';
  if(rxRecording()){
    if(rxRecMs<5000) return 'Still listening. Talk time needs a few seconds of sound.';
    return 'About '+Math.round(rxTalkMs/rxRecMs*100)+' percent of this take is you talking, by my estimate.';
  }
  if(_rxLastTalk!==null) return 'Your last take was about '+_rxLastTalk+' percent talk, by my estimate.';
  return 'Nothing is recording yet. Start a take and I will estimate your talk time.';
}
// ---- anything on screen: say the name of a button, tab, option or field and Abra does what a click would ----
var VOICE_SECRET=/password|passcode|passwd|secret|token|api ?key|access key|card number|cvv|iban|routing|account number|social security|ssn/i;
var VOICE_RISKY=/publish|post|send|delete|remove|discard|sign ?in|sign ?out|log ?in|log ?out|connect|disconnect|pay|buy|purchase|boost|schedule|approve|submit|subscribe|accept|agree|allow|confirm|reset|clear|revoke|unlink|spend|share|upload|overwrite/i;
function voiceVisible(el){
  if(!el || !el.getClientRects || !el.getClientRects().length) return false;
  var cs=getComputedStyle(el); return cs.visibility!=='hidden' && cs.display!=='none' && parseFloat(cs.opacity||'1')>0;
}
function voiceLabelOf(el){
  var t=el.getAttribute('aria-label')||el.getAttribute('title')||'';
  if(!t) t=(el.textContent||'').replace(/ +/g,' ').trim();
  return t.slice(0,90);
}
function voiceControls(){
  var scope=document.querySelector('.view.active');
  var list=[].slice.call(document.querySelectorAll('button, a[href], [role=button], [role=tab], [role=switch], [role=menuitem], label, summary, [onclick]'));
  var out=[];
  list.forEach(function(el){
    if(el.disabled || el.closest('#assistWrap, #voiceToast, #liveMeta, #liveMetaList, .voice-switch')) return;
    if(!el.closest('aside, .mtopbar') && scope && !scope.contains(el)) return;
    if(!voiceVisible(el)) return;
    var lab=voiceLabelOf(el); if(!lab) return;
    out.push({el:el, label:lab});
  });
  return out;
}
function voiceScoreLabel(cmd, lab){
  var c=voiceNorm(cmd), l=voiceNorm(lab);
  if(!c || !l) return 0;
  if(c===l) return 100;
  if(l.indexOf(c)===0) return 85;
  var cw=c.split(' '), lw=l.split(' '), hit=0;
  cw.forEach(function(w){ if(lw.indexOf(w)>=0) hit++; });
  return hit===cw.length ? Math.max(60, 70-(lw.length-cw.length)) : 0;
}
function voiceFindControl(cmd){
  var best=0, hits=[];
  voiceControls().forEach(function(c){
    var s=voiceScoreLabel(cmd, c.label); if(!s) return;
    if(s>best){ best=s; hits=[c]; } else if(s===best){ hits.push(c); }
  });
  return {score:best, hits:hits};
}
// a stored setting: a choice, a tick box, a slider, or a label around one, anywhere outside Create
function voiceStoredSetting(el){
  if(el.closest('#view-film')) return false;
  if(el.tagName==='SELECT') return true;
  if(el.tagName==='INPUT' && (el.type==='radio' || el.type==='checkbox' || el.type==='range')) return true;
  return el.tagName==='LABEL' && !!el.querySelector('input[type=radio], input[type=checkbox], select');
}
function voicePress(el, label){
  if(el.disabled) return 'That is turned off right now.';
  try{ el.scrollIntoView({block:'center'}); }catch(e){}
  el.click();
  return 'Pressed '+(label || voiceLabelOf(el))+'.';
}
// a risky or stored change waits for "Abra confirm"; the check makes sure the control is still there
function voiceAsk(prompt, check, run){
  VOICE.pending={title:prompt, expires:Date.now()+15000, check:check, run:run};
  return prompt+' Say Abra confirm within fifteen seconds.';
}
function voiceSelectName(sel){
  var lab=sel.id && document.querySelector('label[for="'+sel.id+'"]');
  return sel.getAttribute('aria-label') || (lab ? lab.textContent : '') || sel.name || sel.id || '';
}
function voiceSelect(sel, opt){
  var label=voiceSelectName(sel), text=opt.textContent.trim();
  var apply=function(){ sel.value=opt.value; sel.dispatchEvent(new Event('change',{bubbles:true})); return 'Chose '+text+'.'; };
  if(voiceStoredSetting(sel)) return voiceAsk('Change '+label+' to '+text+'?', function(){ return sel.isConnected ? null : 'That is no longer on screen.'; }, apply);
  return apply();
}
function voiceSetSelect(name, value){
  var sels=[].slice.call(document.querySelectorAll('select')).filter(voiceVisible);
  var pick=null, best=0;
  sels.forEach(function(sel){ var s=voiceScoreLabel(name, voiceSelectName(sel)); if(s>best){ best=s; pick=sel; } });
  if(!pick || best<60) return null;
  var opt=null, os=0;
  [].slice.call(pick.options).forEach(function(o){ var s=voiceScoreLabel(value, o.textContent); if(s>os){ os=s; opt=o; } });
  if(!opt || os<60) return 'That list has no option called '+value+'.';
  return voiceSelect(pick, opt);
}
// sliders: "set brightness to 40" (a number, clamped to the slider's own range)
function voiceSetRange(name, value){
  var num=parseFloat(String(value).replace(/[^0-9.]/g,''));
  var ranges=[].slice.call(document.querySelectorAll('input[type=range]')).filter(voiceVisible);
  var pick=null, best=0;
  ranges.forEach(function(r){ var s=voiceScoreLabel(name, voiceFieldName(r)); if(s>best){ best=s; pick=r; } });
  if(!pick || best<60) return null;
  if(isNaN(num)) return 'Say a number to set '+name+' to.';
  var lo=parseFloat(pick.min||'0'), hi=parseFloat(pick.max||'100');
  var v=Math.min(hi, Math.max(lo, num));
  var apply=function(){ pick.value=String(v); pick.dispatchEvent(new Event('input',{bubbles:true})); pick.dispatchEvent(new Event('change',{bubbles:true})); return 'Set '+voiceFieldName(pick)+' to '+v+'.'; };
  if(voiceStoredSetting(pick)) return voiceAsk('Set '+voiceFieldName(pick)+' to '+v+'?', function(){ return pick.isConnected ? null : 'That is no longer on screen.'; }, apply);
  return apply();
}
// scrolling the page: "scroll down", "scroll up", "top", "bottom"
function voiceScroll(cmd){
  // with the teleprompter open in Create, up and down move the script, not the page
  if(_rxFlyKey==='prompt' && voiceInCreate() && _prm.lines.length && /(^| )(up|down)$/.test(cmd)){
    const tp=document.getElementById('rxPrompter');
    if(tp){ _prm.y=Math.max(0, Math.min(tp.scrollHeight, tp.scrollTop+(/down/.test(cmd)?120:-120))); tp.scrollTop=_prm.y; _prm.holdUntil=performance.now()+3000; return /down/.test(cmd)?'Scrolled the script down.':'Scrolled the script up.'; }
  }
  if(/top/.test(cmd)){ window.scrollTo({top:0, behavior:'smooth'}); return 'Back to the top.'; }
  if(/bottom/.test(cmd)){ window.scrollTo({top:document.documentElement.scrollHeight, behavior:'smooth'}); return 'At the bottom.'; }
  var down=/down/.test(cmd);
  window.scrollBy({top: down ? 600 : -600, behavior:'smooth'});
  return down ? 'Scrolled down.' : 'Scrolled up.';
}
function voiceChoose(text){
  var sels=[].slice.call(document.querySelectorAll('select')).filter(voiceVisible);
  var best=0, bestSel=null, bestOpt=null;
  sels.forEach(function(sel){ [].slice.call(sel.options).forEach(function(o){ var s=voiceScoreLabel(text, o.textContent); if(s>best){ best=s; bestSel=sel; bestOpt=o; } }); });
  if(bestSel && best>=60) return voiceSelect(bestSel, bestOpt);
  return voiceDoByName(text, text);
}
function voiceFieldName(f){
  var lab=f.id && document.querySelector('label[for="'+f.id+'"]');
  return f.getAttribute('aria-label') || f.placeholder || (lab ? lab.textContent : '') || f.name || f.id || 'the field';
}
function voiceType(text){
  var m=text.match(/^(.*) in (?:the )?(.+)$/i);
  var content=m ? m[1] : text, fieldName=m ? m[2] : '';
  var fields=[].slice.call(document.querySelectorAll('input, textarea')).filter(function(f){
    var t=(f.type||'text').toLowerCase();
    if(['password','hidden','file','checkbox','radio','submit','button','range','color'].indexOf(t)>=0) return false;
    if(f.readOnly || f.disabled || !voiceVisible(f)) return false;
    return !VOICE_SECRET.test([f.name, f.id, f.placeholder, f.getAttribute('aria-label'), t].join(' '));
  });
  var target=null;
  if(fieldName){
    var best=0;
    fields.forEach(function(f){ var s=voiceScoreLabel(fieldName, voiceFieldName(f)); if(s>best){ best=s; target=f; } });
    if(!target || best<60) return 'I could not find a field called '+fieldName+'.';
  } else {
    target = fields.indexOf(document.activeElement)>=0 ? document.activeElement : null;
    if(!target) return 'Tap the field first, then say type and your words.';
  }
  target.focus();
  target.value=content;
  target.dispatchEvent(new Event('input',{bubbles:true}));
  target.dispatchEvent(new Event('change',{bubbles:true}));
  return 'Typed in '+voiceFieldName(target)+'. Nothing was sent.';
}
// resolves to a reply, or null when nothing on screen matches
function voiceSetSwitch(el, want, label){
  const inp=(el.tagName==='INPUT')?el:((el.querySelector && el.querySelector('input[type=checkbox],input[type=radio]'))||el.control||null);
  if(!inp || (inp.type!=='checkbox' && inp.type!=='radio')) return null;
  const go=function(){ if(inp.checked!==want) voicePress(el, label); return label+(want?' is on.':' is off.'); };
  if(inp.checked===want) return label+(want?' is already on.':' is already off.');
  if(voiceStoredSetting(el)) return voiceAsk('Turn "'+label+'" '+(want?'on':'off')+'?', function(){ return el.isConnected ? null : 'That is no longer on screen.'; }, go);
  return go();
}
function voiceDoByName(cmd, raw){
  var m, r=(raw||'').replace(/^(please|now|okay|ok|hey)[ ,]+/i,'');
  const on1=cmd.match(/^(?:turn|switch) (on|off) (?:the )?(.+)$/), on2=cmd.match(/^(?:check|tick|mark)(?: off)? (?:the )?(.+)$/);
  if(on1 || on2){ const want=on1?(on1[1]==='on'):true, tgt=on1?on1[2]:on2[1]; const tf=voiceFindControl(tgt);
    if(tf.score>=60 && tf.hits.length===1){ const sw=voiceSetSwitch(tf.hits[0].el, want, tf.hits[0].label); if(sw!==null) return sw; } }

  if((m=r.match(/^(type|enter) (.+)$/i))) return voiceType(m[2]);
  if((m=cmd.match(/^(choose|pick|select) (.+)$/))) return voiceChoose(m[2]);
  if((m=cmd.match(/^set (.+) to (.+)$/))){
    var sres=voiceSetSelect(m[1], m[2]); if(sres!==null) return sres;
    var rres=voiceSetRange(m[1], m[2]); if(rres!==null) return rres;
  }
  var stripped=cmd.replace(/^(open|show|go to|take me to|click|press|tap|select|hit|use)( the| my| a)? /,'');
  var f=voiceFindControl(stripped);
  if(f.score<60) f=voiceFindControl(cmd);
  if(f.score<60) return null;
  if(f.hits.length>1){
    var labels=[];
    f.hits.forEach(function(h){ if(labels.indexOf(h.label)<0) labels.push(h.label); });
    if(labels.length>1) return 'Several things match: '+labels.slice(0,3).join(', ')+'. Say the exact name.';
    return 'There are '+f.hits.length+' things called "'+labels[0]+'". Say the exact name, or tap the one you want.';
  }
  var target=f.hits[0].el, label=f.hits[0].label;
  var check=function(){ return (target.isConnected && voiceVisible(target)) ? null : 'That is no longer on screen.'; };
  var go=function(){ return voicePress(target, label); };
  if(VOICE_RISKY.test(label)) return voiceAsk('Press "'+label+'"?', check, go);
  if(voiceStoredSetting(target)) return voiceAsk('Change "'+label+'"?', check, go);
  return go();
}
// ---- recording and video commands: the ways a creator actually talks ----
// Phrases are written the way people say them. voicePhrases() applies the same filler stripping as the matcher,
// so "send it to load" and "send to load" are one command.
function voicePhrases(list){
  var parts=[];
  list.forEach(function(p){ var s=voiceStrip(voiceNorm(p)); if(s && parts.indexOf(s)<0) parts.push(s); });
  return new RegExp('^(' + parts.join('|') + ')$');
}
function voiceInCreate(){ var v=document.getElementById('view-film'); return !!(v && v.classList.contains('active')); }
function voiceRecordingNow(){ return !!(rxRec && (rxRec.state==='recording'||rxRec.state==='paused')); }
// recording words act on Create or while a take is running; elsewhere they fall through to the on-screen matcher
function voiceGate(re){ return function(c){ return re.test(c) && (voiceInCreate() || voiceRecordingNow()); }; }
// while recording these work without "Abra": specific phrases only, so ordinary talk to the camera is not a command
var VOICE_OPEN_RECORDING=voicePhrases(['next line','scroll down','scroll up','stop recording','end recording','stop the recording','end the recording','finish recording','finish the recording','done recording','pause recording','pause the recording','resume recording','resume the recording','new take','another take','retake','send to load','take that to load','start over','stop filming','end filming','thats a wrap','that is a wrap',"that's a wrap",'captions on','captions off','turn on captions','turn off captions','teleprompter on','teleprompter off','hands free on','hands free off','turn on hands free','turn off hands free']);
// durations: "two minutes", "a minute and a half", "90 seconds", "1:30", "half a minute", "two and a half minutes"
var VOICE_WORDNUM={zero:0,one:1,two:2,three:3,four:4,five:5,six:6,seven:7,eight:8,nine:9,ten:10,eleven:11,twelve:12,thirteen:13,fourteen:14,fifteen:15,sixteen:16,seventeen:17,eighteen:18,nineteen:19,twenty:20,thirty:30,forty:40,fifty:50,sixty:60,seventy:70,eighty:80,ninety:90,couple:2};
var VOICE_UNIT={hour:3600,hours:3600,hr:3600,hrs:3600,minute:60,minutes:60,min:60,mins:60,second:1,seconds:1,sec:1,secs:1};
function voiceWordsToNumbers(s){
  var toks=s.split(' '), out=[];
  for(var i=0;i<toks.length;i++){
    var t=toks[i];
    if(Object.prototype.hasOwnProperty.call(VOICE_WORDNUM,t)){
      var v=VOICE_WORDNUM[t], nx=toks[i+1];
      if(v>=20 && v<=90 && nx && Object.prototype.hasOwnProperty.call(VOICE_WORDNUM,nx) && VOICE_WORDNUM[nx]>0 && VOICE_WORDNUM[nx]<10){ v+=VOICE_WORDNUM[nx]; i++; }
      out.push(String(v));
    } else out.push(t);
  }
  return out.join(' ');
}
function voiceClampSec(sec){ if(!(sec>=3)) return 0; return Math.min(600, Math.round(sec)); }
// bare: a number with no unit counts as seconds (used for "set length to 30")
function voiceDuration(text, bare){
  var s=' '+(text||'').toLowerCase().replace(/-/g,' ').replace(/[^a-z0-9.:' ]/g,' ').replace(/ +/g,' ')+' ';
  var clock=s.match(/ ([0-9]+):([0-9][0-9]) /);
  if(clock) return voiceClampSec(parseInt(clock[1],10)*60+parseInt(clock[2],10));
  s=s.replace(/ half an? (minute|min)s? /g,' 30 seconds ');
  s=s.replace(/ half an? (second|sec)s? /g,' 0.5 seconds ');
  s=s.replace(/ an? (hour|minute|min|second|sec)s? /g,function(m,u){ return ' 1 '+u+' '; });
  s=s.replace(/ and a half /g,' and zzhalf ');
  s=voiceWordsToNumbers(s);
  s=s.replace(/ ([0-9.]+) (minutes?|mins?) ([0-9]+)(?= )/g,function(m,a,u,b){ return ' '+(parseFloat(a)*60+parseFloat(b))+' seconds '; });
  s=s.replace(/ ([0-9.]+) and zzhalf (minutes?|mins?) /g,function(m,n){ return ' '+(parseFloat(n)*60+30)+' seconds '; });
  s=s.replace(/ ([0-9.]+) (minutes?|mins?) and zzhalf /g,function(m,n){ return ' '+(parseFloat(n)*60+30)+' seconds '; });
  s=s.replace(/ ([0-9.]+) (seconds?|secs?) and zzhalf /g,function(m,n){ return ' '+(parseFloat(n)+0.5)+' seconds '; });
  s=s.replace(/ ([0-9.]+) and zzhalf (seconds?|secs?) /g,function(m,n){ return ' '+(parseFloat(n)+0.5)+' seconds '; });
  var total=0, got=false, unitRe=/ ([0-9.]+) ?(hours?|hrs?|minutes?|mins?|seconds?|secs?) /g, m2;
  while((m2=unitRe.exec(s))){ total+=parseFloat(m2[1])*VOICE_UNIT[m2[2]]; got=true; }
  if(!got && bare){ var n=s.match(/ ([0-9.]+) /); if(n){ total=parseFloat(n[1]); got=true; } }
  return got ? voiceClampSec(total) : 0;
}
// which recording command a sentence is (these read a length out of the sentence)
function voiceVideoCmd(c){
  if(/(^| )(in|after) /.test(c)) return false;
  if(!/^(create|make|record|film|shoot|do|start|begin|new|a|an|one|capture)( |$)/.test(c)) return false;
  if(!/(^| )(video|videos|reel|reels|clip|clips|short|shorts|story|take|recording|record|film|shoot)( |$)/.test(c)) return false;
  return voiceDuration(c,false)>0;
}
function voiceStartInCmd(c){ return /^(start|begin|record|roll|go)( recording| the recording| the take| a take| take)? (in|after) /.test(c) && voiceDuration(c,true)>0; }
function voiceStopAfterCmd(c){ return /^(stop|end|finish|cut) (after|in|at) /.test(c) && voiceDuration(c,true)>0; }
function voiceSetLengthCmd(c){ return (/^(set|change|update|make|put|limit|cap|keep)( it| the length| the time| the timer| the video| the take| the recording| the limit)?( to| at| under| for| into| around| about)? /.test(c) || /^(length|time limit|timer|limit)( |$)/.test(c)) && voiceDuration(c,true)>0; }
function voiceMakeVideo(cmd){
  var sec=voiceDuration(cmd,false);
  voiceEnsureCreate();
  if(sec) rxSetTarget(sec);
  return (sec ? 'A '+rxFmtDur(sec)+' video. ' : '') + voiceRecord();
}
function voiceNewVideo(){ voiceEnsureCreate(); return 'Opened Create. Say a length, like two minutes, or say record.'; }
function voiceStartIn(cmd){
  var sec=voiceDuration(cmd,true);
  if(!sec) return 'I did not catch how long. Try: record in five seconds.';
  voiceEnsureCreate();
  setTimeout(function(){ voiceRecord(); }, sec*1000);
  return 'Recording in '+sec+' seconds.';
}
function voiceStopAfter(cmd){
  var sec=voiceDuration(cmd,true);
  if(!sec) return 'I did not catch how long. Try: stop after thirty seconds.';
  if(voiceRecordingNow()){ rxSetTarget(Math.min(600, Math.floor(rxRecMs/1000)+sec)); return 'Stopping in '+sec+' seconds.'; }
  voiceEnsureCreate(); rxSetTarget(sec);
  return 'Length set to '+rxFmtDur(sec)+'. Say record to start.';
}
function voiceSetLength(cmd){
  voiceEnsureCreate();
  var sec=voiceDuration(cmd,true);
  if(!sec) return 'I did not catch a length. Try: set length to thirty seconds.';
  rxSetTarget(sec);
  return 'Length set to '+rxFmtDur(sec)+'.';
}
function voiceNoLimit(){ voiceEnsureCreate(); rxSetTarget(0); return 'No time limit. Record as long as you like.'; }
function voiceTimeLeft(){
  if(!voiceRecordingNow()) return 'Nothing is recording right now.';
  var el=Math.floor(rxRecMs/1000);
  if(_rxTarget>0) return Math.max(0, _rxTarget-el)+' seconds left, out of '+rxFmtDur(_rxTarget)+'.';
  return 'Recording for '+el+' seconds, no time limit.';
}
function voiceTakeCount(){ var n=rxClips.length; return n===0 ? 'No takes yet.' : (n===1 ? 'One take.' : n+' takes.'); }
function voicePlay(){
  var v=document.getElementById('rxCam');
  if(!rxClips.length || !v || !v.src) return 'There is no take to play yet.';
  voiceEnsureCreate();
  try{ v.play(); }catch(e){}
  return 'Playing.';
}
function voicePausePlayback(){ var v=document.getElementById('rxCam'); if(v && v.src){ try{ v.pause(); }catch(e){} } return 'Paused playback.'; }
function voiceResume(){
  if(rxRec && rxRec.state==='paused'){ rxResumeAfterBreath(null); return 'Resuming in three.'; }
  if(voiceRecordingNow()) return 'Already recording.';
  return 'Nothing is paused.';
}
function voiceDone(){
  if(voiceRecordingNow()) return voiceStopRec();
  if(rxClips.length) return 'Done. Say send to load to take the take to Load.';
  voiceCloseCam(); return 'Camera closed.';
}
function voiceCut(){ return voiceRecordingNow() ? voiceStopRec() : voiceSmoothCut(); }
// the phrases open Load; the file picker itself must be opened by a tap, which a voice command cannot do
function voiceSendToLoad(){
  showView('studio');
  return 'Opening Load. Tap the box to choose your video.';
}
function voiceHandToSage(){
  if(!rxBlob) return 'Record a take first, then say hand to Sage.';
  rxHandToSage(); return 'Handing the take to Sage.';
}
function voiceDeleteAsk(){
  if(voiceRecordingNow()) return 'Stop the recording first, then delete.';
  if(!rxClips.length) return 'There is no take to delete.';
  var n=rxClips.length;
  VOICE.pending={title:'the last take', expires:Date.now()+15000,
    check:function(){ return (rxClips.length===n && !voiceRecordingNow()) ? null : 'The takes changed. Say delete again to check.'; },
    run:function(){ voiceDelete(); return 'Deleted the last take.'; }};
  return 'Delete the last take? Say Abra confirm within fifteen seconds.';
}
// ---- more things Abra does by voice: sound, chat, engagement, undo, crew, and page sections ----
function voiceOffFromVoice(){ if(VOICE.on){ voiceStop(); return 'Voice is off. Turn it back on with the switch at the top.'; } return 'Voice is already off.'; }
function voiceHearOn(){ if(!soundOn) toggleSound(); return 'Spoken replies are on.'; }
function voiceHearOff(){ if(soundOn) toggleSound(); return 'Spoken replies are off.'; }
function voicePauseEngagement(){ if(engPaused===true) return 'Engagement is already paused.'; toggleEngagement(); return 'Pausing engagement.'; }
function voiceResumeEngagement(){ if(engPaused===false) return 'Engagement is already running.'; toggleEngagement(); return 'Resuming engagement.'; }
function voiceUndo(){ if(!_rxUndo) return 'There is nothing to undo.'; rxUndoDo(); return 'Brought it back.'; }
var VOICE_CREW={nico:'Nico', sage:'Sage', koa:'Koa'};
function voiceTalkTo(cmd){
  const m=cmd.match(/(abra|nico|sage|koa)/); const who=m?m[1]:'abra';
  assistPersona=who; assistAgent=(who==='abra')?null:who; assistMorph(assistAgent);
  toggleAssist(true);
  return who==='abra' ? 'Back to Abra.' : VOICE_CREW[who]+' here. Ask me directly.';
}
var VOICE_SECTIONS=[
  {re:/best time|posting time|when to post/, view:'analytics', find:'best time'},
  {re:/reach|trend/, view:'analytics', find:'reach trend'},
  {re:/watch|retention/, view:'analytics', find:'watch'},
  {re:/spend|spent|cost|ledger/, view:'profile', find:'spent'},
  {re:/upcoming|scheduled posts/, view:'studio', find:'upcoming'},
  {re:/draft/, view:'studio', find:'draft'},
  {re:/next seven|next 7/, view:'home', find:'next 7'}
];
function voiceShowSection(cmd){
  const s=VOICE_SECTIONS.find(function(x){ return x.re.test(cmd); });
  if(!s) return 'I did not find that section.';
  showView(s.view);
  setTimeout(function(){
    const el=[].slice.call(document.querySelectorAll('h2,h3,h4,label,span,div')).find(function(e){
      const t=(e.textContent||'').toLowerCase();
      return voiceVisible(e) && e.children.length<=2 && t.indexOf(s.find)>=0 && t.length<120; });
    if(el){ try{ el.scrollIntoView({block:'start', behavior:'smooth'}); }catch(err){} voiceShow('Showing '+s.find+'.'); }
    else voiceShow('Opened that tab. I could not find the '+s.find+' section.');
  }, 400);
  return 'Opening '+s.find+'.';
}
// ---- take numbers, camera switching, zoom and phone camera by voice ----
var VOICE_ORD={first:1,one:1,second:2,two:2,third:3,three:3,fourth:4,four:4,fifth:5,five:5,last:-1,latest:-1};
function voiceTakeCmd(c){
  let m=c.match(/^(delete|erase|remove|scrap|trash|save|download|send|hand|give)( the)? (take|clip|recording) (\\w+)(?: (?:to )?(sage|load|edit))?$/), act, w, tgt;
  if(m){ act=m[1]; w=m[4]; tgt=m[5]; }
  else { m=c.match(/^(delete|erase|remove|scrap|trash|save|download|send|hand|give)( the)? (\\w+) (take|clip|recording)(?: (?:to )?(sage|load|edit))?$/); if(!m) return null; act=m[1]; w=m[3]; tgt=m[5]; }
  const n=/^\\d+$/.test(w) ? parseInt(w,10) : (VOICE_ORD[w]||0);
  return n ? {act:act, n:n, to:tgt||''} : null;
}
function voiceTakeAct(cmd){
  const t=voiceTakeCmd(cmd); if(!t) return 'I did not catch which take.';
  if(voiceRecordingNow()) return 'Stop the recording first, then choose a take.';
  const i=t.n<0 ? rxClips.length-1 : t.n-1;
  if(i<0 || i>=rxClips.length) return 'There is no take '+(t.n>0?t.n:'')+' yet.';
  if(['delete','erase','remove','scrap','trash'].indexOf(t.act)>=0){ rxClipErase(i); return 'Deleted take '+(i+1)+'. Undo is on screen for 6 seconds.'; }
  if(t.act==='save' || t.act==='download'){ rxClipDownload(i); return 'Saving take '+(i+1)+'.'; }
  rxClipSage(i); return 'Sent take '+(i+1)+' to Load.';
}
function voiceSwitchCamera(cmd){
  if(voiceRecordingNow()) return 'Stop the recording first, then switch cameras.';
  const sel=document.getElementById('rxCamSel');
  if(!sel || sel.options.length<2) return 'There is only one camera to switch to.';
  const opts=Array.prototype.slice.call(sel.options);
  let idx=-1;
  if(/front|user|selfie/.test(cmd)) idx=opts.findIndex(function(o){ return /front|user|face/i.test(o.textContent); });
  else if(/back|rear|environment/.test(cmd)) idx=opts.findIndex(function(o){ return /back|rear|environment/i.test(o.textContent); });
  if(idx<0) idx=(sel.selectedIndex+1)%opts.length;
  sel.selectedIndex=idx; sel.dispatchEvent(new Event('change',{bubbles:true}));
  return 'Using '+opts[idx].textContent.trim()+'.';
}
var VOICE_CMDS=[
  {test:function(c){ return /countdown|count down/.test(c) && /^(set |change |make )?(the )?(start )?(countdown|count down)( to| for)?( \\d+| [a-z ]+)?( seconds?)?$/.test(c) && voiceDuration(c,true)>0; }, label:'countdown length', run:function(c){ const n=voiceDuration(c,true); const v=n<=3?3:(n<=5?5:10); rxSetStartCount(String(v)); const sel=document.getElementById('rxCountSel'); if(sel) sel.value=String(v); return 'The countdown before a take is '+v+' seconds.'; }},
  {test:function(c){ const t=voiceTakeCmd(c); return !!t && t.n>0 && voiceInCreate(); }, label:'take by number', run:voiceTakeAct},
  {re:voicePhrases(['switch camera','flip camera','change camera','next camera','switch the camera','use the front camera','use the back camera','use the other camera','use front camera','use back camera','switch to the front camera','switch to the back camera','switch to front camera','switch to back camera','use the selfie camera','use the rear camera']), label:'switch camera', run:voiceSwitchCamera},
  {test:function(c){ return voiceMicCmd(c)!==null; }, label:'switch microphone', run:function(c){ return voiceSwitchMic(voiceMicCmd(c)); }},
  {test:function(c){ return /^(zoom in|zoom out|zoom closer|zoom back|zoom further)$/.test(c) && !!document.getElementById('rxZoomSl'); }, label:'zoom', run:function(c){ if(_bg.on) return voiceBgZoomAct(/out|back/.test(c)?-10:10); return voiceRoomAct(/out|back/.test(c)?'room smaller':'room bigger'); }},
  {test:function(c){ return !!voiceRoomCmd(c); }, label:'room for your hand', run:function(c){ return voiceRoomAct(c); }},
  {test:function(c){ return voiceInCreate() && (/^(write|make|draft|create|give me|start) (me )?(a |an )?script (about|on|for) .+$/.test(c) || /^(talk|speak) about .+$/.test(c)); }, label:'write a script', run:voicePromptWrite},
  {test:function(c){ return /^make it (fifteen|thirty|forty five|sixty|one minute|ninety|two minutes|[0-9]+) (seconds?|minutes?)$/.test(c); }, label:'write a script', run:voicePromptLength},
  {test:function(c){ return /^use (that|this)( script)?$/.test(c) && !!_prmDraft; }, label:'write a script', run:voicePromptUseDraft},
  {test:function(c){ return /^undo (the )?script$/.test(c); }, label:'write a script', run:function(){ return rxPromptUndo() ? 'Put back your previous script.' : 'There is nothing to undo.'; }},
  {re:voicePhrases(['next line','next sentence','go to the next line','next line please']), label:'teleprompter', run:voicePrompt},
  {test:function(c){ return /^(faster|speed up|quicker|slower|slow down|bigger text|make text bigger|larger text|smaller text|make text smaller|edit script|save script)$/.test(c); }, label:'teleprompter', run:voicePrompt},
  {re:/^approve (replies|reply approval) (on|off)$/, label:'reply approval', run:voiceReplyApproval},
  {test:function(c){ return /^(dms?|direct messages?|messages?) (off|automatic|auto|ask me|ask|on|review)$|^turn (off|on) (the )?(dms?|direct messages?)$/.test(c); }, label:'direct messages', run:voiceDmMode},
  {re:/^approve (the |next )?(next )?reply$/, label:'reply approval', run:voiceApproveNext},
  {re:voicePhrases(['mirror on','mirror off','turn mirror on','turn mirror off','turn on mirror','turn off mirror','mirror preview','mirror the preview','turn mirroring on','turn mirroring off','mirror my preview','unmirror preview','unmirror']), label:'mirror preview', run:voiceMirror},
  {re:voicePhrases(['brighter','make it brighter','a bit brighter','brighten','darker','make it darker','a bit darker','darken','dimmer']), label:'brighter or darker', run:voiceBrighter},
  {test:function(c){ return /^(set|make|change|put) brightness (to |at )?[0-9]+( percent)?$/.test(c); }, label:'set brightness', run:voiceSetBright},
  {re:voicePhrases(['talk time','how much am i talking','how much of this is me talking','am i talking enough','how much talking am i doing']), label:'talk time', run:voiceTalkTime},
  {re:voicePhrases(['add a phone','add my phone','connect my phone','phone camera','add a phone camera','use my phone as a camera','add a webcam','add a wireless camera','add another camera','add a camera']), label:'add a phone camera', run:function(){ rxAddCam(); return 'Showing the phone camera setup. Scan the code with your phone on the same wifi.'; }},
  {test:function(c){ return /gestures?/.test(c) && /^(enable|disable|turn on|turn off|switch on|switch off|gestures? on|gestures? off)/.test(c); }, label:'hands-free on or off', run:function(c){ return voiceHandsFree(/enable|turn on|switch on|gestures? on/.test(c) ? 'hands free on' : 'hands free off'); }},
  {re:voicePhrases(['turn off the abra mic','turn off abra mic','abra mic off','turn the abra mic off','turn off voice','voice off','turn voice off','stop listening','mute abra']), label:'voice off', run:voiceOffFromVoice},
  {re:voicePhrases(['hear replies','hear replies on','turn on spoken replies','turn spoken replies on','read answers out loud','read replies out loud','talk to me out loud','turn on replies out loud','speak to me']), label:'hear replies on', run:voiceHearOn},
  {re:voicePhrases(['hear replies off','turn off spoken replies','turn spoken replies off','stop talking to me','be quiet','stop speaking','no more talking','mute replies','stop reading replies']), label:'hear replies off', run:voiceHearOff},
  {re:voicePhrases(['open the chat','show the chat','open abra chat','open the abra chat','show abra chat','open chat','show chat','open the concierge']), label:'open chat', run:function(){ toggleAssist(true); return 'Chat is open.'; }},
  {re:voicePhrases(['close the chat','hide the chat','close abra chat','hide abra chat','close chat','hide chat','close the concierge']), label:'close chat', run:function(){ toggleAssist(false); return 'Chat is closed.'; }},
  {re:voicePhrases(['pause engagement','pause the replies','stop replying','stop replying to comments','pause the bot','pause replies','pause replying','turn off replies','stop the replies','pause comment replies']), label:'pause engagement', run:voicePauseEngagement},
  {re:voicePhrases(['resume engagement','start replying again','resume the replies','turn the replies back on','turn replies on','resume replying','unpause replies','start replying','restart engagement','resume comment replies']), label:'resume engagement', run:voiceResumeEngagement},
  {re:voicePhrases(['undo','undo that','undo the delete','undo delete','bring it back','put it back','restore it','restore the take','bring the take back']), label:'undo', run:voiceUndo},
  {test:function(c){ return /^(talk to|switch to|hand me to|let me talk to|connect me to|get me|back to)? ?(nico|sage|koa|abra)( please)?$/.test(c); }, label:'talk to a crew member', run:voiceTalkTo},
  {test:function(c){ return /^(show|open|go to|take me to|where is|see|find)? ?(me |my |the )?(best time|posting time|when to post|reach|reach trend|trend|watch through|watch time|retention|spend|spending|spent|ai costs|cost ledger|ledger|upcoming|scheduled posts|drafts|draft|next seven days|next 7 days)( section| list| chart| view)?$/.test(c); }, label:'show a section', run:voiceShowSection},
  {test:voiceVideoCmd, label:'make a video of a length', run:voiceMakeVideo},
  {re:voicePhrases(['create a video','make a video','new video','start a video','make a reel','create a reel','new reel','make a clip','create a clip','new clip','make a short','create a short','new short','make a story','film a video','shoot a video','make a new video','create new video','open a new video']), label:'new video', run:voiceNewVideo},
  {test:voiceStartInCmd, label:'start in a few seconds', run:voiceStartIn},
  {test:voiceStopAfterCmd, label:'stop after a time', run:voiceStopAfter},
  {test:voiceSetLengthCmd, label:'length', run:voiceSetLength},
  {re:voicePhrases(['set length','length','change length','set the length','time limit','set time limit','set the timer','set timer']), label:'length', run:voiceSetLength},
  {re:voicePhrases(['no time limit','no limit','remove the limit','remove limit','remove time limit','drop the limit','unlimited','unlimited length','no length limit','no length','as long as i want','as long as i like','record as long as i want','record as long as i like','as long as you want']), label:'no limit', run:voiceNoLimit},
  {test:voiceGate(voicePhrases(['how long','how long is it','how long so far','how long have i been recording','time left','how much time','how much time is left','how much time left','time remaining','how much is left','what is the timer','where is the timer','timer'])), label:'time left', run:voiceTimeLeft},
  {test:voiceGate(voicePhrases(['how many takes','how many clips','how many recordings','how many videos','number of takes','take count','how many takes do i have'])), label:'take count', run:voiceTakeCount},
  {test:voiceGate(voicePhrases(['pause playback','pause the playback','pause the video','pause video','pause it back','stop playback','stop the playback','stop the video','stop video'])), label:'pause playback', run:voicePausePlayback},
  {test:voiceGate(voicePhrases(['play','play it','play it back','play back','playback','play the take','play the take back','play that','play that back','preview','preview it','preview the take','watch it','watch it back','watch the take','replay','replay it','let me see it','show me the take','review it','review the take'])), label:'play', run:voicePlay},
  {re:voicePhrases(['open camera','start camera','turn on camera','turn camera on','camera on','show camera','show me the camera','open the camera','start the camera','bring up the camera','turn the camera on']), label:'open camera', run:voiceOpenCam},
  {re:voicePhrases(['close camera','hide camera','turn off camera','turn camera off','camera off','turn the camera off','close the camera','shut off camera','shut the camera off','stop camera','stop the camera','hide the camera']), label:'close camera', run:voiceCloseCam},
  {re:voicePhrases(['record','start','start recording','start the recording','start a take','start take','start the take','begin','begin recording','begin the recording','begin the take','begin a take','record now','hit record','press record','roll','roll camera','roll the camera','lets go','let us go','start filming','start shooting','record a take','record take','record the take','record it','go ahead and record','start recording now','we are rolling']), label:'record', run:voiceRecord},
  {test:voiceGate(voicePhrases(['resume','resume recording','resume the recording','resume the take','continue','continue recording','continue the recording','keep going','keep recording','keep rolling','unpause','unpause recording','go on','carry on','back to recording'])), label:'resume', run:voiceResume},
  {test:voiceGate(voicePhrases(['pause','pause recording','pause the recording','pause the take','pause it','hold','hold recording','hold on','hold it','hold up','take a break','freeze','freeze the take','pause filming'])), label:'pause', run:voicePause},
  {test:voiceGate(voicePhrases(['stop','stop recording','stop the recording','stop the take','stop take','stop filming','stop it','stop shooting','end','end recording','end the recording','end the take','end take','end it','end filming','finish','finish recording','finish the recording','finish the take','finish take','wrap','wrap it','wrap up','wrap that','that is a wrap','thats a wrap',"that's a wrap",'that s a wrap','we are done','im done','i am done','and recording','and the recording','and stop','stop there','stop the countdown','cancel the countdown','cancel recording','cut recording','end the shot','stop the shot','done recording'])), label:'stop', run:voiceStopRec},
  {test:voiceGate(voicePhrases(['done'])), label:'done', run:voiceDone},
  {test:voiceGate(voicePhrases(['cut','cut it'])), label:'cut', run:voiceCut},
  {test:voiceGate(voicePhrases(['new take','another take','a new take','one more take','retake','re take','redo','redo it','redo that','do it again','again','one more time','start over','try again','try that again','record again','take two','second take','another one','new one','new recording'])), label:'new take', run:voiceNewTake},
  {test:voiceGate(voicePhrases(['delete last take','delete the last take','delete that take','delete that','delete that one','delete last one','delete the last one','delete last','delete latest take','delete the latest take','remove last take','remove the last take','remove that take','remove that','remove last one','remove the last one','erase last take','erase the last take','erase that','erase that take','erase it','scrap that','scrap it','scrap the take','scrap the last take','scrap last take','trash that','trash it','trash the take','trash the last take','throw that out','throw it out','throw the take out','throw away that take','discard that','discard the take','discard last take','get rid of that','get rid of the take','get rid of that take','bin that','bin the take'])), label:'delete last take', run:voiceDelete},
  {test:voiceGate(voicePhrases(['delete','remove','erase'])), label:'delete last take', run:voiceDeleteAsk},
  {test:voiceGate(voicePhrases(['send to load','send it to load','send this to load','send that to load','send the take to load','send last take to load','send latest take to load','send the last take to load','send it','send this','send that','send it over','send over','next','next take','take this to load','take it to load','take that to load','load it','load this','load that','load the take','load it up','finish and send','done and send','use this','use it','use that','use the take','send the take','send last take','send latest take','send latest','send last','send it to edit','edit this','edit it','edit that','sage this'])), label:'send to load', run:voiceSend},
  {test:voiceGate(voicePhrases(['send to load','send it to load','send this to load','send that to load','send this video to load','send the video to load','load this','load it','load this video','load the video','load that','put this in load','put it in load','take this to load','take it to load'])), label:'send the take to Load', run:voiceSendToLoad},
  {test:voiceGate(voicePhrases(['hand to sage','hand it to sage','hand this to sage','hand that to sage','hand it over to sage','send to sage','send it to sage','send this to sage'])), label:'hand to Sage', run:voiceHandToSage},
  {re:/(^| )hands[- ]?free( on| off)$|^(turn )?(on|off) hands[- ]?free$/, label:'hands-free on or off', run:voiceHandsFree},
  {re:/^(turn )?(on|off) captions?$|^captions? (on|off)$|^turn captions? (on|off)$/, label:'captions on or off', run:voiceCaptions},
  {re:/^(turn )?(on|off) (teleprompter|prompter|script)$|^(teleprompter|prompter) (on|off)$|^(show|hide) (teleprompter|prompter|script)$/, label:'teleprompter on or off', run:voiceTeleprompter},
  {re:/^((show|hide) sides|(turn )?(on|off) sides|sides (on|off))$/, label:'sides on or off', run:voiceSides},
  {re:/^(smooth|smooth cut|smooth (cut|take)|tighten( up)?( cut| take)?)$/, label:'smooth cut', run:voiceSmoothCut},
  {re:/^(open |show )?(camera settings|camera options|camera panel)$/, label:'camera settings', run:voiceCameraPanel},
  {re:/^(scroll )?(up|down|to (the )?top|to (the )?bottom|top|bottom)$|^scroll (up|down|to top|to bottom)$/, label:'scroll', run:voiceScroll},
  {re:/^(open |go to |go |show |take me to )?(create|launchpad|film)$/, label:'open create', run:function(){ return voiceGo('film','Opening Create.'); }},
  {re:/^(open |go to |go |show |take me to )?load( tab| view)?$/, label:'open load', run:function(){ return voiceGo('studio','Opening Load.'); }},
  {re:/^(open |go to |go |show |take me to )?(engagement|engage|replies|comments)( tab)?$/, label:'open engagement', run:function(){ return voiceGo('engage','Opening Engagement.'); }},
  {re:/^(open |go to |go |show |take me to )?(insights?|analytics)$/, label:'open insights', run:function(){ return voiceGo('analytics','Opening Insights.'); }},
  {re:/^(open |go to |go |show |take me to )?(home|dashboard|calendar|tasks|to do|to dos)$/, label:'open home', run:function(){ return voiceGo('home','Opening Home.'); }},
  {re:/^(open |go to |go |show |take me to )?(brand )?deals$/, label:'open deals', run:function(){ return voiceGo('deals','Opening Brand Deals.'); }},
  {re:/^(open |go to |go |show |take me to )?(account|settings|profile|money|earnings|earned|billing)$/, label:'open account', run:function(){ return voiceGo('profile','Opening Account.'); }},
  {re:/^(approve|publish|schedule)( this| it| that)?( post)?$/, label:'approve', wake:true, run:voiceApprove},
  {re:/^(confirm|yes|do it|go ahead)$/, label:'confirm', wake:true, run:voiceConfirm},
  {re:/^(cancel|no|never mind)$/, label:'cancel', wake:true, run:voiceCancel},
  {re:/^(help|what can you do|commands|what can i say)$/, label:'help', run:voiceHelp}
];
// ---- live Meta model calls: a pill shows any Muse / SAM call while it runs; click it for the recent ones ----
var LIVE_META={lastBusy:0, open:false};
function liveMetaRender(s){
  var pill=document.getElementById('liveMeta'), list=document.getElementById('liveMetaList'); if(!pill) return;
  var act=(s&&s.active)||[], rec=(s&&s.recent)||[], txt=pill.querySelector('.lm-text');
  if(act.length){
    LIVE_META.lastBusy=Date.now(); var a=act[0];
    txt.textContent='Meta live: '+a.model+' · '+a.task+' · '+Number(a.seconds).toFixed(1)+'s'+(act.length>1?' (+'+(act.length-1)+' more)':'');
    pill.classList.add('busy'); pill.classList.add('show');
  } else if(rec.length && Date.now()-LIVE_META.lastBusy<6000){
    var last=rec[rec.length-1];
    txt.textContent='Meta: '+last.model+' · '+last.task+(last.ok?' · done':' · failed ('+(last.error||last.status)+')');
    pill.classList.remove('busy'); pill.classList.add('show');
  } else { pill.classList.remove('busy'); pill.classList.remove('show'); }
  if(list && LIVE_META.open){
    list.innerHTML='';
    if(!rec.length){ list.textContent='No Meta model calls yet this session.'; }
    rec.slice().reverse().forEach(function(c){
      var row=document.createElement('div'); row.className='lr';
      var l=document.createElement('span'); l.textContent=c.model+' · '+c.task;
      var r=document.createElement('span'); r.textContent=(c.ok?'ok':'failed')+' · '+c.seconds+'s';
      row.appendChild(l); row.appendChild(r); list.appendChild(row);
    });
  }
}
function liveMetaToggle(){
  var list=document.getElementById('liveMetaList'); LIVE_META.open=!LIVE_META.open;
  if(list) list.classList.toggle('open', LIVE_META.open); liveMetaPoll();
}
function liveMetaPoll(){
  if(document.hidden) return;
  fetch('/live',{cache:'no-store'}).then(function(r){ return r.ok ? r.json() : null; }).then(function(s){ if(s) liveMetaRender(s); }).catch(function(){});
}
document.addEventListener('DOMContentLoaded', function(){ liveMetaPoll(); setInterval(liveMetaPoll, 1500); });
// a file dropped outside a drop zone must not make the browser open it and leave the studio
window.addEventListener('dragover', function(e){ e.preventDefault(); });
window.addEventListener('drop', function(e){ e.preventDefault(); });
// ---- the ? beside "Add a camera from another device": every recording feature and gesture in one place ----
function rxHelpOpen(){
  const h=document.getElementById('rxHelp'); if(!h) return;
  h.style.display='flex';
  const x=h.querySelector('.rxhelp-x'); if(x) x.focus();
}
function rxHelpClose(){ const h=document.getElementById('rxHelp'); if(h) h.style.display='none'; }
document.addEventListener('keydown', function(e){ if(e.key==='Escape') rxHelpClose(); });
function assistSend(){ warmupSpeech(); const t=document.getElementById('assistText'); const v=(t.value||'').trim(); if(v){ t.value=''; sendAssist(v); } }
async function sendAssist(msg, opts){
  if (assistBusy || !msg) return;
  const speakIt = !!(opts && opts.speak);   // a question asked by voice is answered out loud
  const c=document.getElementById('assistChips'); if(c) c.innerHTML='';
  assistAppend('me', msg); assistHistory.push({role:'user', content:msg});
  assistBusy = true;
  const typing = assistAppend('muse', 'Abra is thinking…'); typing.classList.add('typing');
  let reply='';
  const ctl=new AbortController(); const limit=setTimeout(function(){ ctl.abort(); }, 60000);
  try{
    const r = await fetch('/assistant', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message:msg, history:assistHistory.slice(0,-1), persona:assistPersona}), signal:ctl.signal});
    const d = await r.json(); clearTimeout(limit); typing.remove();
    reply = d.reply || 'Hmm, I did not catch that — try again?';
    assistAppend('muse', reply); assistHistory.push({role:'assistant', content:reply});
    assistMorph(d.agent||null);
    if (d.action) doAssistAction(d.action);
    if (!voiceChatOn && !soundOn && assistAgent) talkBurst();   // muted: still bob briefly
  }catch(e){
    clearTimeout(limit); typing.remove();
    assistAppend('muse', ctl.signal.aborted ? 'That took too long — try again?' : 'Connection hiccup — try again?');
  }
  if (reply && (voiceChatOn || soundOn || speakIt)) speakReply(reply);   // speakReply clears busy + resumes listening + animates the orb
  else { assistBusy=false; if(voiceChatOn) listenOnce(); }
}
const AGENT_IMG = {nico:'/crew/cat.png', sage:'/crew/dog.png', koa:'/crew/koala.png', };
function assistMorph(agent){
  assistAgent = agent || null;
  // Abra stays on the orb for every chat: crew members speak in their own voice, never swap the avatar
  const isChar = false;
  const src = '/crew/orb.png';
  const btn = document.getElementById('assistBtn'); if (btn) btn.classList.toggle('ischar', isChar);
  ['assistOrbSm','assistBtnImg'].forEach(function(id){
    const im=document.getElementById(id); if(!im) return;
    im.classList.toggle('ischar', isChar);
    if (im.getAttribute('src')===src) return;
    im.classList.remove('morphing'); void im.offsetWidth; im.classList.add('morphing');
    setTimeout(function(){ im.src=src; }, 190);
  });
}
function doAssistAction(a){
  if(!a) return;
  const map={launchpad:'studio',engagement:'engage',boost:'videos',calendar:'calendar',analytics:'analytics',money:'money',deals:'deals',pages:'pages'};
  try{
    if(a.type==='open' && map[a.view]) showView(map[a.view]);
    else if(a.type==='read'){ showView('analytics'); if(typeof loadAnalyst==='function') loadAnalyst(true); }
  }catch(e){}
}
// ---- spoken replies (works without the mic / without HTTPS) ----
let soundOn=true, _speechWarmed=false, _ttsAudio=null;
try{ soundOn = (localStorage.getItem('abraSound') !== '0'); }catch(e){}
function updateSoundUI(){ var b=document.getElementById('assistSound'); if(b){ b.classList.toggle('muted', !soundOn);
  b.title = soundOn ? 'Replies read aloud — tap to mute' : 'Replies muted — tap to hear them'; } }
// one reusable <audio> element so iOS stays "unlocked" after the first gesture
function _getTtsAudio(){ if(!_ttsAudio){ _ttsAudio=new Audio(); _ttsAudio.preload='auto'; } return _ttsAudio; }
// iOS/Safari only allow speech + audio playback after a user gesture; prime both on the first tap
function warmupSpeech(){
  try{ if(window.speechSynthesis && !_speechWarmed){ var u=new SpeechSynthesisUtterance(' '); u.volume=0; window.speechSynthesis.speak(u); } }catch(e){}
  try{ if(!_speechWarmed){ var a=_getTtsAudio();
    a.src='data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAABErAAABAAgAZGF0YQAAAAA=';
    var p=a.play(); if(p&&p.catch) p.catch(function(){}); } }catch(e){}
  _speechWarmed=true;
}
function toggleSound(){ soundOn=!soundOn; try{ localStorage.setItem('abraSound', soundOn?'1':'0'); }catch(e){}
  updateSoundUI();
  if(soundOn){ warmupSpeech(); }
  else { try{ if(window.speechSynthesis) window.speechSynthesis.cancel(); }catch(e){} setSpeaking(false); speaking=false; } }
// ---- hands-free voice chat (browser speech) ----
let voiceChatOn=false, recog=null, speaking=false, assistAgent=null;
// each crew member (and the orb = Abra, defaulting to Sage's voice) gets its own voice + pitch/pace
const VOICE_PROFILES = {
  sage:  { names:['Kate','Serena','Stephanie','Martha','Shelley','Flo','Sandy','Google UK English Female','Fiona','Microsoft Zira Desktop','Microsoft Hazel Desktop'], lang:'en-GB', female:true, rate:1.04, pitch:1.06 },
  nico:{ names:['Daniel','Google UK English Male','Alex','Oliver','Fred','Microsoft David Desktop','Microsoft Mark Desktop','Microsoft George Desktop'], rate:1.0, pitch:1.02 },
  koa:   { names:['Rishi','Google UK English Male','Ralph','Daniel','Alex','Microsoft Ryan Desktop','Microsoft George Desktop','Microsoft Mark Desktop'], rate:0.9, pitch:0.78 }
};
let _voiceMap = null;
function assignVoices(){
  const vs = ((window.speechSynthesis && window.speechSynthesis.getVoices()) || []).filter(x=>/^en/i.test(x.lang||''));
  if (!vs.length) return null;
  const order = ['sage','nico','koa']; const used = new Set(); const map = {};
  const FEM = ['kate','serena','stephanie','martha','shelley','flo','sandy','fiona','grandma','samantha','karen','moira','tessa','victoria','allison','hazel','zira','susan','catherine','ava','nora'];
  // naturalness score: network + enhanced/neural/Siri voices sound far less robotic than compact system voices
  function vq(x){ let s=0; const n=(x.name||'').toLowerCase();
    if(x.localService===false) s+=6;
    if(/premium|enhanced|neural|natural/.test(n)) s+=5;
    if(/siri/.test(n)) s+=4;
    if(/google/.test(n)) s+=3;
    if(/(eddy|flo|reed|rocko|sandy|shelley|grandma|grandpa|jamie)/.test(n)) s+=2; // newer expressive set
    return s; }
  function best(pool){ return pool.length ? pool.slice().sort((a,b)=>vq(b)-vq(a))[0] : null; }
  order.forEach(function(a){
    const prof = VOICE_PROFILES[a], lang = prof.lang, wantFem = prof.female;
    const avail = vs.filter(x=>!used.has(x.name));
    const named = prof.names.map(n=>n.toLowerCase());
    const nm=(x)=> named.some(n=>x.name && x.name.toLowerCase().indexOf(n)>=0);
    const lm=(x)=> (x.lang||'').toLowerCase().indexOf((lang||'').toLowerCase())>=0;
    const fm=(x)=> FEM.some(f=>x.name && x.name.toLowerCase().indexOf(f)>=0);
    let pool=[];
    if(lang && wantFem)        pool = avail.filter(x=>lm(x) && fm(x));   // accent + female
    if(!pool.length && lang)   pool = avail.filter(x=>lm(x) && nm(x));   // accent + named
    if(!pool.length && lang)   pool = avail.filter(x=>lm(x));           // accent, anyone
    if(!pool.length)           pool = avail.filter(x=>nm(x));           // named, any accent
    if(!pool.length && wantFem)pool = avail.filter(x=>fm(x));           // any female
    if(!pool.length)           pool = avail;                            // anyone
    const pick = best(pool) || avail[0] || vs[0];
    if(pick) used.add(pick.name);
    map[a]=pick;
  });
  return map;
}
function voiceFor(agent){ if(!_voiceMap) _voiceMap = assignVoices(); return _voiceMap ? _voiceMap[agent] : null; }
try{ if(window.speechSynthesis) window.speechSynthesis.addEventListener('voiceschanged', function(){ _voiceMap=null; }); }catch(e){}
function updateMicUI(){ const m=document.getElementById('assistMic'); if(m) m.classList.toggle('rec', voiceChatOn); }
function assistVoice(){
  if(!ABRA_VOICE_ON) return;
  if(recording){ try{ if(_rec && _rec.state!=='inactive') _rec.stop(); }catch(e){} return; }
  if(voiceChatOn) stopVoiceChat(); else startVoiceChat();
}
function startVoiceChat(){
  if(VOICE.on){ voiceStop(); voiceShow('Voice commands paused while the chat mic is on.'); }
  var secure = window.isSecureContext || location.hostname==='localhost' || location.hostname==='127.0.0.1';
  if(!secure){
    assistAppend('muse', 'To talk out loud I need mic access, which phone browsers only allow over a secure (https) link — not plain http. Type your question and I will still answer aloud.');
    return;
  }
  // hands-free browser speech recognition where supported (desktop/Android Chrome)
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(SR){
    voiceChatOn=true; updateMicUI(); warmupSpeech();
    assistAppend('muse', 'Voice chat is on — just talk to me, and tap the mic again to stop.');
    listenOnce(); return;
  }
  // no SpeechRecognition (e.g. iPhone): record + transcribe on the server (Meta Muse Voice)
  if(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && typeof MediaRecorder!=='undefined'){ startRecordVoice(); return; }
  assistAppend('muse', 'Voice input is not available in this browser — type your question and I will answer aloud.');
}
// --- push-to-talk recording -> /assistant/voice (works on iOS too, needs https) ---
let _rec=null, _recChunks=[], _recStream=null, recording=false;
async function startRecordVoice(){
  try{ _recStream = await navigator.mediaDevices.getUserMedia({audio:true}); }
  catch(e){ assistAppend('muse', 'I need mic permission — allow it and tap the mic again. You can also just type.'); return; }
  warmupSpeech(); _recChunks=[];
  try{
    var mime = (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported('audio/webm')) ? 'audio/webm' : '';
    _rec = mime ? new MediaRecorder(_recStream,{mimeType:mime}) : new MediaRecorder(_recStream);
  }catch(e){ try{ _rec = new MediaRecorder(_recStream); }catch(e2){ _stopTracks(); assistAppend('muse','Recording is not supported here — type instead.'); return; } }
  _rec.ondataavailable=function(e){ if(e.data && e.data.size) _recChunks.push(e.data); };
  _rec.onstop=function(){ _stopTracks(); transcribeVoice(new Blob(_recChunks, {type:(_rec&&_rec.mimeType)||'audio/webm'})); };
  recording=true; voiceChatOn=true; updateMicUI();
  assistAppend('muse', 'Listening… tap the mic again when you are done.');
  _rec.start();
}
function _stopTracks(){ try{ if(_recStream){ _recStream.getTracks().forEach(function(t){t.stop();}); _recStream=null; } }catch(e){} }
async function transcribeVoice(blob){
  recording=false; voiceChatOn=false; updateMicUI();
  var typing=assistAppend('muse', 'Transcribing…'); typing.classList.add('typing');
  try{
    var r=await fetch('/assistant/voice', {method:'POST', headers:{'Content-Type':'application/octet-stream'}, body:blob});
    var d=await r.json(); typing.remove();
    var txt=(d.text||'').trim();
    if(d.error){ assistAppend('muse', d.error); return; }
    if(txt){ sendAssist(txt); } else { assistAppend('muse', 'I did not catch that — try again, or type it.'); }
  }catch(e){ typing.remove(); assistAppend('muse', 'Could not transcribe that — you can type instead.'); }
}
function stopVoiceChat(){ voiceChatOn=false; recording=false; updateMicUI();
  try{ if(recog) recog.abort(); }catch(e){}
  try{ if(_rec && _rec.state!=='inactive') _rec.stop(); }catch(e){} _stopTracks();
  try{ if(window.speechSynthesis) window.speechSynthesis.cancel(); }catch(e){} speaking=false; }
function listenOnce(){
  if(!voiceChatOn || assistBusy || speaking) return;
  try{ if(_bargeRec){ var _b=_bargeRec; _bargeRec=null; _b.abort(); } }catch(e){}
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition; if(!SR) return;
  try{
    recog=new SR(); recog.lang='en-US'; recog.interimResults=false; recog.maxAlternatives=1;
    recog.onresult=function(e){ const txt=(e.results[0][0].transcript||'').trim(); if(txt) sendAssist(txt); };
    recog.onerror=function(e){ if(e.error==='not-allowed'||e.error==='service-not-allowed'){ assistAppend('muse', e.error==='not-allowed' ? 'I need mic permission for voice chat — you can type instead.' : 'Speech recognition is not available in this browser — you can type instead.'); stopVoiceChat(); } };
    recog.onend=function(){ if(voiceChatOn && !assistBusy && !speaking) setTimeout(listenOnce, 400); };
    recog.start();
  }catch(e){}
}
// chat always starts as Abra: the orb is the only chooser now
let assistPersona='abra';
// ---- barge-in: start talking over Abra and she yields the floor (needs the neural voice + speech recognition) ----
let _bargeRec=null, _spokenSet={};
function _wordsOf(t){ return (t||'').toLowerCase().replace(/[^a-z0-9' ]/g,' ').split(' ').filter(function(w){ return w.length>1; }); }
function _isEcho(words){ if(!words.length) return true; var n=0; words.forEach(function(w){ if(_spokenSet[w]) n++; }); return (n/words.length)>=0.6; }
function stopSpeaking(){
  try{ if(_ttsAudio){ _ttsAudio.pause(); } }catch(e){}
  try{ if(window.speechSynthesis) window.speechSynthesis.cancel(); }catch(e){}
  speaking=false; setSpeaking(false); assistBusy=false;
}
function bargeListen(){
  if(!voiceChatOn || !speaking) return;
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition; if(!SR) return;
  try{
    const r=new SR(); r.lang='en-US'; r.interimResults=true; r.maxAlternatives=1;
    _bargeRec=r;
    r.onresult=function(e){
      if(_bargeRec!==r) return;
      var last=e.results[e.results.length-1], heard='';
      for(var i=0;i<e.results.length;i++){ heard+=' '+e.results[i][0].transcript; }
      heard=heard.trim();
      var words=_wordsOf(heard);
      if(!words.length || _isEcho(words)) return;       // her own voice leaking back into the mic
      if(speaking && words.length>=3) stopSpeaking();    // she stops as soon as you have said a few real words
      if(last.isFinal){
        stopSpeaking(); _bargeRec=null;
        try{ r.abort(); }catch(_){}
        sendAssist(heard);
      }
    };
    r.onend=function(){ if(_bargeRec!==r) return; _bargeRec=null; if(voiceChatOn && !assistBusy && !speaking) setTimeout(listenOnce, 400); };
    r.onerror=function(){};
    r.start();
  }catch(e){}
}
async function speakReply(text){
  if(!ABRA_VOICE_ON){ assistBusy=false; return; }
  const who = (assistAgent && VOICE_PROFILES[assistAgent]) ? assistAgent : 'abra';   // the orb is Abra: its own generic voice, not a crew voice
  const clean = text.replace(/[*#_>]/g,'');
  _spokenSet={}; _wordsOf(clean).forEach(function(w){ _spokenSet[w]=1; });
  noteSpoken(clean);
  if(who==='abra'){ browserSpeak(clean, who); return; }   // Abra always speaks in the browser's default voice, as it did when voice was first turned on
  // 1) neural voice (server). The creator should never wait long for it: after 3 seconds the browser's own
  //    voice speaks, and the neural request is cancelled so the two never play together.
  const ctl=new AbortController(); let fellBack=false;
  const cap=setTimeout(function(){ fellBack=true; ctl.abort(); browserSpeak(clean, who); }, 3000);
  try{
    const r = await fetch('/assistant/tts', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:clean, agent:who}), signal:ctl.signal});
    clearTimeout(cap);
    if (fellBack) return;
    const ct = (r.headers.get('content-type')||'');
    if (r.ok && ct.indexOf('audio')>=0){
      const url = URL.createObjectURL(await r.blob());
      const audio = _getTtsAudio();   // reuse the gesture-unlocked element (iOS)
      audio.onended=function(){ speaking=false; setSpeaking(false); assistBusy=false; URL.revokeObjectURL(url); if(voiceChatOn) listenOnce(); };
      audio.onerror=function(){ speaking=false; setSpeaking(false); URL.revokeObjectURL(url); browserSpeak(clean, who); };
      audio.src=url; speaking=true; setSpeaking(true);
      if(voiceChatOn) setTimeout(bargeListen, 450);
      try{ await audio.play(); return; }catch(e){ speaking=false; setSpeaking(false); URL.revokeObjectURL(url); browserSpeak(clean, who); return; }
    }
  }catch(e){ clearTimeout(cap); if (fellBack) return; }
  // 2) fallback: browser speech (best local/enhanced voice)
  browserSpeak(clean, who);
}
function browserSpeak(clean, who){
  let done=false, wd=null;
  const finish=function(){ if(done) return; done=true; clearTimeout(wd); speaking=false; setSpeaking(false); assistBusy=false; if(voiceChatOn) listenOnce(); };
  try{
    if(!window.speechSynthesis){ finish(); return; }
    window.speechSynthesis.cancel();
    const isAbra = (who==='abra');
    const prof = isAbra ? {rate:1.05, pitch:1.0} : (VOICE_PROFILES[who] || VOICE_PROFILES.sage);
    const say=clean.replace(/(^|[^A-Za-z])Abra(?![A-Za-z])/gi, '$1Ab-ruh');   // said as Ab-ruh; the screen keeps the names
    const u=new SpeechSynthesisUtterance(say);
    if(!isAbra){ const v=voiceFor(who); if(v) u.voice=v; u.lang='en-US'; }   // crew keep their own voices; Abra keeps the browser default
    u.rate=prof.rate; u.pitch=prof.pitch; speaking=true;
    u.onstart=function(){ setSpeaking(true); };
    u.onend=finish;
    u.onerror=finish;
    wd=setTimeout(finish, 4000 + clean.length*90);   // some browsers never fire onend
    window.speechSynthesis.speak(u);
  }catch(e){ finish(); }
}
</script></body></html>"""


def _sc_bin(name):
    import shutil as _sh, os
    cands = {"ffmpeg": ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"),
             "ffprobe": ("/usr/local/bin/ffprobe", "/opt/homebrew/bin/ffprobe")}[name]
    return _sh.which(name) or next((p for p in cands if os.path.exists(p)), None)


def _sc_duration(ffprobe, path):
    # webm per-stream duration is often N/A -> read format=duration
    import subprocess as _sp
    try:
        out = _sp.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=nw=1:nk=1", path],
                      capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def _sc_has_audio(ffprobe, path):
    # exit code is 0 even with no audio -> test for NON-EMPTY stdout
    import subprocess as _sp
    try:
        out = _sp.run([ffprobe, "-v", "error", "-select_streams", "a",
                       "-show_entries", "stream=index", "-of", "csv=p=0", path],
                      capture_output=True, text=True, timeout=30).stdout.strip()
        return bool(out)
    except Exception:
        return False


def _sc_parse_silences(stderr_text, total):
    import re
    rs = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
    re_ = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")
    out, cur = [], None
    for line in stderr_text.splitlines():          # parse PER LINE
        m = rs.search(line)
        if m:
            s = max(0.0, float(m.group(1)))        # clamp ffmpeg's occasional -0.0015
            cur = {"start": s, "end": None}; out.append(cur); continue
        m = re_.search(line)
        if m and cur is not None and cur["end"] is None:
            cur["end"] = float(m.group(1)); cur = None
    clean = []
    for s in out:
        a = max(0.0, min(s["start"], total))
        b = max(0.0, min(s["end"] if s["end"] is not None else total, total))  # trailing silence -> EOF
        if b > a:
            clean.append((a, b))
    clean.sort()
    return clean


def _sc_invert_to_keeps(silences, total, pad):
    keeps, cursor = [], 0.0
    for a, b in silences:
        if a - cursor > 0: keeps.append([cursor, a])   # leading silence handled (cursor=0)
        cursor = max(cursor, b)
    if cursor < total: keeps.append([cursor, total])
    padded = [[max(0.0, a - pad), min(total, b + pad)] for a, b in keeps]  # clamp to [0,total]
    merged, eps = [], 0.001
    for a, b in padded:                               # merge padded edges that touch
        if merged and a <= merged[-1][1] + eps: merged[-1][1] = max(merged[-1][1], b)
        else: merged.append([a, b])
    return [(a, b) for a, b in merged if (b - a) > 0.02]  # drop micro-segments


def smooth_cut(src_path, out_path, noise_db=-30.0, min_silence=0.6, pad=0.10,
               min_clip=1.5, log=None):
    """Local-only dead-space remover (silencedetect -> invert-to-keep -> trim/concat -> mp4).
    Returns (ok, removed_seconds, out_path, reason). ok=False -> caller keeps the original.
    reason in: too_short | no_audio | no_silence | all_silent | error"""
    import subprocess as _sp, os
    ffmpeg, ffprobe = _sc_bin("ffmpeg"), _sc_bin("ffprobe")
    if not ffmpeg or not ffprobe:
        return (False, 0.0, src_path, "error")
    total = _sc_duration(ffprobe, src_path)
    if total <= 0 or total < min_clip:                       # edge: clip < ~1.5s
        return (False, 0.0, src_path, "too_short")
    if not _sc_has_audio(ffprobe, src_path):                 # edge: no audio track
        return (False, 0.0, src_path, "no_audio")
    # PASS 1 - analysis only; silencedetect writes to STDERR
    try:
        p = _sp.run([ffmpeg, "-hide_banner", "-i", src_path,
                     "-af", "silencedetect=noise=%gdB:d=%g" % (noise_db, min_silence),
                     "-f", "null", "-"], capture_output=True, text=True, timeout=120)
    except Exception as e:
        if log: log.warning("smoothcut detect failed: %s", str(e)[:200])
        return (False, 0.0, src_path, "error")
    silences = _sc_parse_silences(p.stderr, total)
    if not silences:                                         # no silence -> return original
        return (False, 0.0, src_path, "no_silence")
    keeps = _sc_invert_to_keeps(silences, total, pad)
    if not keeps:                                            # entirely silent
        return (False, 0.0, src_path, "all_silent")
    kept = sum(b - a for a, b in keeps)
    if (total - kept) < 0.3:                                 # already tight: don't re-encode
        return (False, 0.0, src_path, "no_silence")
    # PASS 2 - trim/atrim + PTS-STARTPTS + concat, one re-encode, graph via file
    fc = out_path + ".fc.txt"
    rows = []
    for i, (a, b) in enumerate(keeps):
        rows.append("[0:v]trim=%.3f:%.3f,setpts=PTS-STARTPTS[v%d];" % (a, b, i))
        rows.append("[0:a]atrim=%.3f:%.3f,asetpts=PTS-STARTPTS,"
                    "aresample=async=1:first_pts=0[a%d];" % (a, b, i))
    rows.append("".join("[v%d][a%d]" % (i, i) for i in range(len(keeps)))
                + "concat=n=%d:v=1:a=1[outv][outa]" % len(keeps))
    try:
        with open(fc, "w", encoding="utf-8") as f:
            f.write("\n".join(rows))
        _sp.run([ffmpeg, "-y", "-hide_banner", "-i", src_path,
                 "-/filter_complex", fc, "-map", "[outv]", "-map", "[outa]",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-r", "30",
                 "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                 "-movflags", "+faststart", out_path],
                check=True, capture_output=True, timeout=180)
    except Exception as e:
        if log: log.warning("smoothcut encode failed: %s", str(e)[:200])
        return (False, 0.0, src_path, "error")
    finally:
        try: os.remove(fc)
        except Exception: pass
    new = _sc_duration(ffprobe, out_path)
    return (True, max(0.0, total - new), out_path, "ok")



def captions_take(src_path, out_path):
    """Muse hears the take, then the captions are burned in. Returns (ok, reason); reason is no_speech or not_portrait on a skip."""
    import content_pipeline as cp
    db = db_conn()
    try:
        text, _n = cp.transcribe_file(src_path, db)
    finally:
        db.close()
    if not str(text or "").strip():
        return (False, "no_speech")
    try:
        cp.burn_reel_captions(src_path, str(text), out_path)
    except cp.CaptionSkip:
        return (False, "not_portrait")
    return (True, "ok")


def _rights_ensure(db):
    db.execute("CREATE TABLE IF NOT EXISTS content_rights (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "title TEXT, fingerprint TEXT, note TEXT, created_at TEXT NOT NULL)")


def rights_list():
    db = db_conn()
    _rights_ensure(db)
    rows = db.execute("SELECT id,title,fingerprint,note,created_at FROM content_rights "
                      "ORDER BY id DESC LIMIT 100").fetchall()
    db.close()
    return [{"id": r[0], "title": r[1], "fingerprint": r[2], "note": r[3], "created_at": r[4]} for r in rows]


def rights_add(title, note=""):
    import datetime, hashlib
    db = db_conn()
    _rights_ensure(db)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    fp = hashlib.sha256((str(title) + now).encode()).hexdigest()[:16]
    db.execute("INSERT INTO content_rights (title,fingerprint,note,created_at) VALUES (?,?,?,?)",
               (str(title)[:200], fp, str(note)[:300], now))
    db.commit(); db.close()
    return {"ok": True, "created_at": now, "fingerprint": fp}


def rights_dispute(title, claim):
    # Koa drafts a counter-notification for a claim on the creator's OWN original work (Muse Wonder).
    instr = ("You are Koa, the money-and-legal member of " + B.CREATOR_REF + "'s AI crew. A copyright claim or "
             "takedown has been filed against content the creator ORIGINALLY created and owns. Draft a firm, "
             "professional counter-notification / dispute they can submit: assert they are the original creator "
             "and rights holder, that the work is their own original content, and request the claim be withdrawn "
             "and any restriction lifted. Note that Abra keeps a timestamped record of their authorship. Keep it "
             "concise and confident, use [bracketed placeholders] for names/dates/links, and end with one line "
             "that this is a draft, not legal advice, and a serious claim may need an attorney.")
    user = "Content title: " + (str(title) or "(untitled)") + "\n\nThe claim / notice received:\n" + (str(claim) or "")[:6000]
    try:
        return _muse_wonder(instr, user, effort="low")
    except Exception as e:
        return "Draft failed: " + str(e)[:160]



def crew_read() -> dict:
    """One-time setup read for the welcome screen: each crew member surfaces ONE real
    finding about the just-connected creator. Sage = Muse Wonder, Nico = Muse Spark,
    Koa = Facebook Graph (no model). Honest: real data only; an empty finding lets the UI
    show an honest 'starting to learn' line. Caches are cleared on /connect/finish so this
    never shows a previously-connected creator's numbers."""
    try:
        pages = [str(nm).strip() for (_pid, nm, _tok) in enabled_pages()]
    except Exception:
        pages = []
    try:
        watch = reel_watch_insights(limit=8)
    except Exception:
        watch = []
    try:
        mon = monetization_status()
    except Exception:
        mon = {}
    try:
        posts = fetch_posts("recent")
    except Exception:
        posts = []
    scanned = {"pages": pages, "posts": len(posts),
               "reels": len([w for w in watch if w.get("watched_pct") is not None])}

    # Sage (Muse Wonder): the format/length that works, one confident line
    sage = ""
    try:
        wlines = "; ".join(
            "%ds held %s%% (%s views)" % (w.get("len") or 0, w.get("watched_pct"), w.get("views") or 0)
            for w in watch if w.get("watched_pct") is not None)[:800]
        if wlines:
            instr = ("You are Sage, a warm Reels coach meeting this creator for the first time. From their "
                     "REAL reel retention below, say in ONE confident sentence the format or length that "
                     "works best FOR THEM, citing a real number from the data. Specific and encouraging, no "
                     "preamble, no harsh words, one sentence only.")
            sage = (_muse_wonder(instr, wlines, effort="minimal") or "").strip()
    except Exception:
        sage = ""

    # Koa (Facebook Graph, no model): monetization standing + closest program
    koa = {"followers": mon.get("followers"), "mins60": mon.get("mins60"),
           "earn_30": mon.get("earn_30"), "closest": None}
    try:
        best = None
        for p in mon.get("programs", []):
            if p.get("eligible"):
                continue
            reqs = p.get("reqs") or []
            prog_pct = min((r.get("pct") or 0) for r in reqs) if reqs else 0
            if best is None or prog_pct > best[0]:
                best = (prog_pct, p)
        if best:
            r0 = min(best[1].get("reqs") or [{}], key=lambda r: r.get("pct") or 0)
            koa["closest"] = {"name": best[1].get("name"), "pct": best[0],
                              "label": r0.get("label"), "have": r0.get("have"), "need": r0.get("need")}
    except Exception:
        pass

    # Nico (Muse Spark): audience behaviour from the hottest threads, never names a person
    nico = ""
    try:
        hot = fan_love()
        if hot:
            total_replies = sum((h.get("fan_replies") or 0) for h in hot)
            total_likes = sum((h.get("likes") or 0) for h in hot)
            themes = "; ".join(dict.fromkeys(
                (h.get("post") or "").strip()[:48] for h in hot[:5] if (h.get("post") or "").strip()))
            data = ("Across the creator's recent posts the audience left %d comment-replies and %d likes on "
                    "the hottest threads. Those threads sit under posts about: %s"
                    % (total_replies, total_likes, themes or "(recent posts)"))
            instr = ("You are Nico, the creator's warm community manager meeting them for the first time. From "
                     "this REAL engagement data, say in ONE friendly sentence what their audience shows up for "
                     "(the themes they engage with, the warmth) WITHOUT naming any individual person. Specific "
                     "and kind, no preamble, one sentence only.")
            nico = (_muse_spark(instr, data, effort="minimal", max_tokens=400) or "").strip()
    except Exception:
        nico = ""

    return {"scanned": scanned, "sage": sage, "koa": koa, "nico": nico}



# Empty states: when a key is missing the page says what to add, instead of failing quietly.
SPOTLIGHT_NO_KEY_REASON = "Add your Meta Muse key under Connect your AI keys to use Creator Spotlight."
CHAT_NO_KEY_REPLY = "Add your Meta Muse key under Connect your AI keys to chat with Abra."
ANALYZE_NO_KEY_ERROR = "Add your Meta Muse and Anthropic keys under Connect your AI keys to analyze a video."


def spotlight(query) -> dict:
    """Creator Spotlight: Muse Wonder spotlights what is going viral ACROSS short-form platforms
    (TikTok / Reels / Shorts) in a searched genre + 3 film-ready ideas that adapt those cross-platform
    patterns to the creator's page. HONEST: AI analysis of proven cross-platform patterns + the
    creator's real data, NOT a claim of specific videos pulled live from any platform."""
    q = str(query or "").strip()[:80]
    if not B.muse_key():
        return {"query": q, "pulse": "", "trends": [], "ideas": [], "reason": SPOTLIGHT_NO_KEY_REASON}
    if not q:
        return {"query": "", "pulse": "", "ideas": []}
    try:
        watch = reel_watch_insights(limit=8)
        tops = [w for w in watch if w.get("watched_pct") is not None][:5]
        toplines = "; ".join("'%s' held %s%% (%s views)" % ((w.get("title") or "untitled")[:40], w["watched_pct"], w.get("views") or 0) for w in tops)
    except Exception:
        toplines = ""
    instr = (
        "You are Sage running a 'Creator Spotlight' for a Facebook creator on the genre or topic they typed. "
        "Lead with what has worked across short-form platforms in that genre, from general knowledge (say so if unsure) \u2014 the formats, "
        "hooks, trends and challenges driving views on TikTok, Instagram Reels and YouTube Shorts, not just "
        "Facebook. In 2-3 sentences, spotlight the cross-platform pattern: the hook style, ideal length, pacing "
        "and payoff that is winning, and how to make it land with a Facebook audience. Be specific and practical, never generic. Then give EXACTLY 3 concrete videos the creator "
        "could film that ADAPT those cross-platform viral patterns for their page, each a punchy hook title + "
        "one short line on why it is working across platforms. Also list 2-3 short labels for the specific "
        "cross-platform trends or formats you are drawing on. Reply with ONLY a JSON object, no prose and no "
        "code fences: "
        '{"pulse":"<the spotlight read>","trends":["<short trend label>"],"ideas":[{"hook":"<4-9 word title>","why":"<one line>","platform":"<TikTok, Reels or Shorts: where this format performs best>"}]}'
    )
    user = "Genre or topic: " + q
    if toplines:
        user += chr(10) + "The creator's own best reels (use ONLY to tailor the spin to what their audience already responds to, not as the main source): " + toplines
    db = db_conn()
    raw = _muse_wonder(instr, user, effort="low", db=db)
    db.close()
    t = (raw or "").strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t[:4].lower() == "json":
            t = t[4:].strip()
    a = t.find("{"); b = t.rfind("}")
    if a != -1 and b != -1:
        t = t[a:b + 1]
    try:
        d = json.loads(t)
    except Exception:
        d = {}
    pulse = str(d.get("pulse") or "")[:600]
    trends = []
    for tr in (d.get("trends") or [])[:3]:
        tr = str(tr or "").strip()[:48]
        if tr:
            trends.append(tr)
    ideas = []
    for it in (d.get("ideas") or [])[:3]:
        if isinstance(it, dict):
            hook = str(it.get("hook") or "")[:70]
            why = str(it.get("why") or "")[:140]
            if hook:
                pl = str(it.get("platform") or "").lower()
                plat = "TikTok" if "tiktok" in pl else ("Reels" if "reel" in pl else ("Shorts" if "short" in pl else ""))
                ideas.append({"hook": hook, "why": why, "platform": plat})
    return {"query": q, "pulse": pulse, "trends": trends, "ideas": ideas}



class _FormField:
    def __init__(self, name, filename, data):
        self.name = name
        self.filename = filename
        self.file = io.BytesIO(data)
        self.value = "" if filename is not None else data.decode("utf-8", "replace")


class _Form(dict):
    def getfirst(self, name, default=None):
        f = self.get(name)
        return f.value if (f is not None and f.filename is None) else default


def _read_form(handler) -> "_Form":
    """multipart/form-data from the request body. Replaces the cgi module, which Python 3.13 removed."""
    import email.parser
    import email.policy
    n = int(handler.headers.get("Content-Length", 0) or 0)
    body = handler.rfile.read(n) if n else b""
    ctype = handler.headers.get("Content-Type", "")
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    form = _Form()
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_param("name", header="content-disposition")
        if name is None:
            continue
        filename = part.get_param("filename", header="content-disposition")
        form[name] = _FormField(name, filename, part.get_payload(decode=True) or b"")
    return form


class Handler(BaseHTTPRequestHandler):
    timeout = 60   # drop idle or stalled connections
    def _send(self, payload: bytes, ctype: str, cache: str = None, extra: list = None) -> None:
        body = payload; enc = None
        if "gzip" in self.headers.get("Accept-Encoding", "") and len(payload) > 860 \
                and any(t in ctype for t in ("text/", "json", "javascript", "svg")):
            import gzip as _gz
            body = _gz.compress(payload, 6); enc = "gzip"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if cache: self.send_header("Cache-Control", cache)
        for _k, _v in (extra or []): self.send_header(_k, _v)
        if getattr(self, "_tok_cookie", None): self.send_header("Set-Cookie", self._tok_cookie)
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_local(self):
        h = self.client_address[0] if self.client_address else ""
        return h in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _has_token(self):
        q = parse_qs(urlparse(self.path).query)
        tok = self.headers.get("X-Abra-Token") or (q.get("t") or [""])[0]
        if not tok:
            m = re.search(r"abra_t=([A-Za-z0-9_-]+)", self.headers.get("Cookie", "") or "")
            tok = m.group(1) if m else ""
        return bool(tok) and _secrets.compare_digest(str(tok), ABRA_TOKEN)

    def _gate(self, post=False):
        # localhost (this Mac) is trusted; LAN clients (the phone camera) must carry the token.
        if not JUDGE_MODE and not self._is_local() and not self._has_token():
            return False
        # CSRF: block cross-site POSTs from a malicious page the user might be visiting.
        if post:
            o = self.headers.get("Origin")
            if o:
                oh = urlparse(o).hostname or ""
                if not (oh in ("localhost", "127.0.0.1", "::1") or (JUDGE_MODE and oh == (self.headers.get("Host") or "").split(":")[0]) or _private_ip(oh)):
                    return False
        return True

    def _fb_signed_in(self):
        if JUDGE_MODE:
            return True   # the judge build has no Facebook page; its stats are sample data
        # a device that holds this dashboard's access token (the owner's phone on the same wifi) gets the same stats and chat
        if not self._is_local() and self._has_token():
            return True
        # a copy with no Facebook page connected (a judge's own install) has no stats to protect, so this computer is not asked to sign in
        if self._is_local() and _no_page_connected():
            return True
        m = re.search(FB_COOKIE + r"=([A-Za-z0-9_-]+)", self.headers.get("Cookie", "") or "")
        return bool(m) and _secrets.compare_digest(m.group(1), fb_session_value())

    def _fb_locked(self, msg: str = "Sign in with Facebook to see your stats.", status: int = 401):
        body = json.dumps({"signed_in": False, "error": msg}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def finish(self):
        try:
            super().finish()
        finally:
            if getattr(self, "_sem_held", False):
                self._sem_held = False
                _SMOOTH_SEM.release()

    def _send_json_error(self, status: int, msg: str) -> None:
        body = json.dumps({"ok": False, "error": msg}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _visitor_sid(self) -> str:
        m = re.search(VISITOR_COOKIE + r"=([A-Za-z0-9_-]+)", self.headers.get("Cookie", "") or "")
        return m.group(1) if m else ""

    def _visitor_keys(self) -> dict:
        sid = self._visitor_sid()
        return _visitor_get(sid) if sid else {}

    def _visitor_cookie(self, sid: str = "", clear: bool = False) -> str:
        if clear or not sid:
            return VISITOR_COOKIE + "=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        return VISITOR_COOKIE + "=" + sid + "; Path=/; HttpOnly; SameSite=Lax; Max-Age=" + str(VISITOR_TTL)

    def _visitor_post(self, path: str) -> bool:
        """POST /visitor/keys saves a visitor's Muse and/or Claude key; POST /visitor/clear forgets them."""
        if path == "/visitor/clear":
            sid = self._visitor_sid()
            if sid:
                _visitor_drop(sid)
            self._send(json.dumps({"ok": True}).encode(), "application/json", cache="no-store",
                       extra=[("Set-Cookie", self._visitor_cookie(clear=True))])
            return True
        if path != "/visitor/keys":
            return False
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        checks = {"muse": ("Muse", _check_muse_key), "claude": ("Claude", _check_claude_key)}
        updates = {}
        for field, (label, checker) in checks.items():
            val = str(body.get(field, "") or "").strip()
            if not val:
                continue
            if len(val) < 20 or len(val) > 300 or any(c.isspace() for c in val):
                self._send_json_error(400, "That does not look like a " + label + " API key.")
                return True
            verdict = checker(val)
            if verdict == "rejected":
                self._send_json_error(400, label + " did not accept that key (check it, and that its billing is active).")
                return True
            if verdict != "ok":
                self._send_json_error(503, "Could not reach " + label + " right now. Try again in a moment.")
                return True
            updates[field] = val
        if not updates:
            self._send_json_error(400, "Paste at least one key.")
            return True
        sid = _visitor_update(self._visitor_sid(), updates)
        self._send(json.dumps({"ok": True}).encode(), "application/json", cache="no-store",
                   extra=[("Set-Cookie", self._visitor_cookie(sid))])
        return True

    def _deny(self):
        try:
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Forbidden")
        except Exception:
            pass

    def do_GET(self):
        self._tok_cookie = None
        if not self._gate(): return self._deny()
        # a phone opens the link with ?t=TOKEN; the page's own images and scripts then ask without that query,
        # so the token also becomes a secure cookie for this device
        if not self._is_local() and not JUDGE_MODE and self._has_token():
            tok = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
            if tok:
                self._tok_cookie = f"abra_t={tok}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=2592000"
        if JUDGE_MODE and (urlparse(self.path).path in ("/analyst", "/crewread", "/ideas") or self.path.startswith("/spotlight")):
            return self._fb_locked("Not available in the judge demo.")   # these spend the owner's Muse key
        B._req_ctx.muse_key = None
        B._req_ctx.visitor = False
        B._req_ctx.claude_key = None
        if self.path.split("?")[0] == "/live":
            self._send(json.dumps(meta_live.snapshot()).encode(), "application/json", cache="no-store")
            return
        if self.path.split("?")[0] == "/session":
            return self._send(json.dumps({"signed_in": self._fb_signed_in(),
                                          "visitor_muse": bool(self._visitor_keys().get("muse")),
                                          "visitor_claude": bool(self._visitor_keys().get("claude")),
                                          "judge": JUDGE_MODE,
                                          "keys_saved": bool(os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("META_MUSE_API_KEY"))}).encode(),
                              "application/json", cache="no-store")
        if self.path.split("?")[0] == "/session/signout":
            return self._send(json.dumps({"signed_in": False}).encode(), "application/json",
                              cache="no-store", extra=[("Set-Cookie", fb_cookie_header(clear=True))])
        if fb_stats_request(self.path, post=False) and not self._fb_signed_in():
            return self._fb_locked()
        # Phones block the mic on plain http, so auto-forward LAN visitors of the main page
        # to the HTTPS port (the mic needs a secure context). Localhost stays on http (it is
        # already a secure context and the OAuth flow uses it).
        if not JUDGE_MODE and self.server.server_address[1] == PORT and self.path in ("/", "/index.html"):
            host = (self.headers.get("Host") or "").split(":")[0]
            if host and host not in ("localhost", "127.0.0.1", "::1"):
                self.send_response(302)
                self.send_header("Location", f"https://{host}:{HTTPS_PORT}/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if self.path == "/pages":
            self._send(json.dumps(list_pages()).encode(), "application/json")
            return
        if self.path == "/rights":
            self._send(json.dumps(rights_list()).encode(), "application/json")
            return
        if self.path == "/storyqueue":
            self._send(json.dumps(queue_list()).encode(), "application/json")
            return
        if self.path == "/schedule/upcoming":
            self._send(json.dumps(upcoming_content()).encode(), "application/json")
            return
        if self.path == "/checklist":
            self._send(json.dumps(checklist_list()).encode(), "application/json")
            return
        if self.path == "/film/genres":
            out = [{"id": FILM_GENRES[k]["id"], "label": FILM_GENRES[k]["label"],
                    "blurb": FILM_GENRES[k].get("blurb", ""), "inputs": FILM_GENRES[k].get("inputs", [])}
                   for k in FILM_GENRE_ORDER if k in FILM_GENRES]
            self._send(json.dumps({"genres": out}).encode(), "application/json")
            return
        if self.path == "/lan":
            if JUDGE_MODE:   # the hosted demo has no LAN token and no LAN address to share
                self._send(b"{}", "application/json"); return
            self._send(json.dumps({"url": _lan_url(), "t": ABRA_TOKEN}).encode(), "application/json")
            return
        if self.path == "/cam" or self.path.startswith("/cam?"):
            try:
                html = (BASE / "cam.html").read_bytes()
            except Exception:
                html = b"<!doctype html><meta charset=utf-8><title>Abra camera</title><p>Camera page missing.</p>"
            self._send(html, "text/html; charset=utf-8", cache="no-store")
            return
        if self.path == "/welcome" or self.path.startswith("/welcome?"):
            try:
                html = (BASE / "welcome.html").read_bytes()
            except Exception:
                html = b"<!doctype html><meta charset=utf-8><title>Abra</title><p>Welcome page missing.</p>"
            self._send(html, "text/html; charset=utf-8", cache="no-store")
            return
        if self.path.startswith("/rtc/poll"):
            q = parse_qs(urlparse(self.path).query)
            sess = (q.get("s") or [""])[0]
            role = (q.get("role") or [""])[0]
            self._send(json.dumps({"msgs": rtc_poll(sess, role)}).encode(),
                       "application/json", cache="no-store")
            return
        if self.path == "/prompter":
            self._send(json.dumps(prompter_get()).encode(), "application/json", cache="no-store")
            return
        if self.path == "/replies":
            self._send(json.dumps(reply_list()).encode(), "application/json", cache="no-store")
            return
        if self.path == "/storyboard":
            self._send(json.dumps(storyboard_current()).encode(), "application/json")
            return
        if self.path == "/engagement":
            self._send(json.dumps(engagement_state()).encode(), "application/json")
            return
        if self.path == "/watchinsights":
            try:
                self._send(json.dumps(reel_watch_insights()).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200]}).encode(), "application/json")
            return
        if self.path == "/dms":
            try:
                self._send(json.dumps(dm_recent()).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200], "log": []}).encode(), "application/json")
            return
        if self.path == "/monetization" or self.path.startswith("/monetization?"):
            try:
                sel = parse_qs(urlparse(self.path).query).get("pages", [""])[0]
                ids = [x for x in sel.split(",") if x] if sel and sel != "all" else None
                self._send(json.dumps(monetization_status(ids)).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200]}).encode(), "application/json")
            return
        if self.path.startswith("/qr.svg?"):
            d = (parse_qs(urlparse(self.path).query).get("d", [""])[0] or "")[:400]
            if not d:
                self._send(b"", "text/plain")
                return
            import io as _io, segno as _segno
            buf = _io.BytesIO()
            _segno.make(d, error="m").save(buf, kind="svg", scale=6, border=2, dark="#1a2230", light="#ffffff")
            self._send(buf.getvalue(), "image/svg+xml")
            return
        if self.path == "/ads/summary":
            try:
                self._send(json.dumps(ad_summary()).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"ads": [], "error": str(e)[:120]}).encode(), "application/json")
            return
        if self.path == "/ads/flag":
            self._send(json.dumps({"ads": ad_post_ids()}).encode(), "application/json")
            return
        if self.path == "/reviewstats":
            self._send(json.dumps(review_stats()).encode(), "application/json")
            return
        if self.path == "/expenses":
            try:
                self._send(json.dumps(expense_tracker()).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200]}).encode(), "application/json")
            return
        if self.path == "/besttime":
            try:
                self._send(json.dumps(best_time_to_post()).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200]}).encode(), "application/json")
            return
        if self.path == "/calendar":
            try:
                self._send(json.dumps(content_calendar()).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200], "items": []}).encode(), "application/json")
            return
        if self.path == "/analyst" or self.path.startswith("/analyst?"):
            force = "refresh" in self.path
            try:
                self._send(json.dumps(muse_analyst(force=force)).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200], "text": ""}).encode(), "application/json")
            return
        if self.path.startswith("/spotlight"):
            try:
                _q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
                self._send(json.dumps(spotlight(_q)).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200]}).encode(), "application/json")
            return
        if self.path == "/crewread" or self.path.startswith("/crewread?"):
            try:
                self._send(json.dumps(crew_read()).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200]}).encode(), "application/json")
            return
        if self.path == "/ideas" or self.path.startswith("/ideas?"):
            force = "refresh" in self.path
            try:
                self._send(json.dumps(muse_ideas(force=force)).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200], "ideas": []}).encode(), "application/json")
            return
        if self.path == "/trend":
            _maybe_refresh_insights()
            db = db_conn()
            out = []
            try:
                pages = db.execute("SELECT page_id, name FROM pages").fetchall()
                for pid, name in pages:
                    metrics = {}
                    for m in ["page_views_total", "page_video_views", "page_post_engagements"]:
                        rows = db.execute(
                            "SELECT taken_at, value FROM page_insights WHERE page_id=? AND metric=? "
                            "ORDER BY taken_at DESC LIMIT 14", (pid, m)).fetchall()
                        metrics[m] = [{"d": r[0][5:], "v": r[1]} for r in reversed(rows)]
                    out.append({"name": name, "metrics": metrics})
            except Exception:
                pass
            db.close()
            self._send(json.dumps(out).encode(), "application/json")
            return
        if self.path == "/genstate":
            st = dict(GEN_STATE); st["images"] = generated_list()
            self._send(json.dumps(st).encode(), "application/json")
            return
        if self.path.startswith("/generated/"):
            p = (BASE / self.path.lstrip("/")).resolve()
            if str(p).startswith(str(GEN_DIR.resolve())) and p.exists():
                blob = p.read_bytes()
                self._send(blob, "image/png")
            else:
                self._send(b"not found", "text/plain")
            return
        if self.path == "/schedule":
            try:
                self._send(json.dumps(schedule_status()).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200]}).encode(), "application/json")
            return
        if self.path == "/trees":
            try:
                data = fetch_posts("trending")
            except Exception:
                data = []
            toks = _page_tokens()
            # strongest candidates first, then price the top 20 with real earnings
            data.sort(key=lambda q: (q.get("growth", 0) or 0, q.get("score", 0)), reverse=True)
            for q in data[:20]:
                tok = toks.get((q.get("id") or "").split("_")[0], "")
                q["usd"] = post_earnings(q["id"], tok) if tok else 0.0
            data = data[:20]
            if any(q.get("usd", 0) > 0 for q in data):
                data.sort(key=lambda q: (q.get("usd", 0), q.get("growth", 0) or 0,
                                         q.get("score", 0)), reverse=True)
            top = [{k: q.get(k) for k in ("id", "caption", "score", "url", "usd", "page")}
                   | {"money": q.get("growth", 0) or 0} for q in data[:10]]
            self._send(json.dumps(top).encode(), "application/json")
            return
        if self.path.startswith("/ledger.pdf"):
            q = parse_qs(urlparse(self.path).query)
            month = ((q.get("month") or [""])[0] or datetime.now().strftime("%Y-%m"))[:7]   # an empty month means this month
            if not re.fullmatch(r"\d{4}-\d{2}", month):
                self.send_error(400)
                return
            try:
                pdf = build_ledger_pdf(month)
                if not pdf.startswith(b"%PDF"):
                    raise ValueError("the output is not a PDF")
            except Exception as e:
                B.log.error("ledger PDF for %s failed: %s", month, e, exc_info=True)   # the run log (bot.log)
                self._send_json_error(500, f"The ledger PDF could not be built ({type(e).__name__}). The reason is in bot.log.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition",
                             f'attachment; filename="creator-ai-expenses-{month}.pdf"')
            self.send_header("Content-Length", str(len(pdf)))
            self.end_headers()
            self.wfile.write(pdf)
            return
        if self.path == "/promos":
            try:
                self._send((BASE / "promos.json").read_bytes(), "application/json")
            except Exception:
                self._send(b'{"deals":[]}', "application/json")
            return
        if self.path == "/connect" or self.path.startswith("/connect?"):
            if not FB_APP_ID:
                self._send(b"<!doctype html><meta charset=utf-8><title>Abra</title><p>Facebook connect is not set up yet. Set FB_APP_ID in your .env file to your Facebook app's ID, then restart Abra.</p>", "text/html; charset=utf-8")
                return
            self._send(CONNECT_PAGE.format(oauth_url=oauth_start_url()).encode(),
                       "text/html; charset=utf-8")
            return
        if self.path == "/connect/ai" or self.path.startswith("/connect/ai?"):
            self._send(CONNECT_AI.format().encode(), "text/html; charset=utf-8")
            return
        if self.path == "/aistatus":
            self._send(json.dumps(_ai_key_status()).encode(), "application/json")
            return
        if self.path.startswith("/oauth/callback"):
            q = parse_qs(urlparse(self.path).query)
            state = (q.get("state") or [""])[0]
            code = (q.get("code") or [""])[0]
            if state not in _oauth_states or not code:
                self._send(b"Onboarding link expired - go back to /connect",
                           "text/html; charset=utf-8")
                return
            _oauth_states.discard(state)
            try:
                name, pages = oauth_exchange(code)
            except PermissionError:
                self._send(b"This Abra dashboard is connected to a Facebook page that this account does not manage. Sign in with the account that manages it.",
                           "text/html; charset=utf-8")
                return
            except Exception as e:
                try:
                    with open(BASE / "onboard.log", "a", encoding="utf-8") as lf:
                        lf.write(f"{datetime.now().isoformat()} exchange failed: "
                                 f"{type(e).__name__}: {str(e)[:200]}\n")
                except Exception:
                    pass
                self._send(f"Facebook sign-in failed ({type(e).__name__}) - see onboard.log".encode(),
                           "text/html; charset=utf-8")
                return
            sid = _secrets.token_hex(8)
            _pending_pages[sid] = pages
            rows = []
            for p_ in pages:
                ig = (p_.get("instagram_business_account") or {})
                cap = p_.get("_cap", "full")
                badge = ("" if cap == "full"
                         else " <span style='color:#B29D89;font-size:12px'>(reply &amp; like only — moderator)</span>")
                rows.append(
                    f"<label><input type='checkbox' name='pg' value='{p_['id']}' checked> "
                    f"<span> {p_['name']}{badge}</span></label>")
                if ig.get("id"):
                    rows.append(
                        f"<label style='margin-left:22px'><input type='checkbox' "
                        f"name='ig' value='{p_['id']}' checked> "
                        f"<span> Instagram: @{ig.get('username','')}</span></label>")
            opts = "".join(rows) or "<p>No pages found on this account.</p>"
            opts += f"<input type='hidden' name='sid' value='{sid}'>"
            self._send(PICKER_PAGE.format(name=name, options=opts).encode(),
                       "text/html; charset=utf-8")
            return
        if self.path == "/deals":
            self._send(json.dumps(deals_list()).encode(), "application/json")
            return
        if self.path == "/fanlove":
            self._send(json.dumps(fan_love()).encode(), "application/json")
            return
        if self.path == "/budget":
            db = db_conn()
            _maybe_refresh_insights()
            out = {"today": {}, "week": {}, "month": {}, "earned": page_earnings_summary()}
            try:
                today = datetime.now(timezone.utc).date().isoformat()
                week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                month = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                for label, since in (("today", today), ("week", week), ("month", month)):
                    rows = db.execute(
                        "SELECT provider, ROUND(SUM(cost),4), COUNT(*) FROM spend "
                        "WHERE ts>=? GROUP BY provider", (since,)).fetchall()
                    out[label] = {p: {"cost": c or 0, "calls": n} for p, c, n in rows}
            except Exception:
                pass
            db.close()
            self._send(json.dumps(out).encode(), "application/json")
            return
        if self.path.startswith("/crew/"):
            nm = self.path.split("/crew/")[-1].split("?")[0].replace(".png","")
            cp = BASE / "assets" / f"crew_{nm}.png"
            safe = re.fullmatch(r"[a-z_]{1,24}", nm) is not None and cp.resolve().parent == (BASE / "assets").resolve()
            if safe and cp.exists():
                self._send(cp.read_bytes(), "image/png")
            else:
                self.send_error(404)
            return
        if self.path == "/crew3d/list":
            # which crew members have a 3D model dropped in yet (assets/crew_<animal>.glb)
            have = {a: (BASE / "assets" / f"crew_{a}.glb").exists()
                    for a in ("cat", "dog", "koala")}
            self._send(json.dumps(have).encode(), "application/json")
            return
        if self.path.startswith("/crew3d/"):
            nm = os.path.basename(self.path.split("/crew3d/")[-1].split("?")[0])
            gp = BASE / "assets" / nm
            if nm.startswith("crew_") and nm.endswith(".glb") and gp.exists():
                self._send(gp.read_bytes(), "model/gltf-binary")
            else:
                self.send_error(404)
            return
        if self.path.startswith("/mascot/"):
            p = BASE / "assets" / ("mascot_cat.png" if "cat" in self.path else "mascot_dog.png")
            if p.exists():
                self._send(p.read_bytes(), "image/png")
            else:
                self.send_error(404)
            return
        if self.path.split("?")[0] == "/logo.png":
            lp = BASE / "assets" / "logo_abra.png"
            if lp.exists():
                self._send(lp.read_bytes(), "image/png")
            else:
                self.send_error(404)
            return
        if self.path.split("?")[0] == "/sidebar.png":
            sp = BASE / "assets" / "sidebar_metal.png"
            if sp.exists():
                self._send(sp.read_bytes(), "image/png")
            else:
                self.send_error(404)
            return
        if self.path.split("?")[0] == "/favicon.ico":
            lp = BASE / "assets" / "logo_abra.png"
            self._send(lp.read_bytes() if lp.exists() else b"", "image/png", cache="public, max-age=604800")
            return
        if self.path.split("?")[0] == "/avatar":
            prof = _current_profile()
            pp = prof.get("picture_path")
            ap = (BASE / "assets" / pp) if pp else None
            if ap and ap.exists():
                self._send(ap.read_bytes(), "image/jpeg")
            else:
                self.send_error(404)
            return
        if self.path.split("?")[0] == "/profile":
            prof = _current_profile()
            try:
                db = db_conn()
                pgs = db.execute("SELECT name, enabled FROM pages").fetchall()
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                prof["replies_today"] = db.execute(
                    "SELECT COUNT(*) FROM handled WHERE action='replied' AND "
                    "substr(handled_at,1,10)=?", (today,)).fetchone()[0]
                db.close()
                prof["pages"] = [{"name": n, "enabled": bool(e)} for (n, e) in pgs]
            except Exception:
                prof.setdefault("pages", [])
                prof.setdefault("replies_today", 0)
            try:
                prof["followers"] = monetization_status().get("followers")
            except Exception:
                prof["followers"] = None
            self._send(json.dumps(prof).encode(), "application/json")
            return
        if self.path.startswith("/draft/"):
            nm = os.path.basename(self.path.split("/draft/")[-1].split("?")[0])
            dp = BASE / "assets" / "drafts" / nm
            if nm and dp.exists() and dp.suffix.lower() in (".jpg", ".jpeg", ".png"):
                self._send(dp.read_bytes(), "image/jpeg")
            else:
                self.send_error(404)
            return
        if self.path == "/reactions":
            self._send(json.dumps({"reactions": list_reactions()}).encode(), "application/json")
            return
        if self.path.startswith("/reaction/"):
            nm = os.path.basename(self.path.split("/reaction/")[-1].split("?")[0])
            rp = REACT_DIR / nm
            if nm.startswith("rx_") and nm.endswith(".png") and rp.exists():
                self._send(rp.read_bytes(), "image/png")
            else:
                self.send_error(404)
            return
        if self.path == "/data":
            self._send(json.dumps({"events": parse_events(), "stats": stats()}).encode(),
                       "application/json")
        elif self.path.startswith("/posts"):
            qs = parse_qs(urlparse(self.path).query)
            sort = (qs.get("sort") or ["recent"])[0]
            if sort not in ("viral", "trending", "fresh", "earners", "recent"):
                sort = "recent"
            page_id = (qs.get("page") or [None])[0] or None
            if page_id and page_id not in _connected_page_ids():
                self._send(b"[]", "application/json"); return   # not a connected page: nothing to show
            if sort == "fresh":
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)
                          ).strftime("%Y-%m-%dT%H:%M:%S+0000")
                data = [q for q in fetch_posts("recent", page_id) if (q.get("ts") or "") >= cutoff]
            elif sort == "earners":
                data = list(fetch_posts("trending", page_id))
                toks = _page_tokens()
                for q in data[:25]:
                    tok = toks.get((q.get("id") or "").split("_")[0], "")
                    q["usd"] = post_earnings(q["id"], tok) if tok else 0.0
                data = sorted(data[:25], key=lambda q: q.get("usd", 0), reverse=True)[:15]
            else:
                data = fetch_posts(sort, page_id)
            self._send(json.dumps(data).encode(), "application/json")
        else:
            self._send(PAGE.replace("__PAGE_TITLE__", PAGE_TITLE).encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if not self._gate(post=True): return self._deny()
        try:
            _n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            _n = -1
        _path_only = urlparse(self.path).path
        _cap = (300 if _path_only in ("/schedule/analyze", "/smoothcut", "/captions/take") else 25) * 1024 * 1024
        if _path_only == "/rtc/send":
            _cap = 1024 * 1024
        if _n < 0 or _n > _cap:
            return self._send_json_error(413, "That upload is too large.")
        B._req_ctx.muse_key = None
        B._req_ctx.visitor = False
        B._req_ctx.claude_key = None
        if self._visitor_post(urlparse(self.path).path):
            return
        if fb_stats_request(self.path, post=True) and (JUDGE_MODE or not self._fb_signed_in()) \
                and not (not JUDGE_MODE and self._is_local()):
            needs = VISITOR_NEEDS.get(urlparse(self.path).path, ())
            keys = self._visitor_keys() if needs else {}
            if not needs or not all(keys.get(k) for k in needs):
                # a Create step needs the visitor's own keys; the stats message is only for stats
                if JUDGE_MODE:
                    return self._fb_locked("Add your own Muse key (and Claude for Hand to Sage) to try this step." if needs else "Not available in the judge demo.")
                return self._fb_locked("Add your own Muse key (and Claude for Hand to Sage), or sign in with Facebook." if needs else "Sign in with Facebook to see your stats.")
            B._req_ctx.muse_key = keys.get("muse", "")
            B._req_ctx.claude_key = keys.get("claude", "")
            B._req_ctx.visitor = True
        if self.path == "/smoothcut":
            if not _SMOOTH_SEM.acquire(blocking=False):
                return self._send_json_error(429, "Another cut is running. Try again in a moment.")
            self._sem_held = True
            import tempfile, shutil as _sh
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n <= 0:
                self._send(b'{"ok":false,"error":"empty"}', "application/json"); return
            MAX = 300 * 1024 * 1024
            if n > MAX:                                   # reject BEFORE reading the body
                self.send_response(413); self.send_header("Content-Length", "0"); self.end_headers(); return
            buf, left = bytearray(), n
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk: break
                buf += chunk; left -= len(chunk)
            raw = bytes(buf)
            scratch = BASE / "_rxcut"; scratch.mkdir(exist_ok=True)
            work = tempfile.mkdtemp(prefix="cut-", dir=str(scratch))
            src = os.path.join(work, "in.webm"); out = os.path.join(work, "out.mp4")
            try:
                with open(src, "wb") as f: f.write(raw)
                ok, removed, path, reason = smooth_cut(src, out, log=B.log)
                if not ok:
                    code = 422 if reason in ("no_silence", "all_silent", "too_short", "no_audio") else 500
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("X-Reason", reason)
                    body = json.dumps({"ok": False, "reason": reason}).encode()
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body); return
                data = open(path, "rb").read()
                orig = _sc_duration(_sc_bin("ffprobe"), src)
                new = _sc_duration(_sc_bin("ffprobe"), path)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Orig-Dur", "%.3f" % orig)
                self.send_header("X-New-Dur", "%.3f" % new)
                self.send_header("X-Removed", "%.3f" % removed)
                self.end_headers(); self.wfile.write(data)
            except Exception as e:
                B.log.warning("smoothcut failed: %s", str(e)[:200])
                try:
                    self.send_response(500); self.send_header("Content-Type", "application/json")
                    body = b'{"ok":false,"error":"server"}'
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body)
                except Exception: pass
            finally:
                _sh.rmtree(work, ignore_errors=True)
            return
        if self.path == "/captions/take":
            if not _SMOOTH_SEM.acquire(blocking=False):
                return self._send_json_error(429, "Another edit is running. Try again in a moment.")
            self._sem_held = True
            import tempfile, shutil as _sh
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n <= 0:
                self._send(b'{"ok":false,"error":"empty"}', "application/json"); return
            MAX = 300 * 1024 * 1024
            if n > MAX:
                self.send_response(413); self.send_header("Content-Length", "0"); self.end_headers(); return
            buf, left = bytearray(), n
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk: break
                buf += chunk; left -= len(chunk)
            raw = bytes(buf)
            scratch = BASE / "_rxcut"; scratch.mkdir(exist_ok=True)
            work = tempfile.mkdtemp(prefix="cap-", dir=str(scratch))
            src = os.path.join(work, "in.webm"); out = os.path.join(work, "out.mp4")
            try:
                with open(src, "wb") as f: f.write(raw)
                ok, reason = captions_take(src, out)
                if not ok:
                    self.send_response(422)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("X-Reason", reason)
                    body = json.dumps({"ok": False, "reason": reason}).encode()
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body); return
                data = open(out, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
            except Exception as e:
                B.log.warning("captions take failed: %s", str(e)[:200])
                try:
                    self.send_response(500); self.send_header("Content-Type", "application/json")
                    body = b'{"ok":false,"error":"server"}'
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body)
                except Exception: pass
            finally:
                _sh.rmtree(work, ignore_errors=True)
            return
        if self.path == "/rtc/send":
            n = int(self.headers.get("Content-Length", 0))
            try:
                d = json.loads(self.rfile.read(n) or b"{}")
                rtc_send(d.get("s") or "", d.get("from") or "", d.get("data") or {})
            except Exception:
                pass
            self._send(b'{"ok":true}', "application/json")
            return
        if self.path == "/connect/finish":
            n = int(self.headers.get("Content-Length", 0))
            form = parse_qs(self.rfile.read(n).decode())
            sid = (form.get("sid") or [""])[0]
            chosen = set(form.get("pg") or [])
            pages = _pending_pages.pop(sid, [])
            db = db_conn()
            db.execute("""CREATE TABLE IF NOT EXISTS pages (
                page_id TEXT PRIMARY KEY, name TEXT, token TEXT,
                enabled INTEGER NOT NULL DEFAULT 1)""")
            db.execute("""CREATE TABLE IF NOT EXISTS ig_accounts (
                ig_id TEXT PRIMARY KEY, username TEXT, page_id TEXT,
                token TEXT, enabled INTEGER NOT NULL DEFAULT 1)""")
            chosen_ig = set(form.get("ig") or [])
            names = []
            for p_ in pages:
                if p_["id"] in chosen:
                    db.execute("INSERT INTO pages (page_id, name, token, enabled) VALUES (?, ?, ?, 1) "
                               "ON CONFLICT(page_id) DO UPDATE SET name=excluded.name, token=excluded.token",
                               (p_["id"], p_["name"], p_["access_token"]))
                    names.append(" " + p_["name"])
                ig = (p_.get("instagram_business_account") or {})
                if ig.get("id") and p_["id"] in chosen_ig:
                    db.execute("INSERT OR REPLACE INTO ig_accounts "
                               "(ig_id, username, page_id, token, enabled) VALUES (?, ?, ?, ?, 1)",
                               (ig["id"], ig.get("username", ""), p_["id"], p_["access_token"]))
                    names.append(" @" + ig.get("username", ""))
            db.commit(); db.close()
            # The crew-reading setup screen is a FIRST-EXPERIENCE moment only: show it once,
            # then every later connect / re-auth / added page goes straight to the confirmation.
            _welcomed = BASE / ".welcomed"
            if _welcomed.exists():
                self._send(DONE_PAGE.format().encode(), "text/html; charset=utf-8",
                           extra=[("Set-Cookie", fb_cookie_header())])
                return
            try: _welcomed.write_text("1", encoding="utf-8")
            except Exception: pass
            # fresh start for THIS creator: clear the shared analysis caches so the setup
            # read never surfaces a previously-connected creator's numbers.
            for _c in (_WATCH_CACHE, _ANALYST_CACHE, _IDEAS_CACHE, _fanlove_cache):
                try: _c["at"] = 0.0
                except Exception: pass
            _MON_CACHE["at"] = 0.0; _MON_CACHE["pages"] = None
            try:
                html = (BASE / "welcome.html").read_bytes()
            except Exception:
                html = DONE_PAGE.encode()
            self._send(html, "text/html; charset=utf-8", cache="no-store",
                       extra=[("Set-Cookie", fb_cookie_header())])
            return
        if self.path == "/connect/ai":
            n = int(self.headers.get("Content-Length", 0))
            form = parse_qs(self.rfile.read(n).decode())
            try:
                _save_ai_keys(form)
            except Exception:
                pass
            self._send(DONE_PAGE.format().encode(), "text/html; charset=utf-8")
            return
        if self.path == "/deals":
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            deal_save(d)
            self._send(b'{"ok":true}', "application/json")
            return
        if self.path == "/rankhook":
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            db = db_conn()
            try:
                out = rank_hooks(d.get("titles") or [], d.get("content_desc") or "",
                                 d.get("transcript") or "", db=db)
            except Exception as e:
                out = {"winner": 0, "reason": "", "scores": [], "error": str(e)[:160]}
            finally:
                db.close()
            self._send(json.dumps(out).encode(), "application/json")
            return
        if self.path == "/assistant":
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            try:
                out = assistant_reply(d.get("message", ""), d.get("history") or [], d.get("persona"))
            except Exception as e:
                out = {"reply": "Sorry — I hit a snag just now. Mind trying that again?", "action": None}
            self._send(json.dumps(out).encode(), "application/json")
            return
        if self.path == "/assistant/voice":
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            text = ""
            err = ""
            try:
                import tempfile, subprocess as _sp, requests as _rq, shutil as _sh
                # launchd runs with a minimal PATH, so resolve ffmpeg explicitly; fall back to
                # afconvert (a system binary, always present) which handles iOS mp4/aac recordings.
                ffmpeg = (_sh.which("ffmpeg") or
                          next((p for p in ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg")
                                if os.path.exists(p)), None))
                with tempfile.TemporaryDirectory() as td:
                    src = os.path.join(td, "in.audio"); wav = os.path.join(td, "a.wav")
                    with open(src, "wb") as f:
                        f.write(raw)
                    B.wav_16k_mono(src, wav, timeout=60)
                    with open(wav, "rb") as f:
                        resp = meta_live.post("muse-voice-transcribe-1.0", "assistant voice", "https://api.meta.ai/v1/asr/transcribe",
                            headers={"Authorization": f"Bearer {B.muse_key()}", "Accept": "application/json"},
                            files={"request": (None, json.dumps({"model": "muse-voice-transcribe-1.0",
                                    "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}), "application/json"),
                                   "audio": ("a.wav", f, "audio/wav")}, timeout=90)
                    text = (resp.json().get("transcript") or "").strip()
                    try:
                        dbv = db_conn(); B.record_spend(dbv, "muse", "assistant_voice", 0, 0, 0.002); dbv.close()
                    except Exception:
                        pass
            except Exception as e:
                B.log.warning("assistant voice failed: %s", str(e)[:200])
                if "ffmpeg is not installed" in str(e):
                    err = "Voice input needs ffmpeg. Install it (see the README), then try again."
            self._send(json.dumps({"text": text, "error": err}).encode(), "application/json")
            return
        if self.path == "/assistant/tts":
            # neural TTS (ElevenLabs) — human-quality voice per crew member; off unless a key is set
            key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            text = (d.get("text") or "").strip()[:900]
            # pronounce "Abra" like abra-cadabra (AB-ruh), not like Abraham (AY-bruh);
            # the doubled b cues the short-a vowel. On-screen text is unchanged (this is TTS-only).
            text = re.sub(r"\bAbra\b", "Ab-ruh", text, flags=re.IGNORECASE)
            agent = (d.get("agent") or "sage")
            voices = {
                "sage":   os.environ.get("ELEVEN_VOICE_MUSE",   "Xb7hH8MSUJpSbSDYk0k2"),  # Alice — British female
                "nico": os.environ.get("ELEVEN_VOICE_CLAUDE", "JBFqnCBsd6RMkjVDRZzb"),  # George — warm
                "koa":    os.environ.get("ELEVEN_VOICE_KOA",    "onwK4e9ZLuTAKqWW03F9"),  # Daniel — deep, calm
            }
            vid = voices.get(agent, voices["sage"])
            if key and text:
                try:
                    import requests as _rq
                    r = _rq.post(
                        f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
                        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                        json={"text": text, "model_id": "eleven_turbo_v2_5",
                              "voice_settings": {"stability": 0.5, "similarity_boost": 0.8, "style": 0.0}},
                        timeout=30)
                    if r.status_code == 200 and r.content:
                        try:
                            dbt = db_conn(); B.record_spend(dbt, "elevenlabs", "assistant_tts", 0, 0,
                                                            len(text) / 1000.0 * 0.1); dbt.close()
                        except Exception:
                            pass
                        self._send(r.content, "audio/mpeg")
                        return
                    B.log.warning("eleven tts HTTP %d: %s", r.status_code, r.text[:120])
                except Exception as e:
                    B.log.warning("eleven tts failed: %s", str(e)[:120])
            # free neural voices via Microsoft edge-tts (no key, Azure-quality), distinct per crew member
            if text:
                try:
                    import asyncio as _aio, edge_tts as _edge
                    evoice = {"sage": "en-GB-SoniaNeural", "nico": "en-US-AndrewNeural",
                              "koa": "en-GB-RyanNeural"}.get(agent, "en-GB-SoniaNeural")
                    epitch = {"koa": "-8Hz"}.get(agent, "+0Hz")

                    async def _synth():
                        buf = bytearray()
                        comm = _edge.Communicate(text, evoice, pitch=epitch)
                        async for ch in comm.stream():
                            if ch["type"] == "audio":
                                buf.extend(ch["data"])
                        return bytes(buf)

                    audio = _aio.run(_synth())
                    if audio:
                        try:
                            dbt = db_conn(); B.record_spend(dbt, "edge", "assistant_tts", 0, 0, 0.0); dbt.close()
                        except Exception:
                            pass
                        self._send(audio, "audio/mpeg")
                        return
                except Exception as e:
                    B.log.warning("edge tts failed: %s", str(e)[:150])
            self._send(b'{"tts":false}', "application/json")
            return
        if self.path == "/rights/protect":
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            self._send(json.dumps(rights_add(d.get("title") or "", d.get("note") or "")).encode(), "application/json")
            return
        if self.path == "/rights/dispute":
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            letter = rights_dispute(d.get("title") or "", d.get("claim") or "")
            self._send(json.dumps({"letter": letter}).encode(), "application/json")
            return
        if self.path == "/contractreview":
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            try:
                resp = B.claude.messages.create(
                    model=B.MODEL, max_tokens=1500,
                    messages=[{"role": "user", "content":
                        "You are a meticulous contract reviewer helping " + B.CREATOR_REF + " "
                        "understand a brand-deal contract. You are NOT their "
                        "lawyer and must say so. Their standards: "
                        + (B.CREATOR_TERMS or "fair pay for the work, clear deliverables and timelines, usage and exclusivity rights priced separately, and the creator keeps ownership of their content")
                        + ".\n\nReview this contract text:\n---\n"
                        + (d.get("text") or "")[:12000] + "\n---\n"
                        "Output: (1) PLAIN-ENGLISH SUMMARY (3-4 bullets: who does "
                        "what, pay, timeline); (2) RED FLAGS — anything below "
                        "their standards or creator-hostile (perpetual usage rights, "
                        "uncapped exclusivity, IP transfer, net-60+ payment, "
                        "unilateral termination, indemnification traps, morality "
                        "clauses, auto-renewal) each with WHY it hurts and what to "
                        "counter with; (3) FAIR TERMS worth keeping; (4) verdict: "
                        "SIGN / NEGOTIATE (list asks) / WALK. End with: 'I'm a "
                        "koala, not a lawyer — have an attorney confirm any contract "
                        "with real money at stake.'"}])
                B.record_claude_usage(_deals_db(), "legal", resp.usage)
                text = next((b.text for b in resp.content if b.type == "text"), "")
                self._send(json.dumps({"review": text}).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"review": "", "error": str(e)[:150]}).encode(),
                           "application/json")
            return
        if self.path == "/pitch":
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            try:
                text = draft_pitch(d.get("brand", ""), d.get("notes", ""))
                self._send(json.dumps({"pitch": text}).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"pitch": "", "error": str(e)[:150]}).encode(),
                           "application/json")
            return
        if self.path == "/engagement":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            set_engagement(bool(body.get("paused")))
            self._send(json.dumps(engagement_state()).encode(), "application/json")
            return
        if self.path == "/genphotos":
            fs = _read_form(self)
            try:
                count = max(1, min(32, int(fs.getfirst("count", "3"))))
            except ValueError:
                count = 3
            item = fs["file"] if "file" in fs else None
            blob = item.file.read() if item is not None and getattr(item, "file", None) else b""
            name = (getattr(item, "filename", "") or "").lower()
            mime = ("image/png" if name.endswith(".png") else "image/webp" if name.endswith(".webp") else "image/jpeg")
            if not blob:
                self._send(json.dumps({"msg": "attach today's reference selfie first"}).encode(), "application/json")
                return
            generate_photos_async(count, blob, mime)
            self._send(b'{"ok":true}', "application/json")
            return
        if self.path == "/reactions/add":
            fs = _read_form(self)
            item = fs["file"] if "file" in fs else None
            blob = item.file.read() if item is not None and getattr(item, "file", None) else b""
            name = (getattr(item, "filename", "") or "").lower()
            suffix = os.path.splitext(name)[1] or ".jpg"
            if not blob:
                self._send(json.dumps({"ok": False, "error": "attach a photo first"}).encode(), "application/json")
                return
            res = add_reaction(blob, suffix)
            self._send(json.dumps(res).encode(), "application/json")
            return
        if self.path == "/reactions/del":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            nm = os.path.basename(str(body.get("name", "")))
            ok = False
            if nm.startswith("rx_") and nm.endswith(".png"):
                try:
                    (REACT_DIR / nm).unlink(); ok = True
                except Exception:
                    ok = False
            self._send(json.dumps({"ok": ok}).encode(), "application/json")
            return
        if self.path == "/queuegenerated":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            page_id = str(body.get("page_id", ""))
            names = [n for n in body.get("names", []) if re.fullmatch(r"[\w.-]+\.png", str(n))]
            from datetime import date as _date
            day_dir = GEN_DIR / _date.today().isoformat()
            blobs = [(n, (day_dir / n).read_bytes()) for n in names if (day_dir / n).exists()]
            msg = queue_add(page_id, blobs) if (re.fullmatch(r"\d+", page_id) and blobs) else "nothing selected"
            self._send(json.dumps({"msg": msg}).encode(), "application/json")
            return
        if self.path == "/storyboard":
            notes, blob, ptype, dual = "", b"", "image/jpeg", False
            ctype = self.headers.get("Content-Type", "")
            if "multipart" in ctype:
                fs = _read_form(self)
                notes = fs.getfirst("notes", "") or ""
                dual = (fs.getfirst("dual", "") or "") == "1"
                item = fs["file"] if "file" in fs else None
                if item is not None and getattr(item, "file", None):
                    blob = item.file.read()
                    name = (getattr(item, "filename", "") or "").lower()
                    ptype = ("image/png" if name.endswith(".png")
                             else "image/webp" if name.endswith(".webp")
                             else "image/jpeg")
                    if len(blob) > 8_000_000:
                        blob = b"" # too large for the API; skip reference
            generate_storyboard_async(notes, blob, ptype, dual)
            self._send(b'{"ok":true}', "application/json")
            return
        if self.path == "/storyqueue":
            fs = _read_form(self)
            page_id = fs.getfirst("page_id", "")
            items = fs["file"] if "file" in fs else []
            if not isinstance(items, list): items = [items]
            blobs = [(getattr(i, "filename", "p.jpg") or "p.jpg", i.file.read())
                     for i in items if getattr(i, "file", None)]
            msg = queue_add(page_id, blobs) if (re.fullmatch(r"\d+", page_id) and blobs) else "missing photos or page"
            self._send(json.dumps({"msg": msg}).encode(), "application/json")
            return
        if self.path == "/storyqueue_del":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            try: queue_delete(int(body.get("id", 0)))
            except Exception: pass
            self._send(b'{"ok":true}', "application/json")
            return
        if self.path == "/postphoto":
            fs = _read_form(self)
            try:
                page_id = fs.getfirst("page_id", "")
                dest = fs.getfirst("dest", "story")
                caption = fs.getfirst("caption", "")
                item = fs["file"] if "file" in fs else None
                blob = item.file.read() if item is not None and item.file else b""
                if not re.fullmatch(r"\d+", page_id) or not blob:
                    msg = "missing photo or page"
                else:
                    msg = publish_photo(page_id, dest, caption, blob,
                                        getattr(item, "filename", "") or "photo.jpg")
            except Exception as e:
                msg = "error: " + str(e)[:150]
            self._send(json.dumps({"msg": msg}).encode(), "application/json")
            return
        if self.path == "/schedule/analyze":
            import tempfile
            fs = _read_form(self)
            # no keys: say what to add (the Load page shows this error) instead of failing mid-analysis
            _claude_ok = (bool(getattr(B._req_ctx, "claude_key", None)) if getattr(B._req_ctx, "visitor", False)
                          else bool(os.environ.get("ANTHROPIC_API_KEY")))
            if not B.muse_key() or not _claude_ok:
                self._send(json.dumps({"error": ANALYZE_NO_KEY_ERROR}).encode(), "application/json")
                return
            try:
                page_id = fs.getfirst("page_id", "") or B.PRIMARY_PAGE_ID
                if page_id and not str(page_id).isdigit():
                    self._send(b'{"error":"bad page id"}', "application/json"); return
                item = fs["video"] if "video" in fs else None
                blob = item.file.read() if item is not None and item.file else b""
                if not blob:
                    self._send(b'{"error":"no video received"}', "application/json"); return
                if len(blob) > 200 * 1024 * 1024:
                    self._send(b'{"error":"video too large (200MB max)"}', "application/json"); return
                import content_pipeline as cp
                db = db_conn()
                suffix = os.path.splitext(getattr(item, "filename", "") or "v.mp4")[1].lower() or ".mp4"
                if suffix not in (".mp4", ".mov", ".m4v", ".webm"):
                    suffix = ".mp4"
                drafts = BASE / "assets" / "drafts"
                drafts.mkdir(parents=True, exist_ok=True)
                vname = f"vid_{_secrets.token_hex(16)}{suffix}"   # unguessable: visitors cannot list or guess it
                if JUDGE_MODE:   # the hosted demo keeps only the last day of visitor uploads
                    for _old in drafts.glob("*"):
                        try:
                            if _old.is_file() and _time.time() - _old.stat().st_mtime > 86400:
                                _old.unlink()
                        except Exception:
                            pass
                vpath = drafts / vname
                vpath.write_bytes(blob)
                try:
                    card = cp.analyze(str(vpath), page_id, db, taken_times=_scheduled_times(page_id))
                    card["video_file"] = vname
                    card["page_id"] = page_id
                    card["pub_mode"] = get_pub_mode(page_id)
                finally:
                    db.close()
                # Full-auto pages schedule immediately, no review click needed.
                if card.get("pub_mode") == "auto" and (card.get("titles") or card.get("description")):
                    try:
                        card["auto_result"] = confirm_and_schedule({
                            "page_id": page_id, "title": (card.get("titles") or [""])[0],
                            "description": card.get("description", ""), "hashtags": card.get("hashtags", []),
                            "slot": card.get("proposed_slot"), "thumbnail": card.get("thumbnail"),
                            "captions": bool(card.get("transcript")), "transcript": card.get("transcript") or "",
                            "video_file": vname}, dry_run=False)
                    except Exception as e:
                        card["auto_result"] = "auto error: " + str(e)[:120]
                self._send(json.dumps(card).encode(), "application/json")
            except Exception as e:
                B.log.warning("analyze failed: %s", str(e)[:200])
                self._send(json.dumps({"error": "Could not analyze that video. Try a different file."}).encode(), "application/json")
            return
        if self.path == "/schedule/confirm":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                msg = confirm_and_schedule(body, dry_run=bool(body.get("dry_run")))
            except Exception as e:
                msg = "error: " + str(e)[:150]
            self._send(json.dumps({"msg": msg}).encode(), "application/json")
            return
        if self.path == "/pages":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            pid = str(body.get("page_id", ""))
            if re.fullmatch(r"\d+", pid):
                set_page_enabled(pid, bool(body.get("enabled")))
                _cache["recent"]["at"] = _cache["viral"]["at"] = _cache["trending"]["at"] = 0
                _MON_CACHE["at"] = 0.0; _MON_CACHE["pages"] = None
                _WATCH_CACHE["at"] = 0.0; _WATCH_CACHE["data"] = None
            self._send(b'{"ok": true}', "application/json")
            return
        if self.path == "/pubmode":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            pid = str(body.get("page_id", ""))
            if re.fullmatch(r"\d+", pid):
                set_pub_mode(pid, str(body.get("mode", "review")))
            self._send(b'{"ok": true}', "application/json")
            return
        if self.path.startswith("/checklist/"):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            act = self.path.split("/checklist/")[-1]
            if act == "add":
                self._send(json.dumps(checklist_add(body.get("text", ""))).encode(), "application/json")
            elif act == "toggle":
                checklist_toggle(body.get("id"), bool(body.get("done")))
                self._send(b'{"ok": true}', "application/json")
            elif act == "del":
                checklist_del(body.get("id"))
                self._send(b'{"ok": true}', "application/json")
            else:
                self._send(b'{"ok": false}', "application/json")
            return
        if self.path == "/react/plan":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                self._send(json.dumps(muse_react_plan(body.get("link", ""), body.get("note", ""))).encode(),
                           "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200], "beats": []}).encode(), "application/json")
            return
        if self.path == "/prompter":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            self._send(json.dumps(prompter_save(body)).encode(), "application/json")
            return
        if self.path == "/replies/decide":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            self._send(json.dumps(reply_decide(body.get("id"), str(body.get("action") or ""))).encode(), "application/json")
            return
        if self.path == "/replies/approval":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            self._send(json.dumps(reply_set_approval(str(body.get("page_id") or ""), bool(body.get("on")))).encode(), "application/json")
            return
        if self.path == "/prompter/write":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                out = prompter_write(body.get("summary", ""), body.get("links", []), body.get("seconds", 60))
            except Exception as e:
                out = {"lines": [], "error": str(e)[:200]}
            self._send(json.dumps(out).encode(), "application/json")
            return
        if self.path == "/film/plan":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                self._send(json.dumps(muse_film_plan(body.get("genre", ""), body.get("inputs", {}))).encode(),
                           "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)[:200], "beats": []}).encode(), "application/json")
            return
        if self.path == "/dmmode":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            pid = str(body.get("page_id", ""))
            if re.fullmatch(r"\d+", pid):
                set_dm_mode(pid, str(body.get("mode", "auto")))
            self._send(b'{"ok": true}', "application/json")
            return
        if self.path == "/ads/flag":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            pid = str(body.get("post_id", ""))
            if re.fullmatch(r"[\d_]+", pid):
                set_ad_post(pid, bool(body.get("on")))
            self._send(json.dumps({"ads": ad_post_ids()}).encode(), "application/json")
            return
        if self.path == "/targets":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if body.get("clear_all"):
                db = db_conn(); db.execute("DELETE FROM targets"); db.close()
                self._send(json.dumps({"ok": True, "count": 0}).encode(), "application/json")
                return
            ok = re.compile(r"[\d_]+")
            add = [str(p) for p in body.get("add", []) if ok.fullmatch(str(p))]
            remove = [str(p) for p in body.get("remove", []) if ok.fullmatch(str(p))]
            count = apply_target_changes(add, remove)
            self._send(json.dumps({"ok": True, "count": count}).encode(), "application/json")
        else:
            self._send(b'{}', "application/json")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    # the dashboard writes its own warnings to bot.log too (launchd does not capture them on macOS)
    import logging as _lg
    _dash_log = _lg.FileHandler(str(BASE / "bot.log"), encoding="utf-8")
    _dash_log.setFormatter(_lg.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    _lg.getLogger().addHandler(_dash_log)
    # The hosted judge build must never hold the owner's keys or Facebook token. Refuse to start if any are set.
    if JUDGE_MODE:
        _owner = [k for k in ("ANTHROPIC_API_KEY", "META_MUSE_API_KEY", "FB_PAGE_ACCESS_TOKEN", "FB_APP_SECRET", "BASETEN_API_KEY") if os.environ.get(k)]
        if _owner:
            raise SystemExit("Refusing to start the judge build: remove these environment variables from the host: " + ", ".join(_owner))
    # a fresh clone has no database yet: init_db() runs bot.ensure_schema(), which creates every table
    # the dashboard reads (idempotent, so every start is safe)
    try:
        B.init_db().close()
    except Exception as e:
        print("database setup failed:", e)
    _ensure_lan_cert()
    http_srv = _AbraServer((HOST, PORT), Handler)
    # Also serve HTTPS (second port) when a self-signed cert is present, so phones on the
    # LAN get a secure context — the browser requirement for mic access (getUserMedia).
    # Desktop http://localhost:PORT and the OAuth flow are untouched.
    cert, key = BASE / "certs" / "cert.pem", BASE / "certs" / "key.pem"
    if not JUDGE_MODE and cert.exists() and key.exists():
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(cert), str(key))
            https_srv = _AbraServer(("0.0.0.0", HTTPS_PORT), Handler)
            https_srv.socket = ctx.wrap_socket(https_srv.socket, server_side=True)
            threading.Thread(target=https_srv.serve_forever, daemon=True).start()
            B.log.info("Abra HTTPS listening on %d (self-signed)", HTTPS_PORT)
        except Exception as e:
            B.log.warning("HTTPS listener disabled: %s", str(e)[:160])
    http_srv.serve_forever()
