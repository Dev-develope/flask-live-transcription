"""
Text-to-Speech provider abstraction.

This module hides every per-provider quirk behind one consistent interface so
the rest of the app (and the frontend) speaks a single normalized protocol
regardless of which TTS backend is selected via ?provider=.

Two providers are implemented:
- "60db"       -> https://docs.60db.ai
- "elevenlabs" -> https://elevenlabs.io/docs

Normalized options (provider-agnostic), produced by normalize_options():
    voice_id       str    Voice identifier (provider-specific id; falls back to provider default)
    model_id       str    Model id (ElevenLabs only; ignored by 60db)
    speed          float  0.5 - 2.0          (same range for both providers)
    stability      int    0 - 100            (ElevenLabs receives this / 100)
    similarity     int    0 - 100            (ElevenLabs receives this / 100)
    output_format  str    normalized token: "mp3" | "wav" | "pcm16"
    sample_rate    int    PCM/WS sample rate in Hz (default 16000)

Normalized one-shot / stream response (always JSON-friendly):
    {"audio_base64": "<b64>", "output_format": "mp3", "sample_rate": 24000}

Normalized streaming protocol (NDJSON, one JSON object per line):
    {"type": "chunk",    "audio_base64": "<b64>"}
    {"type": "complete"}
    {"type": "error",    "message": "..."}

Normalized WebSocket protocol is documented on WSProxy below.
"""

import base64
import json
import threading

import certifi
import requests
import websocket  # websocket-client (already a dependency for the STT proxy)

# websocket-client uses the system CA store by default, which is unreliable on
# Windows; pin it to certifi (the same bundle requests uses) so WS TLS works
# consistently everywhere REST does.
_SSLOPT = {"ca_certs": certifi.where()}


# ============================================================================
# OPTION NORMALIZATION
# ============================================================================

def _clamp(value, low, high):
    return max(low, min(high, value))


def normalize_options(source):
    """
    Build a normalized options dict from a mapping (Flask request.args or a
    parsed JSON body). Unknown / missing keys fall back to sane defaults.
    """
    def num(key, default):
        raw = source.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    return {
        "voice_id": (source.get("voice_id") or "").strip() or None,
        "model_id": (source.get("model_id") or "").strip() or None,
        "speed": _clamp(num("speed", 1.0), 0.5, 2.0),
        "stability": int(_clamp(num("stability", 50), 0, 100)),
        "similarity": int(_clamp(num("similarity", 75), 0, 100)),
        "output_format": (source.get("output_format") or "mp3").strip().lower(),
        "sample_rate": int(num("sample_rate", 16000)),
    }


# ============================================================================
# BASE PROVIDER
# ============================================================================

class TTSProvider:
    """Common interface every provider implements."""

    name = ""
    default_voice = None

    def __init__(self, api_key):
        if not api_key:
            raise ValueError(f"{self.name} API key is not configured")
        self.api_key = api_key

    def _voice(self, opts):
        return opts.get("voice_id") or self.default_voice

    # --- one-shot synthesis -------------------------------------------------
    def synthesize(self, text, opts):
        """Return a normalized dict: {audio_base64, output_format, sample_rate}."""
        raise NotImplementedError

    # --- chunked / streaming synthesis -------------------------------------
    def stream(self, text, opts):
        """Yield normalized NDJSON-ready dicts: {type, ...}."""
        raise NotImplementedError

    # --- websocket hooks (driven by the generic WSProxy) -------------------
    def ws_connect(self, opts):
        """Open and return a connected websocket-client WebSocket to the provider."""
        raise NotImplementedError

    def ws_init(self, ws, opts):
        """Send any handshake / context-creation messages after connect."""
        raise NotImplementedError

    def ws_send_text(self, ws, opts, text):
        raise NotImplementedError

    def ws_flush(self, ws, opts):
        raise NotImplementedError

    def ws_close(self, ws, opts):
        raise NotImplementedError

    def ws_parse(self, raw):
        """
        Translate one raw provider message into a list of normalized client
        messages. Each item is one of:
            {"type": "audio", "audio_base64": "..."}
            {"type": "done"}
            {"type": "error", "message": "..."}
        Return [] to ignore the raw message (e.g. provider keepalives).
        """
        raise NotImplementedError


