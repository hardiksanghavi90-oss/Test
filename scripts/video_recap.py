"""
Find the latest Jacob Hilton 'Elon Musk posted on X today' video,
pull its transcript, and use Claude to identify business-relevant timestamps.
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
# YouTube RSS feed for a channel (by channel ID)
# We'll search for the channel and video via YouTube Data API
SEARCH_QUERY = "Elon Musk posted on X today Jacob Hilton"


def find_latest_video() -> dict | None:
    """Find the latest Jacob Hilton Elon recap video via YouTube Data API."""
    api_key = os.environ.get("YOUTUBE_API_KEY")

    if api_key:
        return _search_via_api(api_key)
    else:
        print("YOUTUBE_API_KEY not set — trying RSS feed fallback")
        return _search_via_rss()


def _search_via_api(api_key: str) -> dict | None:
    """Search YouTube API for the latest video."""
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
        print(f"YouTube API error: {resp.status_code} {resp.text[:200]}")
        return None

    items = resp.json().get("items", [])
    for item in items:
        title = item["snippet"]["title"]
        if "elon" in title.lower() and ("posted" in title.lower() or "tweets" in title.lower()):
            video_id = item["id"]["videoId"]
            return {
                "id": video_id,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "channel": item["snippet"]["channelTitle"],
                "published": item["snippet"]["publishedAt"][:10],
            }
    print("No matching video found via API")
    return None


def _search_via_rss() -> dict | None:
    """Fallback: try to find the video via noembed or RSS."""
    # Try a known recent pattern - search via Google
    # This is a fallback and less reliable
    print("RSS fallback not implemented — set YOUTUBE_API_KEY for reliable results")
    return None


def get_transcript(video_id: str) -> str | None:
    """Fetch YouTube auto-captions using yt-dlp (works from cloud IPs)."""
    import subprocess
    import tempfile

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"  Downloading subtitles via yt-dlp for {video_id}...")

    with tempfile.TemporaryDirectory() as tmpdir:
        sub_path = os.path.join(tmpdir, "subs")
        try:
            result = subprocess.run(
                [
                    "yt-dlp",
                    "--skip-download",
                    "--write-auto-sub",
                    "--sub-lang", "en",
                    "--sub-format", "json3",
                    "--output", sub_path,
                    video_url,
                ],
                capture_output=True, text=True, timeout=60,
            )
            print(f"  yt-dlp exit code: {result.returncode}")
            if result.returncode != 0:
                print(f"  yt-dlp stderr: {result.stderr[:500]}")

            # Look for the subtitle file
            sub_file = sub_path + ".en.json3"
            if not os.path.exists(sub_file):
                # Try alternate naming
                import glob
                candidates = glob.glob(os.path.join(tmpdir, "*.json3"))
                if candidates:
                    sub_file = candidates[0]
                else:
                    print(f"  No subtitle file found in {tmpdir}")
                    # Try VTT format as fallback
                    return _try_vtt_fallback(video_url, tmpdir, sub_path)

            with open(sub_file, "r") as f:
                data = json.load(f)

            events = data.get("events", [])
            lines = []
            for event in events:
                start_ms = event.get("tStartMs", 0)
                seconds = int(start_ms / 1000)

                # Build text from segments
                segs = event.get("segs", [])
                text = "".join(s.get("utf8", "") for s in segs).strip()
                if not text or text == "\n":
                    continue

                mins, secs = divmod(seconds, 60)
                hours, mins = divmod(mins, 60)
                if hours:
                    ts = f"{hours}:{mins:02d}:{secs:02d}"
                else:
                    ts = f"{mins}:{secs:02d}"
                lines.append(f"[{ts}] {text}")

            result_text = "\n".join(lines)
            print(f"  Transcript assembled: {len(result_text)} chars, {len(lines)} lines")
            return result_text if lines else None

        except subprocess.TimeoutExpired:
            print("  yt-dlp timed out after 60s")
            return None
        except Exception as e:
            print(f"  Transcript fetch error: {type(e).__name__}: {e}")
            return None


def _try_vtt_fallback(video_url: str, tmpdir: str, sub_path: str) -> str | None:
    """Try downloading VTT format as fallback."""
    import subprocess
    import glob

    print("  Trying VTT format fallback...")
    try:
        subprocess.run(
            [
                "yt-dlp",
                "--skip-download",
                "--write-auto-sub",
                "--sub-lang", "en",
                "--sub-format", "vtt",
                "--output", sub_path,
                video_url,
            ],
            capture_output=True, text=True, timeout=60,
        )
        candidates = glob.glob(os.path.join(tmpdir, "*.vtt"))
        if not candidates:
            print("  No VTT file found either")
            return None

        with open(candidates[0], "r") as f:
            vtt_text = f.read()

        # Parse VTT: extract timestamps and text
        lines = []
        for match in re.finditer(
            r"(\d{2}):(\d{2}):(\d{2})\.\d+\s*-->.*?\n(.+?)(?:\n\n|\Z)",
            vtt_text, re.DOTALL
        ):
            h, m, s = int(match.group(1)), int(match.group(2)), int(match.group(3))
            text = re.sub(r"<[^>]+>", "", match.group(4)).strip()
            if not text:
                continue
            total_s = h * 3600 + m * 60 + s
            mins, secs = divmod(total_s, 60)
            hours, mins = divmod(mins, 60)
            if hours:
                ts = f"{hours}:{mins:02d}:{secs:02d}"
            else:
                ts = f"{mins}:{secs:02d}"
            lines.append(f"[{ts}] {text}")

        result_text = "\n".join(lines)
        print(f"  VTT transcript: {len(result_text)} chars, {len(lines)} lines")
        return result_text if lines else None

    except Exception as e:
        print(f"  VTT fallback error: {e}")
        return None


def analyze_transcript(transcript: str, video_url: str) -> list[dict]:
    """Use Claude to identify business-relevant segments with timestamps."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set — skipping transcript analysis")
        return []

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are analyzing a transcript from a YouTube video that recaps Elon Musk's daily posts on X (Twitter).

