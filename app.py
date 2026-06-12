"""
Flask Live Transcription Starter - Backend Server

Simple WebSocket proxy to Deepgram's Live STT API.
Forwards all messages (JSON and binary) bidirectionally between client and Deepgram.

API Endpoints:
- WS /api/live-transcription - WebSocket endpoint for live transcription
- GET /api/session - JWT session token endpoint
- GET /api/metadata - Returns metadata from deepgram.toml
"""

import functools
import json
import os
import secrets
import sys
import threading
import time

import jwt
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_sock import Sock
from flask_cors import CORS
from simple_websocket import Server as _WsServer
from urllib.parse import urlencode
import websocket
import toml
from dotenv import load_dotenv

import tts

# Monkey-patch simple-websocket to echo back the access_token.* subprotocol.
# flask-sock uses simple-websocket's Server class for the WebSocket handshake.
# By default, Server.choose_subprotocol only accepts subprotocols that are in a
# static allow-list, which doesn't work for dynamic JWT-bearing subprotocols.
# This override makes the server echo back any access_token.* subprotocol so the
# client receives the Sec-WebSocket-Protocol response header it expects.
_original_choose_subprotocol = _WsServer.choose_subprotocol


def _choose_subprotocol_with_token(self, ws_request):
    for proto in ws_request.subprotocols:
        if proto.startswith("access_token."):
            return proto
    return _original_choose_subprotocol(self, ws_request)


_WsServer.choose_subprotocol = _choose_subprotocol_with_token

# Load .env file (won't override existing environment variables)
load_dotenv(override=False)

# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_MODEL = "nova-3"
DEFAULT_LANGUAGE = "en"

# Server configuration
CONFIG = {
    "port": int(os.environ.get("PORT", 8081)),
    "host": os.environ.get("HOST", "0.0.0.0"),
}

# ============================================================================
# SESSION AUTH - JWT tokens with rate limiting for production security
# ============================================================================

SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
JWT_EXPIRY = 3600  # 1 hour


