"""
Fetch Elon Musk's posts from X.com, filter for business/building relevance,
and generate a mobile-friendly HTML report with AI-powered summaries.

Uses Twitter API v2 via tweepy, Claude API for summaries, Twitter oEmbed for embeds.
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
import anthropic


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ELON_USERNAME = "elonmusk"
MAX_RESULTS = 100
LOOKBACK_HOURS = 36
MIN_TEXT_LENGTH = 15

# --- NEGATIVE filters: things to exclude ---

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
    r"\bpresident\b", r"\bgovernor\b", r"\bpelosi\b", r"\baoc\b",
    r"\bcongressman", r"\bcongresswoman", r"\bsenator\b",
]

SOCIAL_KEYWORDS = [
    r"\bwoke\b", r"\bgender\b", r"\btrans\b", r"\blgbt",
    r"\bpronoun", r"\bfeminis", r"\bpatriarch",
    r"\bracis[mt]", r"\bwhite\s*supremac", r"\bblack\s*lives",
    r"\bcancel\s*culture", r"\bcrt\b", r"\bcritical\s*race",
    r"\bvaccin", r"\banti[\s-]?vax", r"\bcovid\s*hoax",
    r"\bconspiracy", r"\bflat\s*earth",
    r"\bimmigrant", r"\billegal\s*alien",
    r"\bgun\s*control", r"\bsecond\s*amendment", r"\b2a\b",
    r"\babortion", r"\bpro[\s-]?life", r"\bpro[\s-]?choice",
    r"\breligion\b", r"\bchristian\b.*\bvalues",
    r"\bmainstream\s*media", r"\bmsm\b", r"\bfake\s*news",
    r"\bpropaganda\b", r"\bindoctrinat",
    r"\bculture\s*war", r"\bvirtue\s*signal",
]

PROMO_KEYWORDS = [
    r"\bbuy\s+now\b", r"\border\s+now\b", r"\bpre[\s-]?order\b",
    r"\bavailable\s+now\b",
    r"\bnew\s+feature\b", r"\bupdate\s+(is|now)\b",
    r"\bsubscribe\b", r"\bx\s+premium\b",
    r"\bdownload\b", r"\bapp\s+store\b",
    r"\bcheck\s+(it\s+)?out\b",
]

POLITICS_RE = re.compile("|".join(POLITICS_KEYWORDS), re.IGNORECASE)
SOCIAL_RE = re.compile("|".join(SOCIAL_KEYWORDS), re.IGNORECASE)
PROMO_RE = re.compile("|".join(PROMO_KEYWORDS), re.IGNORECASE)

# --- POSITIVE filter: business/building relevance ---

BUSINESS_KEYWORDS = [
    r"\btesla\b", r"\bspacex\b", r"\bstarship\b", r"\bfalcon",
    r"\bstarlink\b", r"\bneuralink\b", r"\bboring\s*company",
    r"\bxai\b", r"\bgrok\b", r"\boptimus\b",
    r"\bengine", r"\brocket\b", r"\borbit", r"\bmars\b",
    r"\bmanufactur", r"\bfactor[yi]", r"\bproduction\b",
    r"\bscal(e|ing)\b", r"\bgrowth\b", r"\brevenue\b",
    r"\bprofit\b", r"\bmargin\b", r"\bcost\b",
    r"\binnovati", r"\bengineer", r"\bdesign\b",
    r"\bai\b", r"\bartificial\s*intellig", r"\bmachine\s*learn",
    r"\bneural\s*net", r"\bllm\b", r"\bmodel\b.*\btrain",
    r"\bautonom", r"\bself[\s-]driv", r"\bfsd\b",
    r"\bbatter[yi]", r"\benergy\b", r"\bsolar\b",
    r"\bstartup", r"\bfound(er|ing)\b", r"\bcompan[yi]",
    r"\bleadershi", r"\bmanage", r"\bhir(e|ing)\b",
    r"\bstrateg", r"\bexecut(e|ion)\b",
    r"\bship(ping|ped)?\b", r"\blaunch(ed|ing)?\b",
    r"\bbuild(ing)?\b", r"\bcreate\b", r"\bsolv(e|ing)\b",
    r"\boptimiz", r"\befficienc", r"\bautomati",
    r"\bsoftware\b", r"\bhardware\b", r"\bchip\b",
    r"\bsemiconduct", r"\bcompute\b", r"\bdata\s*center",
    r"\bfirst\s*principles", r"\bphysics\b",
    r"\bcapital\b", r"\binvest", r"\bfunding\b", r"\bipo\b",
    r"\bvaluation\b", r"\bmarket\b",
]

BUSINESS_RE = re.compile("|".join(BUSINESS_KEYWORDS), re.IGNORECASE)

# Regex to strip URLs and emoji for length check
URL_RE = re.compile(r"https?://\S+")
EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0\U0001f900-\U0001f9FF"
    "\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------

def is_political(text: str) -> bool:
    return len(POLITICS_RE.findall(text)) >= 1


def is_social_commentary(text: str) -> bool:
    return len(SOCIAL_RE.findall(text)) >= 1


def is_promotional(text: str) -> bool:
    return len(PROMO_RE.findall(text)) >= 2


def is_low_effort(text: str) -> bool:
    stripped = URL_RE.sub("", text)
    stripped = EMOJI_RE.sub("", stripped)
    stripped = stripped.strip()
    return len(stripped) < MIN_TEXT_LENGTH


def has_business_signal(text: str) -> bool:
    return len(BUSINESS_RE.findall(text)) >= 1


# ---------------------------------------------------------------------------
# Scoring & formatting helpers
# ---------------------------------------------------------------------------

def engagement_score(metrics: dict) -> int:
    return (metrics.get("retweet_count", 0) * 3
            + metrics.get("like_count", 0)
            + metrics.get("reply_count", 0) * 2)


def virality_label(score: int) -> str:
    if score > 500_000:
        return "🔥🔥🔥 Mega Viral"
    elif score > 200_000:
        return "🔥🔥 Very Viral"
    elif score > 50_000:
        return "🔥 Viral"
    elif score > 10_000:
        return "📈 Trending"
    return "📊 Normal"


def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def clean_text(text: str, max_len: int = 200) -> str:
    text = URL_RE.sub("", text).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "..."


def get_oembed(tweet_url: str) -> str | None:
    try:
        resp = requests.get(
            "https://publish.twitter.com/oembed",
            params={"url": tweet_url, "theme": "dark", "dnt": "true",
                    "omit_script": "true", "maxwidth": 550},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("html")
    except Exception as e:
        print(f"  oEmbed failed for {tweet_url}: {e}")
    return None


# ---------------------------------------------------------------------------
# AI Summaries via Claude
# ---------------------------------------------------------------------------

def generate_summaries(posts: list) -> None:
    """Use Claude Haiku to generate a business-insight summary for each post."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set — skipping AI summaries")
        for p in posts:
            p["ai_summary"] = None
        return

    client = anthropic.Anthropic(api_key=api_key)

    for i, p in enumerate(posts):
        # Build context including parent tweet if available
        context = f"Elon Musk posted: \"{p['text']}\""
        if p.get("parent"):
            context = (f"Someone (@{p['parent']['username']}) posted: "
                       f"\"{p['parent']['text']}\"\n\n"
                       f"Elon Musk replied: \"{p['text']}\"")

        prompt = f"""Analyze this post from Elon Musk and write a 2-3 sentence summary aimed at entrepreneurs and business operators.

{context}

Your summary should:
1. Explain WHAT Elon is saying (provide context if it's a reply or cryptic)
2. Extract the business insight, strategy, or lesson for entrepreneurs
3. If relevant, mention which company/product it relates to (Tesla, SpaceX, xAI, etc.)

If the post has no meaningful business insight (it's just a joke, meme, or casual comment), say so briefly and note any context.

Write in a direct, concise style. No fluff. Start directly with the insight."""

        try:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            p["ai_summary"] = message.content[0].text.strip()
            print(f"  Summary {i+1}/{len(posts)}: OK")
        except Exception as e:
            print(f"  Summary {i+1}/{len(posts)}: FAILED - {e}")
            p["ai_summary"] = None

        if i < len(posts) - 1:
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# Fetch posts
# ---------------------------------------------------------------------------