Your job: identify the segments that discuss BUSINESS-RELEVANT topics only.

INCLUDE segments about:
- Tesla, SpaceX, xAI, Grok, Starlink, Neuralink, Boring Company, Optimus
- AI/ML developments, model training, compute infrastructure
- Engineering, manufacturing, production, rockets, launches
- Business strategy, product decisions, scaling, growth
- Leadership insights, hiring, company building
- Energy, batteries, autonomous driving, robotics

EXCLUDE segments about:
- Politics, government, elections, DOGE, regulations
- Social commentary, culture war, morality quotes, identity politics
- Memes, jokes, celebrity gossip, personal life
- Generic motivational content not tied to business

TRANSCRIPT:
{transcript[:15000]}

Return a JSON array of business-relevant segments. For each segment, provide:
- "timestamp": the start time (e.g. "2:15")
- "seconds": start time in total seconds (e.g. 135)
- "topic": short topic label (e.g. "Grok Translation Feature")
- "summary": 1-2 sentence business insight summary
- "company": which company/product (e.g. "xAI/Grok", "SpaceX", "Tesla")

Return ONLY valid JSON array. If no business segments found, return [].
Example: [{{"timestamp": "2:15", "seconds": 135, "topic": "Grok Translation", "summary": "Elon announces...", "company": "xAI"}}]"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()

        # Extract JSON array
        json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
        if json_match:
            segments = json.loads(json_match.group(0))
            return segments
        else:
            print("No JSON array found in Claude response")
            return []
    except Exception as e:
        print(f"Claude analysis error: {e}")
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
            ts = html_module.escape(seg.get("timestamp", "0:00"))
            topic = html_module.escape(seg.get("topic", ""))
            summary = html_module.escape(seg.get("summary", ""))
            company = html_module.escape(seg.get("company", ""))
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
    """Main entry: find video, get transcript, analyze, return (html, css).
    Returns empty strings if no video found."""

    print("Looking for latest Jacob Hilton Elon recap video...")
    video = find_latest_video()
    if not video:
        print("No video found today")
        return "", ""

    print(f"Found: {video['title']}")
    print(f"URL: {video['url']}")

    print("Fetching transcript...")
    transcript = get_transcript(video["id"])
    if not transcript:
        print("No transcript available — will show video without timestamps")
        section_html = build_video_section(video, [])
        return section_html, get_video_css()

    print(f"Transcript length: {len(transcript)} chars")
    print("Analyzing transcript for business-relevant segments...")
    segments = analyze_transcript(transcript, video["url"])
    print(f"Found {len(segments)} business-relevant segments")

    section_html = build_video_section(video, segments)
    return section_html, get_video_css()
