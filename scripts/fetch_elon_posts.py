"""
Fetch Elon Musk's posts from X.com, filter out politics/promos/low-effort,
and generate a mobile-friendly HTML report with embedded tweet previews.

Uses Twitter API v2 via tweepy + Twitter oEmbed API for rich embeds.
"""

import os
import sys
import json
import re
import html as html_module
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import tweepy
import requests


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ELON_USERNAME = "elonmusk"
MAX_RESULTS = 100  # max per request (API limit)
LOOKBACK_HOURS = 36  # slightly more than 24h to avoid gaps
MIN_TEXT_LENGTH = 15  # minimum chars of actual text (after removing URLs/emoji)

# Keywords / patterns used for filtering
POLITICS_KEYWORDS = [
    r"\btrump\b", r"\bbiden\b", r"\bdemocrat", r"\brepublican",
    r"\bmaga\b", r"\bgop\b", r"\bliberal\b", r"\bconservat",
    r"\bimmigra", r"\bsocialist", r"\bmarxist", r"\bfascis",
    r"\belection\b", r"\bvote\b", r"\bsenate\b", r"\bcongress\b",
    r"\bwhite\s*house\b", r"\bdoge\b", r"\bgovernment\s*efficiency",
    r"\bdeportation", r"\bborder\b", r"\bwoke\b", r"\bdei\b",
    r"\bpolitical", r"\bparty\b", r"\bright[\s-]wing", r"\bleft[\s-]wing",
    r"\bexecutive\s*order", r"\bregulation", r"\blobby",
    r"\bgeopoliti", r"\bsanction", r"\btariff",
]

PROMO_KEYWORDS = [
    r"\bbuy\s+now\b", r"\border\s+now\b", r"\bpre[\s-]?order\b",
    r"\blaunch(ing|ed)?\b", r"\bavailable\s+now\b",
    r"\bnew\s+feature\b", r"\bupdate\s+(is|now)\b",
    r"\bsubscribe\b", r"\bpremium\b", r"\bx\s+premium\b",
    r"\bgrok\b.*\b(try|check|amazing|feature)\b",
    r"\btesla\b.*\b(order|buy|price|deliver|model)\b",
    r"\bstarlink\b.*\b(order|available|sign\s*up)\b",
    r"\bneuralink\b.*\b(apply|waitlist)\b",
    r"\bdownload\b", r"\bapp\s+store\b",
    r"\bcheck\s+(it\s+)?out\b",
]

POLITICS_RE = re.compile("|".join(POLITICS_KEYWORDS), re.IGNORECASE)
PROMO_RE = re.compile("|".join(PROMO_KEYWORDS), re.IGNORECASE)

# Regex to strip URLs and emoji for length check
URL_RE = re.compile(r"https?://\S+")
EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U0001f900-\U0001f9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_political(text: str) -> bool:
    matches = len(POLITICS_RE.findall(text))
    return matches >= 2


def is_promotional(text: str) -> bool:
    matches = len(PROMO_RE.findall(text))
    return matches >= 2


def is_low_effort(text: str) -> bool:
    """Filter out single-word replies, emoji-only, 'Yes', 'Yup', etc."""
    stripped = URL_RE.sub("", text)
    stripped = EMOJI_RE.sub("", stripped)
    stripped = stripped.strip()
    return len(stripped) < MIN_TEXT_LENGTH


def virality_score(metrics: dict) -> str:
    """Return a virality label based on engagement."""
    retweets = metrics.get("retweet_count", 0)
    likes = metrics.get("like_count", 0)
    replies = metrics.get("reply_count", 0)
    score = retweets * 3 + likes + replies * 2

    if score > 500_000:
        return "🔥🔥🔥 Mega Viral"
    elif score > 200_000:
        return "🔥🔥 Very Viral"
    elif score > 50_000:
        return "🔥 Viral"
    elif score > 10_000:
        return "📈 Trending"
    else:
        return "📊 Normal"


