"""
Video recap pipeline: find daily Elon recap video, get transcript,
mine topics at segment level, resolve clean time windows, render.

Architecture:
  1. Discover video via YouTube Data API
  2. Get transcript: youtube-transcript-api → whisper worker → description fallback
  3. Mine topics: sliding windows → keyword pre-filter → Claude re-ranking
  4. Resolve: snap to sentence boundaries, merge adjacent hits
  5. Render timestamped links with evidence text
"""

import os
import json
import re
import time
from pathlib import Path

import requests
import anthropic


CHANNEL_NAME = "Jacob Hilton"
SEARCH_QUERY = "Elon Musk posted on X today Jacob Hilton"

# Company keyword sets for pre-filtering
COMPANY_KEYWORDS = {
    "Tesla": ["tesla", "model y", "model 3", "model s", "model x", "cybertruck",
              "fsd", "autopilot", "optimus", "megapack", "powerwall", "gigafactory",
              "supercharger", "dojo"],
    "SpaceX": ["spacex", "starship", "falcon", "raptor", "launch", "orbit",
               "starlink", "rocket", "booster", "landing", "dragon", "crew"],
    "xAI": ["xai", "grok", "colossus", "ai model", "training", "inference",
            "machine learning", "neural", "llm", "chatbot", "artificial intelligence"],
    "X/Twitter": ["x platform", "x.com", "twitter", "algorithm", "engagement",
                  "monetization", "creator", "verification"],
    "Neuralink": ["neuralink", "brain", "implant", "bci", "neural interface"],
    "Boring Company": ["boring company", "tunnel", "hyperloop", "loop"],
    "General Business": ["revenue", "profit", "billion", "million", "valuation",
                         "funding", "ipo", "stock", "manufacture", "factory",
                         "engineer", "hire", "scale", "ship", "build", "product",
                         "first principles", "efficiency"],
}

# Flatten for quick lookup
ALL_BUSINESS_KEYWORDS = set()
for kws in COMPANY_KEYWORDS.values():
    ALL_BUSINESS_KEYWORDS.update(kws)

# Topics to exclude
EXCLUDE_KEYWORDS = [
    "trump", "biden", "democrat", "republican", "maga", "woke", "dei",
    "immigration", "border", "election", "vote", "congress", "senate",
    "abortion", "gun", "religion", "culture war", "cancel culture",
    "propaganda", "mainstream media", "conspiracy",
]


# ---------------------------------------------------------------------------
# 1. Discover video
# ---------------------------------------------------------------------------

def find_latest_video() -> dict | None:
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("  YOUTUBE_API_KEY not set — skipping video recap")
        return None

    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet", "q": SEARCH_QUERY,
            "type": "video", "order": "date", "maxResults": 5, "key": api_key,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"  YouTube search error: {resp.status_code}")
        return None

    for item in resp.json().get("items", []):
        title = item["snippet"]["title"]
        if "elon" in title.lower() and ("posted" in title.lower() or "tweets" in title.lower()):
            video_id = item["id"]["videoId"]
            details = _get_video_details(video_id, api_key)
            return {
                "id": video_id,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "channel": item["snippet"]["channelTitle"],
                "description": details.get("description", "") if details else "",
            }
    return None


def _get_video_details(video_id: str, api_key: str) -> dict | None:
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "snippet", "id": video_id, "key": api_key},
        timeout=15,
    )
    if resp.status_code == 200:
        items = resp.json().get("items", [])
        if items:
            return items[0]["snippet"]
    return None


# ---------------------------------------------------------------------------
# 2. Get transcript (tiered: captions → worker → description)
# ---------------------------------------------------------------------------

def get_transcript(video_id: str) -> list[dict] | None:
    """Get transcript segments. Each: {"start": float, "end": float, "text": str}"""

    # Tier 1: YouTube auto-captions via youtube-transcript-api
    transcript = _get_captions(video_id)
    if transcript:
        print(f"  Got transcript from captions: {len(transcript)} segments")
        return transcript

    # Tier 2: Whisper worker (Fly.io)
    transcript = _get_from_worker(video_id)
    if transcript:
        print(f"  Got transcript from worker: {len(transcript)} segments")
        return transcript

    print("  No transcript available")
    return None


