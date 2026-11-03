"""AI comment assistant for a Facebook creator page.

Polls the page's recent posts (videos, photos, reels — anything on the feed),
finds new comments, asks Claude (or Muse, for simple praise) to draft an on-brand reply, and posts it as
the Page. State is kept in a local SQLite database. Each comment is claimed before it is answered and checked against the page's existing replies, which guards against answering it twice.

Run:  python bot.py
Stop: Ctrl+C
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests

import meta_live
from dotenv import load_dotenv

try:                       # optional Meta OPEN-WEIGHT triage (Baseten/Fireworks/Modal)
    import muse_open       # inert until a provider key is set in .env
except Exception:          # never let an import hiccup take down the live bot
    muse_open = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

PRIMARY_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
PRIMARY_PAGE_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
# Personal settings. Each one has a blank or neutral default; set your own values in .env.
CREATOR_NAME = os.environ.get("CREATOR_NAME", "").strip()                   # used in prompts and titles; blank = no name
CREATOR_REF = f"creator {CREATOR_NAME}" if CREATOR_NAME else "the creator"  # how prompts refer to the creator
CREATOR_BIO = os.environ.get("CREATOR_BIO", "").strip()                     # optional facts for brand pitches (never invented)
CREATOR_TERMS = os.environ.get("CREATOR_TERMS", "").strip()                 # optional: your own contract standards
PRIMARY_PAGE_NAME = os.environ.get("PRIMARY_PAGE_NAME", "").strip() or "Main page"
SECOND_PAGE_NAME = os.environ.get("SECOND_PAGE_NAME", "").strip() or "Second page"
FB_APP_ID = os.environ.get("FB_APP_ID", "").strip()                         # your Facebook app's ID (Facebook login)
LAUNCHD_PREFIX = os.environ.get("LAUNCHD_PREFIX", "").strip() or "com.abra"  # macOS launchd label prefix
# current-page cursor (main loop rotates these across enabled pages)
PAGE_ID = PRIMARY_PAGE_ID
PAGE_TOKEN = PRIMARY_PAGE_TOKEN
API_VERSION = os.environ.get("FB_API_VERSION", "v23.0").strip()
GRAPH = f"https://graph.facebook.com/{API_VERSION}"

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
POSTS_TO_MONITOR = int(os.environ.get("POSTS_TO_MONITOR", "400"))
MAX_POST_PAGES = int(os.environ.get("MAX_POST_PAGES", "6"))
MAX_COMMENT_PAGES = int(os.environ.get("MAX_COMMENT_PAGES", "40"))
MAX_COMMENT_AGE_HOURS = float(os.environ.get("MAX_COMMENT_AGE_HOURS", "48"))
MAX_REPLIES_PER_WINDOW = int(os.environ.get("MAX_REPLIES_PER_WINDOW", "30"))
REPLY_WINDOW_MINUTES = float(os.environ.get("REPLY_WINDOW_MINUTES", "15"))
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() in ("1", "true", "yes")

MODEL = "claude-opus-5"
PERSONA_PATH = BASE_DIR / "persona.md"
# a fresh clone has no private persona yet: fall back to the generic example so the bot still starts
if not PERSONA_PATH.exists():
    PERSONA_PATH = BASE_DIR / "persona.example.md"
STYLE_PATH = BASE_DIR / "style_examples.md"
DB_PATH = BASE_DIR / "state.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fb-bot")
if sys.platform != "darwin":
    # launchd writes bot.log on macOS; elsewhere the bot writes it itself, so the dashboard feed works
    _file_log = logging.FileHandler(Path(__file__).resolve().parent / "bot.log", encoding="utf-8")
    _file_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(_file_log)

REPLY_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "should_reply": {
                "type": "boolean",
                "description": (
                    "false if the comment is spam, a scam, self-promotion, "
                    "hateful, divisive, or otherwise best left unanswered"
                ),
            },
            "should_like": {
                "type": "boolean",
                "description": (
                    "true if the page should Like this comment: positive, "
                    "funny, supportive, or genuinely engaging on-topic "
                    "comments. false for anything hateful, racist, divisive, "
                    "spammy, trolling, or that violates Facebook community "
                    "standards — never like those, even jokingly."
                ),
            },
            "reply": {
                "type": "string",
                "description": (
                    "The reply to post, 1-2 short sentences. Empty string "
                    "when should_reply is false."
                ),
            },
            "skip_reason": {
                "type": "string",
                "description": "Brief reason when should_reply is false, else empty.",
            },
        },
        "required": ["should_reply", "should_like", "reply", "skip_reason"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Local state (SQLite)
# ---------------------------------------------------------------------------

# Approve each reply first (per page, off by default): the reply is kept here and posted only when the creator approves it
REPLY_QUEUE_DDL = ("CREATE TABLE IF NOT EXISTS reply_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "comment_id TEXT UNIQUE, page_id TEXT, snippet TEXT, reply TEXT, status TEXT NOT NULL DEFAULT 'pending', "
                   "created_at TEXT, decided_at TEXT, error TEXT)")

# The teleprompter script (liveview.py keeps it here, one row per key)
PROMPTER_DDL = ("CREATE TABLE IF NOT EXISTS prompter (key TEXT PRIMARY KEY, body TEXT NOT NULL, "
                "speed INTEGER, size INTEGER, updated_at TEXT)")

# The Content Scheduler's reel queue. Older databases are upgraded column by column in ensure_content_queue().
CONTENT_QUEUE_DDL = ("CREATE TABLE IF NOT EXISTS content_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "page_id TEXT, title TEXT, description TEXT, hashtags TEXT, slot TEXT, thumbnail TEXT, "
                     "video_file TEXT, status TEXT, video_id TEXT, scheduled_for TEXT, stories INTEGER DEFAULT 0, "
                     "created_at TEXT)")

# Every table the bot, the dashboard, analytics.py, backfill.py and the judge seed read or write.
# All are CREATE TABLE IF NOT EXISTS, so running them on every start is safe.
SCHEMA_DDL = (
    """CREATE TABLE IF NOT EXISTS handled (
           comment_id TEXT PRIMARY KEY,
           action     TEXT NOT NULL,          -- 'replied' | 'skipped' | 'dry_run'
           reply_text TEXT,
           handled_at TEXT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS liked (
           comment_id TEXT PRIMARY KEY,
           liked_at   TEXT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS snapshots (
           taken_at  TEXT NOT NULL,           -- ISO date
           post_id   TEXT NOT NULL,
           comments  INTEGER,
           reactions INTEGER,
           shares    INTEGER,
           PRIMARY KEY (taken_at, post_id)
       )""",
    """CREATE TABLE IF NOT EXISTS page_snapshots (
           taken_at  TEXT PRIMARY KEY,
           followers INTEGER
       )""",
    """CREATE TABLE IF NOT EXISTS pages (
           page_id TEXT PRIMARY KEY,
           name    TEXT,
           token   TEXT,
           enabled INTEGER NOT NULL DEFAULT 1
       )""",
    """CREATE TABLE IF NOT EXISTS post_context (
           post_id     TEXT PRIMARY KEY,
           description TEXT,
           created_at  TEXT
       )""",
    """CREATE TABLE IF NOT EXISTS fans_log (
           ts          TEXT,    -- when we saw it (UTC ISO)
           comment_id  TEXT PRIMARY KEY,
           author_id   TEXT,
           author_name TEXT,
           post_id     TEXT
       )""",
    """CREATE TABLE IF NOT EXISTS spend (
           ts       TEXT,    -- UTC ISO
           provider TEXT,    -- claude | muse
           kind     TEXT,    -- reply | vision | triage | image
           tin      INTEGER, -- input tokens
           tout     INTEGER, -- output tokens
           cost     REAL     -- estimated USD
       )""",
    """CREATE TABLE IF NOT EXISTS story_queue (
           id        INTEGER PRIMARY KEY AUTOINCREMENT,
           page_id   TEXT NOT NULL,
           path      TEXT NOT NULL,
           slot      TEXT NOT NULL,      -- local ISO "YYYY-MM-DDTHH:MM"
           posted_at TEXT,
           error     TEXT
       )""",
    """CREATE TABLE IF NOT EXISTS targets (
           post_id  TEXT PRIMARY KEY,       -- posts the creator checked on the dashboard
           enabled  INTEGER NOT NULL DEFAULT 1,
           added_at TEXT
       )""",
    """CREATE TABLE IF NOT EXISTS throttle (
           id INTEGER PRIMARY KEY CHECK (id = 1),
           paused_until TEXT,
           backoff_level INTEGER NOT NULL DEFAULT 0,
           last_trip_at TEXT,
           reason TEXT
       )""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
    REPLY_QUEUE_DDL,
    "CREATE TABLE IF NOT EXISTS post_transcripts (post_id TEXT PRIMARY KEY, "
    "transcript TEXT, duration_ms INTEGER, created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS dm_handled (convo_id TEXT, last_msg_id TEXT, "
    "handled_at TEXT, PRIMARY KEY (convo_id, last_msg_id))",
    "CREATE TABLE IF NOT EXISTS dm_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "page_id TEXT, convo_id TEXT, sender TEXT, incoming TEXT, category TEXT, "
    "action TEXT, reply TEXT, at TEXT)",
    "CREATE TABLE IF NOT EXISTS seeded (post_id TEXT PRIMARY KEY, at TEXT)",
    "CREATE TABLE IF NOT EXISTS page_insights (taken_at TEXT, page_id TEXT, metric TEXT, value INTEGER, "
    "PRIMARY KEY (taken_at, page_id, metric))",
    "CREATE TABLE IF NOT EXISTS checklist (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "text TEXT NOT NULL, done INTEGER DEFAULT 0, created_at TEXT)",
    PROMPTER_DDL,
    "CREATE TABLE IF NOT EXISTS profile (fb_id TEXT PRIMARY KEY, name TEXT, email TEXT, "
    "picture_path TEXT, connected_at TEXT)",
    "CREATE TABLE IF NOT EXISTS deals (id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT, contact TEXT, "
    "stage TEXT DEFAULT 'prospect', value TEXT, notes TEXT, updated TEXT)",
    "CREATE TABLE IF NOT EXISTS content_rights (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "title TEXT, fingerprint TEXT, note TEXT, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ig_accounts (ig_id TEXT PRIMARY KEY, username TEXT, page_id TEXT, "
    "token TEXT, enabled INTEGER NOT NULL DEFAULT 1)",
)


def ensure_content_queue(db: sqlite3.Connection) -> None:
    """content_queue, plus the column upgrades that older databases need."""
    db.execute(CONTENT_QUEUE_DDL)
    cols = [c[1] for c in db.execute("PRAGMA table_info(content_queue)")]
    for col, decl in (("video_file", "TEXT"), ("video_id", "TEXT"),
                      ("scheduled_for", "TEXT"), ("stories", "INTEGER DEFAULT 0"),
                      ("as_ad", "INTEGER DEFAULT 0"), ("ad_linked", "INTEGER DEFAULT 0")):
        if col not in cols:
            db.execute(f"ALTER TABLE content_queue ADD COLUMN {col} {decl}")


def ensure_schema(db: sqlite3.Connection) -> None:
    """The one startup function for the database: creates every table the app reads, if it is missing.
    init_db() calls it, and bot.py, liveview.py, analytics.py, backfill.py and judge/seed.py all open the
    database through init_db(), so a fresh state.db has every table the pages read."""
    for ddl in SCHEMA_DDL:
        db.execute(ddl)
    ensure_content_queue(db)
    db.execute("INSERT OR IGNORE INTO throttle (id, backoff_level) VALUES (1, 0)")
    db.commit()


def init_db() -> sqlite3.Connection:
    # timeout lets bot.py and backfill.py share the db without lock errors;
    # autocommit mode so the explicit BEGIN IMMEDIATE in claim() works cleanly
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    ensure_schema(db)
    return db


def post_story_photo(db: sqlite3.Connection, page_id: str, path: str) -> str | None:
    """Publish a queued photo as a page Story. Returns error string or None."""
    row = db.execute("SELECT token, name FROM pages WHERE page_id=?", (page_id,)).fetchone()
    if not row:
        return "unknown page"
    token = row[0]
    try:
        with open(path, "rb") as fh:
            r = requests.post(f"{GRAPH}/{page_id}/photos",
                              data={"published": "false", "access_token": token},
                              files={"source": fh}, timeout=60).json()
        if "error" in r:
            return r["error"].get("message", "upload error")
        r2 = requests.post(f"{GRAPH}/{page_id}/photo_stories",
                           data={"photo_id": r["id"], "access_token": token},
                           timeout=60).json()
        if "error" in r2:
            return r2["error"].get("message", "story error")
        return None
    except Exception as e:
        return str(e)[:200]


def post_story_video(db: sqlite3.Connection, page_id: str, path: str) -> str | None:
    """Publish a queued video clip as a page Story (resumable video_stories flow)."""
    row = db.execute("SELECT token FROM pages WHERE page_id=?", (page_id,)).fetchone()
    if not row:
        return "unknown page"
    token = row[0]
    try:
        start = requests.post(f"{GRAPH}/{page_id}/video_stories",
                              data={"upload_phase": "start", "access_token": token}, timeout=30).json()
        vid = start.get("video_id")
        if not vid:
            return f"start: {start.get('error', start)}"[:200]
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            up = requests.post(f"https://rupload.facebook.com/video-upload/{API_VERSION}/{vid}",
                               headers={"Authorization": f"OAuth {token}",
                                        "offset": "0", "file_size": str(size)},
                               data=fh.read(), timeout=600).json()
        if up.get("error"):
            return f"upload: {up.get('error')}"[:200]
        fin = requests.post(f"{GRAPH}/{page_id}/video_stories",
                            data={"upload_phase": "finish", "video_id": vid,
                                  "access_token": token}, timeout=60).json()
        if fin.get("error"):
            return f"finish: {fin.get('error')}"[:200]
        return None
    except Exception as e:
        return str(e)[:200]


def pump_story_queue(db: sqlite3.Connection) -> None:
    """Post any queued stories whose slot time has arrived (local time)."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    due = db.execute(
        "SELECT id, page_id, path FROM story_queue "
        "WHERE posted_at IS NULL AND error IS NULL AND slot <= ? ORDER BY slot LIMIT 3",
        (now,),
    ).fetchall()
    for qid, page_id, path in due:
        is_video = os.path.splitext(path)[1].lower() in (".mp4", ".mov", ".m4v", ".webm")
        err = post_story_video(db, page_id, path) if is_video else post_story_photo(db, page_id, path)
        if err:
            db.execute("UPDATE story_queue SET error=? WHERE id=?", (err[:200], qid))
            log.error("STORY failed (queue #%d): %s", qid, err)
        else:
            db.execute("UPDATE story_queue SET posted_at=? WHERE id=?",
                       (datetime.now(timezone.utc).isoformat(), qid))
            log.info("STORY posted (queue #%d) -> page %s", qid, page_id)
        db.commit()


def load_pages(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Enabled pages the assistant works on: [(page_id, name, token)].
    The primary page from .env is inserted if its row is missing, so it is included unless its row is disabled."""
    if PRIMARY_PAGE_ID:   # never write a placeholder row (a GET or a judge build must not add one)
        db.execute(
            "INSERT OR IGNORE INTO pages (page_id, name, token, enabled) VALUES (?, ?, ?, 1)",
            (PRIMARY_PAGE_ID, PRIMARY_PAGE_NAME, PRIMARY_PAGE_TOKEN),
        )
    db.commit()
    return [(r[0], r[1] or r[0], r[2] or PRIMARY_PAGE_TOKEN) for r in db.execute(
        "SELECT page_id, name, token FROM pages WHERE enabled = 1"
    ).fetchall()]


def get_ad_posts(db: sqlite3.Connection) -> list[str]:
    """Posts the page is running an ad on (flagged in the dashboard). They get priority engagement too."""
    try:
        row = db.execute("SELECT value FROM settings WHERE key = 'ad_posts'").fetchone()
        return [str(x) for x in json.loads(row[0])] if row and row[0] else []
    except Exception:
        return []


def get_targets(db: sqlite3.Connection) -> list[str]:
    """Post ids the creator has checked for priority engagement on the dashboard."""
    return [r[0] for r in db.execute(
        "SELECT post_id FROM targets WHERE enabled = 1"
    ).fetchall()]


THROTTLE_BASE_MINUTES = 15
THROTTLE_MAX_MINUTES = 240


def _ensure_throttle_row(db: sqlite3.Connection) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS throttle (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               paused_until TEXT,
               backoff_level INTEGER NOT NULL DEFAULT 0,
               last_trip_at TEXT,
               reason TEXT
           )"""
    )
    db.execute("INSERT OR IGNORE INTO throttle (id, backoff_level) VALUES (1, 0)")
    db.commit()


def throttle_seconds_remaining(db: sqlite3.Connection) -> float:
    """>0 while Facebook has told us to back off."""
    _ensure_throttle_row(db)
    row = db.execute("SELECT paused_until FROM throttle WHERE id=1").fetchone()
    if not row or not row[0]:
        return 0.0
    remaining = (datetime.fromisoformat(row[0]) - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, remaining)


SAFE_REPLY_CAP = 30    # conservative pace; tripwire falls back here permanently
BURST_REPLY_CAP = 60   # surge mode: only while demand maxes the normal cap


def reply_cap(db: sqlite3.Connection) -> int:
    """Current reply cap per window. Any Facebook pushback (ever) flips the
    tripwire and pins us at the conservative 30 until the pace_safe_mode setting is cleared in state.db (no dashboard control yet). When a
    window recently maxed out (viral moment), surge to 60 until the rush
    passes — write pacing (2s rhythm) is unchanged either way."""
    try:
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        row = db.execute("SELECT value FROM settings WHERE key='pace_safe_mode'").fetchone()
        if row and row[0] == "1":
            return min(SAFE_REPLY_CAP, MAX_REPLIES_PER_WINDOW)
        row = db.execute("SELECT value FROM settings WHERE key='demand_hit_at'").fetchone()
        if row and row[0]:
            hit = datetime.fromisoformat(row[0])
            if datetime.now(timezone.utc) - hit < timedelta(minutes=30):
                return BURST_REPLY_CAP
    except (sqlite3.Error, ValueError):
        pass
    return MAX_REPLIES_PER_WINDOW


def trip_throttle(db: sqlite3.Connection, reason: str) -> float:
    """Facebook pushed back — pause with exponential backoff. Returns pause minutes."""
    _ensure_throttle_row(db)
    try:   # tripwire: drop back to the conservative pace for good
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT OR REPLACE INTO settings VALUES ('pace_safe_mode', '1')")
        log.warning("Pace tripwire: reply cap pinned back to %d/window until cleared",
                    SAFE_REPLY_CAP)
    except sqlite3.Error:
        pass
    level = db.execute("SELECT backoff_level FROM throttle WHERE id=1").fetchone()[0]
    minutes = min(THROTTLE_BASE_MINUTES * (2 ** level), THROTTLE_MAX_MINUTES)
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    db.execute(
        "UPDATE throttle SET paused_until=?, backoff_level=?, last_trip_at=?, reason=? WHERE id=1",
        (until.isoformat(), level + 1, datetime.now(timezone.utc).isoformat(), reason[:300]),
    )
    db.commit()
    log.warning("FACEBOOK THROTTLE tripped (%s) — pausing comment replies and likes for %d min (backoff level %d)",
                reason[:120], minutes, level + 1)
    return minutes


def note_post_success(db: sqlite3.Connection) -> None:
    """A reply landed cleanly; if the last trip was >1h ago, reset the backoff."""
    _ensure_throttle_row(db)
    row = db.execute("SELECT backoff_level, last_trip_at FROM throttle WHERE id=1").fetchone()
    if row and row[0] and row[1]:
        last = datetime.fromisoformat(row[1])
        if datetime.now(timezone.utc) - last > timedelta(hours=1):
            db.execute("UPDATE throttle SET backoff_level=0 WHERE id=1")
            db.commit()
            log.info("Throttle backoff reset — Facebook is happy again.")


def record_like(db: sqlite3.Connection, comment_id: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO liked (comment_id, liked_at) VALUES (?, ?)",
        (comment_id, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def claim(db: sqlite3.Connection, comment_id: str) -> bool:
    """Atomically claim a comment so only ONE process (live bot or backfill)
    can ever reply to it. Returns False if it's already claimed or replied."""
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT action FROM handled WHERE comment_id = ?", (comment_id,)
        ).fetchone()
        if row and row[0] in ("replied", "in_progress"):
            db.execute("ROLLBACK")
            return False
        db.execute(
            "INSERT OR REPLACE INTO handled (comment_id, action, reply_text, handled_at) "
            "VALUES (?, 'in_progress', NULL, ?)",
            (comment_id, datetime.now(timezone.utc).isoformat()),
        )
        db.execute("COMMIT")
        return True
    except sqlite3.Error:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return False