def validate_ws_token():
    """Validates JWT from Sec-WebSocket-Protocol: access_token.<jwt> header."""
    protocol_header = request.headers.get("Sec-WebSocket-Protocol", "")
    protocols = [p.strip() for p in protocol_header.split(",")]
    token_proto = next((p for p in protocols if p.startswith("access_token.")), None)
    if not token_proto:
        return None
    token = token_proto[len("access_token."):]
    try:
        jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
        return token_proto
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def validate_http_token():
    """Validates a session JWT from the `Authorization: Bearer <jwt>` header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[len("Bearer "):].strip()
    try:
        jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
        return True
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return False


# ============================================================================
# TTS PROVIDER KEYS - 60db and ElevenLabs (validated lazily per request)
# ============================================================================

TTS_API_KEYS = {
    "60db": os.environ.get("SIXTYDB_API_KEY"),
    "elevenlabs": os.environ.get("ELEVENLABS_API_KEY"),
}


def resolve_tts_provider():
    """
    Resolve the TTS provider for this request from ?provider=, instantiated with
    its configured key. Returns (provider, None) on success or (None, (json, status))
    describing the error so the caller can return it.
    """
    name = request.args.get("provider", "60db")
    try:
        provider = tts.get_provider(name, TTS_API_KEYS)
    except KeyError:
        return None, (jsonify({
            "error": "BAD_REQUEST",
            "message": f"Unknown provider '{name}'. Supported: {tts.available_providers()}",
        }), 400)
    except ValueError as e:
        return None, (jsonify({"error": "CONFIG_ERROR", "message": str(e)}), 500)
    return provider, None


# ============================================================================
# API KEY VALIDATION
# ============================================================================

def validate_api_key():
    """Validates that the Deepgram API key is configured"""
    api_key = os.environ.get("DEEPGRAM_API_KEY")

    if not api_key:
        print("\n" + "="*70)
        print("ERROR: Deepgram API key not found!")
        print("="*70)
        print("\nPlease set your API key using one of these methods:")
        print("\n1. Create a .env file (recommended):")
        print("   DEEPGRAM_API_KEY=your_api_key_here")
        print("\n2. Environment variable:")
        print("   export DEEPGRAM_API_KEY=your_api_key_here")
        print("\nGet your API key at: https://console.deepgram.com")
        print("="*70 + "\n")
        raise ValueError("DEEPGRAM_API_KEY environment variable is required")

    return api_key

# Validate on startup
API_KEY = validate_api_key()

# ============================================================================
# SETUP - Initialize Flask, WebSocket, and CORS
# ============================================================================

# Initialize Flask app (API server only)
app = Flask(__name__)

# Enable CORS for frontend communication
CORS(app)

# Initialize native WebSocket support
sock = Sock(app)

# ============================================================================
# SESSION ROUTES - Auth endpoints (unprotected)
# ============================================================================

@app.route("/", methods=["GET"])
def serve_index():
    """Serve the built frontend index.html."""
    frontend_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")
    if not os.path.isfile(os.path.join(frontend_dir, "index.html")):
        return "Frontend not built. Run make build first.", 404
    return send_from_directory(frontend_dir, "index.html")


@app.route("/api/session", methods=["GET"])
def get_session():
    """Issues a JWT for session authentication."""
    token = jwt.encode(
        {"iat": int(time.time()), "exp": int(time.time()) + JWT_EXPIRY},
        SESSION_SECRET,
        algorithm="HS256",
    )
    return jsonify({"token": token})


# ============================================================================
# HTTP ROUTES
# ============================================================================

@app.route("/api/metadata", methods=["GET"])
def get_metadata():
    """
    GET /api/metadata

    Returns metadata about this starter application from deepgram.toml
    Required for standardization compliance
    """
    try:
        with open('deepgram.toml', 'r', encoding='utf-8') as f:
            config = toml.load(f)

        if 'meta' not in config:
            return jsonify({
                'error': 'INTERNAL_SERVER_ERROR',
                'message': 'Missing [meta] section in deepgram.toml'
            }), 500

        return jsonify(config['meta']), 200

    except FileNotFoundError:
        return jsonify({
            'error': 'INTERNAL_SERVER_ERROR',
            'message': 'deepgram.toml file not found'
        }), 500

    except Exception as e:
        print(f"Error reading metadata: {e}")
        return jsonify({
            'error': 'INTERNAL_SERVER_ERROR',
            'message': f'Failed to read metadata from deepgram.toml: {str(e)}'
        }), 500

# ============================================================================
# TTS ROUTES - Text-to-Speech (provider selected via ?provider=60db|elevenlabs)
# ============================================================================

def _tts_request_payload():
    """Extract (text, normalized_options) from a JSON POST body, or an error tuple."""
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return None, None, (jsonify({"error": "BAD_REQUEST", "message": "'text' is required"}), 400)
    return text, tts.normalize_options(body), None


@app.route("/api/tts", methods=["POST"])
def tts_synthesize():
    """One-shot synthesis. Returns {audio_base64, output_format, sample_rate}."""
    if not validate_http_token():
        return jsonify({"error": "UNAUTHORIZED", "message": "Valid session token required"}), 401

    provider, err = resolve_tts_provider()
    if err:
        return err
    text, opts, err = _tts_request_payload()
    if err:
        return err

    try:
        result = provider.synthesize(text, opts)
        return jsonify({"success": True, "provider": provider.name, **result})
    except Exception as e:
        print(f"TTS synthesize error ({provider.name}): {e}")
        return jsonify({"error": "TTS_ERROR", "message": str(e)}), 502


@app.route("/api/tts/stream", methods=["POST"])
def tts_stream():
    """Streaming synthesis. Responds with newline-delimited JSON (NDJSON) chunks."""
    if not validate_http_token():
        return jsonify({"error": "UNAUTHORIZED", "message": "Valid session token required"}), 401

    provider, err = resolve_tts_provider()
    if err:
        return err
    text, opts, err = _tts_request_payload()
    if err:
        return err

    def generate():
        try:
            for chunk in provider.stream(text, opts):
                yield json.dumps(chunk) + "\n"
        except Exception as e:
            print(f"TTS stream error ({provider.name}): {e}")
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@sock.route('/api/tts/ws')
def tts_websocket(ws):
    """
    Live TTS WebSocket. Authenticated with the same access_token.<jwt> subprotocol
    as the STT endpoint. Config comes from query params (provider, voice_id,
    speed, stability, similarity, output_format, sample_rate). Speaks the
    normalized WSProxy protocol documented in tts.py.
    """
    if not validate_ws_token():
        ws.close(4401, "Unauthorized")
        return

    name = request.args.get("provider", "60db")
    try:
        provider = tts.get_provider(name, TTS_API_KEYS)
    except KeyError:
        ws.send(json.dumps({"type": "error", "message": f"Unknown provider '{name}'"}))
        ws.close(1008, "Unknown provider")
        return
    except ValueError as e:
        ws.send(json.dumps({"type": "error", "message": str(e)}))
        ws.close(1011, "Provider not configured")
        return

    opts = tts.normalize_options(request.args)
    print(f"Client connected to /api/tts/ws (provider={provider.name})")
    try:
        tts.WSProxy(ws, provider, opts).run()
    finally:
        print("Client disconnected from /api/tts/ws")


# ============================================================================
# WEBSOCKET ENDPOINT
# ============================================================================

@sock.route('/api/live-transcription')
def live_transcription(ws):
    """
    WebSocket endpoint for live speech-to-text transcription
    Simple bidirectional proxy to Deepgram's Live STT API

    Query parameters:
    - model: Deepgram model (default: nova-3)
    - language: Language code (default: en)
    - encoding: Audio encoding (default: linear16)
    - sample_rate: Sample rate in Hz (default: 16000)
    - channels: Number of audio channels (default: 1)

    The client sends binary audio data and receives JSON transcription messages.
    """
    # Validate JWT from WebSocket subprotocol
    valid_proto = validate_ws_token()
    if not valid_proto:
        ws.close(4401, "Unauthorized")
        return

    print("Client connected to /api/live-transcription")

    # Get query parameters from request
    model = request.args.get('model', DEFAULT_MODEL)
    language = request.args.get('language', DEFAULT_LANGUAGE)
    smart_format = request.args.get('smart_format', 'true')
    encoding = request.args.get('encoding', 'linear16')
    sample_rate = request.args.get('sample_rate', '16000')
    channels = request.args.get('channels', '1')

    print(f"STT Config - model: {model}, language: {language}, encoding: {encoding}, sample_rate: {sample_rate}, channels: {channels}")

    # Build Deepgram WebSocket URL with query parameters
    deepgram_params = {
        'model': model,
        'language': language,
        'smart_format': smart_format,
        'encoding': encoding,
        'sample_rate': sample_rate,
        'channels': channels
    }
    deepgram_url = f"wss://api.deepgram.com/v1/listen?{urlencode(deepgram_params)}"

    # Message counters for logging
    client_message_count = 0
    deepgram_message_count = 0
    stop_event = threading.Event()
    deepgram_ready = threading.Event()

    def on_deepgram_message(dg_ws, message):
        """Forward messages from Deepgram to client"""
        nonlocal deepgram_message_count

        # Wait for client to be ready before forwarding
        if not deepgram_ready.wait(timeout=5):
            print("Timeout waiting for client to be ready")
            stop_event.set()
            return

        deepgram_message_count += 1

        # Log every 10th message or non-binary messages
        if deepgram_message_count % 10 == 0 or isinstance(message, str):
            print(f"← Deepgram message #{deepgram_message_count}")

        try:
            ws.send(message)
        except Exception as e:
            print(f"Error forwarding to client: {e}")
            stop_event.set()

    def on_deepgram_error(dg_ws, error):
        """Handle Deepgram errors"""
        print(f"Deepgram error: {error}")
        stop_event.set()

    def on_deepgram_close(dg_ws, close_status_code, close_msg):
        """Handle Deepgram connection close"""
        print(f"Deepgram connection closed: {close_status_code} {close_msg}")
        stop_event.set()

    def on_deepgram_open(dg_ws):
        """Handle Deepgram connection open"""
        print("✓ Connected to Deepgram STT API")

    # Create WebSocket connection to Deepgram
    try:
        deepgram_ws = websocket.WebSocketApp(
            deepgram_url,
            header={
                'Authorization': f'Token {API_KEY}'
            },
            on_open=on_deepgram_open,
            on_message=on_deepgram_message,
            on_error=on_deepgram_error,
            on_close=on_deepgram_close
        )

        # Run Deepgram WebSocket in background thread
        dg_thread = threading.Thread(target=deepgram_ws.run_forever)
        dg_thread.daemon = True
        dg_thread.start()

        # Wait a moment for Deepgram connection to initialize
        time.sleep(0.1)

        # Signal that we're ready to receive Deepgram messages
        deepgram_ready.set()
        print("✓ Ready to forward messages")

        # Forward messages from client to Deepgram
        while not stop_event.is_set():
            try:
                # Receive message from client (with timeout)
                message = ws.receive(timeout=0.1)
                if message is None:
                    continue

                client_message_count += 1

                # Log every 100th binary message
                if client_message_count % 100 == 0:
                    print(f"→ Client message #{client_message_count}")

                # Forward to Deepgram
                if isinstance(message, bytes):
                    deepgram_ws.send(message, opcode=websocket.ABNF.OPCODE_BINARY)
                else:
                    deepgram_ws.send(message)

            except Exception as e:
                if "timeout" not in str(e).lower():
                    print(f"Error in client message loop: {e}")
                    break

    except Exception as e:
        print(f"Error setting up STT connection: {e}")
        try:
            ws.close(1011, "Internal server error")
        except:
            pass
        return

    finally:
        # Cleanup
        print("Cleaning up STT connection")
        stop_event.set()
        try:
            deepgram_ws.close()
        except Exception as e:
            print(f"Error closing Deepgram connection: {e}")

        print("Client disconnected from /api/live-transcription")

# ============================================================================
# SERVER START
# ============================================================================

if __name__ == "__main__":
    # Emoji banner below is UTF-8; Windows consoles default to cp1252 and would
    # crash on encode. Reconfigure stdout/stderr to UTF-8 where supported.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    port = CONFIG["port"]
    host = CONFIG["host"]
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    print("\n" + "=" * 70)
    print(f"🚀 Flask Live Transcription Server (Backend API)")
    print("=" * 70)
    print(f"Server:   http://{host}:{port}")
    print(f"Debug:    {'ON' if debug else 'OFF'}")
    print("")
    print("📡 GET  /api/session")
    print("📡 WS   /api/live-transcription (auth required)")
    print("📡 GET  /api/metadata")
    print("🔊 POST /api/tts            (auth required) - one-shot TTS")
    print("🔊 POST /api/tts/stream     (auth required) - NDJSON streaming TTS")
    print("🔊 WS   /api/tts/ws         (auth required) - live TTS")
    print("    providers: ?provider=60db | elevenlabs")
    print("=" * 70 + "\n")

    app.run(host=host, port=port, debug=debug)