def _get_captions(video_id: str) -> list[dict] | None:
    """Try youtube-transcript-api (multiple API versions)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        # Try new API (v1.x+)
        try:
            api = YouTubeTranscriptApi()
            result = api.fetch(video_id, languages=["en"])
            segments = []
            for snippet in result:
                segments.append({
                    "start": snippet.start,
                    "end": snippet.start + snippet.duration,
                    "text": snippet.text.strip(),
                })
            if segments:
                return segments
        except (TypeError, AttributeError):
            pass

        # Try old API (v0.x)
        try:
            entries = YouTubeTranscriptApi.get_transcript(video_id, languages=["en"])
            return [
                {"start": e["start"], "end": e["start"] + e["duration"], "text": e["text"].strip()}
                for e in entries
            ]
        except Exception:
            pass

    except ImportError:
        print("  youtube-transcript-api not installed")
    except Exception as e:
        print(f"  Captions fetch error: {type(e).__name__}: {e}")
    return None


def _get_from_worker(video_id: str) -> list[dict] | None:
    """Call the Fly.io whisper worker."""
    worker_url = os.environ.get("WHISPER_WORKER_URL")
    auth_token = os.environ.get("WORKER_AUTH_TOKEN", "")
    if not worker_url:
        return None

    print(f"  Calling whisper worker at {worker_url}...")
    try:
        resp = requests.post(
            f"{worker_url}/transcribe",
            json={"video_id": video_id, "auth_token": auth_token},
            timeout=360,
        )
        if resp.status_code == 200:
            data = resp.json()
            segments = data.get("segments", [])
            # Normalize format
            return [
                {"start": s["start"], "end": s["end"], "text": s["text"]}
                for s in segments if s.get("text")
            ]
        print(f"  Worker error: {resp.status_code} {resp.text[:500]}")
    except Exception as e:
        print(f"  Worker call failed: {e}")
    return None


# ---------------------------------------------------------------------------
# 3. Mine topics: sliding windows → keyword filter → Claude re-ranking
# ---------------------------------------------------------------------------

def mine_topics(transcript: list[dict], video: dict) -> list[dict]:
    """Extract business-relevant topic hits from transcript segments."""

    # Step 1: Build sliding windows (30s window, 15s stride)
    if not transcript:
        return []

    duration = max(s["end"] for s in transcript)
    window_size = 30.0
    stride = 15.0
    windows = []

    t = 0.0
    while t < duration:
        window_end = t + window_size
        # Collect segments overlapping this window
        texts = []
        for seg in transcript:
            if seg["end"] > t and seg["start"] < window_end:
                texts.append(seg["text"])

        window_text = " ".join(texts).strip()
        if window_text:
            windows.append({
                "start": t,
                "end": min(window_end, duration),
                "text": window_text,
            })
        t += stride

    print(f"  Built {len(windows)} sliding windows from transcript")

    # Step 2: Keyword pre-filter
    candidates = []
    for w in windows:
        text_lower = w["text"].lower()

        # Skip windows with exclude keywords
        if any(kw in text_lower for kw in EXCLUDE_KEYWORDS):
            continue

        # Check for business keyword matches
        matched_companies = []
        for company, keywords in COMPANY_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                matched_companies.append(company)

        if matched_companies:
            w["companies"] = matched_companies
            candidates.append(w)

    print(f"  {len(candidates)} windows passed keyword pre-filter (from {len(windows)})")

    if not candidates:
        return []

    # Step 3: Claude re-ranking
    topic_hits = _claude_rerank(candidates, video)
    print(f"  Claude identified {len(topic_hits)} topic hits")

    # Step 4: Resolve clean time windows
    resolved = _resolve_windows(topic_hits, transcript)
    print(f"  Resolved to {len(resolved)} clean segments")

    return resolved


def _claude_rerank(candidates: list[dict], video: dict) -> list[dict]:
    """Send candidate windows to Claude for scoring and labeling."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # No Claude: return candidates as-is with basic labels
        return [
            {
                "start_sec": c["start"], "end_sec": c["end"],
                "topic": ", ".join(c.get("companies", [])),
                "summary": c["text"][:100] + "...",
                "company": c.get("companies", [""])[0],
                "confidence": 0.5, "evidence_text": c["text"],
            }
            for c in candidates[:15]
        ]

    client = anthropic.Anthropic(api_key=api_key)

    # Build the windows text for Claude
    window_entries = []
    for i, c in enumerate(candidates[:30]):  # limit to 30 windows
        secs = int(c["start"])
        mins, s = divmod(secs, 60)
        ts = f"{mins}:{s:02d}"
        window_entries.append(f"[Window {i+1}, {ts}] {c['text'][:300]}")

    windows_text = "\n\n".join(window_entries)

    prompt = f"""You are analyzing transcript windows from a YouTube video: "{video['title']}"

Each window is a ~30 second chunk of the narrator reading Elon Musk's posts. Score each window for business/tech relevance.

WINDOWS:
{windows_text}

For each window, provide:
- window_number (matching the [Window N] label)
- score: 0-10 (10 = pure business/tech content, 0 = politics/social/irrelevant)
- topic: specific label (e.g. "Grok auto-translation feature", "Falcon 9 34th flight")
- summary: 1 sentence business insight
- company: primary company discussed

Only return windows with score >= 6. Skip politics, social commentary, memes, jokes.

Return ONLY a JSON array:
[{{"window_number": 1, "score": 8, "topic": "...", "summary": "...", "company": "SpaceX"}}]

If no windows score >= 6, return []."""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()
        json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
        if not json_match:
            return []

        hits = json.loads(json_match.group(0))
        results = []
        for hit in hits:
            idx = hit.get("window_number", 0) - 1
            if 0 <= idx < len(candidates):
                c = candidates[idx]
                results.append({
                    "start_sec": c["start"],
                    "end_sec": c["end"],
                    "topic": hit.get("topic", ""),
                    "summary": hit.get("summary", ""),
                    "company": hit.get("company", ""),
                    "confidence": hit.get("score", 0) / 10.0,
                    "evidence_text": c["text"][:200],
                })
        return results

    except Exception as e:
        print(f"  Claude re-ranking error: {e}")
        return []