def release_claim(db: sqlite3.Connection, comment_id: str) -> None:
    """Failed mid-flight — free the comment so a later cycle can retry it."""
    db.execute(
        "DELETE FROM handled WHERE comment_id = ? AND action = 'in_progress'",
        (comment_id,),
    )


def page_reply_count(comment_id: str) -> int:
    """Fresh check with Facebook: how many replies has the PAGE already
    posted under this comment? (Defense against double-replying.)"""
    data = graph_get(f"{comment_id}/comments", fields="from{id}", limit=100)
    return sum(1 for c in data.get("data", [])
               if c.get("from", {}).get("id") == PAGE_ID)


def graph_delete(path: str) -> dict:
    resp = requests.delete(f"{GRAPH}/{path}", params={"access_token": PAGE_TOKEN}, timeout=30)
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise GraphError(f"Graph API error on DELETE /{path}: {err}",
                         code=err.get("code", 0), subcode=err.get("error_subcode", 0))
    return data


def already_handled(db: sqlite3.Connection, comment_id: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM handled WHERE comment_id = ?", (comment_id,)
    ).fetchone()
    return row is not None


def mark_handled(db: sqlite3.Connection, comment_id: str, action: str, reply: str | None) -> None:
    db.execute(
        "INSERT OR REPLACE INTO handled (comment_id, action, reply_text, handled_at) VALUES (?, ?, ?, ?)",
        (comment_id, action, reply, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()




def reply_needs_approval(db: sqlite3.Connection, page_id: str) -> bool:
    try:
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        row = db.execute("SELECT value FROM settings WHERE key=?", (f"reply_approval:{page_id}",)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0] == "1")


def queue_reply(db: sqlite3.Connection, comment_id: str, page_id: str, snippet: str, reply: str) -> None:
    db.execute(REPLY_QUEUE_DDL)
    db.execute("INSERT OR IGNORE INTO reply_queue (comment_id, page_id, snippet, reply, status, created_at) "
               "VALUES (?, ?, ?, ?, 'pending', ?)",
               (comment_id, page_id, snippet, reply, datetime.now(timezone.utc).isoformat()))
    db.commit()


def replies_in_window(db: sqlite3.Connection) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=REPLY_WINDOW_MINUTES)).isoformat()
    row = db.execute(
        "SELECT COUNT(*) FROM handled WHERE action = 'replied' AND handled_at > ?",
        (cutoff,),
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Facebook Graph API
# ---------------------------------------------------------------------------

class GraphError(RuntimeError):
    """Graph API error carrying Facebook's error code for throttle detection."""

    def __init__(self, message: str, code: int = 0, subcode: int = 0):
        super().__init__(message)
        self.code = code
        self.subcode = subcode


# Facebook codes that mean "slow down / you are blocked from this action":
#   4/17/32/613 = API rate limits, 368 = temporarily blocked for going too fast
THROTTLE_CODES = {4, 17, 32, 368, 613}


def graph_get(path: str, **params) -> dict:
    params["access_token"] = PAGE_TOKEN
    resp = requests.get(f"{GRAPH}/{path}", params=params, timeout=30)
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise GraphError(f"Graph API error on GET /{path}: {err}",
                         code=err.get("code", 0), subcode=err.get("error_subcode", 0))
    return data


def graph_post(path: str, **params) -> dict:
    params["access_token"] = PAGE_TOKEN
    resp = requests.post(f"{GRAPH}/{path}", data=params, timeout=30)
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise GraphError(f"Graph API error on POST /{path}: {err}",
                         code=err.get("code", 0), subcode=err.get("error_subcode", 0))
    return data


def fetch_recent_posts() -> list[dict]:
    """The page's most recent published posts (up to POSTS_TO_MONITOR, default 400), newest first, with the
    total comment count so finished posts can be skipped cheaply."""
    posts: list[dict] = []
    fields = ("id,message,created_time,permalink_url,full_picture,"
              "attachments{title,description,type},comments.summary(true).limit(0)")
    try:
        data = graph_get(f"{PAGE_ID}/published_posts", fields=fields, limit=100)
    except RuntimeError as e:
        if "reduce the amount of data" not in str(e):
            raise
        # Facebook is moody about big pages today — ask for smaller bites
        log.info("[%s] Facebook asked for smaller requests — fetching 25 at a time", PAGE_ID)
        data = graph_get(f"{PAGE_ID}/published_posts", fields=fields, limit=25)
    posts.extend(data.get("data", []))
    pages_left = MAX_POST_PAGES
    while pages_left > 0:
        nxt = data.get("paging", {}).get("next")
        if not nxt or len(posts) >= POSTS_TO_MONITOR:
            break
        try:
            data = requests.get(nxt, timeout=30).json()
        except Exception:
            break
        if "error" in data:
            break
        batch = data.get("data", [])
        if not batch:
            break
        posts.extend(batch)
        pages_left -= 1
    seen, unique = set(), []
    for p in posts:                      # de-dupe across pagination overlap
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)
    return unique[:POSTS_TO_MONITOR]