def fetch_posts():
    bearer_token = os.environ.get("TWITTER_BEARER_TOKEN")
    if not bearer_token:
        print("ERROR: TWITTER_BEARER_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    client = tweepy.Client(bearer_token=bearer_token)

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

    # Build lookup of included tweets
    included_tweets = {}
    included_users = {}
    if tweets.includes:
        for u in tweets.includes.get("users", []):
            included_users[u.id] = {"username": u.username, "name": u.name}
        for t in tweets.includes.get("tweets", []):
            author = included_users.get(t.author_id, {})
            included_tweets[t.id] = {
                "id": t.id, "text": t.text,
                "username": author.get("username", "unknown"),
                "name": author.get("name", "Unknown"),
            }

    posts = []
    for tweet in tweets.data:
        text = tweet.text
        metrics = tweet.public_metrics or {}
        score = engagement_score(metrics)

        # Hard filters: always exclude politics and social commentary
        # Check both the tweet AND parent context for political content
        parent_text = ""
        parent_context = None
        if tweet.referenced_tweets:
            for ref in tweet.referenced_tweets:
                if ref.type == "replied_to" and ref.id in included_tweets:
                    parent = included_tweets[ref.id]
                    parent_text = parent.get("text", "")
                    parent_context = {
                        "id": parent["id"],
                        "text": clean_text(parent["text"], 300),
                        "username": parent["username"],
                        "name": parent["name"],
                        "url": f"https://x.com/{parent['username']}/status/{ref.id}",
                    }
                    break

        combined_text = text + " " + parent_text
        if is_political(combined_text):
            continue
        if is_social_commentary(combined_text):
            continue
        if is_promotional(text):
            continue

        # Low-effort posts: keep only if viral
        if is_low_effort(text) and score < 50_000:
            continue

        # Prefer posts with business relevance; allow high-viral through
        if not has_business_signal(combined_text) and score < 50_000:
            continue

        posts.append({
            "id": tweet.id,
            "text": text,
            "summary": clean_text(text),
            "created_at": tweet.created_at.strftime("%b %d, %I:%M %p UTC"),
            "url": f"https://x.com/elonmusk/status/{tweet.id}",
            "retweets": metrics.get("retweet_count", 0),
            "likes": metrics.get("like_count", 0),
            "replies": metrics.get("reply_count", 0),
            "impressions": metrics.get("impression_count", 0),
            "score": score,
            "virality": virality_label(score),
            "parent": parent_context,
        })

    posts.sort(key=lambda p: p["score"], reverse=True)
    return posts[:20]


def fetch_embeds(posts: list) -> None:
    """Fetch oEmbed HTML for each post and its parent."""
    for i, p in enumerate(posts):
        print(f"  Embed {i+1}/{len(posts)}: {p['url']}")
        p["embed_html"] = get_oembed(p["url"])
        if p.get("parent"):
            p["parent"]["embed_html"] = get_oembed(p["parent"]["url"])
        if i < len(posts) - 1:
            time.sleep(0.3)


# ---------------------------------------------------------------------------
# Generate HTML
# ---------------------------------------------------------------------------

def generate_html(posts: list) -> str:
    now = datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC")

    if not posts:
        cards = '<div class="empty">No business-relevant posts found in the last 36 hours.</div>'
    else:
        cards = ""
        for i, p in enumerate(posts, 1):
            summary_escaped = html_module.escape(p['summary'])

            # AI summary block
            ai_block = ""
            if p.get("ai_summary"):
                ai_text = html_module.escape(p["ai_summary"])
                ai_block = f"""
                <div class="ai-summary">
                    <div class="ai-label">💡 Business Insight</div>
                    <p>{ai_text}</p>
                </div>"""

            # Parent context
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

            # Main tweet embed
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
                {ai_block}
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
<title>Elon Musk - Business Insights</title>
<style>
  :root {{
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --reply-bg: #1c2128;
    --insight-bg: #1a2332;
    --insight-border: #1f6feb44;
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

  /* AI Summary */
  .ai-summary {{
    margin: 10px 16px 0;
    background: var(--insight-bg);
    border: 1px solid var(--insight-border);
    border-radius: 10px;
    padding: 12px 14px;
  }}
  .ai-label {{
    font-size: 0.75em;
    font-weight: 700;
    color: var(--accent);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .ai-summary p {{
    font-size: 0.88em;
    line-height: 1.5;
    color: var(--text);
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
  .parent-embed .twitter-tweet {{ margin: 0 !important; }}
  .parent-tweet-fallback {{
    display: block;
    background: var(--reply-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 14px;
    text-decoration: none;
    color: var(--text);
  }}
  .parent-tweet-fallback:hover {{ border-color: var(--accent); }}
  .parent-author {{ font-size: 0.82em; margin-bottom: 4px; }}
  .parent-handle {{ color: var(--muted); margin-left: 4px; }}
  .parent-text {{ font-size: 0.85em; color: var(--muted); line-height: 1.4; }}

  .tweet-embed {{ padding: 8px 16px; }}
  .tweet-embed .twitter-tweet {{ margin: 0 !important; }}

  .tweet-embed-fallback {{ padding: 12px 16px; }}
  .tweet-author {{ font-size: 0.85em; margin-bottom: 6px; }}
  .tweet-handle {{ color: var(--muted); margin-left: 4px; }}
  .tweet-text {{ font-size: 1em; line-height: 1.5; margin-bottom: 6px; }}
  .tweet-date {{ color: var(--muted); font-size: 0.78em; }}

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
  <h1>Elon Musk - Business Insights</h1>
  <div class="subtitle">Filtered for business, building & strategy &mdash; no politics, no social commentary</div>
</header>

<div class="info-bar">
  <span>Updated: {now}</span>
  <span>Top {len(posts)} posts (last 36h)</span>
</div>

{cards}

<footer>
  Auto-updated daily at 7:00 AM UTC &bull; Powered by GitHub Actions + Claude AI<br>
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
    print(f"Found {len(posts)} business-relevant posts after filtering")

    if posts:
        print("Generating AI summaries via Claude...")
        generate_summaries(posts)

        print("Fetching tweet embeds via oEmbed API...")
        fetch_embeds(posts)

    html = generate_html(posts)
    out_dir = Path("docs")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Report written to {out_path}")

    json_path = out_dir / "posts.json"
    json_path.write_text(json.dumps(posts, indent=2, default=str), encoding="utf-8")
    print(f"JSON data written to {json_path}")


if __name__ == "__main__":
    main()
