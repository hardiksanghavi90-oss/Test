"""
Find the latest Jacob Hilton 'Elon Musk posted on X today' video,
get its description (via YouTube Data API), parse timestamps/chapters,
and use Claude to identify business-relevant segments.
"""

import os
import sys
import json
import re
import time
from pathlib import Path

import requests
import anthropic


CHANNEL_NAME = "Jacob Hilton"
SEARCH_QUERY = "Elon Musk posted on X today Jacob Hilton"


def find_latest_video() -> dict | None:
    """Find the latest Jacob Hilton Elon recap video via YouTube Data API."""
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("  YOUTUBE_API_KEY not set — skipping video recap")
        return None

    # Search for the latest video
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": SEARCH_QUERY,
            "type": "video",
            "order": "date",
            "maxResults": 5,
            "key": api_key,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"  YouTube search API error: {resp.status_code} {resp.text[:200]}")
        return None

    items = resp.json().get("items", [])
    for item in items:
        title = item["snippet"]["title"]
        if "elon" in title.lower() and ("posted" in title.lower() or "tweets" in title.lower()):
            video_id = item["id"]["videoId"]

            # Fetch full video details (description with timestamps)
            details = _get_video_details(video_id, api_key)
            description = details.get("description", "") if details else ""

            return {
                "id": video_id,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "channel": item["snippet"]["channelTitle"],
                "published": item["snippet"]["publishedAt"][:10],
                "description": description,
            }

    print("  No matching video found")
    return None


def _get_video_details(video_id: str, api_key: str) -> dict | None:
    """Fetch full video details including description."""
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={
            "part": "snippet",
            "id": video_id,
            "key": api_key,
        },
        timeout=15,
    )
    if resp.status_code == 200:
        items = resp.json().get("items", [])
        if items:
            return items[0]["snippet"]
    return None


def parse_description_timestamps(description: str) -> list[dict]:
    """Extract timestamps from video description (e.g. '2:15 - Grok update')."""
    timestamps = []
    pattern = re.compile(
        r"(?:^|\n)\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—:]?\s*(.+?)(?:\n|$)"
    )
    for match in pattern.finditer(description):
        time_str = match.group(1)
        topic = match.group(2).strip()
        parts = time_str.split(":")
        if len(parts) == 3:
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        else:
            seconds = int(parts[0]) * 60 + int(parts[1])
        timestamps.append({
            "timestamp": time_str,
            "seconds": seconds,
            "topic": topic,
        })
    return timestamps


def analyze_with_claude(video: dict, timestamps: list[dict]) -> list[dict]:
    """Use Claude to classify which timestamps are business-relevant."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ANTHROPIC_API_KEY not set — returning all timestamps")
        return timestamps

    client = anthropic.Anthropic(api_key=api_key)

    # Build context from timestamps or description
    if timestamps:
        ts_text = "\n".join(
            f"- {t['timestamp']} {t['topic']}" for t in timestamps
        )
        context = f"Video: {video['title']}\n\nChapters/Timestamps from description:\n{ts_text}"
    else:
        context = f"Video: {video['title']}\n\nVideo description:\n{video.get('description', 'No description available')}"

    prompt = f"""Analyze this YouTube video that recaps Elon Musk's daily X/Twitter posts.

{context}

TASK: For each chapter/timestamp above, decide if it's business-relevant and return it with a useful summary.

INCLUDE chapters about: Tesla, SpaceX, xAI, Grok, Starlink, Neuralink, AI/ML, engineering, manufacturing, rockets, product strategy, scaling, leadership, energy, batteries, autonomous driving, robotics, company building.

EXCLUDE chapters about: Politics, social commentary, culture war, morality quotes, memes, jokes, personal life, celebrity gossip, government, DOGE, regulations. Also exclude "Intro" and generic "Random" sections unless the topic name suggests business content.

For each RELEVANT chapter, return:
- "timestamp": the exact timestamp from the description (e.g. "4:04")
- "seconds": total seconds (e.g. 244)
- "topic": a descriptive label for the section (use the description label but make it more specific if possible, e.g. "SpaceX Launches & Starship" instead of just "SpaceX")
- "summary": 1 sentence describing what business topics are covered in this section
- "company": primary company discussed (e.g. "SpaceX", "xAI", "Tesla")

IMPORTANT: Return one entry per chapter. Do NOT skip chapters that are business-relevant. Use the exact timestamps provided.

