"""Subject cutouts via Meta SAM 3.1 -> transparent "reaction stickers".

A creator uploads photos of themselves in Settings; SAM cleanly segments the
person out of the background so the cutout can be dropped onto a Story like a
sticker. This is the creator's OWN photo, cut out -- not an AI-generated
likeness -- so it looks authentic. The code does not check identity or policy.

Pipeline (all verified against the live API 2026-10-28):
  POST https://api.meta.ai/v1/responses  model=sam-3.1
    input = [user msg: input_text (concept noun phrase) + input_image (data URL)]
    metadata.mask_encoding = "one_bit"   (smaller payload; simpler decode)
  -> SSE stream of response.output_text.delta, concatenated into a token string
     of comma-separated records: <0f>N<|box;...|><|mask;x=;y=;data=H,W,!<payload>|>
  -> decode each mask's custom arithmetic-coded payload to a row-major binary
     raster, keep the LARGEST subject, composite as the cutout's alpha.

The mask codec is Meta's own (not standard base85); the decoder below is ported
1:1 from @meta-sam/parser's mask-codec.js (one_bit path). No Node / numpy / external parser needed.

Nothing here publishes anything -- it only turns a local image into a PNG.
"""
from __future__ import annotations
import base64
import io
import json
import re

import requests

import meta_live
from PIL import Image, ImageFilter

import bot as B

SAM_URL = "https://api.meta.ai/v1/responses"
SAM_MODEL = "sam-3.1"

# ---------------------------------------------------------------------------
# one_bit mask decoder -- ported from @meta-sam/parser (mask-codec.js)
# ---------------------------------------------------------------------------
_EXCLUDED = set('"\\,;<>|')
_ALPHABET = "".join(chr(c) for c in range(0x21, 0x7F) if chr(c) not in _EXCLUDED)
_RADIX = 85
_PREFIX = 5
_TOP = 1 << 24
_BOTTOM = 1 << 16
_M32 = 0xFFFFFFFF
_INCREMENT = 14
_COUNT_LIMIT = 4096
_CONTEXT_COUNT = 1 << 12
_PADDING = 2
_VAL = [-1] * 128
for _i, _ch in enumerate(_ALPHABET):
    _VAL[ord(_ch)] = _i


def _digit(ch: str) -> int:
    v = _VAL[ord(ch)] if ord(ch) < 128 else -1
    if v < 0 or v >= _RADIX:
        raise ValueError("invalid mask character")
    return v


