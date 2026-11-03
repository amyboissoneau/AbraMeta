"""Publish/schedule a reviewed content draft to Facebook as a scheduled Reel.

Uses the resumable Reels API (start -> upload bytes -> finish with
video_state=SCHEDULED). The engagement bot seeds a first comment on new posts it sees within their first 90 minutes, and it likes and replies once a post is live, so nothing extra is needed while the bot is running.

SAFETY: dry_run=True (the default here for tests) performs NO writes to
Facebook — it only validates and returns the plan. A real publish happens when
the creator approves a draft (review mode) or opts a page into auto mode.
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone

import requests

import bot as B

RUPLOAD = "https://rupload.facebook.com/video-upload"
MIN_LEAD_SECONDS = 15 * 60          # FB requires scheduled time ~10+ min out
MAX_LEAD_SECONDS = 70 * 24 * 3600   # ~75 day ceiling


def compose_caption(title: str, description: str, hashtags) -> str:
    parts = []
    if title:
        parts.append(title.strip())
    if description:
        parts.append(description.strip())
    tags = hashtags if isinstance(hashtags, list) else json.loads(hashtags or "[]")
    if tags:
        parts.append(" ".join(t if t.startswith("#") else "#" + t for t in tags))
    return "\n\n".join(parts)[:2200]


def _clamp_schedule(slot_iso: str) -> int:
    """ISO slot -> unix seconds, clamped into FB's allowed scheduling window."""
    now = int(time.time())
    try:
        ts = int(datetime.fromisoformat(slot_iso).timestamp())
    except Exception:
        ts = now + MIN_LEAD_SECONDS
    return max(now + MIN_LEAD_SECONDS, min(ts, now + MAX_LEAD_SECONDS))


def _set_cover(graph: str, video_id: str, token: str, thumb_path: str) -> bool:
    """Best-effort custom cover/thumbnail for the reel. Non-fatal if unsupported."""
    if not thumb_path or not os.path.exists(thumb_path):
        return False
    try:
        with open(thumb_path, "rb") as fh:
            r = requests.post(f"{graph}/{video_id}/thumbnails",
                              data={"is_preferred": "true", "access_token": token},
                              files={"source": fh}, timeout=60).json()
        return not r.get("error")
    except Exception:
        return False


def publish_reel(page_id: str, token: str, video_path: str, caption: str,
                 slot_iso: str, api_version: str = "v23.0", dry_run: bool = True,
                 thumb_path: str = "") -> dict:
    """Schedule a Reel. Returns {ok, video_id?, scheduled_for, plan|error}."""
    when = _clamp_schedule(slot_iso)
    plan = {"edge": f"{page_id}/video_reels", "video_state": "SCHEDULED",
            "scheduled_publish_time": when,
            "scheduled_for": datetime.fromtimestamp(when, tz=timezone.utc).isoformat(),
            "caption_chars": len(caption), "bytes": os.path.getsize(video_path) if os.path.exists(video_path) else 0}
    if dry_run:
        return {"ok": True, "dry_run": True, "plan": plan}

    graph = f"https://graph.facebook.com/{api_version}"
    try:
        # phase 1: start -> upload session
        start = requests.post(f"{graph}/{page_id}/video_reels",
                              data={"upload_phase": "start", "access_token": token}, timeout=30).json()
        vid = start.get("video_id")
        if not vid:
            return {"ok": False, "error": f"start failed: {start.get('error', start)}"}
        # phase 2: upload the bytes
        size = os.path.getsize(video_path)
        with open(video_path, "rb") as f:
            up = requests.post(f"{RUPLOAD}/{api_version}/{vid}",
                               headers={"Authorization": f"OAuth {token}",
                                        "offset": "0", "file_size": str(size)},
                               data=f.read(), timeout=600).json()
        if not up.get("success", True) and up.get("error"):
            return {"ok": False, "error": f"upload failed: {up.get('error')}"}
        # phase 3: finish -> schedule
        fin = requests.post(f"{graph}/{page_id}/video_reels",
                            data={"upload_phase": "finish", "video_id": vid,
                                  "video_state": "SCHEDULED", "scheduled_publish_time": when,
                                  "description": caption, "access_token": token}, timeout=60).json()
        if fin.get("success") or fin.get("post_id") or fin.get("id"):
            cover = _set_cover(graph, vid, token, thumb_path)
            return {"ok": True, "video_id": vid, "scheduled_for": plan["scheduled_for"],
                    "cover_set": cover, "finish": fin}
        return {"ok": False, "error": f"finish failed: {fin.get('error', fin)}", "video_id": vid}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


if __name__ == "__main__":  # dry-run only: validate plan construction, no FB writes
    import tempfile, subprocess
    with tempfile.TemporaryDirectory() as td:
        clip = os.path.join(td, "t.mp4")
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=24:duration=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", clip], check=True, capture_output=True)
        cap = compose_caption("What If This Is Our Future?",
                              "An 1878 Bible held a secret no one expected.", ["history", "#fyp"])
        print("caption:\n" + cap + "\n")
        res = publish_reel("example-page-id", "FAKE", clip, cap,
                           "2026-10-27T19:00:00-04:00", dry_run=True)
        print("dry-run plan:", json.dumps(res, indent=2))