Return ONLY a JSON array. If no business chapters found, return [].
Example: [{{"timestamp":"4:04","seconds":244,"topic":"SpaceX Launches & Starship Development","summary":"Covers 12 posts about Falcon Heavy launches, Starship progress, and rocket engineering milestones.","company":"SpaceX"}}]"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()
        json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
        if json_match:
            segments = json.loads(json_match.group(0))
            print(f"  Claude identified {len(segments)} business-relevant segments")
            return segments
    except Exception as e:
        print(f"  Claude analysis error: {e}")

    return []


def build_video_section(video: dict, segments: list[dict]) -> str:
    """Build HTML section for the video with timestamped links."""
    import html as html_module

    video_id = video["id"]
    title = html_module.escape(video["title"])

    if not segments:
        segment_html = '<p class="no-segments">No business-relevant segments identified in today\'s video.</p>'
    else:
        segment_html = ""
        for seg in segments:
            seconds = seg.get("seconds", 0)
            ts = html_module.escape(str(seg.get("timestamp", "0:00")))
            topic = html_module.escape(str(seg.get("topic", "")))
            summary = html_module.escape(str(seg.get("summary", "")))
            company = html_module.escape(str(seg.get("company", "")))
            link = f"https://www.youtube.com/watch?v={video_id}&t={seconds}s"

            segment_html += f"""
            <a href="{link}" target="_blank" rel="noopener" class="segment">
                <span class="seg-time">{ts}</span>
                <div class="seg-content">
                    <div class="seg-topic">{topic} <span class="seg-company">{company}</span></div>
                    <div class="seg-summary">{summary}</div>
                </div>
                <span class="seg-play">▶</span>
            </a>"""

    return f"""
    <div class="video-section">
        <h2 class="section-title">📺 Today's Video Recap</h2>
        <div class="video-card">
            <div class="video-embed">
                <iframe src="https://www.youtube.com/embed/{video_id}"
                        frameborder="0" allowfullscreen
                        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                        style="width:100%;aspect-ratio:16/9;border-radius:8px;"></iframe>
            </div>
            <div class="video-title">{title}</div>
            <div class="segments-label">Business-Relevant Timestamps — tap to jump:</div>
            <div class="segments-list">
                {segment_html}
            </div>
        </div>
    </div>"""


def get_video_css() -> str:
    """Return CSS for the video section."""
    return """
  .video-section {
    margin-bottom: 24px;
  }
  .section-title {
    font-size: 1.1em;
    font-weight: 700;
    margin-bottom: 12px;
  }
  .video-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    padding: 16px;
  }
  .video-embed {
    margin-bottom: 12px;
  }
  .video-title {
    font-size: 0.9em;
    font-weight: 600;
    margin-bottom: 12px;
  }
  .segments-label {
    font-size: 0.75em;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 8px;
    font-weight: 600;
  }
  .segments-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .segment {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    background: var(--reply-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    text-decoration: none;
    color: var(--text);
  }
  .segment:hover {
    border-color: var(--accent);
    background: var(--insight-bg);
  }
  .seg-time {
    font-family: monospace;
    font-size: 0.85em;
    color: var(--accent);
    font-weight: 700;
    min-width: 45px;
  }
  .seg-content {
    flex: 1;
  }
  .seg-topic {
    font-size: 0.85em;
    font-weight: 600;
    margin-bottom: 2px;
  }
  .seg-company {
    font-size: 0.75em;
    color: var(--accent);
    font-weight: 400;
  }
  .seg-summary {
    font-size: 0.78em;
    color: var(--muted);
    line-height: 1.4;
  }
  .seg-play {
    color: var(--accent);
    font-size: 1.1em;
  }
  .no-segments {
    color: var(--muted);
    font-size: 0.85em;
    padding: 12px;
    text-align: center;
  }"""


def process_video() -> tuple[str, str]:
    """Main entry: find video, parse timestamps, analyze, return (html, css)."""

    print("Looking for latest Jacob Hilton Elon recap video...")
    video = find_latest_video()
    if not video:
        print("No video found today")
        return "", ""

    print(f"Found: {video['title']}")
    print(f"URL: {video['url']}")

    # Parse description timestamps
    description = video.get("description", "")
    print(f"Description length: {len(description)} chars")
    timestamps = parse_description_timestamps(description)
    print(f"Found {len(timestamps)} timestamps in description")

    if timestamps:
        for ts in timestamps[:8]:
            print(f"  {ts['timestamp']} - {ts['topic']}")
        if len(timestamps) > 8:
            print(f"  ... and {len(timestamps) - 8} more")

    # Use Claude to classify which chapters are business-relevant
    print("Analyzing segments with Claude...")
    segments = analyze_with_claude(video, timestamps)
    print(f"Final: {len(segments)} business-relevant segments")

    section_html = build_video_section(video, segments)
    return section_html, get_video_css()