# ============================================================================
# 60db PROVIDER
# ============================================================================

class SixtyDBProvider(TTSProvider):
    name = "60db"
    default_voice = "fbb75ed2-975a-40c7-9e06-38e30524a9a1"

    REST_BASE = "https://api.60db.ai"
    WS_URL = "wss://api.60db.ai/ws/tts"

    # normalized output_format token -> 60db output_format value
    _FORMATS = {"mp3": "mp3", "wav": "wav", "ogg": "ogg", "flac": "flac", "pcm16": "wav"}

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _body(self, text, opts):
        return {
            "text": text,
            "voice_id": self._voice(opts),
            "speed": opts["speed"],
            "stability": opts["stability"],     # 60db uses 0-100 natively
            "similarity": opts["similarity"],   # 60db uses 0-100 natively
            "output_format": self._FORMATS.get(opts["output_format"], "mp3"),
        }

    def synthesize(self, text, opts):
        resp = requests.post(
            f"{self.REST_BASE}/tts-synthesize",
            headers=self._headers(),
            json=self._body(text, opts),
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success", True):
            raise RuntimeError(data.get("message", "60db synthesis failed"))
        return {
            "audio_base64": data.get("audio_base64", ""),
            "output_format": data.get("output_format", self._FORMATS.get(opts["output_format"], "mp3")),
            "sample_rate": data.get("sample_rate"),
        }

    def stream(self, text, opts):
        with requests.post(
            f"{self.REST_BASE}/tts-stream",
            headers=self._headers(),
            json=self._body(text, opts),
            stream=True,
            timeout=300,
        ) as resp:
            resp.raise_for_status()
            # 60db streams newline-delimited JSON (NDJSON).
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "chunk":
                    audio = (msg.get("result") or {}).get("audioContent")
                    if audio:
                        yield {"type": "chunk", "audio_base64": audio}
                elif mtype == "complete":
                    yield {"type": "complete"}
                elif mtype == "error":
                    yield {"type": "error", "message": msg.get("message", "60db stream error")}

    # --- websocket ---------------------------------------------------------
    def ws_connect(self, opts):
        url = f"{self.WS_URL}?apiKey={self.api_key}"
        return websocket.create_connection(url, timeout=30, sslopt=_SSLOPT)

    def ws_init(self, ws, opts):
        # context_id is fixed per connection so subsequent messages can reference it.
        opts["_context_id"] = "ctx"
        ws.send(json.dumps({
            "create_context": {
                "context_id": opts["_context_id"],
                "voice_id": self._voice(opts),
                "audio_config": {
                    "audio_encoding": "LINEAR16",
                    "sample_rate_hertz": opts["sample_rate"],
                },
                "speed": opts["speed"],
                "stability": opts["stability"],
                "similarity": opts["similarity"],
            }
        }))

    def ws_send_text(self, ws, opts, text):
        ws.send(json.dumps({"send_text": {"context_id": opts["_context_id"], "text": text}}))

    def ws_flush(self, ws, opts):
        ws.send(json.dumps({"flush_context": {"context_id": opts["_context_id"]}}))

    def ws_close(self, ws, opts):
        ws.send(json.dumps({"close_context": {"context_id": opts["_context_id"]}}))

    def ws_parse(self, raw):
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if "audio_chunk" in msg:
            audio = msg["audio_chunk"].get("audioContent")
            return [{"type": "audio", "audio_base64": audio}] if audio else []
        if "flush_completed" in msg or "context_closed" in msg:
            return [{"type": "done"}]
        if "error" in msg:
            return [{"type": "error", "message": msg["error"].get("message", "60db error")}]
        # connecting / connection_established / context_created -> ignore
        return []


# ============================================================================
# ELEVENLABS PROVIDER
# ============================================================================

class ElevenLabsProvider(TTSProvider):
    name = "elevenlabs"
    default_voice = "21m00Tcm4TlvDq8ikWAM"  # "Rachel" — a standard public voice
    default_model = "eleven_multilingual_v2"

    REST_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
    WS_BASE = "wss://api.elevenlabs.io/v1/text-to-speech"

    # normalized output_format token -> ElevenLabs output_format value
    _FORMATS = {"mp3": "mp3_44100_128", "pcm16": "pcm_16000", "wav": "pcm_16000"}

    def _model(self, opts):
        return opts.get("model_id") or self.default_model

    def _headers(self):
        return {"xi-api-key": self.api_key, "Content-Type": "application/json"}

    def _voice_settings(self, opts):
        # ElevenLabs expects 0.0 - 1.0 for stability / similarity_boost.
        return {
            "stability": opts["stability"] / 100.0,
            "similarity_boost": opts["similarity"] / 100.0,
            "speed": opts["speed"],
        }

    def _format_value(self, opts):
        return self._FORMATS.get(opts["output_format"], "mp3_44100_128")

    def _normalized_format(self, opts):
        # what the client should treat the bytes as
        return "mp3" if self._format_value(opts).startswith("mp3") else "pcm16"

    def synthesize(self, text, opts):
        fmt = self._format_value(opts)
        resp = requests.post(
            f"{self.REST_BASE}/{self._voice(opts)}",
            headers=self._headers(),
            params={"output_format": fmt},
            json={
                "text": text,
                "model_id": self._model(opts),
                "voice_settings": self._voice_settings(opts),
            },
            timeout=60,
        )
        resp.raise_for_status()
        return {
            "audio_base64": base64.b64encode(resp.content).decode("ascii"),
            "output_format": self._normalized_format(opts),
            "sample_rate": 16000 if fmt.startswith("pcm") else None,
        }

    def stream(self, text, opts):
        fmt = self._format_value(opts)
        with requests.post(
            f"{self.REST_BASE}/{self._voice(opts)}/stream",
            headers=self._headers(),
            params={"output_format": fmt},
            json={
                "text": text,
                "model_id": self._model(opts),
                "voice_settings": self._voice_settings(opts),
            },
            stream=True,
            timeout=300,
        ) as resp:
            resp.raise_for_status()
            # ElevenLabs streams raw audio bytes; wrap each chunk as normalized NDJSON.
            for chunk in resp.iter_content(chunk_size=4096):
                if chunk:
                    yield {"type": "chunk", "audio_base64": base64.b64encode(chunk).decode("ascii")}
            yield {"type": "complete"}

    # --- websocket ---------------------------------------------------------
    def ws_connect(self, opts):
        fmt = self._FORMATS.get(opts["output_format"], "pcm_16000")
        # Force a PCM format for the live WS path so chunks are concatenatable.
        if not fmt.startswith("pcm"):
            fmt = "pcm_16000"
        opts["_ws_format"] = fmt
        url = f"{self.WS_BASE}/{self._voice(opts)}/stream-input?model_id={self._model(opts)}&output_format={fmt}"
        return websocket.create_connection(url, header=[f"xi-api-key: {self.api_key}"], timeout=30, sslopt=_SSLOPT)

    def ws_init(self, ws, opts):
        ws.send(json.dumps({
            "text": " ",
            "voice_settings": self._voice_settings(opts),
            "generation_config": {"chunk_length_schedule": [120, 160, 250, 290]},
        }))

    def ws_send_text(self, ws, opts, text):
        # A trailing space helps ElevenLabs tokenize buffered text correctly.
        ws.send(json.dumps({"text": text + " ", "try_trigger_generation": True}))

    def ws_flush(self, ws, opts):
        ws.send(json.dumps({"text": " ", "flush": True}))

    def ws_close(self, ws, opts):
        ws.send(json.dumps({"text": ""}))

    def ws_parse(self, raw):
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        out = []
        if msg.get("audio"):
            out.append({"type": "audio", "audio_base64": msg["audio"]})
        if msg.get("isFinal"):
            out.append({"type": "done"})
        if msg.get("error"):
            out.append({"type": "error", "message": str(msg.get("message") or msg.get("error"))})
        return out


# ============================================================================
# REGISTRY
# ============================================================================

_PROVIDERS = {
    SixtyDBProvider.name: SixtyDBProvider,
    ElevenLabsProvider.name: ElevenLabsProvider,
}

# Accept a few friendly aliases for the provider query param.
_ALIASES = {
    "60db": "60db",
    "sixtydb": "60db",
    "sixty_db": "60db",
    "elevenlabs": "elevenlabs",
    "eleven_labs": "elevenlabs",
    "11labs": "elevenlabs",
}


def get_provider(name, api_keys):
    """
    Resolve a provider by (possibly aliased) name and instantiate it with the
    matching API key from api_keys = {"60db": ..., "elevenlabs": ...}.

    Raises KeyError for an unknown provider and ValueError if its key is unset.
    """
    canonical = _ALIASES.get((name or "").strip().lower())
    if canonical is None:
        raise KeyError(name)
    cls = _PROVIDERS[canonical]
    return cls(api_keys.get(canonical))


def available_providers():
    return list(_PROVIDERS.keys())


# ============================================================================
# GENERIC WEBSOCKET PROXY
# ============================================================================

class WSProxy:
    """
    Bridges one client WebSocket to one provider WebSocket using the provider's
    ws_* hooks, exposing a single normalized protocol to the client.

    Client -> server (JSON text frames):
        {"type": "text",  "text": "..."}   append text to the synthesis buffer
        {"type": "flush"}                   trigger synthesis of buffered text
        {"type": "close"}                   flush remaining text and end the session

    Server -> client (JSON text frames):
        {"type": "audio", "audio_base64": "...", "encoding": "pcm16", "sample_rate": 16000}
        {"type": "done"}
        {"type": "error", "message": "..."}

    Synthesis config (voice/speed/etc.) is taken from the normalized `opts`
    captured from the WS query string at connect time.
    """

    def __init__(self, client_ws, provider, opts):
        self.client_ws = client_ws
        self.provider = provider
        self.opts = opts
        self.upstream = None
        self.stop = threading.Event()

    def _send_client(self, obj):
        try:
            self.client_ws.send(json.dumps(obj))
        except Exception:
            self.stop.set()

    def _pump_upstream(self):
        """Read provider messages and forward normalized audio to the client."""
        encoding = "pcm16"
        sample_rate = self.opts["sample_rate"]
        try:
            while not self.stop.is_set():
                raw = self.upstream.recv()
                if raw is None or raw == "":
                    continue
                for out in self.provider.ws_parse(raw):
                    if out["type"] == "audio":
                        out["encoding"] = encoding
                        out["sample_rate"] = sample_rate
                    self._send_client(out)
                    if out["type"] == "done":
                        self.stop.set()
        except Exception as e:
            if not self.stop.is_set():
                self._send_client({"type": "error", "message": f"upstream closed: {e}"})
        finally:
            self.stop.set()

    def run(self):
        try:
            self.upstream = self.provider.ws_connect(self.opts)
            self.provider.ws_init(self.upstream, self.opts)
        except Exception as e:
            self._send_client({"type": "error", "message": f"failed to connect to {self.provider.name}: {e}"})
            return

        reader = threading.Thread(target=self._pump_upstream, daemon=True)
        reader.start()

        try:
            while not self.stop.is_set():
                message = self.client_ws.receive(timeout=0.1)
                if message is None:
                    continue
                try:
                    msg = json.loads(message)
                except (json.JSONDecodeError, TypeError):
                    continue

                mtype = msg.get("type")
                if mtype == "text" and msg.get("text"):
                    self.provider.ws_send_text(self.upstream, self.opts, msg["text"])
                elif mtype == "flush":
                    self.provider.ws_flush(self.upstream, self.opts)
                elif mtype == "close":
                    self.provider.ws_close(self.upstream, self.opts)
                    # let the reader drain remaining audio; it sets stop on "done"
        except Exception as e:
            if "timeout" not in str(e).lower():
                self._send_client({"type": "error", "message": str(e)})
        finally:
            self.stop.set()
            try:
                self.upstream.close()
            except Exception:
                pass