def post_comment_total(post: dict) -> int:
    return post.get("comments", {}).get("summary", {}).get("total_count", 0)


def handled_count_for_post(db: sqlite3.Connection, post_id: str) -> int:
    """How many comments under this post we've already dealt with."""
    prefix = post_id.split("_")[-1] + "_"
    row = db.execute(
        "SELECT COUNT(*) FROM handled WHERE comment_id LIKE ?", (prefix + "%",)
    ).fetchone()
    return row[0]


def fetch_comments(post_id: str, max_pages: int = MAX_COMMENT_PAGES) -> list[dict]:
    """Top-level comments on a post (paginated, up to MAX_COMMENT_PAGES pages), with each comment's
    replies when Facebook returns them, so we can tell if the page already answered."""
    comments: list[dict] = []
    # Reels occasionally reject the nested comments{from{id}} expansion with a (#100)
    # error. Degrade gracefully through simpler field sets so we never abandon a post
    # (and its unanswered comments) just because the richest query failed.
    _FIELD_SETS = (
        "id,message,from{id,name},created_time,is_hidden,comments.limit(25){from{id}}",
        "id,message,from{id,name},created_time,is_hidden",
        "id,message,from{id,name},created_time",
    )
    data = None
    for _fields in _FIELD_SETS:
        try:
            data = graph_get(
                f"{post_id}/comments",
                fields=_fields,
                order="reverse_chronological",
                filter="toplevel",
                limit=100,
            )
            break
        except Exception as _e:
            data = None
            continue
    if data is None:
        return []   # genuinely unfetchable — caller moves on to the next post quietly
    comments.extend(data.get("data", []))
    pages_left = max_pages
    while pages_left > 0:
        nxt = data.get("paging", {}).get("next")
        if not nxt:
            break
        try:
            data = requests.get(nxt, timeout=30).json()
        except Exception:
            break
        if "error" in data:
            break
        batch = data.get("data", [])
        if not batch:
            break
        comments.extend(batch)
        pages_left -= 1
    seen, unique = set(), []
    for c in comments:
        if c["id"] not in seen:
            seen.add(c["id"])
            unique.append(c)
    return unique


