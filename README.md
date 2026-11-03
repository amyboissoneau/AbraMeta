# Abra

### Abra is a verb. To *abra* is to **open**.

Every creator knows the closed-door feeling: a video sitting unposted because you don't know how to frame it, an inbox too full to face, earnings and contracts you'd rather not look at, a brand door you don't know how to knock on. The block was never a *scheduling* problem — it's a *closed* one.

The name is the old word everyone knows for magic — *abracadabra* — long read as **"I create as I speak."** Creation that doesn't begin with having it all figured out, but with *speaking*: *let there be*, and then an opening. It's the oldest instruction ever given to a creator, and still the only one that matters — you take nothing, a thought or a shaky clip, and speak something into being that wasn't there before. That's quietly sacred, even on a Tuesday.

**Abra doesn't create *for* you. It opens what's closed so *you* can create again.**

- Drop in a raw video and **Sage** opens the story inside it — the hook, the post, the Reel, the Stories (staged for your approval).
- Open your comments and **Nico** opens the conversation again — replying in your own voice, lifting the people who showed up with heart, and skipping spam and hate.
- Open your money and **Koa** opens clarity — earnings, AI costs, contracts, page standing — so you can create without fear underneath it.

Three openings. One crew. All in service of one thing: *you, creating.*

Many creator tools are built on **schedule, automate, optimize** — they treat you like a content machine, and more posting just leaves you with a bigger inbox, murkier money, and more doors you still can't open. Abra is built on one verb: **open.** Other tools start with the *post*. Abra starts with the *person*.

**Abra. Open, and create.**

---

Built for the **Meta Global AI Developer Hackathon** (Indie Dev Track, Oct 26 – Nov 4, 2026).

## Built during the hackathon

The hackathon project is Abra's **AI creator studio**, built on the Meta Model API and Claude, within the event window. The crew and the comment loop predate the event (first snapshot, Oct 21, 2026); the features below were added or substantially reworked during it:

- **Drop a video → a finished post:** Muse Voice hears it, Claude reads the frames, Muse Spark writes three titles + caption + hashtags, and **Muse Wonder picks the hook** it expects to do best
- **SAM 3.1 reaction stickers** (segment the creator for borderless Story stickers) + **subject-tracking auto-reframe**
- **Auto-captions** burned onto the Reel and Stories from the Muse Voice transcript when you schedule, in Load, on auto-publish pages, and in batches (Reels need portrait 9:16 video)
- **The Muse Wonder analyst** — reads the page's real watch-through, views and earnings and writes a strategic "read"
- **Abra, the concierge** — a floating orb you **type to**; Muse Wonder answers from your *live* numbers (retention, reach, earnings, schedule, replies), routes the job to the right crew member (the orb visibly **morphs into that character**), and can open the right screen
- **Watch-through insights, a monetization cockpit, and a content calendar**
- **Reel captions:** the Muse Voice transcript is burned onto the Reel (portrait 9:16) as well as the Stories, for Load, auto-publish and batch posts.
- **Reply approval:** a page can hold each drafted comment reply until the creator approves it. It is off by default.

*Honest scope: the comment-reply loop and the first dashboard snapshot (Oct 21, 2026) predate the event. The list above is what was added or substantially reworked during it. For this submission the live camera recorder, hands-free gestures, Greenscreen, the Create templates and the Create teleprompter, and voice commands and spoken replies are switched off in the app. Their code stays in the repository. The camera, Greenscreen and voice have one switch each in `liveview.py`; the Create templates are off because `film_genres.json` is empty; and they are not part of the demo.*

---

## Meta models at the core

Abra calls **five** hosted Meta Model API models, each tied to a feature in the product. Muse Image renders the Story FX overlays. One live test call returned an image; the full overlay path is not yet tested end to end.

| Meta model | Where it runs | What it does |
|---|---|---|
| **Muse Wonder** (`muse-wonder-1.0`) | Analytics — "Sage's read" | Meta's flagship model reads the page's real watch-through, views and earnings data and writes a sharp strategic read (the AI Creator Analyst) |
| **Muse Spark** (`muse-spark-1.3`) | Engagement triage + copywriting | Screens each new comment for spam and hate while Muse is available, and writes titles, captions and hashtags aimed at reach |
| **Muse Image** (`muse-image-1.0`) | Story FX overlays (one live test call passed, Nov 2) | Renders the Story FX overlay art in the content pipeline |
| **Muse Voice Transcribe** (`muse-voice-transcribe-1.0`) | Launchpad + reply grounding | *Hears* each video you drop in — transcribes the audio so copy and replies are about what you actually said |
| **SAM 3.1** (`sam-3.1`) | Reaction stickers + auto-reframe | Segments the creator out of a photo for Story reaction stickers, and tracks the subject to keep them centered in vertical crops |

