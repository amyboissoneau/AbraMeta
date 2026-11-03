# Putting the judge demo online (Render, step by step)

**Not built or tested yet.** The Docker build has not been run on this Mac, so do not point judges at a hosted link until it has been checked.

**Optional.** Hosting is not required for judging. Judges work from the demo video and the
repo's README steps. Use this only if you want a clickable sample link too.

This puts a sample copy of Abra on the internet so judges can open a link.
It has no bot, no connection to any real Facebook page, and none of your keys.
Browsing the sample needs no keys. Only Create plan, reaction plan and schedule analysis accept the
judge's own Muse or Claude keys; the keys are optional and everything else is turned off in the judge copy.

## Before you start
- Do NOT copy your `.env` file or any API key into Render. The demo must never hold your keys.
- The GitHub repo must contain the latest commit (the owner pushes it).

## 1. Create a Render account
1. Go to https://render.com and sign up (free).
2. Connect your GitHub account when asked.

## 2. Create the web service
1. Click **New +** then **Web Service**.
2. Pick the Abra repository.
3. Runtime: **Docker** (Render finds the `Dockerfile` in the repo root).
4. Instance type: **Free**.
5. Health check path: `/`
6. Environment variables: **add none**. The Dockerfile already sets `ABRA_JUDGE_MODE=1`.
7. Click **Create Web Service** and wait for the build to finish (5 to 10 minutes the first time).

## 3. Check it
1. Open the `onrender.com` link Render gives you.
2. You should see the Abra login card with "Continue as Demo Creator" and "Try Abra now".
3. Click **Try Abra now**. The top banner should say the data is sample data.

## Things to know
- **Free instances sleep** after about 15 minutes with no visitors. The first load after that can take 30 to 60 seconds. Tell judges to wait.
- **The sample data resets** every time the service restarts or redeploys. That's expected.
- **Judges' keys are kept in memory only.** A restart clears them.
- **To update the demo**, push a new commit. Render rebuilds it.
- **If the build fails**, copy the last 30 lines of the build log to me. The Dockerfile stops on purpose if anything personal is in the build.

## Turning it off
Render dashboard, your service, **Settings**, then **Suspend** (or delete the service). Your Mac copy is not affected.
