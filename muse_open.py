"""Meta OPEN-WEIGHT model triage — provider-agnostic (Baseten / Fireworks / Modal).

Why this exists: the hackathon's "Best Build on Meta Open-Weight Models" prize ($10K) requires
running a Meta open-weight model (e.g. Muse Spark Open / Muse Glimmer / Llama), and using an
open-weight model also lifts the "Use of Meta AI Tooling" score (25%). This routes the
comment-triage first pass through an open-weight model instead of the hosted Muse Spark, with a
graceful fallback to the hosted path.

This module sends one OpenAI-style chat-completions request shape to each provider, so it works with any of them once its key is set. You only need ONE for the prize. Pick whichever credits you get:

  - Baseten  (recommended — hosts Meta's "Muse Spark Open" by name):
      BASETEN_API_KEY=...        model default: meta-models/muse-spark-open
  - Fireworks (hosts Meta's Llama open weights):
      FIREWORKS_API_KEY=...      model default: accounts/fireworks/models/llama-v3p1-8b-instruct
  - Modal     (you deploy your own OpenAI-compatible endpoint; set its URL):
      MODAL_API_KEY=... and MODAL_BASE_URL=https://<your-modal-endpoint>/v1/chat/completions

Selection: set OPENWEIGHT_PROVIDER=baseten|fireworks|modal, or leave it unset and the first
provider whose key is present (in that order) is used. Per-provider model override envs:
BASETEN_MODEL / FIREWORKS_MODEL / MODAL_MODEL.

STATUS: wired into bot.muse_triage (spark_open() is tried first, with a fallback to _muse_call()). It does nothing until a provider key is set.

TO ACTIVATE:
  1. Claim the Baseten hackathon credits (Fireworks and Modal set their own credit terms).
  2. (Baseten) enable "Muse Spark Open" in the dashboard; (Fireworks) nothing to enable;
     (Modal) deploy a vLLM/OpenAI endpoint for a Meta open-weight model and copy its URL.
  3. Put the key (and MODAL_BASE_URL if using Modal) in .env.
  4. Verify:  ./venv/bin/python muse_open.py   (should print EASY / ENGAGE / SKIP)
  5. Then wire spark_open() into bot.muse_triage with a fallback to _muse_call().
"""
import os
import requests

import meta_live

# provider -> (chat-completions URL, API-key env name, default model, model-override env name)
_PROVIDERS = {
    "baseten": ("https://inference.baseten.co/v1/chat/completions",
                "BASETEN_API_KEY", "meta-models/muse-spark-open", "BASETEN_MODEL"),
    "fireworks": ("https://api.fireworks.ai/inference/v1/chat/completions",
                  "FIREWORKS_API_KEY", "accounts/fireworks/models/llama-v3p1-8b-instruct",
                  "FIREWORKS_MODEL"),
    # Modal has no fixed URL — you deploy your own endpoint, so MODAL_BASE_URL is required.
    "modal": (os.environ.get("MODAL_BASE_URL", ""),
              "MODAL_API_KEY", os.environ.get("MODAL_MODEL", "muse-spark-open"), "MODAL_MODEL"),
}
_ORDER = ("baseten", "fireworks", "modal")


def _providers():
    """Every usable provider, in preference order (OPENWEIGHT_PROVIDER first if set), as a list
    of (name, url, key, model). Supports running on one — or several, as a resilient chain."""
    chosen = os.environ.get("OPENWEIGHT_PROVIDER", "").strip().lower()
    order = ([chosen] if chosen in _PROVIDERS else []) + [p for p in _ORDER if p != chosen]
    out = []
    for name in order:
        url, key_env, default_model, model_env = _PROVIDERS[name]
        key = os.environ.get(key_env, "").strip()
        if key and url:  # Modal also needs its URL set
            model = os.environ.get(model_env, "").strip() or default_model
            out.append((name, url, key, model))
    return out


def available() -> bool:
    return len(_providers()) > 0


def active_provider() -> str:
    p = _providers()
    return p[0][0] if p else ""


def providers_configured() -> list:
    return [name for name, *_ in _providers()]


def spark_open(instructions: str, input_text: str, timeout: int = 25,
               max_tokens: int = 500) -> str:
    """Open-weight chat call, tried across every configured provider in order until one answers.
    Returns text, or '' if all fail — so callers can fall back to the hosted model. Mirrors
    bot._muse_call's signature. With several keys set it's a resilient Baseten→Fireworks→Modal chain."""
    for name, url, key, model in _providers():
        try:
            r = meta_live.post(model, "triage (open weights)",
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": instructions},
                        {"role": "user", "content": input_text},
                    ],
                },
                timeout=timeout,
            )
            r.raise_for_status()
            j = r.json()
            text = (j.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
            if text:
                return text  # first provider that answers wins
        except Exception as e:
            print(f"[muse_open:{name}] failed, trying next provider: {str(e)[:120]}")
            continue
    return ""


if __name__ == "__main__":
    if not available():
        print("No open-weight provider configured. Set one of BASETEN_API_KEY / FIREWORKS_API_KEY / "
              "(MODAL_API_KEY + MODAL_BASE_URL) in .env, then re-run.  See the header of this file.")
        raise SystemExit(1)
    print(f"providers (in fallback order): {', '.join(providers_configured())}")
    triage_system = (
        "You are a fast first-pass comment screener for a Facebook creator page. "
        "Classify the comment. Answer with exactly one word: EASY (short pure praise/reaction), "
        "ENGAGE (a question, opinion, story, criticism — anything with substance), or "
        "SKIP (spam/scam/links, slurs/hate, harassment, threats, gibberish). "
        "If unsure, answer ENGAGE. One word only."
    )
    for sample in ["so cute 😂😂", "wait how did you actually film this??",
                   "FREE MONEY click my profile link now"]:
        verdict = spark_open(triage_system, sample)
        print(f"{verdict or '(no response)':8}  <-  {sample}")
    print("\nIf the three lines above read EASY / ENGAGE / SKIP, the open-weight model works. "
          "Open-weight triage is already wired into bot.muse_triage, with a fallback to _muse_call().")
