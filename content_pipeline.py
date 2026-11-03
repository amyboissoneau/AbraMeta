"""Content Scheduler pipeline: a dropped video -> a draft 'content card'.

Muse HEARS it (Muse Voice Transcribe), Claude SEES it (frames and vision), then writes
draft copy from the page's past top posts (Muse Spark via content_brain) and proposes a best-time,
conflict-free slot. Also picks a thumbnail frame. Publishes NOTHING — it returns
a draft the creator reviews (or that auto-publish later consumes).
"""
from __future__ import annotations
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

import requests

import meta_live

import bot as B
import content_brain as brain

TRANSCRIBE_URL = "https://api.meta.ai/v1/asr/transcribe"
DRAFTS_DIR = os.path.join(os.path.dirname(__file__), "assets", "drafts")


def _bin(name: str) -> str:
    """Absolute path to a CLI tool — launchd's PATH omits /usr/local/bin."""
    import shutil
    p = shutil.which(name)
    if p:
        return p
    for cand in (f"/usr/local/bin/{name}", f"/opt/homebrew/bin/{name}", f"/usr/bin/{name}"):
        if os.path.exists(cand):
            return cand
    return name


FFMPEG = _bin("ffmpeg")
FFPROBE = _bin("ffprobe")


def _ffprobe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip() or 0)
    except Exception:
        return 0.0


def transcribe_file(path: str, db) -> tuple[str, int]:
    """Muse Voice Transcribe on a local video file. ('' , 0) if unavailable."""
    if not B.muse_key():
        return "", 0
    try:
        with tempfile.TemporaryDirectory() as td:
            wav = os.path.join(td, "a.wav")
            B.wav_16k_mono(path, wav, timeout=180)
            with open(wav, "rb") as f:
                resp = meta_live.post("muse-voice-transcribe-1.0", "transcribe a video",
                    TRANSCRIBE_URL,
                    headers={"Authorization": f"Bearer {B.muse_key()}", "Accept": "application/json"},
                    files={"request": (None, json.dumps(
                                {"model": "muse-voice-transcribe-1.0",
                                 "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}), "application/json"),
                           "audio": ("a.wav", f, "audio/wav")},
                    timeout=180)
            d = resp.json()
            transcript = (d.get("transcript") or "").strip()[:2500]
            dur = int(d.get("audioDurationMs") or 0)
            try:
                B.record_spend(db, "muse", "schedule_transcribe", 0, 0, dur / 3600000.0 * 0.18)
            except Exception:
                pass
            return transcript, dur
    except Exception as e:
        B.log.warning("schedule transcribe failed: %s", str(e)[:120])
        return "", 0


def extract_frames(path: str, out_dir: str, n: int = 6) -> list[str]:
    """Evenly sample n frames across the video; return the saved PNG paths."""
    dur = _ffprobe_duration(path) or 1.0
    paths = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        fp = os.path.join(out_dir, f"frame_{i}.png")
        try:
            subprocess.run([FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", path,
                            "-frames:v", "1", "-q:v", "3", fp],
                           check=True, capture_output=True, timeout=60)
            if os.path.exists(fp):
                paths.append(fp)
        except Exception:
            pass
    return paths


def _frame_score(path: str) -> float:
    """Favor bright, sharp, colorful frames (good thumbnails), penalize near-black."""
    try:
        from PIL import Image, ImageFilter, ImageStat
        im = Image.open(path).convert("RGB")
        bright = sum(ImageStat.Stat(im).mean) / 3.0
        if bright < 28:            # near-black frame — bad thumbnail
            return 0.0
        sharp = ImageStat.Stat(im.convert("L").filter(ImageFilter.FIND_EDGES)).stddev[0]
        colorful = sum(ImageStat.Stat(im).stddev) / 3.0
        bright_bonus = 1.0 - abs(bright - 135) / 135.0   # peak around mid-bright
        return sharp * 1.0 + colorful * 0.6 + bright_bonus * 20
    except Exception:
        return 0.0


def pick_thumbnail(frames: list[str], dest: str) -> str | None:
    """Choose the most thumbnail-worthy frame, copy it to `dest`."""
    if not frames:
        return None
    best = max(frames, key=_frame_score)
    try:
        from PIL import Image
        Image.open(best).convert("RGB").save(dest, "JPEG", quality=88)
        return dest
    except Exception:
        return None


def top_thumbnails(frames: list[str], stamp: str, k: int = 3) -> list[str]:
    """The k most cover-worthy real frames, saved as drafts (best first). Returns file names."""
    from PIL import Image
    ranked = sorted(frames, key=_frame_score, reverse=True)
    out = []
    for i, src in enumerate(ranked[:k], 1):
        if _frame_score(src) <= 0:
            continue
        name = f"thumb_{stamp}_{i}.jpg"
        try:
            Image.open(src).convert("RGB").save(os.path.join(DRAFTS_DIR, name), "JPEG", quality=88)
            out.append(name)
        except Exception:
            pass
    return out


def describe_frame(frame_path: str) -> str:
    """Claude vision: 2-3 sentence read of what the video shows (for copy grounding)."""
    try:
        import base64
        with open(frame_path, "rb") as f:
            img = f.read()
        if len(img) > 4_000_000:
            return ""
        resp = B.claude.messages.create(
            model=B.MODEL, max_tokens=220,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                 "data": base64.standard_b64encode(img).decode()}},
                {"type": "text", "text": "This is one frame from a short vertical video a "
                 "creator wants to post. In 2-3 sentences, describe what it shows and the "
                 "vibe/topic, for writing a caption. Only describe what you can see."}]}])
        try:
            B.record_claude_usage(resp.usage) if False else None
        except Exception:
            pass
        return next((b.text for b in resp.content if b.type == "text"), "").strip()
    except Exception as e:
        B.log.warning("describe_frame failed: %s", str(e)[:100])
        return ""