**Claude Opus** handles the substantive, in-voice comment replies, and the frame vision that grounds copy. One product: **five Meta models plus Claude**, with Muse Image's overlay path not yet tested end to end. The crew's spoken voices are free neural TTS (edge-tts). Honest about what powers what.

---

## The crew

Each character owns a domain of the creator's business, and Nico, Sage and Koa each have their own desk in the app:

| Crew | Role | What they do |
|---|---|---|
| **Nico** (cat) | Engagement | Reads comments on your posts and replies in your voice, grounded in the video. Likes the good ones, and skips spam and hate. |
| **Sage** (dog) | Content Studio | *Watches and listens* to a dropped video, then writes the title, caption, hashtags and best-time schedule. |
| **Koa** (koala) | Money & Legal | Keeps the books (AI spend vs. page earnings), keeps a dated record of what you earn and drafts disputes, watches page standing. |

*The crew are the teammates; the models that power them — Meta's **Muse** family for content/analysis, **Claude** for in-voice replies and frame vision — are the ones named in the table above.*

---

## Flagship: the Sage Content Scheduler

Drop a video, and the whole post makes itself — then you approve:

1. **Muse Voice** transcribes the audio and **Claude** reads the frames (vision)
2. **Muse Spark** writes three title options, a caption and hashtags — written from this video's transcript and frames, in a tone matched to your top-reaching posts, not generic hooks
3. A thumbnail is picked, and the best-engagement time slot is proposed (at least three hours from already-scheduled posts)
4. You review the card, tweak anything, and approve
5. Abra schedules the Reel, cuts Stories 15 minutes apart (three clips for a clip under 90 seconds; otherwise a title card and a hook), and once it's live **Nico** takes over the comments

Publishing is per-page: **review** (you approve each post, the default) or **full auto**. Comment replies and first-comment seeds post automatically under the reply rules. In review mode, Reels and Stories post only after you approve them.

---

## How reach is won (the engagement engine)