def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def summarize(text: str, max_len: int = 200) -> str:
    """Clean up tweet text for summary display."""
    text = URL_RE.sub("", text).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "..."


def get_oembed(tweet_url: str, theme: str = "dark") -> str | None:
    """Fetch Twitter oEmbed HTML for a tweet. Returns embed HTML or None."""
    try:
        resp = requests.get(
            "https://publish.twitter.com/oembed",
            params={
                "url": tweet_url,
                "theme": theme,
                "dnt": "true",
                "omit_script": "true",  # we load widgets.js once at the end
                "maxwidth": 550,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("html")
    except Exception as e:
        print(f"  oEmbed failed for {tweet_url}: {e}")
    return None


# ---------------------------------------------------------------------------
# Fetch posts
# ---------------------------------------------------------------------------

def fetch_posts():
    bearer_token = os.environ.get("TWITTER_BEARER_TOKEN")
    if not bearer_token:
        print("ERROR: TWITTER_BEARER_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    client = tweepy.Client(bearer_token=bearer_token)

    # Get Elon's user ID
    user = client.get_user(username=ELON_USERNAME)
    if not user or not user.data:
        print("ERROR: Could not find user @elonmusk", file=sys.stderr)
        sys.exit(1)
    user_id = user.data.id

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    tweets = client.get_users_tweets(
        id=user_id,
        max_results=MAX_RESULTS,
        start_time=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        tweet_fields=["created_at", "public_metrics", "text", "conversation_id",
                       "referenced_tweets", "in_reply_to_user_id", "author_id"],
        expansions=["referenced_tweets.id", "referenced_tweets.id.author_id"],
        user_fields=["username", "name"],
        exclude=["retweets"],
    )

    if not tweets or not tweets.data:
        return []

    # Build lookup of included tweets (parent tweets he's replying to)
    included_tweets = {}
    included_users = {}
    if tweets.includes:
        for u in tweets.includes.get("users", []):
            included_users[u.id] = {"username": u.username, "name": u.name}
        for t in tweets.includes.get("tweets", []):
            author = included_users.get(t.author_id, {})
            included_tweets[t.id] = {
                "id": t.id,
                "text": t.text,
                "username": author.get("username", "unknown"),
                "name": author.get("name", "Unknown"),
            }

    posts = []
    for tweet in tweets.data:
        text = tweet.text
        metrics = tweet.public_metrics or {}

        if is_political(text):
            continue
        if is_promotional(text):
            continue

        # Keep low-effort posts if they're highly engaged (50K+ score)
        score = metrics.get("retweet_count", 0) * 3 + metrics.get("like_count", 0) + metrics.get("reply_count", 0) * 2
        if is_low_effort(text) and score < 50_000:
            continue

        # Find parent tweet context if this is a reply
        parent_context = None
        if tweet.referenced_tweets:
            for ref in tweet.referenced_tweets:
                if ref.type == "replied_to" and ref.id in included_tweets:
                    parent = included_tweets[ref.id]
                    parent_context = {
                        "id": parent["id"],
                        "text": summarize(parent["text"], 200),
                        "username": parent["username"],
                        "name": parent["name"],
                        "url": f"https://x.com/{parent['username']}/status/{ref.id}",
                    }
                    break

        posts.append({
            "id": tweet.id,
            "text": text,
            "summary": summarize(text),
            "created_at": tweet.created_at.strftime("%b %d, %I:%M %p UTC"),
            "url": f"https://x.com/elonmusk/status/{tweet.id}",
            "retweets": metrics.get("retweet_count", 0),
            "likes": metrics.get("like_count", 0),
            "replies": metrics.get("reply_count", 0),
            "impressions": metrics.get("impression_count", 0),
            "virality": virality_score(metrics),
            "parent": parent_context,
        })

    # Sort by engagement (retweets weighted most)
    posts.sort(key=lambda p: p["retweets"] * 3 + p["likes"] + p["replies"] * 2, reverse=True)
    return posts[:20]  # top 20


def fetch_embeds(posts: list) -> None:
    """Fetch oEmbed HTML for each post and its parent (in-place)."""
    for i, p in enumerate(posts):
        print(f"  Fetching embed {i+1}/{len(posts)}: {p['url']}")
        p["embed_html"] = get_oembed(p["url"])

        if p.get("parent"):
            print(f"    Fetching parent embed: {p['parent']['url']}")
            p["parent"]["embed_html"] = get_oembed(p["parent"]["url"])

        # Small delay to avoid rate limiting on oEmbed endpoint
        if i < len(posts) - 1:
            time.sleep(0.3)


# ---------------------------------------------------------------------------
# Generate HTML
# ---------------------------------------------------------------------------

def generate_html(posts: list) -> str:
    now = datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC")

    if not posts:
        cards = '<div class="empty">No substantive posts found in the last 36 hours.</div>'
    else:
        cards = ""
        for i, p in enumerate(posts, 1):
            summary_escaped = html_module.escape(p['summary'])

            # Parent context: use oEmbed if available, else fallback to text
            parent_html = ""
            if p.get("parent"):
                parent = p["parent"]
                if parent.get("embed_html"):
                    parent_html = f"""
                <div class="reply-context">
                    <div class="reply-label">↩ Replying to @{html_module.escape(parent['username'])}</div>
                    <div class="parent-embed">{parent['embed_html']}</div>
                </div>"""
                else:
                    parent_text = html_module.escape(parent["text"])
                    parent_html = f"""
                <div class="reply-context">
                    <div class="reply-label">↩ Replying to @{html_module.escape(parent['username'])}</div>
                    <a href="{parent['url']}" target="_blank" rel="noopener" class="parent-tweet-fallback">
                        <div class="parent-author">
                            <strong>{html_module.escape(parent['name'])}</strong>
                            <span class="parent-handle">@{html_module.escape(parent['username'])}</span>
                        </div>
                        <div class="parent-text">{parent_text}</div>
                    </a>
                </div>"""

            # Main tweet: use oEmbed if available, else fallback
            if p.get("embed_html"):
                tweet_html = f'<div class="tweet-embed">{p["embed_html"]}</div>'
            else:
                tweet_html = f"""
                <div class="tweet-embed-fallback">
                    <div class="tweet-author">
                        <strong>Elon Musk</strong> <span class="tweet-handle">@elonmusk</span>
                    </div>
                    <div class="tweet-text">{summary_escaped}</div>
                    <div class="tweet-date">{p['created_at']}</div>
                </div>"""

            cards += f"""
            <div class="card">
                <div class="card-header">
                    <span class="rank">#{i}</span>
                    <span class="virality-badge">{p['virality']}</span>
                </div>
                {parent_html}
                {tweet_html}
                <div class="card-footer">
                    <div class="stats-row">
                        <span title="Retweets">🔁 {format_number(p['retweets'])}</span>
                        <span title="Likes">❤️ {format_number(p['likes'])}</span>
                        <span title="Replies">💬 {format_number(p['replies'])}</span>
                    </div>
                    <a href="{p['url']}" target="_blank" rel="noopener" class="open-btn">Open on 𝕏 ↗</a>
                </div>
            </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Elon Feed">
<title>Elon Musk - Top Substantive Posts</title>
<style>
  :root {{
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --reply-bg: #1c2128;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 16px;
    padding-top: env(safe-area-inset-top, 16px);
    -webkit-text-size-adjust: 100%;
    max-width: 700px;
    margin: 0 auto;
  }}
  header {{
    text-align: center;
    padding: 20px 0 10px;
  }}
  header h1 {{
    font-size: 1.4em;
    font-weight: 700;
  }}
  header .subtitle {{
    color: var(--muted);
    font-size: 0.85em;
    margin-top: 4px;
  }}
  .info-bar {{
    display: flex;
    justify-content: space-between;
    color: var(--muted);
    font-size: 0.75em;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 16px;
  }}
  .empty {{
    text-align: center;
    padding: 40px;
    color: var(--muted);
  }}

  /* Card layout */
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    margin-bottom: 16px;
    overflow: hidden;
  }}
  .card-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 16px 0;
  }}
  .rank {{
    font-weight: 700;
    color: var(--accent);
    font-size: 0.9em;
  }}
  .virality-badge {{
    font-size: 0.78em;
    white-space: nowrap;
  }}

  /* Reply context */
  .reply-context {{
    margin: 10px 16px 0;
  }}
  .reply-label {{
    color: var(--muted);
    font-size: 0.8em;
    margin-bottom: 6px;
    font-weight: 600;
  }}
  .parent-embed {{
    border-radius: 10px;
    overflow: hidden;
  }}
  .parent-embed .twitter-tweet {{
    margin: 0 !important;
  }}
  .parent-tweet-fallback {{
    display: block;
    background: var(--reply-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 14px;
    text-decoration: none;
    color: var(--text);
  }}
  .parent-tweet-fallback:hover {{
    border-color: var(--accent);
  }}
  .parent-author {{
    font-size: 0.82em;
    margin-bottom: 4px;
  }}
  .parent-handle {{
    color: var(--muted);
    margin-left: 4px;
  }}
  .parent-text {{
    font-size: 0.85em;
    color: var(--muted);
    line-height: 1.4;
  }}

  /* Tweet embed area */
  .tweet-embed {{
    padding: 8px 16px;
  }}
  .tweet-embed .twitter-tweet {{
    margin: 0 !important;
  }}

  /* Fallback styling for when oEmbed fails */
  .tweet-embed-fallback {{
    padding: 12px 16px;
  }}
  .tweet-author {{
    font-size: 0.85em;
    margin-bottom: 6px;
  }}
  .tweet-handle {{
    color: var(--muted);
    margin-left: 4px;
  }}
  .tweet-text {{
    font-size: 1em;
    line-height: 1.5;
    margin-bottom: 6px;
  }}
  .tweet-date {{
    color: var(--muted);
    font-size: 0.78em;
  }}

  /* Footer of each card */
  .card-footer {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 16px 14px;
    border-top: 1px solid var(--border);
  }}
  .stats-row {{
    display: flex;
    gap: 14px;
    font-size: 0.8em;
  }}
  .open-btn {{
    color: var(--accent);
    text-decoration: none;
    font-size: 0.85em;
    font-weight: 600;
    padding: 6px 14px;
    border: 1px solid var(--accent);
    border-radius: 8px;
    white-space: nowrap;
  }}
  .open-btn:hover {{
    background: var(--accent);
    color: var(--bg);
  }}

  footer {{
    text-align: center;
    color: var(--muted);
    font-size: 0.7em;
    padding: 20px 0;
  }}
</style>
</head>
<body>

<header>
  <h1>Elon Musk - Substantive Posts</h1>
  <div class="subtitle">Filtered: No politics, no promos &mdash; just real takes</div>
</header>

<div class="info-bar">
  <span>Updated: {now}</span>
  <span>Top {len(posts)} posts (last 36h)</span>
</div>

{cards}

<footer>
  Auto-updated daily at 7:00 AM UTC &bull; Powered by GitHub Actions<br>
  Add to Home Screen for app-like access on iPhone
</footer>

<script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Fetching Elon Musk's posts...")
    posts = fetch_posts()
    print(f"Found {len(posts)} substantive posts after filtering")

    print("Fetching tweet embeds via oEmbed API...")
    fetch_embeds(posts)

    html = generate_html(posts)
    out_dir = Path("docs")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Report written to {out_path}")

    # Also save raw JSON for potential further use
    json_path = out_dir / "posts.json"
    json_path.write_text(json.dumps(posts, indent=2, default=str), encoding="utf-8")
    print(f"JSON data written to {json_path}")


if __name__ == "__main__":
    main()
