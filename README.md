# FFmpeg Trim Studio

A password-protected web interface for cutting and encoding media with FFmpeg. Jobs run in a background worker process, so closing the browser does not stop an active job.

## Features

- Upload a media file or provide a direct HTTP/HTTPS URL.
- User-controlled `-ss` start and `-to` end values.
- Optional ASS, SSA, SRT, or VTT upload. Subtitle cues are trimmed, clamped, and rebased to zero before muxing.
- Embedded ASS/SSA/SRT/WebVTT/mov_text subtitle streams are extracted, trimmed, and remuxed; bitmap subtitle streams are preserved in MKV but cannot be cue-trimmed as text.
- MKV, MP4, and WebM outputs.
- MKV preserves text subtitle tracks, data streams, metadata, chapters, and Matroska attachments. Attachments are extracted and reattached so output seeking cannot drop them.
- H.264 and H.265 medium/slow presets.
- AnimeThemes-inspired two-pass VP9 mode using `cpu-used 4` for pass one and `cpu-used 0` for pass two, tile columns, row multithreading, alt-ref frames, lag-in-frames, Opus audio, adaptive GOP, and the optional `hqdn3d,gradfun,unsharp` filter chain.
- Persistent SQLite queue and files under `/data`.
- Live progress, process logs, clear/close log controls, completed-file downloads, and job deletion.
- URL SSRF protection blocks private/local/reserved destinations and validates redirects.
- Interrupted jobs are returned to the queue after a service restart.

## Run with Docker Compose

```bash
cp .env.example .env
# Edit SESSION_SECRET before exposing the app publicly.
docker compose up -d --build
```

Open `http://SERVER-IP:8000`. Set `APP_PASSWORD` in `.env` before use.

## Run without Docker

Install FFmpeg and Python 3.13+, then:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR="$PWD/data"
export APP_PASSWORD='change-me'
export SESSION_SECRET='replace-with-a-long-random-value'
./start.sh
```

## Render deployment

This branch includes `render.yaml`. Deploy it using the Render Blueprint link and enter the requested `APP_PASSWORD` secret in the Render Dashboard.

The Blueprint creates a paid Starter Docker web service in Frankfurt with a 20 GB persistent disk mounted at `/data`. The web and worker processes run in one container so queued FFmpeg jobs continue after the browser closes.

## Important container notes

- MKV is the best output choice when you need ASS styling and original Matroska attachments.
- MP4 converts compatible text subtitles to `mov_text`; ASS styling is not preserved.
- WebM converts compatible text subtitles to WebVTT; Matroska attachments are not preserved.
- Stream-copy cuts are keyframe-dependent. Choose an encoding preset for frame-accurate output.
- This personal single-node design runs one background encoding job at a time to prevent multiple FFmpeg processes from exhausting the server.
