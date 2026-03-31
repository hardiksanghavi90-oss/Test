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


PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.in.projectsegfau.lt",
]


def get_audio_url(video_id: str) -> str | None:
    """Get direct audio stream URL via Piped API (YouTube alternative frontend)."""
    for instance in PIPED_INSTANCES:
        try:
            print(f"  Trying Piped instance: {instance}")
            resp = requests.get(
                f"{instance}/streams/{video_id}",
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"    Status {resp.status_code}")
                continue

            data = resp.json()
            audio_streams = data.get("audioStreams", [])
            if not audio_streams:
                print(f"    No audio streams found")
                continue

            # Pick the best quality audio stream
            best = max(audio_streams, key=lambda s: s.get("bitrate", 0))
            url = best.get("url")
            if url:
                print(f"    Got audio URL ({best.get('bitrate', '?')} bitrate, {best.get('mimeType', '?')})")
                return url

        except Exception as e:
            print(f"    Error: {e}")
            continue

    print("  All Piped instances failed")
    return None


def transcribe_with_assemblyai(video_id: str) -> str | None:
    """Transcribe a YouTube video: get audio URL via Piped, transcribe via AssemblyAI."""
    api_key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not api_key:
        print("  ASSEMBLYAI_API_KEY not set — skipping transcription")
        return None

    # Get direct audio URL via Piped
    audio_url = get_audio_url(video_id)
    if not audio_url:
        print("  Could not get direct audio URL — skipping transcription")
        return None

    headers = {"authorization": api_key, "content-type": "application/json"}

    print(f"  Submitting audio to AssemblyAI...")
    resp = requests.post(
        "https://api.assemblyai.com/v2/transcript",
        headers=headers,
        json={
            "audio_url": audio_url,
            "speech_models": ["universal-3-pro"],
        },
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"  AssemblyAI submit error: {resp.status_code} {resp.text[:300]}")
        return None

    transcript_id = resp.json().get("id")
    print(f"  Transcription job started: {transcript_id}")

    poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    for attempt in range(90):  # ~7.5 min max
        time.sleep(5)
        poll_resp = requests.get(poll_url, headers=headers, timeout=15)
        data = poll_resp.json()
        status = data.get("status")
        if status == "completed":
            print(f"  Transcription completed (attempt {attempt + 1})")
            break
        elif status == "error":
            print(f"  Transcription failed: {data.get('error')}")
            return None
        elif attempt % 6 == 0:
            print(f"  Status: {status} (waiting...)")
    else:
        print("  Transcription timed out")
        return None

    words = data.get("words", [])
    if not words:
        text = data.get("text", "")
        if text:
            print(f"  Got transcript ({len(text)} chars, no word timestamps)")
            return text
        return None

    # Group words into ~10-second chunks
    lines = []
    chunk_words = []
    chunk_start = 0
    for word in words:
        if not chunk_words:
            chunk_start = word["start"]
        chunk_words.append(word["text"])
        if word["end"] - chunk_start >= 10000:
            seconds = int(chunk_start / 1000)
            mins, secs = divmod(seconds, 60)
            hours, mins = divmod(mins, 60)
            ts = f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"
            lines.append(f"[{ts}] {' '.join(chunk_words)}")
            chunk_words = []
    if chunk_words:
        seconds = int(chunk_start / 1000)
        mins, secs = divmod(seconds, 60)
        hours, mins = divmod(mins, 60)
        ts = f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"
        lines.append(f"[{ts}] {' '.join(chunk_words)}")

    result = "\n".join(lines)
    print(f"  Transcript assembled: {len(result)} chars, {len(lines)} lines")
    return result