def analyze(path: str, page_id: str, db, taken_times=None) -> dict:
    """Full pipeline on a local video -> draft content card (publishes nothing)."""
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "_" + os.urandom(4).hex()
    thumb_dest = os.path.join(DRAFTS_DIR, f"thumb_{stamp}.jpg")

    transcript, dur_ms = transcribe_file(path, db)
    content_desc, thumb = "", None
    options = []
    with tempfile.TemporaryDirectory() as td:
        frames = extract_frames(path, td, n=6)
        if frames:
            thumb = pick_thumbnail(frames, thumb_dest)
            options = top_thumbnails(frames, stamp, k=3)
            best = max(frames, key=_frame_score)
            content_desc = describe_frame(best)

    copy = brain.draft_copy(B._muse_call, db, transcript, content_desc, page_id) or {}
    slots = brain.propose_slots(db, page_id, taken_times=taken_times or [], count=6)
    slot_options = [s.isoformat() for s in slots]
    slot = slot_options[0] if slot_options else None

    previews = story_previews(path, (copy.get("titles") or [""])[0], DRAFTS_DIR, stamp)

    return {
        "transcript": transcript,
        "duration_ms": dur_ms,
        "content_desc": content_desc,
        "titles": copy.get("titles", []),
        "description": copy.get("description", ""),
        "hashtags": copy.get("hashtags", []),
        "thumbnail": os.path.basename(thumb) if thumb else None,
        "thumbnail_options": options,
        "proposed_slot": slot,
        "slot_options": slot_options,
        "best_slots": brain.best_slots(db, page_id),
        "story_previews": previews,
    }


def story_previews(video_path: str, clickbait: str, dest_dir: str, stamp: str) -> dict:
    """Small preview slides for each Story template so the user can see them before
    choosing. Returns {'chopped': [filenames], 'teaser_hook': filename or None, 'teaser_styles': [filenames]}."""
    from PIL import Image
    os.makedirs(dest_dir, exist_ok=True)
    dur = _ffprobe_duration(video_path) or 1.0
    out = {"chopped": [], "teaser_hook": None, "teaser_styles": []}

    def _frame(td, pct):
        fp = os.path.join(td, f"pf_{int(pct*100)}.png")
        subprocess.run([FFMPEG, "-y", "-ss", f"{dur*pct:.2f}", "-i", video_path,
                        "-frames:v", "1", "-q:v", "3", fp], check=True, capture_output=True, timeout=60)
        return fp

    def _small(src, name):
        im = _cover_vertical(Image.open(src), 360, 640)
        im.save(os.path.join(dest_dir, name), "JPEG", quality=82)
        return name

    with tempfile.TemporaryDirectory() as td:
        # chopped: one frame from each third (shows it plays through as clips)
        for i, pct in enumerate((0.16, 0.5, 0.84)):
            try:
                out["chopped"].append(_small(_frame(td, pct), f"sp_chop_{stamp}_{i}.jpg"))
            except Exception:
                pass
        # teaser: the hook frame + a Watch-now slide for each of the 3 styles
        try:
            out["teaser_hook"] = _small(_frame(td, 0.08), f"sp_teaser_hook_{stamp}.jpg")
        except Exception:
            pass
        try:
            fr = _frame(td, 0.8)
            for s in (1, 2, 3):
                big = os.path.join(td, f"cta{s}.jpg")
                if _clickbait_frame(fr, clickbait, big, style=s):
                    name = f"sp_teaser_cta{s}_{stamp}.jpg"
                    Image.open(big).resize((360, 640), Image.LANCZOS).save(
                        os.path.join(dest_dir, name), "JPEG", quality=82)
                    out["teaser_styles"].append(name)
        except Exception:
            pass
    return out


STORY_DIR = os.path.join(os.path.dirname(__file__), "story_queue")
_FONTS = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
          "/System/Library/Fonts/Supplemental/Arial.ttf",
          "/System/Library/Fonts/Helvetica.ttc",
          "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
          "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]

# ---- Story FX: topic-aware Muse overlays + cinematic grade ----
_FX_THEMES = [
    (("water", "ocean", "wave", "tsunami", "sea", "flood", "rain", "river", "beach", "surf", "storm"),
     "heavy driving rain streaks and white water splash particles and mist, bright white and pale blue, "
     "diagonal motion blur, on a solid pure black background, cinematic, no text, no logos, full frame"),
    (("fire", "lava", "volcano", "flame", "burn", "ember", "explosion", "blaze", "inferno"),
     "glowing orange embers, sparks and rising smoke, fiery particles, on a solid pure black background, "
     "cinematic, no text, no logos, full frame"),
    (("space", "star", "galaxy", "planet", "cosmic", "nebula", "universe", "astronaut", "meteor", "sky"),
     "drifting glowing stars, sparkling light particles and soft nebula haze, white and cyan, on a solid "
     "pure black background, cinematic, no text, no logos, full frame"),
    (("snow", "ice", "blizzard", "winter", "frost", "glacier", "arctic"),
     "falling snowflakes and frost particles with soft white bokeh, on a solid pure black background, "
     "cinematic, no text, no logos, full frame"),
    (("desert", "sand", "dust", "dune", "sandstorm"),
     "blowing sand and dust particles with warm tan haze and motion blur, on a solid pure black background, "
     "cinematic, no text, no logos, full frame"),
]
_FX_DEFAULT = ("floating glowing light particles, soft sparkles and bokeh, white and silver, on a solid pure "
               "black background, cinematic, no text, no logos, full frame")