def _unpack(payload: str) -> bytes:
    """base85-with-length-prefix -> raw bytes (the arithmetic-coder stream)."""
    if len(payload) < _PREFIX:
        raise ValueError("mask payload missing length prefix")
    length = 0
    for i in range(_PREFIX):
        length = length * _RADIX + _digit(payload[i])
    rem = length % 4
    expected = _PREFIX + (length // 4) * 5 + (0 if rem == 0 else rem + 1)
    if len(payload) != expected:
        raise ValueError("mask payload length mismatch")
    out = bytearray(length)
    src = _PREFIX
    dst = 0
    full = length - rem
    while dst < full:
        v = 0
        for i in range(5):
            v = v * _RADIX + _digit(payload[src + i])
        if v > _M32:
            raise ValueError("mask group out of range")
        src += 5
        out[dst] = (v >> 24) & 0xFF
        out[dst + 1] = (v >> 16) & 0xFF
        out[dst + 2] = (v >> 8) & 0xFF
        out[dst + 3] = v & 0xFF
        dst += 4
    if rem > 0:
        v = 0
        for i in range(5):
            v = v * _RADIX + (_digit(payload[src + i]) if i < rem + 1 else _RADIX - 1)
        if v > _M32:
            raise ValueError("mask tail out of range")
        for i in range(rem):
            out[dst + i] = (v >> (24 - i * 8)) & 0xFF
    return bytes(out)


class _RangeDecoder:
    def __init__(self, data: bytes):
        if len(data) < 4:
            raise ValueError("mask payload missing finalization")
        self.d = data
        self.p = 0
        self.low = 0
        self.range = _M32
        self.code = 0
        self.scaled = 0
        for _ in range(4):
            self.code = ((self.code << 8) | self._read()) & _M32

    def _read(self) -> int:
        if self.p >= len(self.d):
            raise ValueError("mask payload ended early")
        b = self.d[self.p]
        self.p += 1
        return b

    def frequency(self, total: int) -> int:
        self.scaled = self.range // total
        if self.scaled == 0:
            raise ValueError("invalid coding state")
        val = ((self.code - self.low) & _M32) // self.scaled
        return total - 1 if val >= total else val

    def decode(self, cumulative: int, frequency: int) -> None:
        self.low = (self.low + self.scaled * cumulative) & _M32
        self.range = (self.scaled * frequency) & _M32
        while True:
            if ((self.low ^ ((self.low + self.range) & _M32)) & _M32) < _TOP:
                pass
            elif self.range < _BOTTOM:
                self.range = ((-self.low) & _M32) & (_BOTTOM - 1)
            else:
                break
            self.code = ((self.code << 8) | self._read()) & _M32
            self.low = (self.low << 8) & _M32
            self.range = (self.range << 8) & _M32


def _context_at(r, pos, prior, earlier):
    return (r[pos - 1] | (r[pos - 2] << 1) | (r[prior - 2] << 2) | (r[prior - 1] << 3)
            | (r[prior] << 4) | (r[prior + 1] << 5) | (r[prior + 2] << 6)
            | (r[earlier - 2] << 7) | (r[earlier - 1] << 8) | (r[earlier] << 9)
            | (r[earlier + 1] << 10) | (r[earlier + 2] << 11))


def _update(counts, off, bit):
    counts[off + bit] += _INCREMENT
    if counts[off] + counts[off + 1] >= _COUNT_LIMIT:
        counts[off] = counts[off] >> 1 or 1
        counts[off + 1] = counts[off + 1] >> 1 or 1


def decode_one_bit(payload: str, width: int, height: int) -> bytearray:
    """Decode a one_bit (``!``) mask payload to a row-major 0/1 raster."""
    if not payload.startswith("!"):
        raise ValueError("one_bit mask must start with !")
    dec = _RangeDecoder(_unpack(payload[1:]))
    counts = [1] * (_CONTEXT_COUNT * 2)
    pw = width + 2 * _PADDING
    padded = bytearray(pw * (height + _PADDING))
    out = bytearray(width * height)
    for y in range(height):
        row = (y + _PADDING) * pw + _PADDING
        for x in range(width):
            pos = row + x
            off = _context_at(padded, pos, pos - pw, pos - 2 * pw) << 1
            zero = counts[off]
            one = counts[off + 1]
            val = dec.frequency(zero + one)
            bit = 0 if val < zero else 1
            if bit == 0:
                dec.decode(0, zero)
            else:
                dec.decode(zero, one)
            padded[pos] = bit
            out[y * width + x] = bit
            _update(counts, off, bit)
    return out


# ---------------------------------------------------------------------------
# Segmentation + sticker building
# ---------------------------------------------------------------------------
_REC_RE = re.compile(
    r"<\|box;x1=(\d+);y1=(\d+);x2=(\d+);y2=(\d+);w=\d+;h=\d+\|>"
    r"<\|mask;x=(\d+);y=(\d+);data=([^|]+)\|>")


def _call_sam(img: Image.Image, concept: str, timeout: int = 180) -> str:
    """Return the concatenated output_text for `concept` on `img` (one_bit)."""
    if not B.muse_key():
        raise RuntimeError("META_MUSE_API_KEY not set")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    durl = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    body = {
        "model": SAM_MODEL,
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": concept},
            {"type": "input_image", "image_url": durl},
        ]}],
        "stream": True,
        "metadata": {"mask_encoding": "one_bit"},
    }
    headers = {"Authorization": f"Bearer {B.muse_key()}",
               "Content-Type": "application/json", "Accept": "text/event-stream"}
    r = meta_live.post(SAM_MODEL, "reaction cutout", SAM_URL, headers=headers, json=body, stream=True, timeout=timeout)
    r.raise_for_status()
    parts = []
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        if ev.get("type") == "response.output_text.delta":
            parts.append(ev.get("delta", ""))
    return "".join(parts)


