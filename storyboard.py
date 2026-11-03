"""Storyboard the day: 16 hourly Story concepts (6 AM - 9 PM) with ready-to-
paste image-generation prompts for your AI photo tool.

Run each morning (or the night before):   python storyboard.py
Requires Anthropic API credits. Saves to storyboards/YYYY-MM-DD.md.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
CREATOR_NAME = os.environ.get("CREATOR_NAME", "").strip()
CREATOR_REF = f"creator {CREATOR_NAME}" if CREATOR_NAME else "the creator"

PROMPT = """You are planning a day of Facebook Stories for {creator}, a
lifestyle/curiosity creator (warm and approachable).
Create a storyboard of 16 hourly Story moments from 6 AM to 9 PM for a
{weekday}. The day should feel like a normal, believable, lightly aspirational
day — coffee, work, errands, movement, golden hour, winding down. Nothing
exotic or out of the ordinary; cozy-real, with small visual variety across
the day (different rooms, light, outfits, one or two outdoor moments).

For each hour give:
1. **Time + moment** (one short line, e.g. "7 AM — first coffee by the window")
2. **Image prompt** — one paragraph they can paste directly into an AI photo
   generator trained on the creator's face. Describe setting, action, outfit, lighting,
   camera feel (casual phone-photo look, not editorial). Keep the creator the clear
   subject.

Rules: no brand names, no text-in-image requirements, no other identifiable
people, consistent outfit logic through the day (morning loungewear ->
daytime casual -> evening cozy), seasonal for {month}. Every image prompt must
end with: "Set against a deep obsidian and liquid mercury gradient background."

Output as a markdown list, one entry per hour, nothing else."""


def main() -> None:
    now = datetime.now()
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": PROMPT.format(
            weekday=now.strftime("%A"), month=now.strftime("%B"), creator=CREATOR_REF)}],
    )
    text = next(b.text for b in msg.content if b.type == "text")
    out = BASE / "storyboards" / f"{now.date().isoformat()}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(f"# Story storyboard — {now.strftime('%A %Y-%m-%d')}\n\n{text}\n", encoding="utf-8")
    print(f"Saved {out.name}\n")
    print(text[:1500])
    print("\nNext: paste the prompts into your AI photo tool to make the 16 photos.")


if __name__ == "__main__":
    main()