def fx_theme(text: str) -> str:
    """Pick a Muse overlay prompt matched to the video's topic."""
    t = (text or "").lower()
    for kws, prompt in _FX_THEMES:
        if any(k in t for k in kws):
            return prompt
    return _FX_DEFAULT


_FX_NAMED = {
    "rain": _FX_THEMES[0][1],
    "embers": _FX_THEMES[1][1],
    "stars": _FX_THEMES[2][1],
    "snow": _FX_THEMES[3][1],
    "sparkle": _FX_DEFAULT,
}


def fx_prompt(name: str, text: str = "") -> str:
    """Overlay prompt for a chosen effect name; 'auto'/unknown falls back to topic-matched fx_theme."""
    n = (name or "").strip().lower()
    if n in _FX_NAMED:
        return _FX_NAMED[n]
    return fx_theme(text)


def generate_overlay(prompt: str, dest: str, db=None) -> str | None:
    """Asks Muse Image (muse-image-1.0) for a themed effect overlay on black, for screen-compositing. One live test call returned an image (Nov 2, 2026); the full Story overlay is not yet tested end to end."""
    if not B.muse_key():
        return None
    try:
        import base64 as _b64, io as _io
        import requests as _rq
        from PIL import Image
        r = meta_live.post("muse-image-1.0", "Story FX overlay", "https://api.meta.ai/v1/images/generations",
                     headers={"Authorization": f"Bearer {B.muse_key()}"},
                     json={"model": "muse-image-1.0", "prompt": prompt}, timeout=120)
        im = Image.open(_io.BytesIO(_b64.b64decode(r.json()["data"][0]["b64_json"]))).convert("RGB")
        _cover_vertical(im, 1080, 1920).save(dest, "JPEG", quality=85)
        try:
            if db is not None:
                B.record_spend(db, "muse", "story_fx_overlay", 0, 0, 0.01)
        except Exception:
            pass
        return dest
    except Exception as e:
        B.log.warning("story FX overlay gen failed: %s", str(e)[:120])
        return None


def _apply_fx_image(im, overlay_path=None):
    """Cinematic grade (contrast/saturation/cool lift/vignette) + screen-composited overlay."""
    from PIL import Image, ImageEnhance, ImageChops, ImageDraw, ImageFilter
    im = im.convert("RGB"); W, H = im.size
    g = ImageEnhance.Contrast(im).enhance(1.15)
    g = ImageEnhance.Color(g).enhance(1.26)
    cool = Image.new("RGB", (W, H), (16, 34, 58))
    g = Image.blend(g, ImageChops.screen(g, cool), 0.16)
    m = Image.new("L", (W, H), 0); ImageDraw.Draw(m).ellipse([-W * 0.25, -H * 0.08, W * 1.25, H * 1.08], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(220))
    g = Image.composite(g, Image.blend(g, Image.new("RGB", (W, H), (0, 0, 0)), 0.40), m)
    if overlay_path and os.path.exists(overlay_path):
        ov = _cover_vertical(Image.open(overlay_path), W, H)
        g = Image.blend(g, ImageChops.screen(g, ov), 0.52)
    return g


def _font() -> str:
    for f in _FONTS:
        if os.path.exists(f):
            return f
    return ""


def _pil_font(size: int):
    from PIL import ImageFont
    f = _font()
    try:
        return ImageFont.truetype(f, size) if f else ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default(size)


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _cover_vertical(im, W=1080, H=1920):
    from PIL import Image
    im = im.convert("RGB")
    scale = max(W / im.width, H / im.height)
    im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
    x = (im.width - W) // 2
    y = (im.height - H) // 2
    return im.crop((x, y, x + W, y + H))


_CHROME = [(0.0, (142, 153, 172)), (0.26, (198, 208, 222)), (0.48, (255, 255, 255)),
           (0.55, (243, 246, 251)), (0.72, (170, 181, 198)), (1.0, (122, 134, 153))]


def _lerp_stops(stops, t):
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]; t1, c1 = stops[i + 1]
        if t0 <= t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 0
            return tuple(int(c0[j] + (c1[j] - c0[j]) * f) for j in range(3))
    return stops[-1][1]


def _vgrad(w, h, stops):
    from PIL import Image
    h = max(1, int(h)); w = max(1, int(w))
    col = Image.new("RGB", (1, h)); p = col.load()
    for y in range(h):
        p[0, y] = _lerp_stops(stops, y / (h - 1) if h > 1 else 0)
    return col.resize((w, h))