import threading as _threading

_WRITE_LOCK = _threading.Lock()          # one Facebook write at a time
_WRITE_SPACING = 2.0                     # seconds between writes (human-paced)
_last_write = [0.0]


def _paced() -> None:
    # call while holding _WRITE_LOCK: keeps the same 2s write rhythm Facebook
    # has seen all along, no matter how many drafting threads run behind it
    wait = _WRITE_SPACING - (time.monotonic() - _last_write[0])
    if wait > 0:
        time.sleep(wait)
    _last_write[0] = time.monotonic()


def post_reply(comment_id: str, message: str) -> None:
    with _WRITE_LOCK:
        _paced()
        graph_post(f"{comment_id}/comments", message=message)


def like_comment(comment_id: str) -> None:
    with _WRITE_LOCK:
        _paced()
        graph_post(f"{comment_id}/likes")


def page_already_replied(comment: dict) -> bool:
    for child in comment.get("comments", {}).get("data", []):
        if child.get("from", {}).get("id") == PAGE_ID:
            return True
    return False


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

_OWNER_CLAUDE = anthropic.Anthropic()


class _ClaudeRouter:
    """Stands in for the Claude client. A visitor request uses only the visitor's own key and never falls
    back to the owner's; the owner's own requests use the owner's key as before."""

    def __getattr__(self, name):
        key = getattr(_req_ctx, "claude_key", None)
        if key:
            return getattr(anthropic.Anthropic(api_key=key), name)
        if getattr(_req_ctx, "visitor", False):
            raise RuntimeError("This step needs your own Claude key.")
        return getattr(_OWNER_CLAUDE, name)


claude = _ClaudeRouter()


def load_persona() -> str:
    """Persona + (if present) real reply examples harvested by learn_style.py."""
    persona = PERSONA_PATH.read_text(encoding="utf-8")
    if STYLE_PATH.exists():
        persona += "\n\n" + STYLE_PATH.read_text(encoding="utf-8")
        log.info("Loaded real reply examples from %s", STYLE_PATH.name)
    else:
        log.info("No style_examples.md yet — run 'python learn_style.py' after "
                 "setup so replies imitate the page's real voice.")
    return persona


TRANSCRIBE_URL = "https://api.meta.ai/v1/asr/transcribe"


def _video_source_url(post: dict) -> str:
    """Best-effort mp4 URL for a post's video (own-page videos only)."""
    try:
        att = graph_get(post["id"], fields="attachments{media_type,media,target}")
        for a in (att.get("attachments") or {}).get("data") or []:
            if a.get("media_type") != "video":
                continue
            src = (a.get("media") or {}).get("source")
            if src:
                return src
            vid = (a.get("target") or {}).get("id")
            if vid:
                v = graph_get(vid, fields="source")
                if v.get("source"):
                    return v["source"]
    except Exception as e:
        log.warning("no video source for %s: %s", post.get("id"), str(e)[:80])
    return ""


def get_video_transcript(db: sqlite3.Connection, post: dict) -> str:
    """HACKATHON SPRINT FEATURE (Muse Voice Transcribe): the bot HEARS the
    video — downloads the reel's audio, transcribes it via Meta's ASR, and
    grounds replies in what the creator actually says. Cached per post; ~$0.003/min."""
    if not muse_key():
        return ""
    pid = post["id"]
    db.execute("CREATE TABLE IF NOT EXISTS post_transcripts (post_id TEXT PRIMARY KEY, "
               "transcript TEXT, duration_ms INTEGER, created_at TEXT)")
    row = db.execute("SELECT transcript FROM post_transcripts WHERE post_id=?",
                     (pid,)).fetchone()
    if row is not None:
        return row[0] or ""
    transcript, dur = "", 0
    src = _video_source_url(post)
    if src:
        import tempfile
        import subprocess as _sp
        try:
            with tempfile.TemporaryDirectory() as td:
                mp4 = os.path.join(td, "v.mp4")
                wav = os.path.join(td, "v.wav")
                with requests.get(src, stream=True, timeout=90) as r:
                    r.raise_for_status()
                    size = 0
                    with open(mp4, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            size += len(chunk)
                            if size > 30 * 1024 * 1024:
                                raise RuntimeError("video too large for transcription")
                            f.write(chunk)
                wav_16k_mono(mp4, wav, timeout=120)
                with open(wav, "rb") as f:
                    resp = meta_live.post("muse-voice-transcribe-1.0", "transcribe a video",
                        TRANSCRIBE_URL,
                        headers={"Authorization": f"Bearer {muse_key()}",
                                 "Accept": "application/json"},
                        files={"request": (None, json.dumps(
                                    {"model": "muse-voice-transcribe-1.0",
                                     "audioEncoding": "WAV",
                                     "mode": "PUSH_TO_TALK"}), "application/json"),
                               "audio": ("v.wav", f, "audio/wav")},
                        timeout=120)
                d = resp.json()
                transcript = (d.get("transcript") or "").strip()[:2500]
                dur = int(d.get("audioDurationMs") or 0)
                record_spend(db, "muse", "transcribe", 0, 0, dur / 3600000.0 * 0.18)
                log.info("EARS   transcribed %s (%.0fs): \"%s…\"",
                         pid, dur / 1000, transcript[:70])
        except Exception as e:
            log.warning("transcribe failed for %s: %s", pid, str(e)[:100])
            return ""   # not cached: a missing ffmpeg or a network hiccup must not blank this post for good
    db.execute("INSERT OR REPLACE INTO post_transcripts VALUES (?, ?, ?, ?)",
               (pid, transcript, dur, datetime.now(timezone.utc).isoformat()))
    db.commit()
    return transcript


def get_post_context(db: sqlite3.Connection, post: dict) -> str:
    """One-time grounded description of what the post actually shows (vision on
    the thumbnail + attachment text), cached per post. Empty string if unknown."""
    pid = post.get("id", "")
    row = db.execute("SELECT description FROM post_context WHERE post_id=?", (pid,)).fetchone()
    if row is not None:
        return row[0] or ""
    att = (post.get("attachments", {}) or {}).get("data", [{}])
    att0 = att[0] if att else {}
    att_text = " ".join(filter(None, [att0.get("title", ""), att0.get("description", "")]))[:500]
    caption = (post.get("message") or "")[:1000]
    content = []
    thumb_url = post.get("full_picture")
    if thumb_url:
        try:
            img = requests.get(thumb_url, timeout=30).content
            if len(img) < 4_000_000:
                import base64
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(img).decode()}})
        except Exception:
            pass
    desc = ""
    if content or caption or att_text:
        content.append({"type": "text", "text":
            "This is a Facebook post (image = the video's thumbnail frame).\n"
            f"Caption: {caption or '(none)'}\nAttachment text: {att_text or '(none)'}\n\n"
            "In 2-3 sentences, state what this post/video appears to be about, for a "
            "comment-reply assistant. ONLY describe what you can actually see or read "
            "above — no guessing beyond it. If you genuinely can't tell, reply exactly: "
            "UNCLEAR"})
        try:
            resp = claude.messages.create(model=MODEL, max_tokens=300,
                                          messages=[{"role": "user", "content": content}])
            record_claude_usage(db, "vision", resp.usage)
            desc = next((b.text for b in resp.content if b.type == "text"), "").strip()
            if desc.upper().startswith("UNCLEAR"):
                desc = ""
        except Exception as e:
            log.error("post-context failed for %s: %s", pid, str(e)[:120])
            return ""   # don't cache failures — retry next time
    # HACKATHON SPRINT: fuse the eyes (thumbnail vision) with the new ears
    # (Muse Voice Transcribe) so replies can quote the actual video
    transcript = get_video_transcript(db, post)
    if transcript:
        desc = (desc + "\n\nWHAT THE CREATOR SAYS IN THE VIDEO (verbatim transcript): \""
                + transcript + "\"").strip()
    db.execute("INSERT OR REPLACE INTO post_context (post_id, description, created_at) VALUES (?, ?, ?)",
               (pid, desc, datetime.now(timezone.utc).isoformat()))
    db.commit()
    return desc


# USD per token — Claude Opus 5 ($5/$25 per M, 1h cache write 2x, cache read 0.1x)
# and Muse Spark 1.3 ($1.25/$4.25 per M). Estimates for the koala's books.
CLAUDE_PRICE = {"in": 5.0e-6, "out": 25.0e-6, "cw": 10.0e-6, "cr": 0.5e-6}
MUSE_PRICE = {"in": 1.25e-6, "out": 4.25e-6}


def record_spend(db, provider: str, kind: str, tin: int = 0, tout: int = 0,
                 cost: float = 0.0) -> None:
    """Log an AI call's estimated cost so the dashboard koala can keep books."""
    if getattr(_req_ctx, "visitor", False):
        return   # visitor sessions never write to the owner's books
    try:
        if db is None:
            return
        db.execute("INSERT INTO spend VALUES (?, ?, ?, ?, ?, ?)",
                   (datetime.now(timezone.utc).isoformat(), provider, kind,
                    tin, tout, round(cost, 6)))
        db.commit()
    except Exception:
        pass


