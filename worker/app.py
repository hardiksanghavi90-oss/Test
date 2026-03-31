"""
Whisper transcription worker - downloads YouTube audio and transcribes it.
Deployed to Fly.io as the media acquisition layer.
"""

import os
import json
import tempfile
import glob

from flask import Flask, request, jsonify

app = Flask(__name__)
AUTH_TOKEN = os.environ.get("WORKER_AUTH_TOKEN", "")

# Pre-load whisper model at import time (baked into Docker image)
_model = None


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _model


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/transcribe", methods=["POST"])
def transcribe():
    data = request.get_json(force=True)
    token = data.get("auth_token", "")
    video_id = data.get("video_id", "")

    if AUTH_TOKEN and token != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    if not video_id:
        return jsonify({"error": "video_id required"}), 400

    try:
        audio_path, error_detail = download_audio(video_id)
        if not audio_path:
            return jsonify({"error": "audio download failed", "detail": error_detail}), 502

        segments = transcribe_audio(audio_path)
        return jsonify({"segments": segments, "video_id": video_id})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def download_audio(video_id: str) -> tuple[str | None, str]:
    """Download audio from YouTube using yt-dlp with proxy support.
    Returns (audio_path, error_detail)."""
    import subprocess
    import signal

    url = f"https://www.youtube.com/watch?v={video_id}"
    tmpdir = tempfile.mkdtemp()
    output_path = os.path.join(tmpdir, "audio")

    proxy = os.environ.get("PROXY_URL", "")
    last_error = ""

    # Start the bgutil POT server (needed for YouTube challenge solving)
    pot_server = None
    try:
        pot_server = subprocess.Popen(
            ["python", "-m", "bgutil_ytdlp_pot_provider", "serve"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        import time
        time.sleep(2)  # give it time to start
        app.logger.info("bgutil POT server started")
    except Exception as e:
        app.logger.warning(f"Could not start POT server: {e}")

    # Build base command
    base_cmd = [
        "yt-dlp",
        "--extract-audio",
        "--audio-format", "wav",
        "--postprocessor-args", "ffmpeg:-ar 16000 -ac 1",
        "--format", "worstaudio",
        "--output", output_path + ".%(ext)s",
        "--no-playlist",
    ]
    if proxy:
        base_cmd += ["--proxy", proxy]

    # Try multiple player clients
    try:
        for client in ["ios,web", "android,web", "web"]:
            cmd = base_cmd + ["--extractor-args", f"youtube:player_client={client}", url]
            app.logger.info(f"Trying client={client}, proxy={'yes' if proxy else 'no'}")
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if result.returncode == 0:
                    audio_files = glob.glob(os.path.join(tmpdir, "audio.*"))
                    if audio_files:
                        return audio_files[0], ""
                last_error = result.stderr[:300]
                app.logger.error(f"client={client} failed: {last_error}")
            except subprocess.TimeoutExpired:
                last_error = f"Timeout with client={client}"
            except Exception as e:
                last_error = str(e)

        return None, f"proxy={'yes' if proxy else 'no'}, last_error={last_error}"
    finally:
        # Kill the POT server
        if pot_server:
            pot_server.terminate()
            try:
                pot_server.wait(timeout=5)
            except Exception:
                pot_server.kill()


def transcribe_audio(audio_path: str) -> list[dict]:
    """Transcribe audio with faster-whisper, returning segment+word timestamps."""
    model = get_model()
    segments_iter, info = model.transcribe(
        audio_path,
        language="en",
        beam_size=1,
        word_timestamps=True,
        vad_filter=True,
    )

    segments = []
    for seg in segments_iter:
        segment_data = {
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
        }
        if seg.words:
            segment_data["words"] = [
                {"word": w.word.strip(), "start": round(w.start, 2), "end": round(w.end, 2)}
                for w in seg.words
            ]
        segments.append(segment_data)

    # Clean up audio file
    try:
        os.remove(audio_path)
        os.rmdir(os.path.dirname(audio_path))
    except OSError:
        pass

    return segments
