"""Learn the page's real comment-reply style.

Scans the page's recent posts, collects the comment replies the PAGE ITSELF has written (how the creator already responds to fans: up to 100 top-level comments per post, with up to 50 replies under each), and saves up to 40 of them to
style_examples.md. bot.py automatically feeds those examples to Claude so
generated replies imitate the page's genuine voice.

Run this once after setting up .env (and re-run any time you want the bot to
refresh its idea of your style):

    python learn_style.py
"""

from __future__ import annotations

import sys

from bot import (
    BASE_DIR,
    PAGE_ID,
    fetch_recent_posts,
    graph_get,
    validate_setup,
)

STYLE_PATH = BASE_DIR / "style_examples.md"
MAX_EXAMPLES = 40


def collect_page_replies() -> list[dict]:
    """Return [{comment, reply, caption}] for every page-authored reply found."""
    examples: list[dict] = []
    posts = fetch_recent_posts()
    print(f"Scanning {len(posts)} recent posts for the page's own replies...")

    for post in posts:
        caption = (post.get("message") or "").strip()
        try:
            data = graph_get(
                f"{post['id']}/comments",
                fields="id,message,from{id,name},"
                       "comments.limit(50){id,message,from{id,name}}",
                filter="toplevel",
                limit=100,
            )
        except RuntimeError as e:
            print(f"  ! could not read comments on {post['id']}: {e}")
            continue

        for comment in data.get("data", []):
            author = comment.get("from", {}).get("id")
            comment_text = (comment.get("message") or "").strip()

            # Case 1: the page replied to someone's comment
            for child in comment.get("comments", {}).get("data", []):
                if child.get("from", {}).get("id") == PAGE_ID and child.get("message"):
                    examples.append({
                        "comment": comment_text,
                        "reply": child["message"].strip(),
                        "caption": caption,
                    })

            # Case 2: the page's own top-level comment on its post
            if author == PAGE_ID and comment_text:
                examples.append({
                    "comment": "",
                    "reply": comment_text,
                    "caption": caption,
                })

    return examples


def main() -> None:
    validate_setup()
    examples = collect_page_replies()
    if not examples:
        sys.exit(
            "No page-authored replies found in recent posts. Reply to a few "
            "comments manually first, or increase POSTS_TO_MONITOR in .env, "
            "then re-run."
        )

    lines = [
        "# Real reply examples from this page",
        "",
        "These are genuine replies the page has posted. Imitate their tone,",
        "length, warmth, punctuation, and emoji habits as closely as possible.",
        "",
    ]
    for ex in examples[:MAX_EXAMPLES]:
        lines.append("---")
        if ex["caption"]:
            lines.append(f"Post: {ex['caption'][:200]}")
        if ex["comment"]:
            lines.append(f"Fan comment: {ex['comment'][:300]}")
            lines.append(f"Page's reply: {ex['reply']}")
        else:
            lines.append(f"Page's own comment on the post: {ex['reply']}")
        lines.append("")

    STYLE_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved {min(len(examples), MAX_EXAMPLES)} examples to {STYLE_PATH.name}.")
    print("bot.py will now use them automatically — restart it if it's running.")


if __name__ == "__main__":
    main()
