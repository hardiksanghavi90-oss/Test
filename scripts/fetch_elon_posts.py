"""
Fetch Elon Musk's posts from X.com, filter for business/building relevance,
and generate a mobile-friendly HTML report with AI-powered summaries.

Uses Twitter API v2 via tweepy, Claude API for classification + summaries,
Twitter oEmbed for rich embeds.
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
MAX_THREAD_DEPTH = 5  # how many levels up to walk in a reply chain

# Regex helpers
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
# Helpers
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


def clean_text(text: str, max_len: int = 300) -> str:
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
# Walk the reply chain to get full conversation context
# ---------------------------------------------------------------------------

def walk_reply_chain(client: tweepy.Client, tweet, included_tweets: dict,
                     included_users: dict) -> list[dict]:
    """Walk up the reply chain to build conversation context.
    Returns a list of dicts [{username, text, id}, ...] from root to parent."""
    chain = []
    current_refs = tweet.referenced_tweets

    # First check the included_tweets from the initial API call
    if current_refs:
        for ref in current_refs:
            if ref.type == "replied_to" and ref.id in included_tweets:
                parent = included_tweets[ref.id]
                chain.append(parent)

    # If we found a parent in includes, try to walk further up via API
    # Also if we didn't find the parent in includes, fetch it directly
    if not chain and current_refs:
        for ref in current_refs:
            if ref.type == "replied_to":
                try:
                    result = client.get_tweet(
                        ref.id,
                        tweet_fields=["text", "author_id", "referenced_tweets"],
                        expansions=["author_id"],
                        user_fields=["username", "name"],
                    )
                    if result and result.data:
                        author_info = {}
                        if result.includes and result.includes.get("users"):
                            u = result.includes["users"][0]
                            author_info = {"username": u.username, "name": u.name}
                        chain.append({
                            "id": result.data.id,
                            "text": result.data.text,
                            "username": author_info.get("username", "unknown"),
                            "name": author_info.get("name", "Unknown"),
                            "_refs": result.data.referenced_tweets,
                        })
                except Exception as e:
                    print(f"    Chain fetch failed for {ref.id}: {e}")
                break

    # Continue walking up from the last parent we found
    for depth in range(1, MAX_THREAD_DEPTH):
        if not chain:
            break
        last = chain[-1]
        refs = last.get("_refs")
        if not refs:
            break

        parent_ref = None
        for ref in refs:
            if ref.type == "replied_to":
                parent_ref = ref
                break
        if not parent_ref:
            break

        try:
            result = client.get_tweet(
                parent_ref.id,
                tweet_fields=["text", "author_id", "referenced_tweets"],
                expansions=["author_id"],
                user_fields=["username", "name"],
            )
            if result and result.data:
                author_info = {}
                if result.includes and result.includes.get("users"):
                    u = result.includes["users"][0]
                    author_info = {"username": u.username, "name": u.name}
                chain.append({
                    "id": result.data.id,
                    "text": result.data.text,
                    "username": author_info.get("username", "unknown"),
                    "name": author_info.get("name", "Unknown"),
                    "_refs": result.data.referenced_tweets,
                })
            else:
                break
        except Exception as e:
            print(f"    Chain fetch failed at depth {depth}: {e}")
            break

    # Reverse so it goes from root → ... → parent (chronological order)
    chain.reverse()

    # Clean up internal fields and build display text
    for item in chain:
        item.pop("_refs", None)
        if "url" not in item:
            item["url"] = f"https://x.com/{item.get('username', 'unknown')}/status/{item['id']}"

    return chain


# ---------------------------------------------------------------------------
# Claude AI: Classify + Summarize
# ---------------------------------------------------------------------------

def classify_and_summarize(posts: list) -> list:
    """Use Claude to classify relevance and generate summaries.
    Returns only the business-relevant posts with summaries attached."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set — skipping AI classification/summaries")
        for p in posts:
            p["ai_summary"] = None
            p["is_relevant"] = True  # can't filter without AI, keep all
        return posts

    ai_client = anthropic.Anthropic(api_key=api_key)
    relevant_posts = []

    for i, p in enumerate(posts):
        # Build full thread context
        thread_text = ""
        if p.get("thread_chain"):
            for j, msg in enumerate(p["thread_chain"]):
                thread_text += f"[{j+1}] @{msg['username']}: \"{msg['text']}\"\n"
            thread_text += f"[Reply] @elonmusk: \"{p['text']}\""
        else:
            thread_text = f"@elonmusk: \"{p['text']}\""

        prompt = f"""You are a strict filter for a daily business insights digest aimed at entrepreneurs and operators. You must analyze this Elon Musk post (and its conversation thread) and decide if it belongs.

FULL CONVERSATION THREAD:
{thread_text}

STRICT CLASSIFICATION RULES:
- Mark as NOT relevant (relevant: false) if ANY of these apply:
  * You cannot determine what the conversation is actually about (missing context, image-only content, cryptic with no clear business meaning)
  * The post is just agreement ("Yes", "True", "Exactly", emoji) WITHOUT clear business context in the thread
  * It's about politics, social commentary, culture war, justice system, morality quotes, partisan opinions
  * It's a meme, joke, or casual banter with no business substance
  * It's a motivational or philosophical quote not directly tied to building/running a business
  * It's about celebrities, personal life, or gossip

- Mark as relevant (relevant: true) ONLY if the thread CLEARLY discusses:
  * Building companies, products, or technology (Tesla, SpaceX, xAI, Grok, Starlink, Neuralink, etc.)
  * Engineering decisions, manufacturing, scaling, or technical challenges
  * AI/ML developments, model training, compute infrastructure
  * Business strategy, hiring, leadership, execution, market dynamics
  * Space exploration milestones or rocket engineering
  * Energy, batteries, autonomous driving, robotics

When in doubt, mark as NOT relevant. Be very strict.

If RELEVANT, write a 2-3 sentence summary that:
1. Explains the FULL context of the conversation (not just Elon's short reply)
2. Extracts the specific business insight, lesson, or takeaway
3. Names which company/product it relates to

Respond ONLY with this exact JSON:
{{"relevant": true/false, "summary": "your summary here or null if not relevant"}}"""

        try:
            message = ai_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = message.content[0].text.strip()

            # Extract JSON object from response (Claude may add extra text)
            json_match = re.search(r'\{[^{}]*"relevant"\s*:\s*(true|false)[^{}]*\}',
                                   response_text, re.DOTALL)
            if not json_match:
                print(f"  Post {i+1}/{len(posts)}: No JSON found in response — filtering out")
                continue

            result = json.loads(json_match.group(0))

            if result.get("relevant"):
                p["ai_summary"] = result.get("summary")
                p["is_relevant"] = True
                relevant_posts.append(p)
                print(f"  Post {i+1}/{len(posts)}: RELEVANT ✓")
            else:
                print(f"  Post {i+1}/{len(posts)}: filtered out (not business)")

        except Exception as e:
            print(f"  Post {i+1}/{len(posts)}: FAILED ({e}) — keeping post")
            p["ai_summary"] = None
            p["is_relevant"] = True
            relevant_posts.append(p)

        if i < len(posts) - 1:
            time.sleep(0.2)

    return relevant_posts


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
                "_refs": t.referenced_tweets,
            }

    posts = []
    for tweet in tweets.data:
        text = tweet.text
        metrics = tweet.public_metrics or {}
        score = engagement_score(metrics)

        # Walk the reply chain to get full context
        print(f"  Walking thread for tweet {tweet.id}...")
        thread_chain = walk_reply_chain(client, tweet, included_tweets, included_users)

        # Get the immediate parent for display purposes
        parent_context = None
        if thread_chain:
            parent = thread_chain[-1]  # most recent parent
            parent_context = {
                "id": parent["id"],
                "text": clean_text(parent["text"], 300),
                "username": parent["username"],
                "name": parent.get("name", parent["username"]),
                "url": parent.get("url", f"https://x.com/{parent['username']}/status/{parent['id']}"),
            }

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
            "thread_chain": thread_chain,  # full chain for Claude
        })

    # Sort by engagement
    posts.sort(key=lambda p: p["score"], reverse=True)
    return posts[:30]  # fetch top 30, Claude will filter down


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
                            <strong>{html_module.escape(parent.get('name', parent['username']))}</strong>
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
  <div class="subtitle">AI-curated: only business, tech & building insights &mdash; no politics, no social commentary</div>
</header>

<div class="info-bar">
  <span>Updated: {now}</span>
  <span>{len(posts)} posts (last 36h)</span>
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
    print(f"Found {len(posts)} posts before AI filtering")

    if posts:
        print("Classifying relevance + generating summaries via Claude...")
        posts = classify_and_summarize(posts)
        print(f"Kept {len(posts)} business-relevant posts after AI filtering")

        if posts:
            print("Fetching tweet embeds via oEmbed API...")
            fetch_embeds(posts)

    html = generate_html(posts)
    out_dir = Path("docs")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Report written to {out_path}")

    # Save JSON (strip thread_chain to keep it clean)
    for p in posts:
        p.pop("thread_chain", None)
    json_path = out_dir / "posts.json"
    json_path.write_text(json.dumps(posts, indent=2, default=str), encoding="utf-8")
    print(f"JSON data written to {json_path}")


if __name__ == "__main__":
    main()
