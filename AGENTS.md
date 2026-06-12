# flask-live-transcription

Flask demo app for Deepgram Live Transcription.

## Architecture

- **Backend:** Flask (Python) on port 8081
- **Frontend:** Vite + vanilla JS on port 8080 (git submodule: `live-transcription-html`)
- **API type:** WebSocket — `WS /api/live-transcription`
- **Deepgram API:** Live Speech-to-Text (`wss://api.deepgram.com/v1/listen`)
- **Auth:** JWT session tokens via `/api/session` (WebSocket auth uses `access_token.<jwt>` subprotocol)

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | Main backend — API endpoints and WebSocket proxy |
| `deepgram.toml` | Metadata, lifecycle commands, tags |
| `Makefile` | Standardized build/run targets |
| `sample.env` | Environment variable template |
| `frontend/main.js` | Frontend logic — UI controls, WebSocket connection, audio streaming |
| `frontend/index.html` | HTML structure and UI layout |
| `deploy/Dockerfile` | Production container (Caddy + backend) |
| `deploy/Caddyfile` | Reverse proxy, rate limiting, static serving |

## Quick Start

```bash
# Initialize (clone submodules + install deps)
make init

# Set up environment
test -f .env || cp sample.env .env  # then set DEEPGRAM_API_KEY

# Start both servers
make start
# Backend: http://localhost:8081
# Frontend: http://localhost:8080
```

## Start / Stop

**Start (recommended):**
```bash
make start
```

**Start separately:**
```bash
# Terminal 1 — Backend
./venv/bin/python app.py

# Terminal 2 — Frontend
cd frontend && corepack pnpm run dev -- --port 8080 --no-open
```

**Stop all:**
```bash
lsof -ti:8080,8081 | xargs kill -9 2>/dev/null
```

**Clean rebuild:**
```bash
rm -rf venv frontend/node_modules frontend/.vite
make init
```

## Dependencies

- **Backend:** `requirements.txt` — Uses Python venv for isolation. Always activate venv before running.
- **Frontend:** `frontend/package.json` — Vite dev server
- **Submodules:** `frontend/` (live-transcription-html), `contracts/` (starter-contracts)

Install: `python3 -m venv venv && ./venv/bin/pip install -r requirements.txt`
Frontend: `cd frontend && corepack pnpm install`