- **Golden hour:** posts under 90 minutes old get instant engagement, so replies land while the post is fresh
- **Adaptive pacing:** 3 parallel drafting threads behind a single rate-paced write gate (surge mode to 60/15min on demand, auto-tripwire back to 30 on any platform pushback)
- **Atomic claim-locking** (SQLite) so concurrent workers never double-reply
- **Data-driven:** posting times follow the hours whose posts drew the most reactions, comments and shares (from the page's own post data), titles match your top-engaging tone, and replies follow your persona and real examples of your past replies
- **Safety-first:** hate, slurs and spam are skipped by the Muse screen when it is available and by the reply rules; the creator's reply persona forbids asking for likes or @-mentions, and a throttle guard backs off when Facebook returns a rate-limit error

---

## Architecture

- `bot.py` — 24/7 engagement engine: Meta Graph API + Meta Model API (Muse Spark via `/v1/responses`) + Claude, vision-grounded replies, throttle guard, spend ledger, first-comment seeding, video-story posting
- `content_pipeline.py` — video → draft: Muse Voice transcribe, ffmpeg frame sampling, vision read, thumbnail pick, Story cutting
- `content_brain.py` — reach/timing intelligence learned from the page's own history; Muse Spark copywriting
- `content_publish.py` — resumable Reels publishing/scheduling to the Graph API
- `liveview.py` — live dashboard on Python's standard-library HTTP server: the crew, live engagement feed, Content Scheduler, Koa's ledger, per-page settings
- `analytics.py` — daily insights + earnings snapshots

The UI is a liquid-chrome / brushed-metal theme. Story overlay art is the one part rendered with Muse Image, and one live test call returned an image; the full overlay path is not yet tested end to end.

---

## Under the hood (the hard parts)

- **We ported SAM 3.1's mask decoder from an existing JavaScript parser (`@meta-sam/parser`) into pure Python.** SAM returns segmentation masks in a custom arithmetic range-coded `one_bit` encoding (not base64). The decoder lives in `sam_cutout.py`, and we added matte cleanup (erode → feather, empty-mask rejection) so the cutouts are clean enough to composite as borderless stickers.
- **All compositing is PIL, by necessity.** This ffmpeg build has no `drawtext`/`subtitles` filter, so every caption, clickbait frame and Story overlay is composited in Pillow and burned in via timed `overlay … enable='between(t,a,b)'` filter graphs.
- **Concurrency-safe at the write layer.** Three parallel drafting threads sit behind a single rate-paced write gate with **atomic SQLite claim-locking**, so workers never double-reply and replies are paced within a cap of 30 per 15 minutes (60 during demand surges, back to 30 after any Facebook pushback).
- **The concierge is genuinely agentic, safely.** Muse Wonder answers from a live data snapshot and may emit a whitelisted action (open a screen / run a read) the UI executes — but **never** an action that posts or sends; anything outward stays in the creator's review-mode approval. It also tags the responsible crew member so the orb morphs and the right voice speaks.
- **Resilient to the Graph API's quirks.** Comment fetches degrade through simpler field sets when a Reel rejects a rich nested query, so a Reel that rejects the richest comment query still gets its comments fetched with simpler fields.

---

## Running it

These steps run the full app on your own computer. You don't need a hosted copy, a domain or any account to look around, and the keys are optional until you want the AI features.

### Supported system

**Windows and Linux: not supported yet.** The project has only been run on macOS. Judges should use a Mac or the demo video. A Windows and Linux version is planned after the hackathon, but it hasn't been tested.

### What you need

- **Python 3.12.** The pinned versions in `requirements.txt` install on it.
- **ffmpeg**, for video, thumbnails, Stories and voice transcription: `brew install ffmpeg`.

### Keys for the AI features

The AI features use two keys. You can look around without them; add them to try the AI.

- **Meta Model API key** (`META_MUSE_API_KEY`): the Muse models (Muse Voice, Muse Spark, Muse Wonder and Muse Image). Hackathon participants can claim Meta API credits through the credits form linked from the hackathon's announcements. Claiming needs a payment method on the account, so set a spend limit.
- **Anthropic API key** (`ANTHROPIC_API_KEY`): Claude, which reads video frames and drafts some replies. Get one at https://platform.claude.com.

Add them with the **Connect your AI keys** banner, or in `.env`. Facebook is not needed for any of the AI features.

### Connect your Facebook page (optional)

Connecting a Page shows its real numbers in Insights and lets Abra schedule posts to it. It needs your own Meta app, once:

1. At developers.facebook.com, create an app (type Business) and add the **Facebook Login** product.
2. Under Facebook Login → Settings, add the Valid OAuth Redirect URI `http://localhost:8765/oauth/callback` (use your `ABRA_PORT` if you changed it).
3. Copy the app's **App ID** and **App Secret** (Settings → Basic). Paste them into the **Connect your AI keys** banner, under "Facebook — your Meta app", and save.
4. Open the **Connect a Facebook page** link in Account, sign in, and choose the Page you admin.

While the app is in Development mode, it works for Pages you admin, with no app review needed.

### Set up and run

```
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp persona.example.md persona.md
python3 liveview.py
```

Open http://localhost:8765 in Chrome. The dashboard runs in that window; stop it with Ctrl+C. The first start creates `state.db` with every table the app reads, plus `bot.log`, `.mon_cache.json` and `certs/` (a self-signed certificate for the phone link). All of them are git-ignored.

To use another port, set `ABRA_PORT` on the command line or in `.env`. `ABRA_HTTPS_PORT` sets the HTTPS port for the phone link (default 8766). A blank value means the default:

```
ABRA_PORT=9000 python3 liveview.py     # then open http://localhost:9000
```

If you change `ABRA_PORT`, also add `http://localhost:9000/oauth/callback` (with your port) as a Valid OAuth Redirect URI in your Facebook app. Otherwise Facebook sign-in will not return to Abra.

The HTTPS port also listens on your home network, so a phone on the same Wi-Fi can reach Abra. Requests from other devices need the access token in the link Abra gives you (`/lan` on your Mac). Do not forward this port to the internet.

For a step-by-step tour of the screens, see [JUDGE_WALKTHROUGH.md](JUDGE_WALKTHROUGH.md).

### Keep the bot running

The bot (`bot.py`) is the engagement engine. It needs keys. The owner runs the dashboard and the bot under launchd on macOS, and `watchdog.py` restarts them when they stop answering. Without launchd, run `python3 bot.py` in its own terminal.

**Adding your keys:** either edit `.env`, or open the dashboard and use the **Connect your AI keys** banner, which saves them to `.env` on your computer. Keys are never committed.

Secrets (`.env`), live state (`state.db`) and logs are never committed.

### Check your setup

```
python preflight.py          # free checks (run with the venv active): dashboard, bot, keys present (names only), ffmpeg, database, Internet
python preflight.py --live   # also makes one small Claude and Facebook call each, plus one small Muse call (a direct key check)
```

Each line says PASS, INFO, WARN or FAIL and what to do. INFO means expected for this version (for example, the Create templates are switched off in this version), not a fault. Preflight never prints a key or token.

### Try it without keys

You can open and click through the whole dashboard without any keys. Run `python3 liveview.py` and open http://localhost:8765.

What works with no keys at all:
- The dashboard loads and every tab opens. The database is created on first start.
- **Account:** the connections and the appearance settings.

What shows an empty state until you add keys (nothing errors out):
- **Insights, Best time and earnings:** need a connected Facebook Page and its token. **Sage's read and Spotlight** also need the Muse key.
- **Engagement and comment replies:** need the Facebook token, the Muse key and the Anthropic key. Without the Muse key, replies post without the Muse screen and check.
- **The orb's answers:** need the Muse key. Spoken replies are switched off in this version.
- **Load (analyze a video):** needs the Muse and Anthropic keys. The page says which keys to add.

The Stories and reels pipeline needs the Facebook token and `ffmpeg`. Without them, nothing is scheduled.