def analyze_with_claude(video: dict, timestamps: list[dict],
                        transcript: str | None = None) -> list[dict]:
    """Use Claude to classify which timestamps are business-relevant."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ANTHROPIC_API_KEY not set — returning all timestamps")
        return timestamps

    client = anthropic.Anthropic(api_key=api_key)

    # Build context: prefer full transcript, fall back to description timestamps
    if transcript:
        context = f"Video: {video['title']}\n\nFULL TRANSCRIPT (with timestamps):\n{transcript[:14000]}"
    elif timestamps:
        ts_text = "\n".join(
            f"- {t['timestamp']} {t['topic']}" for t in timestamps
        )
        context = f"Video: {video['title']}\n\nChapters/Timestamps from description:\n{ts_text}"
    else:
        context = f"Video: {video['title']}\n\nVideo description:\n{video.get('description', 'No description available')}"

    prompt = f"""Analyze this YouTube video that recaps Elon Musk's daily X/Twitter posts.

{context}

TASK: Identify EACH individual business/tech topic discussed in the video and return it with its timestamp and a summary.

INCLUDE topics about: Tesla, SpaceX, xAI, Grok, Starlink, Neuralink, AI/ML, engineering, manufacturing, rockets, product strategy, scaling, leadership, energy, batteries, autonomous driving, robotics, company building.

EXCLUDE topics about: Politics, social commentary, culture war, morality quotes, memes, jokes, personal life, celebrity gossip, government, DOGE, regulations.

IMPORTANT RULES:
- Return ONE entry per INDIVIDUAL topic (e.g. "Grok auto-translation", "Falcon Heavy landing", "Starship heat shield test")
- Do NOT group multiple topics into one entry
- Use the EXACT timestamp from the transcript where that specific topic is first mentioned
- "topic" must be specific (e.g. "Grok auto-translation feature") not vague ("AI stuff")

For each relevant topic:
- "timestamp": start time (e.g. "4:12")
- "seconds": total seconds (e.g. 252)
- "topic": specific topic label
- "summary": 1 sentence about what Elon said/posted about this
- "company": which company (e.g. "xAI", "SpaceX", "Tesla")

Return ONLY a JSON array. If nothing relevant, return [].
Example: [{{"timestamp":"4:12","seconds":252,"topic":"Falcon Heavy dual booster landing","summary":"Elon celebrates the successful simultaneous landing of both Falcon Heavy boosters.","company":"SpaceX"}},{{"timestamp":"6:45","seconds":405,"topic":"Grok auto-translation launch","summary":"Elon announces Grok now auto-translates and recommends posts across languages on X.","company":"xAI"}}]"""

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
            if segments:
                return segments
    except Exception as e:
        print(f"  Claude analysis error: {e}")

    # Fallback: if Claude returned nothing but we have timestamps,
    # return business-relevant looking ones directly
    if timestamps:
        BUSINESS_TERMS = ["spacex", "tesla", "xai", "grok", "starlink", "neuralink",
                          "ai", "rocket", "launch", "starship", "falcon", "optimus",
                          "boring", "energy", "battery", "fsd", "autopilot", "engineer"]
        fallback = []
        for ts in timestamps:
            topic_lower = ts["topic"].lower()
            if any(term in topic_lower for term in BUSINESS_TERMS):
                fallback.append({
                    "timestamp": ts["timestamp"],
                    "seconds": ts["seconds"],
                    "topic": ts["topic"],
                    "summary": f"Section covering {ts['topic']}",
                    "company": "",
                })
        if fallback:
            print(f"  Using {len(fallback)} keyword-matched timestamps as fallback")
            return fallback

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

    # Try AssemblyAI transcription (with correct speech model)
    print("Transcribing video with AssemblyAI...")
    transcript = transcribe_with_assemblyai(video["id"])

    # Parse description timestamps as fallback
    description = video.get("description", "")
    timestamps = parse_description_timestamps(description)
    print(f"Found {len(timestamps)} timestamps in description (fallback)")

    # Analyze with Claude
    print("Analyzing with Claude...")
    segments = analyze_with_claude(video, timestamps, transcript=transcript)
    print(f"Final: {len(segments)} business-relevant segments")

    section_html = build_video_section(video, segments)
    return section_html, get_video_css()