## API Endpoints

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/api/session` | GET | None | Issue JWT session token |
| `/api/metadata` | GET | None | Return app metadata (useCase, framework, language) |
| `/api/live-transcription` | WS | JWT | Streams microphone audio to Deepgram for real-time transcription. |
| `/api/tts` | POST | JWT (Bearer) | One-shot text-to-speech. Returns `{audio_base64, output_format, sample_rate}`. |
| `/api/tts/stream` | POST | JWT (Bearer) | Streaming text-to-speech as newline-delimited JSON (NDJSON) chunks. |
| `/api/tts/ws` | WS | JWT (subprotocol) | Live text-to-speech; normalized WebSocket protocol (see `tts.py`). |

## Text-to-Speech (TTS)

In addition to Deepgram speech-to-text, the app provides TTS through two
interchangeable providers behind one normalized interface. The provider is
selected per-request via the `?provider=` query param (`60db` or `elevenlabs`).

- **Provider abstraction:** `tts.py` — `TTSProvider` base class + `SixtyDBProvider`
  + `ElevenLabsProvider` + a registry (`get_provider`). Each provider implements
  `synthesize()` (one-shot), `stream()` (NDJSON), and the `ws_*` hooks used by the
  generic `WSProxy`.
- **Normalized options** (same for both providers): `voice_id`, `model_id`
  (ElevenLabs only), `speed` (0.5–2.0), `stability` (0–100), `similarity` (0–100),
  `output_format` (`mp3` | `wav` | `pcm16`), `sample_rate`. The abstraction converts
  these to each provider's native shape (e.g. ElevenLabs receives `stability`/`similarity`
  as 0.0–1.0).
- **Normalized responses:** one-shot/stream return base64 audio in JSON/NDJSON
  regardless of provider; the live WebSocket emits `{type: "audio"|"done"|"error"}`.
- **Frontend:** `frontend/tts.js` drives a "Synthesize" tab (added to `index.html`)
  with all three modes and audio playback (live PCM is scheduled via Web Audio).

To add a third provider: subclass `TTSProvider`, implement the methods, and register
it in `_PROVIDERS` in `tts.py`. No route or frontend changes are required.

## Customization Guide

### Changing Default Parameters
The WebSocket connection URL passes parameters to Deepgram. Find where the Deepgram WebSocket URL is constructed in the backend and modify defaults:

| Parameter | Default | Options | Effect |
|-----------|---------|---------|--------|
| `model` | `nova-3` | `nova-3`, `nova-2`, `base` | STT model |
| `language` | `en` | Any BCP-47 code | Transcription language |
| `smart_format` | `true` | `true`/`false` | Smart formatting |
| `encoding` | `linear16` | `linear16`, `opus`, `flac` | Audio encoding |
| `sample_rate` | `16000` | `8000`, `16000`, `44100`, `48000` | Audio sample rate |
| `channels` | `1` | `1`, `2` | Mono or stereo |

### Adding More Deepgram Features via Query Params
These can be appended to the Deepgram WebSocket URL as query parameters:

| Feature | Parameter | Example | Effect |
|---------|-----------|---------|--------|
| Interim results | `interim_results` | `true` | Show partial transcripts while speaking |
| Endpointing | `endpointing` | `300` | Silence duration (ms) before finalization |
| Utterance end | `utterance_end_ms` | `1000` | Detect end of utterance |
| VAD events | `vad_events` | `true` | Voice activity detection events |
| Diarization | `diarize` | `true` | Speaker identification |
| Punctuation | `punctuate` | `true` | Auto-punctuation |
| Keywords | `keywords` | `deepgram:2` | Boost keyword with weight |
| No delay | `no_delay` | `true` | Minimize latency (may reduce accuracy) |

**Backend:** Append params to the Deepgram URL in the WebSocket proxy handler.

**Frontend:** The frontend sends these as query params when opening the WebSocket. To add a UI control for a new param, edit `frontend/main.js` — add an input/checkbox and include it in the `URLSearchParams` when connecting.

### Changing Audio Format
If changing from browser microphone (Linear16) to another source:
1. Update `encoding` and `sample_rate` params
2. The frontend captures audio via `AudioContext` at 16kHz and converts Float32 → Int16 PCM
3. If your audio source uses a different format, modify the frontend audio processing pipeline

## Frontend Changes

The frontend is a git submodule from `deepgram-starters/live-transcription-html`. To modify:

1. **Edit files in `frontend/`** — this is the working copy
2. **Test locally** — changes reflect immediately via Vite HMR
3. **Commit in the submodule:** `cd frontend && git add . && git commit -m "feat: description"`
4. **Push the frontend repo:** `cd frontend && git push origin main`
5. **Update the submodule ref:** `cd .. && git add frontend && git commit -m "chore(deps): update frontend submodule"`

**IMPORTANT:** Always edit `frontend/` inside THIS starter directory. The standalone `live-transcription-html/` directory at the monorepo root is a separate checkout.

### Adding a UI Control for a New Feature
1. Add the HTML element in `frontend/index.html` (input, checkbox, dropdown, etc.)
2. Read the value in `frontend/main.js` when making the API call or opening the WebSocket
3. Pass it as a query parameter in the WebSocket URL
4. Handle it in the backend `app.py` — read the param and pass it to the Deepgram API

## Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DEEPGRAM_API_KEY` | Yes | — | Deepgram API key (speech-to-text) |
| `SIXTYDB_API_KEY` | For 60db TTS | — | 60db API key (text-to-speech) |
| `ELEVENLABS_API_KEY` | For ElevenLabs TTS | — | ElevenLabs API key (text-to-speech) |
| `PORT` | No | `8081` | Backend server port |
| `HOST` | No | `0.0.0.0` | Backend bind address |
| `SESSION_SECRET` | No | — | JWT signing secret (production) |

## Conventional Commits

All commits must follow conventional commits format. Never include `Co-Authored-By` lines for Claude.

```
feat(flask-live-transcription): add diarization support
fix(flask-live-transcription): resolve WebSocket close handling
refactor(flask-live-transcription): simplify session endpoint
chore(deps): update frontend submodule
```

## Testing

```bash
# Run conformance tests (requires app to be running)
make test

# Manual endpoint check
curl -sf http://localhost:8081/api/metadata | python3 -m json.tool
curl -sf http://localhost:8081/api/session | python3 -m json.tool
```