def _largest_subject_alpha(text: str, W: int, H: int) -> Image.Image | None:
    """Pick the biggest mask among the records and return a full-frame L alpha."""
    best = None
    best_n = -1
    for x1, y1, x2, y2, _mx, _my, data in _REC_RE.findall(text):
        mh, mw, rest = data.split(",", 2)
        mh, mw = int(mh), int(mw)
        try:
            ras = decode_one_bit(rest, mw, mh)
        except Exception as e:
            B.log.warning("SAM mask decode failed: %s", str(e)[:120])
            continue
        n = sum(ras)
        if n > best_n:
            best_n = n
            best = (int(x1), int(y1), mw, mh, ras)
    if not best:
        return None
    x1, y1, mw, mh, ras = best
    alpha = Image.new("L", (W, H), 0)
    tile = Image.frombytes("L", (mw, mh), bytes(v * 255 for v in ras))
    alpha.paste(tile, (x1, y1))
    return alpha


def cutout_subject(image_path: str, concept: str = "person",
                   max_side: int = 1024, feather: float = 0.8) -> Image.Image | None:
    """Segment the main `concept` out of `image_path`.

    Returns an RGBA image cropped to the subject's bounding box (transparent
    background), or None if nothing was found. Raises if Muse is unavailable (missing key or HTTP error).
    """
    src = Image.open(image_path).convert("RGB")
    src.thumbnail((max_side, max_side))  # keep masks a sane size
    W, H = src.size
    text = _call_sam(src, concept)
    alpha = _largest_subject_alpha(text, W, H)
    if alpha is None:
        return None
    # reject an empty/near-empty mask (SAM matched nothing meaningful)
    hist = alpha.histogram()
    on = sum(hist[128:])
    if on < 0.004 * W * H:
        return None
    # clean the matte to cut most background fringe:
    #   1) close tiny interior holes, 2) erode ~1px to drop any background fringe,
    #   3) soften the edge. The edge is softened to reduce halos.
    alpha = alpha.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))  # close holes
    alpha = alpha.filter(ImageFilter.MinFilter(3))                                   # erode fringe
    if feather > 0:
        alpha = alpha.filter(ImageFilter.GaussianBlur(feather))
    cut = src.convert("RGBA")
    cut.putalpha(alpha)
    box = alpha.getbbox()
    return cut.crop(box) if box else cut


def make_reaction_sticker(image_path: str, dest: str, concept: str = "person",
                          outline: bool = False, outline_px: int = 18,
                          db=None) -> str | None:
    """Turn a creator's uploaded photo into a Story-ready cutout sticker PNG.

    Default is a clean borderless cutout. `outline=True` adds the classic white
    border if ever wanted. Returns `dest` on success.
    """
    cut = cutout_subject(image_path, concept)
    if cut is None:
        return None
    if outline:
        a = cut.split()[-1]
        border = a.filter(ImageFilter.MaxFilter(outline_px | 1))  # odd kernel
        sw, sh = cut.size
        pad = outline_px
        canvas = Image.new("RGBA", (sw + pad * 2, sh + pad * 2), (0, 0, 0, 0))
        white = Image.new("RGBA", (sw, sh), (255, 255, 255, 255))
        white.putalpha(border)
        canvas.alpha_composite(white, (pad, pad))
        canvas.alpha_composite(cut, (pad, pad))
        cut = canvas
    cut.save(dest, "PNG")
    try:
        if db is not None:
            B.record_spend(db, "muse", "reaction_sticker", 0, 0, 0.003)
    except Exception:
        pass
    return dest


_CORNERS = {"br", "bl", "tr", "tl"}


def place_on_story(canvas: Image.Image, sticker_path: str, corner: str = "br",
                   scale: float = 0.66, margin: int = 0) -> Image.Image:
    """Composite a reaction sticker onto a 9:16 Story canvas without clipping.

    The sticker is scaled to FIT within `scale` of both the canvas width and
    height (so a wide pose can never run off the edge, which cut the finger in
    the first draft), then anchored to `corner` (br/bl/tr/tl). `margin=0`
    (the default) lets the cutout sit flush to the frame edges with no gap
    on the anchored sides; a positive margin insets it.
    Returns a new RGBA image.
    """
    base = canvas.convert("RGBA")
    W, H = base.size
    st = Image.open(sticker_path).convert("RGBA")
    sw, sh = st.size
    fit = min((W * scale) / sw, (H * scale) / sh)
    nw, nh = max(1, int(sw * fit)), max(1, int(sh * fit))
    st = st.resize((nw, nh))
    c = corner if corner in _CORNERS else "br"
    x = margin if c in ("bl", "tl") else W - nw - margin
    y = margin if c in ("tl", "tr") else H - nh - margin
    out = base.copy()
    out.alpha_composite(st, (max(0, x), max(0, y)))
    return out