def _resolve_windows(hits: list[dict], transcript: list[dict]) -> list[dict]:
    """Snap to sentence boundaries, merge adjacent hits."""
    if not hits:
        return []

    # Sort by start time
    hits.sort(key=lambda h: h["start_sec"])

    # Merge overlapping/adjacent hits (within 15s, same company)
    merged = [hits[0].copy()]
    for hit in hits[1:]:
        prev = merged[-1]
        if (hit["start_sec"] - prev["end_sec"] <= 15
                and hit.get("company") == prev.get("company")):
            # Merge
            prev["end_sec"] = max(prev["end_sec"], hit["end_sec"])
            prev["topic"] += " + " + hit["topic"]
            prev["evidence_text"] += " " + hit.get("evidence_text", "")
            prev["confidence"] = max(prev["confidence"], hit["confidence"])
        else:
            merged.append(hit.copy())

    # Snap to nearest sentence boundary using transcript
    for hit in merged:
        # Find the transcript segment nearest to start that begins a sentence
        best_start = hit["start_sec"]
        for seg in transcript:
            if abs(seg["start"] - hit["start_sec"]) < 5:
                best_start = seg["start"]
                break
        hit["start_sec"] = best_start

        # Widen end slightly for context
        hit["end_sec"] = hit["end_sec"] + 3

    return merged


# ---------------------------------------------------------------------------
# 4. Description fallback (current behavior)
# ---------------------------------------------------------------------------

def parse_description_timestamps(description: str) -> list[dict]:
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
        timestamps.append({"timestamp": time_str, "seconds": seconds, "topic": topic})
    return timestamps


def description_fallback(video: dict) -> list[dict]:
    """Fall back to description chapter timestamps with keyword matching."""
    timestamps = parse_description_timestamps(video.get("description", ""))
    print(f"  Description fallback: {len(timestamps)} timestamps found")

    BUSINESS_TERMS = ["spacex", "tesla", "xai", "grok", "starlink", "neuralink",
                      "ai", "rocket", "launch", "starship", "falcon", "optimus",
                      "boring", "energy", "battery", "fsd", "autopilot", "engineer"]
    results = []
    for ts in timestamps:
        topic_lower = ts["topic"].lower()
        if any(term in topic_lower for term in BUSINESS_TERMS):
            results.append({
                "start_sec": ts["seconds"],
                "end_sec": ts["seconds"] + 120,  # assume 2 min per chapter
                "topic": ts["topic"],
                "summary": f"Section covering {ts['topic']}",
                "company": "",
                "confidence": 0.5,
                "evidence_text": "",
            })
    return results