def _paste_chrome(im, box, radius, stops=_CHROME, circle=False):
    """Fill a rounded-rect (or circle) region of `im` with a vertical chrome gradient."""
    from PIL import Image, ImageDraw
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    grad = _vgrad(w, h, stops)
    mask = Image.new("L", (w, h), 0); md = ImageDraw.Draw(mask)
    if circle:
        md.ellipse([0, 0, w - 1, h - 1], fill=255)
    else:
        md.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    im.paste(grad, (x0, y0), mask)


def _clickbait_frame(frame_path: str, title: str, out_path: str, style: int = 1,
                     overlay_path=None, reaction_path=None, reaction_xy=None) -> bool:
    """Paused frame + eye-catching clickbait title + a 'Watch now' CTA, rendered with
    PIL (this ffmpeg has no drawtext). style 1=Pop button, 2=Player, 3=Banner.
    overlay_path adds the cinematic grade + Muse FX overlay under the text."""
    try:
        from PIL import Image, ImageDraw
        base = _cover_vertical(Image.open(frame_path), 1080, 1920)
        if overlay_path and os.path.exists(overlay_path):
            base = _apply_fx_image(base, overlay_path)       # graded + storm overlay
            darkness = {1: 0.26, 2: 0.40, 3: 0.24}.get(style, 0.26)
        else:
            darkness = {1: 0.46, 2: 0.56, 3: 0.44}.get(style, 0.46)
        im = Image.blend(base, Image.new("RGB", base.size, (12, 14, 20)), darkness)  # toward obsidian
        W, H = im.size
        d = ImageDraw.Draw(im)
        ttl = (title or "You won't believe this").upper()
        INK, WHITE, STEEL = (36, 41, 52), (255, 255, 255), (122, 134, 153)

        def title_block(cy, fs):
            big = _pil_font(fs)
            lines = _wrap(d, ttl, big, W - 130)
            lh = int(fs * 1.14)
            y = cy - (lh * len(lines)) // 2
            for ln in lines:
                w = d.textlength(ln, font=big); x = (W - w) / 2      # centered
                d.text((x, y), ln, font=big, fill=WHITE, stroke_width=max(4, fs // 16), stroke_fill=(18, 21, 28))
                y += lh
            return y            # bottom of the title block

        def _pill_h(fs=72):
            cf = _pil_font(fs); asc, desc = cf.getmetrics()
            return (asc + desc) + 72    # matches chrome_pill ph (pady*2=72)

        def chrome_pill(cx, cy, label):
            cf = _pil_font(72); asc, desc = cf.getmetrics(); th = asc + desc
            cw = d.textlength(label, font=cf); triw, gap = 44, 22; total = triw + gap + cw
            padx, pady = 60, 36; pw = total + padx * 2; ph = th + pady * 2
            box = (cx - pw / 2, cy - ph / 2, cx + pw / 2, cy + ph / 2); rad = ph / 2
            _paste_chrome(im, box, rad)
            d.rounded_rectangle(box, radius=rad, outline=WHITE, width=5)
            d.rounded_rectangle([box[0] + 6, box[1] + 6, box[2] - 6, box[3] - 6], radius=rad - 6, outline=STEEL, width=2)
            px = cx - total / 2
            d.polygon([(px, cy - 32), (px, cy + 32), (px + triw, cy)], fill=INK)   # dark engraved triangle
            d.text((px + triw + gap, cy - th / 2), label, font=cf, fill=INK)

        rx_on = bool(reaction_path and os.path.exists(reaction_path))
        rx_im = rx_x = rx_y = None
        if rx_on:
            # keep ALL title + CTA graphics in the clear band relative to wherever
            # the reaction cutout actually sits (above it, or below it if dragged up),
            # keep title and CTA in the clear space above or below the sticker where possible; a very tall sticker at the top leaves no room below, and the fallback band can then run past the bottom edge.
            rx_im = Image.open(reaction_path).convert("RGBA")
            sw, sh = rx_im.size
            rx_x, rx_y = (reaction_xy if reaction_xy else (W - sw, H - sh))
            above = rx_y                          # clear space above the sticker
            below = H - (rx_y + sh)               # clear space below the sticker
            if above >= below:
                band_top, band_h = 0, max(260, above - 20)
            else:
                band_top, band_h = rx_y + sh + 20, max(260, below - 20)
            # center the title + play-button group within the clear band (not jammed
            # at the top), with the button right below the title.
            fs = 72
            big = _pil_font(fs)
            nlines = len(_wrap(d, ttl, big, W - 130))
            title_h = int(fs * 1.14) * nlines
            gap = 34
            cluster_h = title_h + gap + _pill_h()
            cluster_top = band_top + max(24, (band_h - cluster_h) / 2)
            title_cy = int(cluster_top + title_h / 2)
            title_bottom = title_block(title_cy, fs)
            chrome_pill(W / 2, int(title_bottom + gap + _pill_h() / 2), "WATCH NOW")
        elif style == 2:      # PLAYER — centered title, chrome play circle below
            title_block(int(H * 0.40), 74)
            r, ccx, ccy = 138, W / 2, int(H * 0.63)
            _paste_chrome(im, (ccx - r, ccy - r, ccx + r, ccy + r), r, circle=True)
            d.ellipse([ccx - r, ccy - r, ccx + r, ccy + r], outline=WHITE, width=6)
            t = 60
            d.polygon([(ccx - t * 0.45, ccy - t), (ccx - t * 0.45, ccy + t), (ccx + t, ccy)], fill=INK)
            lab, cf = "WATCH THE FULL VIDEO", _pil_font(48)
            cw = d.textlength(lab, font=cf)
            d.text(((W - cw) / 2, ccy + r + 36), lab, font=cf, fill=WHITE, stroke_width=4, stroke_fill=(18, 21, 28))
        elif style == 3:      # BANNER — brushed-chrome bottom bar (centered text)
            title_block(int(H * 0.44), 80)
            bar_h = 196
            _paste_chrome(im, (0, H - bar_h, W, H), 0)
            d.line([(0, H - bar_h), (W, H - bar_h)], fill=WHITE, width=5)
            lab, cf = "WATCH NOW", _pil_font(80); asc, desc = cf.getmetrics()
            cw = d.textlength(lab, font=cf); gap, triw = 26, 52; total = cw + gap + triw
            lx = (W - total) / 2; cyb = H - bar_h / 2
            d.text((lx, cyb - (asc + desc) / 2), lab, font=cf, fill=INK)
            tx = lx + cw + gap
            d.polygon([(tx, cyb - 30), (tx, cyb + 30), (tx + triw, cyb)], fill=INK)
        else:                 # POP — chrome button, centered
            title_block(int(H * 0.42), 80)
            chrome_pill(W / 2, int(H * 0.66), "WATCH NOW")

        if rx_on and rx_im is not None:   # creator reaction, at its chosen position (may bleed off-edge)
            comp = im.convert("RGBA")
            layer = Image.new("RGBA", comp.size, (0, 0, 0, 0))
            layer.paste(rx_im, (int(rx_x), int(rx_y)), rx_im)   # paste clips negative/overflow
            im = Image.alpha_composite(comp, layer).convert("RGB")

        im.save(out_path, "JPEG", quality=90)
        return True
    except Exception:
        return False


def _prep_reaction(reaction_path, dest, frame_w=1080, frame_h=1920, scale=0.66, scale_h=None):
    """Pre-scale a reaction-sticker PNG for the Story frame, preserving alpha.

    scale_h (0..1), when given, sets the sticker HEIGHT to that fraction of the
    frame height (the size the user picked by dragging the resize handle).
    Otherwise it FITs within `scale` of both frame dimensions (the default)."""
    from PIL import Image
    st = Image.open(reaction_path).convert("RGBA")
    if scale_h:
        nh = max(1, int(max(0.08, min(0.98, scale_h)) * frame_h))
        nw = max(1, int(st.size[0] * nh / st.size[1]))
    else:
        fit = min((frame_w * scale) / st.size[0], (frame_h * scale) / st.size[1])
        nw, nh = max(1, int(st.size[0] * fit)), max(1, int(st.size[1] * fit))
    st.resize((nw, nh)).save(dest, "PNG")
    return dest


def _track_centers(video_path, start, length, fps_sample=0.6, max_samples=40):
    """Sample [start, start+length] and return [(t_rel, cx_norm)] — the horizontal
    center (0..1) of the largest person SAM finds in each sampled frame. Returns []
    if SAM is unavailable or nothing is detected (caller falls back to center crop)."""
    try:
        import sam_cutout as sc
        from PIL import Image
    except Exception:
        return []
    n = max(2, min(max_samples, int(length * fps_sample) + 1))
    tmpd = tempfile.mkdtemp()
    centers = []
    try:
        for i in range(n):
            t_rel = (length * i) / max(1, n - 1)
            f = os.path.join(tmpd, f"trk_{i}.jpg")
            try:
                subprocess.run([FFMPEG, "-y", "-ss", f"{start + t_rel:.2f}", "-i", video_path,
                                "-frames:v", "1", "-q:v", "4", f],
                               check=True, capture_output=True, timeout=30)
                im = Image.open(f).convert("RGB")
                w = im.size[0]
                txt = sc._call_sam(im, "person")
                best, ba = None, -1
                for x1, y1, x2, y2, _mx, _my, _data in sc._REC_RE.findall(txt):
                    x1i, x2i = int(x1), int(x2)
                    a = (x2i - x1i) * (int(y2) - int(y1))
                    if a > ba:
                        ba = a
                        best = (x1i + x2i) / 2.0 / w
                if best is not None:
                    centers.append((round(t_rel, 2), round(best, 4)))
            except Exception:
                continue
    finally:
        try:
            import shutil
            shutil.rmtree(tmpd, ignore_errors=True)
        except Exception:
            pass
    return centers


def _crop_x_expr(centers):
    """Piecewise-linear normalized subject-center(t) as an ffmpeg crop `x` expression
    that keeps the subject centered (clamped to the frame). Returns an escaped
    expression string, or None when there are too few samples."""
    if len(centers) < 2:
        return None
    xs = [c for _, c in centers]
    win = 3
    sm = []
    for i in range(len(xs)):        # light moving-average smooth (no jitter)
        a = max(0, i - win // 2)
        b = min(len(xs), i + win // 2 + 1)
        sm.append(sum(xs[a:b]) / (b - a))
    s = [(centers[i][0], sm[i]) for i in range(len(centers))]
    if s[0][0] > 0:
        s = [(0.0, s[0][1])] + s
    cxn = f"{s[-1][1]:.4f}"
    for i in range(len(s) - 1):
        t0, c0 = s[i]
        t1, c1 = s[i + 1]
        if t1 <= t0:
            continue
        seg = f"({c0:.4f}+({c1 - c0:.4f})*(t-{t0:.3f})/{t1 - t0:.3f})"
        cxn = f"if(lt(t,{t1:.3f}),{seg},{cxn})"
    xexpr = f"max(0,min(iw-1080,({cxn})*iw-540))"
    return xexpr.replace(",", "\\,")


def _caption_phrases(transcript, total_dur, words_per=4):
    """Split a transcript into short caption phrases with even-distributed timing.
    Muse Voice gives no word timings, so we spread the words evenly across the whole
    video (documented limitation). Returns [(text, t_start, t_end)] in seconds."""
    words = (transcript or "").split()
    if not words or total_dur <= 0:
        return []
    per_word = total_dur / len(words)
    out = []
    i = 0
    while i < len(words):
        chunk = words[i:i + words_per]
        t0 = i * per_word
        t1 = (i + len(chunk)) * per_word
        out.append((" ".join(chunk), t0, t1))
        i += words_per
    return out


def _caption_png(text, dest, W=1080, H=1920):
    """Render one caption phrase as a full-frame transparent PNG — bold white text in a
    soft dark rounded pill, centered in the lower third (TikTok/Reels caption style)."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    max_w = int(W * 0.84)
    for fs in (60, 52, 44):
        font = _pil_font(fs)
        lines = _wrap(d, text.strip(), font, max_w)
        if len(lines) <= 2:
            break
    lines = lines[:3]
    if not lines:
        return None
    asc, desc = font.getmetrics()
    lh = asc + desc + 10
    block_h = lh * len(lines)
    cy = int(H * 0.74)                 # lower third, above a bottom-right sticker
    top = cy - block_h // 2
    # pill behind the text
    widths = [d.textlength(ln, font=font) for ln in lines]
    pill_w = int(max(widths) + 56)
    pill_x = (W - pill_w) // 2
    pad_y = 18
    d.rounded_rectangle([pill_x, top - pad_y, pill_x + pill_w, top + block_h + pad_y],
                        radius=26, fill=(14, 16, 22, 196))
    y = top
    for ln, w in zip(lines, widths):
        x = (W - w) / 2
        # subtle outline for legibility over the pill edge
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            d.text((x + dx, y + dy), ln, font=font, fill=(0, 0, 0, 210))
        d.text((x, y), ln, font=font, fill=(255, 255, 255, 255))
        y += lh
    im.save(dest, "PNG")
    return dest


def _build_captions(transcript, clip_start, clip_len, total_dur, dest_dir, stamp, tag):
    """For a clip [clip_start, clip_start+clip_len], render the caption PNGs whose phrases
    fall in that window. Returns [(png_path, rel_start, rel_end)] with clip-relative times."""
    out = []
    phrases = _caption_phrases(transcript, total_dur)
    k = 0
    for text, t0, t1 in phrases:
        a, b = max(t0, clip_start), min(t1, clip_start + clip_len)
        if b - a < 0.25:
            continue
        png = os.path.join(dest_dir, f"_cap_{stamp}_{tag}_{k}.png")
        if _caption_png(text, png):
            out.append((png, round(a - clip_start, 2), round(b - clip_start, 2)))
            k += 1
    return out


def _crop_clip(video_path, start, length, out, overlay_path=None, reaction_path=None,
               reaction_xy=None, track=False, captions=None):
    crop = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    if track:        # SAM auto-reframe: follow the subject instead of center-cropping
        xexpr = _crop_x_expr(_track_centers(video_path, start, length))
        if xexpr:
            crop = ("scale=1080:1920:force_original_aspect_ratio=increase,"
                    f"crop=1080:1920:x='{xexpr}':y=0")
    has_fx = bool(overlay_path and os.path.exists(overlay_path))
    has_rx = bool(reaction_path and os.path.exists(reaction_path))
    caps = [c for c in (captions or []) if os.path.exists(c[0])]
    if not has_fx and not has_rx and not caps:
        cmd = [FFMPEG, "-y", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", video_path,
               "-vf", crop, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
               "-r", "30", out]
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return
    # build a filter graph: crop (+ grade + Muse FX) then the reaction sticker on top
    inputs = ["-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", video_path]
    filt = []
    idx = 1
    if has_fx:
        filt.append(f"[0:v]{crop},eq=contrast=1.12:saturation=1.28,vignette[v0]")
        inputs += ["-loop", "1", "-i", overlay_path]
        filt.append(f"[{idx}:v]scale=1080:1920[ov]")
        filt.append("[v0][ov]blend=all_mode=screen:all_opacity=0.5[vb]")
        idx += 1
    else:
        filt.append(f"[0:v]{crop}[vb]")
    last = "[vb]"
    if has_rx:
        inputs += ["-loop", "1", "-i", reaction_path]
        pos = f"{int(reaction_xy[0])}:{int(reaction_xy[1])}" if reaction_xy else "W-w:H-h"
        filt.append(f"{last}[{idx}:v]overlay={pos}[vout]")   # user position or flush br
        last = "[vout]"
        idx += 1
    # burn-in captions: one looped PNG per phrase, shown only in its time window
    for png, t0, t1 in caps:
        inputs += ["-loop", "1", "-i", png]
        nxt = f"[vc{idx}]"
        filt.append(f"{last}[{idx}:v]overlay=0:0:enable='between(t,{t0},{t1})'{nxt}")
        last = nxt
        idx += 1
    cmd = ([FFMPEG, "-y"] + inputs + ["-filter_complex", ";".join(filt),
           "-map", last, "-map", "0:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-r", "30", "-shortest", out])
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)


class CaptionSkip(Exception):
    """The captions cannot be added to this video (it is not portrait 9:16). The Reel is posted without them."""


def _video_dims(path):
    """Width and height as the picture is shown: a phone clip stored sideways is turned back upright."""
    import json as _json
    info = _json.loads(subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                       "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
                                       "-of", "json", path], capture_output=True, text=True, timeout=60).stdout or "{}")
    st = (info.get("streams") or [{}])[0]
    w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    turned = str((st.get("tags") or {}).get("rotate", "0"))
    for sd in st.get("side_data_list", []) or []:
        if "rotation" in sd:
            turned = str(sd["rotation"])
    if turned.lstrip("-") in ("90", "270"):
        w, h = h, w
    return w, h


def burn_reel_captions(video_path, transcript, out_path):
    """Burn the transcript onto a Reel in ONE overlay pass. Each phrase is rendered once (the same PNG the
    Stories use), laid out on a transparent track in time order, and composited over the video. Raises
    CaptionSkip for a video that is not portrait 9:16 (the caption layer is built for 1080x1920). Timing is
    even across the transcript, the same approximation the Stories use."""
    import uuid
    w, h = _video_dims(video_path)
    if h <= 0 or abs((w / h) - (9 / 16)) > 0.02:
        raise CaptionSkip(f"captions need a portrait 9:16 video (this one is {w}x{h})")
    dur = _ffprobe_duration(video_path)
    if dur <= 0:
        raise RuntimeError("could not read the length of the video")
    phrases = [p for p in _caption_phrases(transcript, dur) if p[2] - p[1] >= 0.12]
    work = os.path.dirname(os.path.abspath(out_path))
    stamp = uuid.uuid4().hex[:12]
    made = []
    try:
        from PIL import Image
        blank = os.path.join(work, f"_capblank_{stamp}.png")
        Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)).save(blank, "PNG"); made.append(blank)
        segs, t = [], 0.0
        for k, (text, a, b) in enumerate(phrases):
            a = max(0.0, min(a, dur)); b = max(a, min(b, dur))
            if a - t > 0.01:
                segs.append((blank, a - t))
            png = os.path.join(work, f"_cap_{stamp}_{k}.png")
            if b - a > 0.01 and _caption_png(text, png):
                made.append(png); segs.append((png, b - a))
                t = b
            else:
                t = a
        if dur - t > 0.01 or not segs:
            segs.append((blank, max(0.05, dur - t)))
        listfile = os.path.join(work, f"_capseq_{stamp}.txt"); made.append(listfile)
        with open(listfile, "w", encoding="utf-8") as f:
            for p, sec in segs:
                f.write("file '" + p.replace("'", "'\\''") + "'\n" + f"duration {sec:.3f}\n")
            f.write("file '" + segs[-1][0].replace("'", "'\\''") + "'\n")   # concat needs the last frame repeated
        track = os.path.join(work, f"_captrack_{stamp}.mov"); made.append(track)
        subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", listfile, "-r", "30",
                        "-t", f"{dur:.3f}", "-c:v", "qtrle", "-pix_fmt", "argb", track], check=True, capture_output=True, timeout=600)
        try:
          subprocess.run([FFMPEG, "-y", "-i", video_path, "-i", track,
                        "-filter_complex", "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[base];"
                                           "[base][1:v]overlay=0:0[vout]",
                        "-map", "[vout]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "128k", "-t", f"{dur:.3f}", out_path],
                       check=True, capture_output=True, timeout=600)
        except Exception:
            try: os.remove(out_path)   # never leave a half-written Reel behind
            except OSError: pass
            raise
    finally:
        for p in made:
            try: os.remove(p)
            except OSError: pass


def _queue_story(db, page_id, path, slot):
    if db is None:
        return
    db.execute("CREATE TABLE IF NOT EXISTS story_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "page_id TEXT NOT NULL, path TEXT NOT NULL, slot TEXT NOT NULL, "
               "posted_at TEXT, error TEXT)")
    db.execute("INSERT INTO story_queue (page_id, path, slot) VALUES (?, ?, ?)", (page_id, path, slot))
    db.commit()


def make_stories(video_path: str, page_id: str, caption_text: str = "",
                 db=None, base_time_iso: str = "", n: int = 3,
                 clip_len: float = 60.0, gap_min: int = 15,
                 template: str = "chopped", clickbait: str = "", teaser_style: int = 1,
                 overlay_path: str = "", reaction_path: str = "", track: bool = False,
                 reaction_x=None, reaction_y=None, reaction_scale=None,
                 captions: bool = False, transcript: str = "") -> int:
    """Build Story posts from the video and queue them `gap_min` apart.

    template='chopped' : the whole video split into n CONSECUTIVE 9:16 clips (with
                         audio), together covering the whole video only when it is no longer than n x clip_len.
    template='teaser'  : Story 1 = a paused dramatic frame with a clickbait title and a
                         'Watch now' call-to-action (style 2 with no reaction sticker says 'Watch the full video'), posted first; Story 2 =
                         the hook (the opening of the video, up to 20 seconds), posted after it.
    Returns count queued.
    """
    os.makedirs(STORY_DIR, exist_ok=True)
    dur = _ffprobe_duration(video_path) or 0
    if dur <= 0:
        return 0
    if template == "smart":        # by length: under 1:30 -> chopped, over -> teaser
        template = "chopped" if dur < 90 else "teaser"
    try:
        base_local = datetime.fromisoformat(base_time_iso).astimezone().replace(tzinfo=None) \
            if base_time_iso else datetime.now()
    except Exception:
        base_local = datetime.now()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    made = 0

    # pre-scale the creator's reaction sticker once, and resolve its top-left (x,y):
    # the user's dragged position/size if given, else flush bottom-right.
    rx, rx_xy = "", None
    if reaction_path and os.path.exists(reaction_path):
        try:
            from PIL import Image as _Im
            rx = _prep_reaction(reaction_path, os.path.join(STORY_DIR, f"_rx_{stamp}.png"),
                                scale_h=reaction_scale)
            sw, sh = _Im.open(rx).size
            if reaction_x is not None and reaction_y is not None:
                rx_xy = (int(round(reaction_x * 1080 - sw / 2)),
                         int(round(reaction_y * 1920 - sh / 2)))
            else:
                rx_xy = (1080 - sw, 1920 - sh)   # flush bottom-right default
        except Exception as e:
            B.log.warning("reaction prep failed: %s", str(e)[:120])
            rx, rx_xy = "", None

    if template == "teaser":
        # head-start: post the teaser a few minutes AFTER the Reel goes live, so the
        # full video is already up and findable when the "Watch now" slide drops.
        base_local = base_local + timedelta(minutes=4)
        # Story 1 — the clickbait style slide (paused dramatic frame + CTA), posted FIRST
        frame = os.path.join(STORY_DIR, f"_frame_{stamp}.png")
        card = os.path.join(STORY_DIR, f"story_{stamp}_0.jpg")
        try:
            subprocess.run([FFMPEG, "-y", "-ss", f"{dur*0.8:.2f}", "-i", video_path,
                            "-frames:v", "1", "-q:v", "3", frame],
                           check=True, capture_output=True, timeout=60)
            if _clickbait_frame(frame, clickbait or caption_text, card, style=teaser_style, overlay_path=(overlay_path or None), reaction_path=(rx or None), reaction_xy=rx_xy):
                _queue_story(db, page_id, card, base_local.strftime("%Y-%m-%dT%H:%M"))
                made += 1
            try:
                os.unlink(frame)
            except Exception:
                pass
        except Exception:
            pass
        # Story 2 — the video preview (hook): first ~20s, posted after the style slide
        hook_len = min(20.0, max(5.0, dur * 0.5))
        hook = os.path.join(STORY_DIR, f"story_{stamp}_1.mp4")
        try:
            caps = (_build_captions(transcript, 0.0, hook_len, dur, STORY_DIR, stamp, "h")
                    if (captions and transcript) else None)
            _crop_clip(video_path, 0.0, hook_len, hook, overlay_path=(overlay_path or None), reaction_path=(rx or None), reaction_xy=rx_xy, track=track, captions=caps)
            slot = (base_local + timedelta(minutes=gap_min)).strftime("%Y-%m-%dT%H:%M")
            _queue_story(db, page_id, hook, slot)
            made += 1
        except Exception:
            pass
        return made

    # template == 'chopped' (default): consecutive segments covering the whole video
    seg = dur / n
    for i in range(n):
        st = seg * i
        length = min(seg, clip_len, max(1.0, dur - st))
        out = os.path.join(STORY_DIR, f"story_{stamp}_{i}.mp4")
        try:
            caps = (_build_captions(transcript, st, length, dur, STORY_DIR, stamp, f"c{i}")
                    if (captions and transcript) else None)
            _crop_clip(video_path, st, length, out, overlay_path=(overlay_path or None), reaction_path=(rx or None), reaction_xy=rx_xy, track=track, captions=caps)
        except Exception:
            continue
        if not os.path.exists(out):
            continue
        slot = (base_local + timedelta(minutes=gap_min * i)).strftime("%Y-%m-%dT%H:%M")
        _queue_story(db, page_id, out, slot)
        made += 1
    return made


if __name__ == "__main__":  # validate the ffmpeg/thumbnail plumbing with a generated clip
    import sqlite3
    db = sqlite3.connect("state.db")
    with tempfile.TemporaryDirectory() as td:
        clip = os.path.join(td, "test.mp4")
        # 3s test clip: moving gradient + tone, so frames differ
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=24:duration=3",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", clip],
                       check=True, capture_output=True)
        print("test clip:", round(_ffprobe_duration(clip), 2), "s")
        frames = extract_frames(clip, td, n=6)
        print("frames extracted:", len(frames))
        scored = sorted(((round(_frame_score(f), 1), os.path.basename(f)) for f in frames), reverse=True)
        print("frame scores:", scored)
        os.makedirs(DRAFTS_DIR, exist_ok=True)
        thumb = pick_thumbnail(frames, os.path.join(DRAFTS_DIR, "test_thumb.jpg"))
        print("thumbnail chosen ->", thumb)
        print("brain slot proposal:", (lambda t: f"{t.strftime('%a')} {t.hour % 12 or 12} {t.strftime('%p')} ET")(brain.propose_slots(db, "example-page-id", [], 1)[0]))
