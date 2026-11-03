# Judge walkthrough: Abra, step by step

Abra runs on your own Mac. Nothing is hosted, and no account is needed to look around.
Follow these steps in order. Each step says what you should see, and what needs a key or a Facebook Page.

## Before you start

- **A Mac.** Abra has only been run on macOS. Windows and Linux are not supported yet.
- **Python 3.12** and **ffmpeg** (`brew install ffmpeg`).
- **Google Chrome.** The dashboard is built for it.
- **Optional keys.** The AI features need a Meta Muse API key and an Anthropic (Claude) key. You can add them in the app (step 3 below), or in `.env`. Without keys you can still open every screen.
- **Facebook is not needed.** Insights, Engagement and scheduling need a connected Facebook Page, which judges will not have. Those screens show empty states, and the demo video shows them with a real page.

## Step 1: Install and start

From the repository folder, in Terminal:

```
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp persona.example.md persona.md
python3 liveview.py
```

Open **http://localhost:8765** in Chrome. The dashboard opens on **Home**.

*Check:* the page loads with a left menu and a top bar. Press Ctrl+C in Terminal to stop it.

## Step 2: Home

- Four numbers at the top (followers, replies, minutes viewed, earnings). Without a Facebook Page they show as empty.
- Three crew cards: **Nico** (engagement), **Sage** (content) and **Koa** (money).
- A "Next 7 days" list and a to-do list. You can add and tick off to-dos.

## Step 3: Add your keys (optional, needed for steps 4 to 6)

Click the **Connect your AI keys** banner. Paste a Meta Muse key and an Anthropic key. They are saved to `.env` on your computer and are never committed.

*Check:* the banner goes away once the keys are saved.

## Step 4: Launchpad, then Load (the main demo)

Click **Launchpad** in the menu. It opens **Load**, with the sub-tabs Load, Engagement and Insights.

1. Click the box that says **Drop content Sage watches & listens to it**, and choose a short portrait video (9:16 works best).
2. Sage listens to the audio (Muse Voice), reads the frames (Claude), and writes three titles, a caption and hashtags (Muse Spark). Muse Wonder picks the hook it expects to do best.
3. Captions are burned onto the video from the transcript when you schedule. Reels need a portrait video.
4. Choose a time and press **Approve & schedule**.

*Check:* you see titles, a caption and a cover. *Needs:* both keys for steps 1 to 2. **Approve & schedule needs a connected Facebook Page**, so it does not publish anything on a judge's computer.

## Step 5: Insights, and Creator Spotlight

Click **Insights**.

- **Creator Spotlight** (the bar at the top): type a genre, such as "cooking hacks", and press **Spotlight**. Sage returns short-form hooks and ideas, each with a **Make this →** button. Pressing it sends the hook to Load as the next title.
- **Sage's read, Watch-through, Best time to post and Reach trend** use your Facebook Page's real numbers. Without a connected Page they show empty states. Spotlight needs the Meta Muse key.

## Step 6: Stickers and the Abra chat

- **Account → Your custom stickers:** choose a photo of yourself. Abra cuts you out of the background (Meta SAM 3.1) and saves you as a sticker. *Needs:* the Meta Muse key.
- **Abra chat:** click the Abra orb (bottom right), type a question, and press Enter. Abra answers in text from the data it has. *Needs:* the Meta Muse key.

## Step 7: Help

Click **Help** for the FAQ and the list of what is built.

## What is not in this version

These parts exist in the code but are switched off for the submission, so they are not part of the demo:

- The live camera recorder, hands-free gestures and the phone camera.
- Greenscreen, the Create templates and the Create teleprompter.
- Voice: the chat's microphone and spoken replies, and voice commands.

The camera, Greenscreen and voice can be turned back on with one switch each in `liveview.py` (`RX_CAMERA_ON`, `RX_GREENSCREEN_ON`, `ABRA_VOICE_ON`). The Create templates are switched off by an empty `film_genres.json`; the Create teleprompter has no switch in this version.

## If something looks wrong

- **A screen is empty:** it needs a connected Facebook Page or a key (see the notes above). That is expected.
- **Load shows an error after you drop a video:** check that the Meta Muse and Anthropic keys are saved, then try again.
- **Port 8765 is in use:** stop the other program that is using it, then start Abra again. Or start Abra on another port, for example `ABRA_PORT=9000 python3 liveview.py`, and open http://localhost:9000.