# ---------------------------------------------------------------------------
# 5. Build HTML
# ---------------------------------------------------------------------------

def build_video_section(video: dict, topic_hits: list[dict]) -> str:
    import html as html_module

    video_id = video["id"]
    title = html_module.escape(video["title"])

    if not topic_hits:
        segment_html = '<p class="no-segments">No business-relevant segments identified.</p>'
    else:
        segment_html = ""
        for hit in topic_hits:
            start_sec = int(hit["start_sec"])
            end_sec = int(hit.get("end_sec", start_sec + 30))
            mins, secs = divmod(start_sec, 60)
            ts_str = f"{mins}:{secs:02d}"
            end_mins, end_secs = divmod(end_sec, 60)
            end_str = f"{end_mins}:{end_secs:02d}"

            topic = html_module.escape(str(hit.get("topic", "")))
            summary = html_module.escape(str(hit.get("summary", "")))
            company = html_module.escape(str(hit.get("company", "")))
            evidence = html_module.escape(str(hit.get("evidence_text", ""))[:150])
            confidence = hit.get("confidence", 0)
            conf_pct = int(confidence * 100)

            link = f"https://www.youtube.com/watch?v={video_id}&t={start_sec}s"

            evidence_block = ""
            if evidence:
                evidence_block = f'<div class="seg-evidence">&ldquo;{evidence}&hellip;&rdquo;</div>'

            segment_html += f"""
            <a href="{link}" target="_blank" rel="noopener" class="segment">
                <span class="seg-time">{ts_str}<br><span class="seg-end">–{end_str}</span></span>
                <div class="seg-content">
                    <div class="seg-topic">{topic} <span class="seg-company">{company}</span></div>
                    <div class="seg-summary">{summary}</div>
                    {evidence_block}
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
    return """
  .video-section { margin-bottom: 24px; }
  .section-title { font-size: 1.1em; font-weight: 700; margin-bottom: 12px; }
  .video-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; overflow: hidden; padding: 16px;
  }
  .video-embed { margin-bottom: 12px; }
  .video-title { font-size: 0.9em; font-weight: 600; margin-bottom: 12px; }
  .segments-label {
    font-size: 0.75em; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 8px; font-weight: 600;
  }
  .segments-list { display: flex; flex-direction: column; gap: 6px; }
  .segment {
    display: flex; align-items: flex-start; gap: 10px; padding: 10px 12px;
    background: var(--reply-bg); border: 1px solid var(--border);
    border-radius: 8px; text-decoration: none; color: var(--text);
  }
  .segment:hover { border-color: var(--accent); background: var(--insight-bg); }
  .seg-time {
    font-family: monospace; font-size: 0.85em; color: var(--accent);
    font-weight: 700; min-width: 50px; text-align: center;
  }
  .seg-end { font-size: 0.7em; color: var(--muted); font-weight: 400; }
  .seg-content { flex: 1; }
  .seg-topic { font-size: 0.85em; font-weight: 600; margin-bottom: 2px; }
  .seg-company { font-size: 0.75em; color: var(--accent); font-weight: 400; }
  .seg-summary { font-size: 0.78em; color: var(--muted); line-height: 1.4; }
  .seg-evidence {
    font-size: 0.72em; color: var(--muted); line-height: 1.3;
    margin-top: 4px; font-style: italic; opacity: 0.7;
  }
  .seg-play { color: var(--accent); font-size: 1.1em; margin-top: 4px; }
  .no-segments {
    color: var(--muted); font-size: 0.85em; padding: 12px; text-align: center;
  }"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def process_video() -> tuple[str, str]:
    print("Looking for latest Jacob Hilton Elon recap video...")
    video = find_latest_video()
    if not video:
        print("No video found today")
        return "", ""

    print(f"Found: {video['title']}")
    print(f"URL: {video['url']}")

    # Get transcript (tiered approach)
    print("Getting transcript...")
    transcript = get_transcript(video["id"])

    if transcript:
        # Full pipeline: sliding windows → keyword filter → Claude → resolve
        print("Running topic mining pipeline...")
        topic_hits = mine_topics(transcript, video)
    else:
        # Fallback: description chapter timestamps
        print("No transcript — using description fallback...")
        topic_hits = description_fallback(video)

    print(f"Final: {len(topic_hits)} business-relevant segments")

    section_html = build_video_section(video, topic_hits)
    return section_html, get_video_css()
