"""
Deterministic unit checks for tts.py — provider option mapping, normalization,
WebSocket message construction, and message parsing. No network required.

Run: ./venv/Scripts/python.exe test_tts.py
"""

import json
import tts


passed = 0
failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


# --- normalize_options -------------------------------------------------------
print("normalize_options:")
o = tts.normalize_options({
    "voice_id": " v1 ", "speed": "1.5", "stability": "80",
    "similarity": "10", "output_format": "MP3", "sample_rate": "24000",
})
check("voice trimmed", o["voice_id"] == "v1")
check("speed parsed", o["speed"] == 1.5)
check("stability int", o["stability"] == 80 and isinstance(o["stability"], int))
check("output_format lowered", o["output_format"] == "mp3")
check("sample_rate int", o["sample_rate"] == 24000)

clamped = tts.normalize_options({"speed": "9", "stability": "999", "similarity": "-5"})
check("speed clamped to 2.0", clamped["speed"] == 2.0)
check("stability clamped to 100", clamped["stability"] == 100)
check("similarity clamped to 0", clamped["similarity"] == 0)

defaults = tts.normalize_options({})
check("default speed", defaults["speed"] == 1.0)
check("default stability", defaults["stability"] == 50)
check("default similarity", defaults["similarity"] == 75)
check("default format mp3", defaults["output_format"] == "mp3")
check("voice_id None when absent", defaults["voice_id"] is None)

# --- registry / aliases ------------------------------------------------------
print("registry:")
keys = {"60db": "K60", "elevenlabs": "KEL"}
check("60db resolves", tts.get_provider("60db", keys).name == "60db")
check("sixtydb alias", tts.get_provider("sixtydb", keys).name == "60db")
check("11labs alias", tts.get_provider("11labs", keys).name == "elevenlabs")
check("eleven_labs alias", tts.get_provider("eleven_labs", keys).name == "elevenlabs")
try:
    tts.get_provider("nope", keys)
    check("unknown raises KeyError", False)
except KeyError:
    check("unknown raises KeyError", True)
try:
    tts.get_provider("60db", {"60db": None})
    check("missing key raises ValueError", False)
except ValueError:
    check("missing key raises ValueError", True)

# --- 60db option mapping -----------------------------------------------------
print("SixtyDBProvider mapping:")
six = tts.get_provider("60db", keys)
opts = tts.normalize_options({"voice_id": "abc", "speed": "1.2", "stability": "30",
                              "similarity": "90", "output_format": "wav"})
body = six._body("hello", opts)
check("60db text", body["text"] == "hello")
check("60db voice", body["voice_id"] == "abc")
check("60db stability stays 0-100", body["stability"] == 30)
check("60db similarity stays 0-100", body["similarity"] == 90)
check("60db format wav", body["output_format"] == "wav")
check("60db default voice when none", six._voice(tts.normalize_options({})) == six.default_voice)

# 60db WS message builders
six.ws_init_msgs = None
wsopts = tts.normalize_options({"voice_id": "vv", "sample_rate": "16000"})


class FakeWS:
    def __init__(self): self.sent = []
    def send(self, m): self.sent.append(m)


fw = FakeWS()
six.ws_init(fw, wsopts)
init = json.loads(fw.sent[0])
check("60db create_context present", "create_context" in init)
check("60db ctx voice", init["create_context"]["voice_id"] == "vv")
check("60db ctx encoding LINEAR16", init["create_context"]["audio_config"]["audio_encoding"] == "LINEAR16")
check("60db ctx sample_rate", init["create_context"]["audio_config"]["sample_rate_hertz"] == 16000)

fw2 = FakeWS()
six.ws_send_text(fw2, wsopts, "hi")
check("60db send_text", json.loads(fw2.sent[0])["send_text"]["text"] == "hi")
six.ws_flush(fw2, wsopts)
check("60db flush_context", "flush_context" in json.loads(fw2.sent[1]))
six.ws_close(fw2, wsopts)
check("60db close_context", "close_context" in json.loads(fw2.sent[2]))

# 60db parsing
check("60db parse audio_chunk", tts.SixtyDBProvider("k").ws_parse(
    json.dumps({"audio_chunk": {"context_id": "c", "audioContent": "AAA"}})
) == [{"type": "audio", "audio_base64": "AAA"}])
check("60db parse flush_completed -> done", tts.SixtyDBProvider("k").ws_parse(
    json.dumps({"flush_completed": {"context_id": "c"}})) == [{"type": "done"}])
check("60db parse error", tts.SixtyDBProvider("k").ws_parse(
    json.dumps({"error": {"message": "bad"}})) == [{"type": "error", "message": "bad"}])
check("60db parse connecting -> ignored", tts.SixtyDBProvider("k").ws_parse(
    json.dumps({"connecting": True})) == [])

# --- ElevenLabs option mapping ----------------------------------------------
print("ElevenLabsProvider mapping:")
el = tts.get_provider("elevenlabs", keys)
elopts = tts.normalize_options({"stability": "30", "similarity": "90", "speed": "1.2"})
vs = el._voice_settings(elopts)
check("EL stability scaled to 0-1", abs(vs["stability"] - 0.30) < 1e-9)
check("EL similarity_boost scaled to 0-1", abs(vs["similarity_boost"] - 0.90) < 1e-9)
check("EL speed passthrough", vs["speed"] == 1.2)
check("EL default model", el._model(tts.normalize_options({})) == el.default_model)
check("EL custom model", el._model(tts.normalize_options({"model_id": "eleven_turbo_v2"})) == "eleven_turbo_v2")
check("EL format mp3 mapping", el._format_value(tts.normalize_options({"output_format": "mp3"})) == "mp3_44100_128")
check("EL format pcm mapping", el._format_value(tts.normalize_options({"output_format": "pcm16"})) == "pcm_16000")
check("EL normalized format mp3", el._normalized_format(tts.normalize_options({"output_format": "mp3"})) == "mp3")
check("EL normalized format pcm16", el._normalized_format(tts.normalize_options({"output_format": "pcm16"})) == "pcm16")

# EL WS builders
fe = FakeWS()
el.ws_init(fe, elopts)
init_el = json.loads(fe.sent[0])
check("EL ws init has voice_settings", "voice_settings" in init_el)
check("EL ws init has generation_config", "generation_config" in init_el)
fe2 = FakeWS()
el.ws_send_text(fe2, elopts, "hi")
check("EL ws send appends space", json.loads(fe2.sent[0])["text"] == "hi ")
el.ws_flush(fe2, elopts)
check("EL ws flush has flush:true", json.loads(fe2.sent[1])["flush"] is True)
el.ws_close(fe2, elopts)
check("EL ws close sends empty text", json.loads(fe2.sent[2])["text"] == "")

# EL parsing
check("EL parse audio", tts.ElevenLabsProvider("k").ws_parse(
    json.dumps({"audio": "BBB"})) == [{"type": "audio", "audio_base64": "BBB"}])
check("EL parse isFinal -> done", tts.ElevenLabsProvider("k").ws_parse(
    json.dumps({"isFinal": True})) == [{"type": "done"}])
check("EL parse garbage -> ignored", tts.ElevenLabsProvider("k").ws_parse("not json") == [])

print(f"\n==== {passed} passed, {failed} failed ====")
raise SystemExit(1 if failed else 0)
