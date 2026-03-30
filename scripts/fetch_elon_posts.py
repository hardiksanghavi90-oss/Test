"""
Fetch Elon Musk's posts from X.com, filter out politics/promos,
and generate a mobile-friendly HTML report.

Uses Twitter API v2 via tweepy.
"""

import os
import sys
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import tweepy


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ELON_USERNAME = "elonmusk"
MAX_RESULTS = 100  # max per request (API limit)
LOOKBACK_HOURS = 36  # slightly more than 24h to avoid gaps

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_political(text: str) -> bool:
    matches = len(POLITICS_RE.findall(text))
    return matches >= 2  # need 2+ signals to flag as political


def is_promotional(text: str) -> bool:
    matches = len(PROMO_RE.findall(text))
    return matches >= 2


def virality_score(metrics: dict) -> str:
    """Return a virality label based on engagement."""
    retweets = metrics.get("retweet_count", 0)
    likes = metrics.get("like_count", 0)
    replies = metrics.get("reply_count", 0)
    impressions = metrics.get("impression_count", 0)
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


def summarize(text: str, max_len: int = 140) -> str:
    """Simple summarizer: first sentence or truncated text."""
    text = re.sub(r"https?://\S+", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "..."


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
        tweet_fields=["created_at", "public_metrics", "text", "conversation_id"],
        exclude=["retweets", "replies"],
    )

    if not tweets or not tweets.data:
        return []

    posts = []
    for tweet in tweets.data:
        text = tweet.text
        metrics = tweet.public_metrics or {}

        if is_political(text):
            continue
        if is_promotional(text):
            continue

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
        })

    # Sort by engagement (retweets weighted most)
    posts.sort(key=lambda p: p["retweets"] * 3 + p["likes"] + p["replies"] * 2, reverse=True)
    return posts[:20]  # top 20


# ---------------------------------------------------------------------------
# Generate HTML
# ---------------------------------------------------------------------------

def generate_html(posts: list) -> str:
    now = datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC")

    if not posts:
        rows = '<tr><td colspan="5" style="text-align:center;padding:40px;color:#888;">No substantive posts found in the last 36 hours.</td></tr>'
    else:
        rows = ""
        for i, p in enumerate(posts, 1):
            rows += f"""
            <tr>
                <td class="rank">{i}</td>
                <td class="summary">
                    <a href="{p['url']}" target="_blank" rel="noopener">{p['summary']}</a>
                    <div class="meta">{p['created_at']}</div>
                </td>
                <td class="stats">
                    <span title="Retweets">🔁 {format_number(p['retweets'])}</span><br>
                    <span title="Likes">❤️ {format_number(p['likes'])}</span><br>
                    <span title="Replies">💬 {format_number(p['replies'])}</span>
                </td>
                <td class="virality">{p['virality']}</td>
                <td class="link"><a href="{p['url']}" target="_blank" rel="noopener">Open&nbsp;↗</a></td>
            </tr>"""

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
    --viral: #f78166;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 16px;
    padding-top: env(safe-area-inset-top, 16px);
    -webkit-text-size-adjust: 100%;
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
    margin-bottom: 12px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
  }}
  th {{
    text-align: left;
    color: var(--muted);
    font-size: 0.75em;
    text-transform: uppercase;
    padding: 8px 6px;
    border-bottom: 2px solid var(--border);
    position: sticky;
    top: 0;
    background: var(--bg);
  }}
  tr {{
    border-bottom: 1px solid var(--border);
  }}
  td {{
    padding: 12px 6px;
    vertical-align: top;
    font-size: 0.9em;
  }}
  .rank {{
    font-weight: 700;
    color: var(--muted);
    width: 30px;
    text-align: center;
  }}
  .summary a {{
    color: var(--text);
    text-decoration: none;
    line-height: 1.4;
  }}
  .summary a:hover {{ color: var(--accent); }}
  .meta {{
    color: var(--muted);
    font-size: 0.75em;
    margin-top: 4px;
  }}
  .stats {{
    font-size: 0.78em;
    white-space: nowrap;
    line-height: 1.6;
  }}
  .virality {{
    font-size: 0.78em;
    white-space: nowrap;
  }}
  .link a {{
    color: var(--accent);
    text-decoration: none;
    font-size: 0.85em;
  }}
  /* Mobile: card layout */
  @media (max-width: 600px) {{
    thead {{ display: none; }}
    tr {{
      display: block;
      background: var(--card);
      border-radius: 10px;
      padding: 14px;
      margin-bottom: 10px;
      border: 1px solid var(--border);
    }}
    td {{
      display: block;
      padding: 2px 0;
      border: none;
    }}
    .rank {{
      text-align: left;
      font-size: 0.8em;
      color: var(--accent);
    }}
    .rank::before {{ content: "#"; }}
    .summary {{ font-size: 1em; margin: 6px 0; }}
    .stats {{
      display: flex;
      gap: 12px;
      margin-top: 6px;
    }}
    .virality {{ margin-top: 6px; }}
    .link {{ margin-top: 8px; }}
    .link a {{
      display: inline-block;
      padding: 6px 16px;
      background: var(--accent);
      color: var(--bg);
      border-radius: 8px;
      font-weight: 600;
    }}
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

<table>
<thead>
  <tr>
    <th>#</th>
    <th>Post</th>
    <th>Engagement</th>
    <th>Virality</th>
    <th>Link</th>
  </tr>
</thead>
<tbody>
{rows}
</tbody>
</table>

<footer>
  Auto-updated daily at 7:00 AM UTC &bull; Powered by GitHub Actions<br>
  Add to Home Screen for app-like access on iPhone
</footer>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Fetching Elon Musk's posts...")
    posts = fetch_posts()
    print(f"Found {len(posts)} substantive posts after filtering")

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