def record_claude_usage(db, kind: str, usage) -> None:
    try:
        tin = getattr(usage, "input_tokens", 0) or 0
        tout = getattr(usage, "output_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cost = (tin * CLAUDE_PRICE["in"] + tout * CLAUDE_PRICE["out"]
                + cw * CLAUDE_PRICE["cw"] + cr * CLAUDE_PRICE["cr"])
        record_spend(db, "claude", kind, tin + cw + cr, tout, cost)
    except Exception:
        pass


def draft_reply(persona: str, post: dict, comment: dict, video_context: str = "",
                db=None) -> dict:
    """Ask Claude whether/how to reply. Returns the parsed decision dict."""
    post_caption = (post.get("message") or "(no caption)")[:2000]
    commenter = comment.get("from", {}).get("name") or "A fan"
    comment_text = (comment.get("message") or "")[:2000]

    ctx_block = (f"<what_the_video_shows>\n{video_context}\n</what_the_video_shows>\n\n"
                 if video_context else
                 "<what_the_video_shows>\nUNKNOWN — you cannot see this video. "
                 "Reply ONLY to what the commenter said. Do NOT mention or invent any "
                 "detail about the video's content.\n</what_the_video_shows>\n\n")
    prompt = (
        "A new comment was posted on the page. Decide whether to reply and, "
        "if so, write the reply.\n\n"
        "GROUNDING RULE: base any statement about the video STRICTLY on "
        "<what_the_video_shows> and <post_caption>. Never invent names, events, "
        "transformations, or topics that are not in those blocks. When in doubt, "
        "keep the reply about the commenter's words only.\n\n"
        "DATE RULE: today is " + datetime.now().strftime("%B %d, %Y") + ". The post "
        "was published " + (post.get("created_time", "")[:10] or "at an unknown date") + ". "
        "If the post is about a holiday, season, or event that is not current "
        "(e.g. New Year, Christmas, Halloween), do NOT use seasonal greetings or "
        "wishes — reply in a timeless way about the content itself.\n\n"
        + ctx_block +
        f"<post_caption>\n{post_caption}\n</post_caption>\n\n"
        f"<commenter_name>{commenter}</commenter_name>\n\n"
        f"<comment>\n{comment_text}\n</comment>"
    )

    response = claude.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": persona,
                # 1h TTL keeps the ~3.2K-token persona cached across the bot's
                # polling cycles (5m expired between quiet bursts -> low hit rate).
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ],
        output_config={"format": REPLY_SCHEMA, "effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )

    record_claude_usage(db, "reply", response.usage)

    if response.stop_reason == "refusal":
        return {"should_reply": False, "reply": "", "skip_reason": "model refusal"}

    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# ---------------------------------------------------------------------------
# Direct messages (needs the pages_messaging scope — granted via /connect re-auth)
# ---------------------------------------------------------------------------
DM_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string",
                "enum": ["greeting", "praise", "question", "business", "complaint", "spam", "other"],
                "description": "best single label for the latest incoming DM"},
            "should_reply": {"type": "boolean",
                "description": "true ONLY for a greeting, praise/thanks, or a simple general "
                "question that is safe to auto-answer in the creator's voice. false for brand/"
                "business/sponsorship, money, complaints, anything needing the creator personally, "
                "personal/sensitive topics, spam, or scams."},
            "reply": {"type": "string",
                "description": "warm 1-2 sentence reply in the creator's voice; empty when should_reply is false"},
        },
        "required": ["category", "should_reply", "reply"],
        "additionalProperties": False,
    },
}


def draft_dm_reply(persona: str, sender: str, history: str, latest: str, db=None) -> dict:
    """Triage a DM: auto-answer safe chit-chat, flag everything sensitive for the creator."""
    prompt = (
        "You handle the creator's Facebook direct messages. Read the recent thread and the "
        "latest incoming message, then TRIAGE it.\n"
        "- Auto-reply ONLY to greetings, praise/thanks, and simple general questions a creator "
        "would happily answer, in the creator's voice (warm, 1-2 sentences).\n"
        "- Do NOT auto-reply to brand/business/sponsorship inquiries, money, complaints, anything "
        "needing the creator personally, personal/sensitive topics, spam or scams — set "
        "should_reply=false so the creator handles them.\n"
        "- Never promise anything, never share links or contact/financial info, never make deals.\n"
        "Today is " + datetime.now().strftime("%B %d, %Y") + ".\n\n"
        f"<sender>{sender}</sender>\n<recent_thread>\n{history}\n</recent_thread>\n\n"
        f"<latest_incoming>\n{latest}\n</latest_incoming>"
    )
    try:
        response = claude.messages.create(
            model=MODEL, max_tokens=600,
            system=[{"type": "text", "text": persona, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            output_config={"format": DM_SCHEMA, "effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        record_claude_usage(db, "dm", response.usage)
        if response.stop_reason == "refusal":
            return {"category": "other", "should_reply": False, "reply": ""}
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)
    except Exception as e:
        log.warning("DM draft failed: %s", str(e)[:120])
        return {"category": "other", "should_reply": False, "reply": ""}


def send_dm(recipient_id: str, text: str) -> bool:
    try:
        graph_post(f"{PAGE_ID}/messages",
                   recipient=json.dumps({"id": recipient_id}),
                   message=json.dumps({"text": text}),
                   messaging_type="RESPONSE")
        return True
    except Exception as e:
        log.warning("send_dm failed: %s", str(e)[:140])
        return False


_dm_perm_warned = [False]


def pump_dms(db: sqlite3.Connection, persona: str) -> None:
    """Read recent DMs on the current page; auto-reply the safe ones, flag the rest.
    dm_mode:<page_id> setting = auto (reply safe + flag) | flag (draft/flag only) | off."""
    db.execute("CREATE TABLE IF NOT EXISTS dm_handled (convo_id TEXT, last_msg_id TEXT, "
               "handled_at TEXT, PRIMARY KEY (convo_id, last_msg_id))")
    db.execute("CREATE TABLE IF NOT EXISTS dm_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "page_id TEXT, convo_id TEXT, sender TEXT, incoming TEXT, category TEXT, "
               "action TEXT, reply TEXT, at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    row = db.execute("SELECT value FROM settings WHERE key=?", (f"dm_mode:{PAGE_ID}",)).fetchone()
    mode = (row[0] if row else "auto")
    db.commit()
    if mode == "off":
        return
    try:
        convos = graph_get(f"{PAGE_ID}/conversations",
                           fields="id,unread_count,updated_time,messages.limit(1){id}",
                           limit=10).get("data", [])
    except GraphError as e:
        if getattr(e, "code", 0) in (200, 10, 3) and not _dm_perm_warned[0]:
            log.info("DMs skipped — pages_messaging not granted yet (re-connect via /connect to enable)")
            _dm_perm_warned[0] = True
        return
    except Exception:
        return
    _dm_perm_warned[0] = False
    for c in convos:
        cid = c.get("id")
        last_msg = (c.get("messages", {}).get("data") or [{}])[0].get("id")
        if not cid or not last_msg:
            continue
        if db.execute("SELECT 1 FROM dm_handled WHERE convo_id=? AND last_msg_id=?",
                      (cid, last_msg)).fetchone():
            continue
        try:
            msgs = graph_get(cid, fields="messages.limit(6){id,message,from,created_time}").get(
                "messages", {}).get("data", [])
        except Exception:
            continue
        if not msgs:
            continue
        latest = msgs[0]
        from_id = latest.get("from", {}).get("id")
        now_iso = datetime.now(timezone.utc).isoformat()
        if from_id == PAGE_ID:          # last message is ours — nothing to answer
            db.execute("INSERT OR REPLACE INTO dm_handled VALUES (?,?,?)", (cid, last_msg, now_iso))
            db.commit()
            continue
        sender = latest.get("from", {}).get("name") or "Someone"
        latest_text = (latest.get("message") or "")[:1500]
        history = "\n".join(f"{m.get('from', {}).get('name', '?')}: {(m.get('message') or '')[:200]}"
                            for m in reversed(msgs))
        decision = draft_dm_reply(persona, sender, history, latest_text, db=db)
        cat = decision.get("category", "other"); action = "flagged"; reply = ""
        if (mode == "auto" and decision.get("should_reply") and decision.get("reply")
                and cat in ("greeting", "praise", "question")):
            if send_dm(from_id, decision["reply"]):
                action = "replied"; reply = decision["reply"]
        db.execute("INSERT INTO dm_log (page_id,convo_id,sender,incoming,category,action,reply,at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (PAGE_ID, cid, sender, latest_text[:300], cat, action, reply[:300], now_iso))
        db.execute("INSERT OR REPLACE INTO dm_handled VALUES (?,?,?)", (cid, last_msg, now_iso))
        db.commit()
        log.info("DM [%s] %s from %s -> %s", PAGE_ID, cat, sender, action)


MUSE_API_KEY = os.environ.get("META_MUSE_API_KEY", "")
# Per-request overrides for visitor sessions (set by liveview; never saved). Thread-local, so one
# visitor's key cannot reach another request, and visitor calls are never written to the owner's books.
_req_ctx = _threading.local()



def wav_16k_mono(src: str, dst: str, timeout: int = 180) -> None:
    """Any audio or video file -> 16 kHz mono WAV for speech recognition.
    ffmpeg runs on Windows, macOS and Linux; afconvert is only a macOS fallback when ffmpeg is missing."""
    import shutil
    import subprocess
    ff = shutil.which("ffmpeg")
    if ff:
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", src, "-vn", "-ar", "16000", "-ac", "1",
                        "-c:a", "pcm_s16le", dst], check=True, capture_output=True, timeout=timeout)
        return
    af = shutil.which("afconvert") or ("/usr/bin/afconvert" if sys.platform == "darwin" else None)
    if af and os.path.exists(af):
        subprocess.run([af, src, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", dst],
                       check=True, capture_output=True, timeout=timeout)
        return
    raise RuntimeError("ffmpeg is not installed: install it to transcribe voice")

def muse_key() -> str:
    if getattr(_req_ctx, "visitor", False):
        return getattr(_req_ctx, "muse_key", None) or ""   # a visitor never falls back to the owner's key
    return MUSE_API_KEY
MUSE_API_URL = "https://api.meta.ai/v1/responses"   # effort control lives here
MUSE_TRIAGE_MODEL = "muse-spark-1.3"

TRIAGE_SYSTEM = (
    "You are a fast first-pass comment screener for a Facebook creator page. "
    "Classify the comment. Answer with exactly one word:\n"
    "EASY — short, purely positive praise or reaction with no question and no "
    "substance to answer: emoji-only, \"love this\", \"so cute 😂\", a tag of a "
    "friend's name, simple compliments.\n"
    "ENGAGE — anything with substance: a question, opinion, story, joke with "
    "content, criticism, rudeness, negativity or heated debate — the page "
    "answers these with warmth to turn them around — in ANY language.\n"
    "SKIP — only for the unanswerable: spam/scam/links/self-promo, slurs or "
    "explicit hate speech, sexual harassment, threats, gibberish.\n"
    "If unsure between EASY and ENGAGE, answer ENGAGE. "
    "If unsure about SKIP, answer ENGAGE. One word only."
)

EASY_REPLY_SYSTEM = (
    "You write ONE short, warm reply from the creator's Facebook page to a simple "
    "positive comment. The creator's voice: casual, warm, a touch playful — like texting "
    "a friend back. HARD RULES:\n"
    "- One short sentence, under 120 characters. A light question back is nice "
    "when it fits naturally.\n"
    "- Reply in the commenter's language.\n"
    "- NEVER mention or guess anything about the video's content.\n"
    "- Never use @ mentions. Never mention SEO or algorithms. Never promise "
    "anything (no giveaways, DMs, shoutouts). Never ask for likes, shares or "
    "follows. No holiday/seasonal greetings.\n"
    "- Output ONLY the reply text, nothing else."
)

_EASY_BANNED = ("@", "seo", "algorithm", "giveaway", "dm me", "shoutout",
                "follow me", "share this", "hit like")


def _muse_call(instructions: str, input_text: str, db, kind: str) -> str:
    """One minimal-effort Muse Spark call. Returns text, '' on any problem."""
    if not muse_key():
        return ""
    r = meta_live.post(MUSE_TRIAGE_MODEL, kind,
        MUSE_API_URL,
        headers={"Authorization": f"Bearer {muse_key()}"},
        json={
            "model": MUSE_TRIAGE_MODEL,
            "reasoning": {"effort": "minimal"},
            "max_output_tokens": 500,
            "instructions": instructions,
            "input": input_text,
        },
        timeout=20,
    )
    if r.status_code in (401, 402, 403):
        # bad key or no billing — not transient, stop asking this run
        if not getattr(_req_ctx, "visitor", False):   # a visitor's bad key must not clear the owner's
            globals()["MUSE_API_KEY"] = ""
        log.warning("Muse disabled for this run (HTTP %d — check key/billing "
                    "at dev.meta.ai). Claude handles everything.", r.status_code)
        return ""
    r.raise_for_status()
    u = r.json().get("usage", {})
    record_spend(db, "muse", kind,
                 u.get("input_tokens", 0), u.get("output_tokens", 0),
                 u.get("input_tokens", 0) * MUSE_PRICE["in"]
                 + u.get("output_tokens", 0) * MUSE_PRICE["out"])
    return " ".join(
        c.get("text", "")
        for item in r.json().get("output", [])
        if isinstance(item.get("content"), list)
        for c in item["content"]
        if c.get("type") in ("output_text", "text")
    ).strip()


def muse_triage(post: dict, comment: dict, db=None) -> tuple[str, str]:
    """Cheap first pass. Prefers a Meta OPEN-WEIGHT model (Muse Spark Open, served
    via Baseten/Fireworks/Modal) when one is configured, and falls back to the
    hosted Muse Spark otherwise. Returns (verdict, reason) where verdict is
    ENGAGE / EASY / SKIP. Fails open to ENGAGE (Claude handles it)."""
    input_text = (f"Post caption: {(post.get('message') or '(none)')[:300]}\n"
                  f"Comment: {(comment.get('message') or '')[:500]}")
    try:
        verdict = ""
        if muse_open is not None and muse_open.available():
            verdict = muse_open.spark_open(TRIAGE_SYSTEM, input_text).upper()
        if not verdict:          # open-weight off or silent — use the hosted model
            verdict = _muse_call(TRIAGE_SYSTEM, input_text, db, "triage").upper()
        if verdict.startswith("SKIP"):
            return "SKIP", "muse triage: spam/hate/empty — not engagement-worthy"
        if verdict.startswith("EASY"):
            return "EASY", ""
        return "ENGAGE", ""
    except Exception as e:
        log.warning("Muse triage unavailable (%s) — sending straight to Claude",
                    str(e)[:80])
        return "ENGAGE", ""


MUSE_REVIEW_SYSTEM = (
    "You check ONE Facebook reply that an AI assistant drafted for a creator, before it is posted. "
    "Reply with ONLY a JSON object, no prose: {\"ok\": true or false, \"notes\": \"<one short reason>\"}. "
    "Set ok to false if the draft does not answer the comment, sounds generic, robotic or copy-pasted, "
    "promises a deal, price, date, giveaway or refund, is rude, sarcastic, political, religious, or medical "
    "or financial advice, asks for personal details or sends people to a link, mentions being an AI, a bot "
    "or a reviewer, or runs longer than two sentences. Otherwise set ok to true."
)


def muse_review_reply(comment: dict, reply: str, db=None) -> tuple[bool, str]:
    """Muse Spark reads a Claude-drafted reply before it posts. Returns (ok, note).
    Fails open: if Muse is unavailable or its answer is unreadable, the draft goes out as Claude wrote it."""
    input_text = (f"Comment: {(comment.get('message') or '')[:500]}\n"
                  f"Draft reply: {reply[:600]}")
    try:
        raw = _muse_call(MUSE_REVIEW_SYSTEM, input_text, db, "review") or ""
        a, b = raw.find("{"), raw.rfind("}")
        if a == -1 or b == -1:
            return True, ""
        d = json.loads(raw[a:b + 1])
        return bool(d.get("ok", True)), str(d.get("notes") or "")[:200]
    except Exception as e:
        log.warning("Muse review skipped (%s) - posting the draft as Claude wrote it", str(e)[:80])
        return True, ""


AD_REPLY_NOTE = ("This post is running a paid ad. Never promise prices, discounts, results or delivery dates. "
                "For purchase or pricing questions, say the creator will follow up personally.")


def post_context_for_reply(db, post: dict) -> str:
    """Post context for a reply draft, plus the ad note when the post is running an ad."""
    ctx = get_post_context(db, post)
    try:
        if post.get("id") in get_ad_posts(db):
            ctx += "\n" + AD_REPLY_NOTE
    except Exception:
        pass
    return ctx


def review_claude_reply(persona: str, post: dict, comment: dict, decision: dict, db) -> dict:
    """Muse reviews Claude's draft. OK: post it. Flagged: Claude gets the note and writes ONE revision,
    which is reviewed again. Still flagged: hold it as skipped with the reason. A flagged draft never posts."""
    ok, note = muse_review_reply(comment, decision["reply"], db=db)
    if ok:
        return decision
    log.info("REVIEW %s | Muse flagged the draft: %s", comment.get("id", "?"), note or "off-brand")
    revised = draft_reply(
        persona, post, comment,
        post_context_for_reply(db, post) + "\nReviewer note on the last draft (fix it; never mention a reviewer): "
        + (note or "off-brand"),
        db=db)
    if revised.get("should_reply") and (revised.get("reply") or "").strip():
        ok2, note2 = muse_review_reply(comment, revised["reply"], db=db)
        if ok2:
            revised.setdefault("should_like", decision.get("should_like", False))
            return revised
        note = note2 or note
    return {"should_reply": False, "should_like": decision.get("should_like", False),
            "skip_reason": "held by Muse review: " + (note or "off-brand")}


def muse_easy_reply(comment: dict, db=None) -> str:
    """Let Muse answer a pure-praise comment. Strict output checks; returns ''
    on any doubt so Claude takes over. Only called for EASY-verdict comments."""
    text = (comment.get("message") or "").strip()
    if len(text) > 80 or "?" in text:     # too meaty for the easy lane
        return ""
    try:
        commenter = comment.get("from", {}).get("name") or ""
        reply = _muse_call(
            EASY_REPLY_SYSTEM,
            f"Commenter: {commenter[:60]}\nComment: {text[:200]}",
            db, "reply").strip().strip('"').strip()
        low = reply.lower()
        if not reply or len(reply) > 160:
            return ""
        if any(b in low for b in _EASY_BANNED):
            return ""
        return reply
    except Exception as e:
        log.warning("Muse easy-reply failed (%s) — Claude takes it", str(e)[:80])
        return ""


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def comment_age_ok(comment: dict) -> bool:
    if MAX_COMMENT_AGE_HOURS <= 0:
        return True
    created = comment.get("created_time")
    if not created:
        return True
    ts = datetime.strptime(created, "%Y-%m-%dT%H:%M:%S%z")
    return datetime.now(timezone.utc) - ts <= timedelta(hours=MAX_COMMENT_AGE_HOURS)


def page_still_enabled(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT enabled FROM pages WHERE page_id=?", (PAGE_ID,)).fetchone()
    return bool(row and row[0])


def process_comment(db: sqlite3.Connection, persona: str, post: dict, comment: dict,
                    ignore_age: bool = False) -> None:
    if not page_still_enabled(db):   # pause button takes effect immediately
        raise RateCapReached
    cid = comment["id"]
    author_id = comment.get("from", {}).get("id")

    if already_handled(db, cid):
        return
    if author_id == PAGE_ID:  # the page's own comment
        return
    if comment.get("is_hidden"):
        mark_handled(db, cid, "skipped", "hidden comment")
        return
    if not (comment.get("message") or "").strip():
        mark_handled(db, cid, "skipped", "no text (sticker/gif only)")
        return
    if not ignore_age and not comment_age_ok(comment):
        mark_handled(db, cid, "skipped", "too old")
        return
    if page_already_replied(comment):
        mark_handled(db, cid, "skipped", "page already replied")
        return
    cap = reply_cap(db)
    if replies_in_window(db) >= cap:
        try:   # demand outran the window — arm surge mode for the next one
            db.execute("INSERT OR REPLACE INTO settings VALUES ('demand_hit_at', ?)",
                       (datetime.now(timezone.utc).isoformat(),))
            db.commit()
        except sqlite3.Error:
            pass
        log.warning("Reply cap reached (%d per %.0f min) — deferring remaining comments",
                    cap, REPLY_WINDOW_MINUTES)
        raise RateCapReached
    if throttle_seconds_remaining(db) > 0:
        log.warning("Facebook throttle active (%.0f min left) — deferring",
                    throttle_seconds_remaining(db) / 60)
        raise RateCapReached

    if not claim(db, cid):   # another process (backfill) owns this comment
        return

    snippet = (comment.get("message") or "").replace("\n", " ")[:80]
    try:
        if author_id:   # superfans ledger: remember who engages
            db.execute(
                "INSERT OR IGNORE INTO fans_log VALUES (?, ?, ?, ?, ?)",
                (comment.get("created_time") or datetime.now(timezone.utc).isoformat(),
                 cid, author_id,
                 (comment.get("from", {}) or {}).get("name") or "", post["id"]))
            db.commit()
        verdict, triage_reason = muse_triage(post, comment, db=db)
        if verdict == "SKIP":
            mark_handled(db, cid, "skipped", triage_reason)
            log.info("SKIP   %s | \"%s\" | %s", cid, snippet, triage_reason)
            return

        decision = None
        early_liked = False
        if verdict == "EASY":   # pure praise: like instantly, Muse drafts (cheaper per token)
            if not DRY_RUN:
                try:
                    like_comment(cid)
                    record_like(db, cid)
                    early_liked = True
                    log.info("LIKE   %s | \"%s\"", cid, snippet)
                except GraphError as e:
                    if e.code in THROTTLE_CODES:
                        trip_throttle(db, str(e))
                        release_claim(db, cid)
                        raise RateCapReached
                    log.error("Like failed on %s: %s", cid, e)
            easy = muse_easy_reply(comment, db=db)
            if easy:
                decision = {"should_reply": True, "should_like": False, "reply": easy}
        from_claude = decision is None
        if decision is None:
            decision = draft_reply(persona, post, comment, post_context_for_reply(db, post), db=db)
        if from_claude and decision.get("should_reply") and (decision.get("reply") or "").strip():
            decision = review_claude_reply(persona, post, comment, decision, db)

        if decision.get("should_like") and not DRY_RUN:
            try:
                like_comment(cid)
                record_like(db, cid)
                log.info("LIKE   %s | \"%s\"", cid, snippet)
            except GraphError as e:
                if e.code in THROTTLE_CODES:
                    trip_throttle(db, str(e))
                    release_claim(db, cid)
                    raise RateCapReached
                log.error("Like failed on %s: %s", cid, e)
        elif decision.get("should_like"):
            log.info("DRYLIKE %s | \"%s\"", cid, snippet)

        if not decision.get("should_reply"):
            reason = decision.get("skip_reason") or "model chose to skip"
            log.info("SKIP   %s | \"%s\" | %s", cid, snippet, reason)
            mark_handled(db, cid, "skipped", reason)
            return

        reply = decision["reply"].strip()
        if DRY_RUN:
            log.info("DRYRUN %s | \"%s\" -> \"%s\"", cid, snippet, reply)
            mark_handled(db, cid, "dry_run", reply)
            return

        page_id = str(post.get("id", "")).split("_")[0]
        if reply_needs_approval(db, page_id):
            queue_reply(db, cid, page_id, snippet, reply)
            log.info("QUEUE  %s | \"%s\" -> \"%s\" (waiting for approval)", cid, snippet, reply)
            mark_handled(db, cid, "queued", reply)
            return

        # belt-and-braces: ask Facebook itself right before posting
        if page_reply_count(cid) > 0:
            log.info("SKIP   %s | \"%s\" | page already replied (fresh check)", cid, snippet)
            mark_handled(db, cid, "skipped", "page already replied (fresh check)")
            return

        try:
            post_reply(cid, reply)
        except GraphError as e:
            if e.code in THROTTLE_CODES:
                trip_throttle(db, str(e))
                release_claim(db, cid)
                raise RateCapReached
            raise
        note_post_success(db)
        log.info("REPLY  %s | \"%s\" -> \"%s\"", cid, snippet, reply)
        mark_handled(db, cid, "replied", reply)
    except RateCapReached:
        raise
    except Exception:
        release_claim(db, cid)   # free it so a later cycle can retry
        raise


class RateCapReached(Exception):
    pass


GOLDEN_HOUR_MINUTES = int(os.environ.get("GOLDEN_HOUR_MINUTES", "90"))


def _post_age_minutes(post: dict) -> float:
    try:
        ts = datetime.strptime(post.get("created_time", ""), "%Y-%m-%dT%H:%M:%S%z")
        return (datetime.now(timezone.utc) - ts).total_seconds() / 60
    except Exception:
        return 1e9


_last_golden = [0.0]


def maybe_golden(db: sqlite3.Connection, persona: str) -> None:
    """Run the golden pass at most every 60s — callable mid-sweep so a fresh
    post never waits behind a long backlog crawl. Restores page context."""
    if time.monotonic() - _last_golden[0] < 60:
        return
    _last_golden[0] = time.monotonic()
    cur = (PAGE_ID, PAGE_TOKEN)
    try:
        golden_pass(db, persona)
    finally:
        globals()["PAGE_ID"], globals()["PAGE_TOKEN"] = cur


SEED_SUFFIX = "Thank you for watching & follow for more content ❤️"


def seed_first_comment(db: sqlite3.Connection, post: dict) -> None:
    """The creator's rule: each new post gets a first comment from the page when the bot sees it in its first 90 minutes — an
    open-ended question about the video + their thank-you/follow sign-off."""
    pid = post["id"]
    db.execute("CREATE TABLE IF NOT EXISTS seeded (post_id TEXT PRIMARY KEY, at TEXT)")
    try:   # insert-first = atomic claim so we never double-seed
        db.execute("INSERT INTO seeded VALUES (?, ?)",
                   (pid, datetime.now(timezone.utc).isoformat()))
        db.commit()
    except sqlite3.IntegrityError:
        return
    ctx = get_post_context(db, post)
    caption = (post.get("message") or "")[:500]
    q = ""
    try:
        resp = claude.messages.create(
            model=MODEL, max_tokens=100,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content":
                "Write ONE casual, open-ended question (under 120 characters) that "
                + CREATOR_REF + " would post as the first comment under their own "
                "Facebook video to get viewers answering. Base it ONLY on:\n"
                f"Caption: {caption or '(none)'}\n"
                f"What the video shows: {ctx or 'unknown - ask about the caption topic only'}\n"
                "Their voice: warm, playful, like texting friends. No hashtags, no emoji "
                "spam (one emoji max). Output ONLY the question."}])
        record_claude_usage(db, "seed", resp.usage)
        q = next((b.text for b in resp.content if b.type == "text"), "").strip().strip('"')
    except Exception as e:
        log.error("seed question failed for %s: %s", pid, str(e)[:80])
    if not q or len(q) > 200:
        q = "What did you think of this one?"
    msg = f"{q} {SEED_SUFFIX}"
    if DRY_RUN:
        log.info("DRYSEED %s | \"%s\"", pid, msg)
        return
    try:
        post_reply(pid, msg)   # paced, serialized write like every other
        log.info("SEEDED first comment on %s -> \"%s\"", pid, msg[:100])
    except GraphError as e:
        if e.code in THROTTLE_CODES:
            trip_throttle(db, str(e))
        log.error("seed failed on %s: %s", pid, e)
    except Exception as e:
        log.error("seed failed on %s: %s", pid, str(e)[:100])


def golden_pass(db: sqlite3.Connection, persona: str) -> None:
    """First-hour boost: before each slow full sweep, hit every page's
    newest posts so early commenters get near-instant replies — the
    early engagement, which we expect to help distribution (not measured yet)."""
    if replies_in_window(db) >= reply_cap(db):
        return
    for pid, pname, ptoken in load_pages(db):
        globals()["PAGE_ID"], globals()["PAGE_TOKEN"] = pid, ptoken
        try:
            data = graph_get(
                f"{pid}/published_posts",
                fields="id,message,created_time,permalink_url,full_picture,"
                       "attachments{title,description,type},"
                       "comments.summary(true).limit(0)",
                limit=5,
            )
        except Exception as e:
            log.error("[%s] golden pass fetch failed: %s", pname, str(e)[:100])
            continue
        for post in data.get("data", []):
            age = _post_age_minutes(post)
            if age > GOLDEN_HOUR_MINUTES:
                continue
            try:
                seed_first_comment(db, post)
            except Exception as e:
                log.error("seed error on %s: %s", post.get("id"), str(e)[:80])
            if post_comment_total(post) == 0:
                continue
            if _golden_last_total.get(post["id"]) == post_comment_total(post):
                continue   # no new comments since the last boost pass for this post
            _golden_last_total[post["id"]] = post_comment_total(post)
            if handled_count_for_post(db, post["id"]) >= post_comment_total(post):
                continue
            log.info("[%s] GOLDEN HOUR: fresh post (%d min old) — boosting now",
                     pname, int(age))
            try:
                for comment in fetch_comments(post["id"]):
                    process_comment(db, persona, post, comment, ignore_age=True)
            except RateCapReached:
                return   # window full — golden posts get first claim next window
            except Exception as e:
                log.error("[%s] golden pass error: %s", pname, str(e)[:100])


_golden_last_total: dict = {}   # post id -> comment count at its last golden pass


_earners_cache = {"at": 0.0, "ids": set()}


def _top_earner_ids() -> set:
    """Post ids of the money grove's top earners (dashboard /trees), cached 10 min."""
    now = time.monotonic()
    if now - _earners_cache["at"] > 600:
        try:
            r = requests.get(f"http://localhost:{os.environ.get('ABRA_PORT') or '8765'}/trees", timeout=25)
            _earners_cache["ids"] = {q["id"] for q in r.json() if q.get("id")}
        except Exception:
            pass   # keep last known set; retry next window
        _earners_cache["at"] = now
    return _earners_cache["ids"]


PARALLEL_WORKERS = int(os.environ.get("PARALLEL_WORKERS", "3"))
_tlocal = _threading.local()


def _thread_db() -> sqlite3.Connection:
    if not hasattr(_tlocal, "db"):
        _tlocal.db = init_db()
    return _tlocal.db


def run_cycle(db: sqlite3.Connection, persona: str, page_name: str = "") -> None:
    posts = fetch_recent_posts()
    # only this page's targeted posts (ids look like "{page_id}_{post_id}")
    targets = {t for t in (get_targets(db) + get_ad_posts(db)) if t.startswith(PAGE_ID + "_") or "_" not in t}

    # make sure every targeted post is in the work list, even if it's older
    # than the recent-posts window
    known = {p["id"] for p in posts}
    for tid in targets - known:
        try:
            posts.append(graph_get(tid, fields="id,message,created_time,permalink_url"))
        except RuntimeError as e:
            log.error("Could not fetch targeted post %s: %s", tid, e)

    # The creator's priority ladder: (1) posts from the past 24h, (2) the money
    # grove's top earners, (3) targeted posts, (4) everything else
    earners = _top_earner_ids()
    posts.sort(key=lambda p: (_post_age_minutes(p) > 1440,
                              p["id"] not in earners,
                              p["id"] not in targets))
    if targets:
        log.info("[%s] Checking %d posts (%d targeted for priority engagement)...",
                 page_name or PAGE_ID, len(posts), len(targets & {p['id'] for p in posts}))
    else:
        log.info("[%s] Checking %d recent posts...", page_name or PAGE_ID, len(posts))

    swept = 0
    for post in posts:
        maybe_golden(db, persona)   # fresh posts jump the queue, even mid-sweep
        total = post_comment_total(post)
        if total == 0:
            continue
        # cheap skip: everything under this post is already dealt with
        if handled_count_for_post(db, post["id"]) >= total:
            swept += 1
            continue
        try:
            comments = fetch_comments(post["id"])
        except RuntimeError as e:
            log.error("Could not fetch comments for %s: %s", post["id"], e)
            continue
        # warm the vision-context cache once so parallel workers don't race it
        try:
            get_post_context(db, post)
        except Exception:
            pass
        capped = False

        def _one(comment):
            try:
                process_comment(_thread_db(), persona, post, comment, ignore_age=True)
                return None
            except RateCapReached:
                return "cap"
            except anthropic.APIStatusError as e:
                log.error("Claude API error (%s) on comment %s — will retry next cycle",
                          e.status_code, comment["id"])
            except RuntimeError as e:
                log.error("Facebook error on comment %s: %s", comment["id"], e)
            except Exception as e:
                log.error("worker error on comment %s: %s", comment["id"], str(e)[:120])
            return None

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            for outcome in pool.map(_one, comments):
                if outcome == "cap":
                    capped = True
        if capped:
            log.info("[%s] paused for pacing — %d posts already fully swept",
                     page_name or PAGE_ID, swept)
            return
    log.info("[%s] full sweep complete — %d/%d posts had nothing left to answer",
             page_name or PAGE_ID, swept, len(posts))


def validate_setup() -> None:
    missing = []
    if not PRIMARY_PAGE_ID:
        missing.append("FB_PAGE_ID")
    if not PRIMARY_PAGE_TOKEN:
        missing.append("FB_PAGE_ACCESS_TOKEN")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        sys.exit(f"Missing required settings in .env: {', '.join(missing)}. "
                 f"Copy .env.example to .env and fill them in (see README.md).")
    if not PERSONA_PATH.exists():
        sys.exit("No persona file found. Copy persona.example.md to persona.md and make it yours.")

    # Verify the token actually belongs to this page
    try:
        me = graph_get("me", fields="id,name")
    except GraphError as e:
        sys.exit(f"Facebook rejected FB_PAGE_ACCESS_TOKEN ({str(e)[:160]}). Re-run setup_token.py to get a fresh Page token.")
    if me["id"] != PRIMARY_PAGE_ID:
        sys.exit(
            f"Token mismatch: FB_PAGE_ACCESS_TOKEN belongs to '{me.get('name')}' "
            f"(id {me['id']}), but FB_PAGE_ID is {PRIMARY_PAGE_ID}. Use a PAGE access "
            f"token for that page, not a user token."
        )
    log.info("Authenticated as page: %s (%s)", me.get("name"), me["id"])


LOG_PATH = BASE_DIR / "bot.log"
MAX_LOG_BYTES = 100 * 1024 * 1024   # trim when past 100MB, keep last 5MB
LOG_KEEP_BYTES = 5 * 1024 * 1024


def trim_log() -> None:
    """launchd appends to bot.log forever; keep it from growing unbounded.

    launchd opens StandardOutPath with O_APPEND, so rewriting the file in
    place is safe — subsequent writes land at the new end of file.
    """
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            with open(LOG_PATH, "rb") as fh:
                fh.seek(-LOG_KEEP_BYTES, 2)
                tail = fh.read()
            with open(LOG_PATH, "wb") as fh:
                fh.write(b"[older log trimmed]\n" + tail)
            log.info("bot.log trimmed to last %dMB", LOG_KEEP_BYTES // (1024 * 1024))
    except Exception as e:
        log.error("log trim failed: %s", e)


def _heartbeat() -> None:
    """Writes the time of each poll. The dashboard reads it on systems without launchd (Windows, Linux)."""
    try:
        (Path(__file__).resolve().parent / ".bot_heartbeat").write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    validate_setup()
    db = init_db()
    persona = load_persona()

    if DRY_RUN:
        log.info("DRY RUN mode — comment replies, likes and first comments are printed, not posted. "
                 "Set DRY_RUN=false in .env to go live.")
    log.info("Polling every %ds. Press Ctrl+C to stop.", POLL_INTERVAL)

    while True:
        _heartbeat()
        trim_log()
        try:
            pump_story_queue(db)
        except Exception as e:
            log.error("story queue pump failed: %s", e)
        try:
            _last_golden[0] = time.monotonic()
            golden_pass(db, persona)
        except RateCapReached:
            pass
        except Exception as e:
            log.error("golden pass failed: %s", e)
        for pid, pname, ptoken in load_pages(db):
            globals()["PAGE_ID"], globals()["PAGE_TOKEN"] = pid, ptoken
            try:
                run_cycle(db, persona, pname)
            except Exception as e:  # keep the daemon alive through transient failures
                log.error("Cycle failed on %s: %s", pname, e)
            try:
                pump_dms(db, persona)
            except Exception as e:
                log.error("DM pump failed on %s: %s", pname, e)
        globals()["PAGE_ID"], globals()["PAGE_TOKEN"] = PRIMARY_PAGE_ID, PRIMARY_PAGE_TOKEN
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
