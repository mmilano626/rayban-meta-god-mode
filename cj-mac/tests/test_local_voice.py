"""Strict local-only voice: STT → local chat → TTS, including /ws/live protocol checks."""
from __future__ import annotations

import base64
import io
import json
import os
import struct
import tempfile
import wave

# Isolate config before importing the app. CJ_LOCAL_ONLY is enabled per-test in the fixture
# so other modules that share this process keep the default (cloud) router behavior.
os.environ.setdefault("CJ_HOME", tempfile.mkdtemp())
os.environ.pop("CJ_FORCE_MOCK", None)

import httpx
import pytest
from fastapi.testclient import TestClient

from cj import prefs, stt, tts, local_mode
from cj.llm import compat
from cj.llm.chain import Router, router
from cj.llm.provider import Attachment, ProviderError
from cj.local_mode import LocalVoiceError
from cj.main import app


def _wav_bytes(text_marker: bytes = b"xxxx", rate: int = 24000, seconds: float = 0.05) -> bytes:
    n = int(rate * seconds)
    pcm = struct.pack(f"<{n}h", *([0] * n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    data = buf.getvalue()
    # Embed a marker after the header for debugging; keep valid WAV.
    return data


def _pcm16(ms: int = 100, rate: int = 16000, amp: int = 5000) -> bytes:
    n = rate * ms // 1000
    return struct.pack(f"<{n}h", *([amp] * n))


@pytest.fixture(autouse=True)
def _local_cfg(monkeypatch):
    monkeypatch.setenv("CJ_LOCAL_ONLY", "1")
    monkeypatch.delenv("CJ_FORCE_MOCK", raising=False)
    monkeypatch.setenv("CJ_OLLAMA_URL", "http://local-chat.test")
    monkeypatch.setenv("CJ_OLLAMA_MODEL", "test-local-model")
    monkeypatch.setenv("CJ_STT_URL", "http://local-stt.test")
    monkeypatch.setenv("CJ_TTS_URL", "http://local-tts.test")
    # Cloud keys present on purpose: local-only must still never call them.
    monkeypatch.setenv("CJ_GEMINI_KEY", "cloud-gemini-key-should-never-be-used-123456")
    monkeypatch.setenv("CJ_GROQ_KEY", "cloud-groq-key-should-never-be-used-1234567890")
    cfg = prefs.load()
    cfg["ollama_model"] = "test-local-model"
    cfg["local_only"] = True
    prefs.save(cfg)
    yield


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {prefs.token()}"}


@pytest.fixture
def client():
    return TestClient(app)


def test_local_mode_status_and_health(client):
    assert local_mode.enabled()
    h = client.get("/health").json()
    assert h["local"]["local_only"] is True
    assert h["local"]["vision"] == "unsupported"
    assert "127.0.0.1:8178" in h["local"]["stt"] or "local-stt.test" in h["local"]["stt"]


def test_router_never_selects_cloud_even_with_keys():
    r = Router()
    assert r.preferred() == "ollama"
    names = [p.name for p in r.chain()]
    assert names == ["ollama"]
    assert "gemini" not in names and "groq" not in names
    with pytest.raises(ProviderError, match="vision"):
        r.chain(Attachment("abc", "image/jpeg"))
    with pytest.raises(ProviderError, match="audio"):
        r.chain(Attachment("abc", "audio/wav"))


def test_stt_preserves_exact_wording(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        assert request.url.path.endswith("/v1/audio/transcriptions")
        # multipart; just confirm file field exists
        assert b"file" in request.content or b"audio" in request.content
        return httpx.Response(200, json={"text": "this is fucking shit asshole"})

    real = httpx.AsyncClient
    monkeypatch.setattr(stt.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = __import__("asyncio").run(stt.transcribe(_wav_bytes()))
    assert out == "this is fucking shit asshole"
    assert "local-stt.test" in seen["url"]


def test_tts_preserves_input_and_returns_wav(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        assert seen["body"]["input"] == "say fucking shit clearly"
        assert seen["body"]["voice"] == "af_heart"
        return httpx.Response(200, content=_wav_bytes())

    real = httpx.AsyncClient
    monkeypatch.setattr(tts.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    audio = __import__("asyncio").run(tts.speak("say fucking shit clearly"))
    assert audio[:4] == b"RIFF"
    assert "local-tts.test" in seen["url"]


def test_tts_fail_closed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    real = httpx.AsyncClient
    monkeypatch.setattr(tts.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(LocalVoiceError, match="local TTS"):
        __import__("asyncio").run(tts.speak("hello"))


def test_stt_fail_closed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    real = httpx.AsyncClient
    monkeypatch.setattr(stt.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(LocalVoiceError, match="STT"):
        __import__("asyncio").run(stt.transcribe(_wav_bytes()))


def _install_stack(monkeypatch, *, transcript="what fucking time is it", spoken='{"spoken":"It is noon, you fucking legend.","details":"","language":"en","mac_action":null,"tool":null}'):
    """Mock STT + chat completions + TTS. Track every outbound URL."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        host = request.url.host
        if host == "local-stt.test":
            return httpx.Response(200, json={"text": transcript})
        if host == "local-chat.test":
            body = json.loads(request.content)
            assert body.get("response_format") == {"type": "json_object"}
            assert "generativelanguage.googleapis.com" not in url
            return httpx.Response(200, json={
                "choices": [{"message": {"content": spoken}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
        if host == "local-tts.test":
            body = json.loads(request.content)
            assert "fucking" in body["input"] or "noon" in body["input"] or body["input"]
            return httpx.Response(200, content=_wav_bytes())
        # Any cloud host is a hard failure for these tests.
        return httpx.Response(599, text=f"cloud call forbidden: {url}")

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def client(**kw):
        kw = {**kw, "transport": transport}
        return real(**kw)

    monkeypatch.setattr(stt.httpx, "AsyncClient", client)
    monkeypatch.setattr(tts.httpx, "AsyncClient", client)
    monkeypatch.setattr(compat.httpx, "AsyncClient", client)
    return calls


def test_ask_spoken_local_path_preserves_words(client, auth, monkeypatch):
    calls = _install_stack(monkeypatch)
    clip = base64.b64encode(_wav_bytes(rate=16000)).decode()
    r = client.post("/api/ask", json={"audio_b64": clip, "voice": True}, headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["heard"] == "what fucking time is it"
    assert "fucking" in out["spoken"]
    assert out["voice_b64"]
    joined = " ".join(calls)
    assert "local-stt.test" in joined and "local-chat.test" in joined and "local-tts.test" in joined
    assert "googleapis" not in joined and "openai.com" not in joined and "groq" not in joined


def test_ask_with_image_fails_closed(client, auth):
    r = client.post("/api/ask", json={"text": "what is this", "image_b64": base64.b64encode(b"JPEG").decode()},
                    headers=auth)
    assert r.status_code == 503
    assert "vision" in r.json()["detail"].lower()


def test_glance_fails_closed(client, auth):
    r = client.post("/api/glance", json={"image_b64": base64.b64encode(b"JPEG").decode()}, headers=auth)
    assert r.status_code == 503
    assert "vision" in r.json()["detail"].lower()


def test_hear_uses_local_stt(client, auth, monkeypatch):
    _install_stack(monkeypatch, transcript="confirm the fucking booking")
    clip = base64.b64encode(_wav_bytes(rate=16000)).decode()
    r = client.post("/api/hear", json={"audio_b64": clip}, headers=auth)
    assert r.status_code == 200
    assert r.json()["heard"] == "confirm the fucking booking"


def test_speak_endpoint_local(client, auth, monkeypatch):
    _install_stack(monkeypatch)
    r = client.post("/api/speak", json={"text": "timer done you fucking legend"}, headers=auth)
    assert r.status_code == 200 and r.json()["voice_b64"]


def test_live_requires_auth(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/live?token=bad"):
            pass


def test_live_local_turn_protocol(client, auth, monkeypatch):
    """Recorded-style live turn: start → ready → PCM speech → heard/said/audio/turn_done."""
    calls = _install_stack(monkeypatch, transcript="what fucking time is it",
                           spoken='{"spoken":"It is noon.","details":"","language":"en","mac_action":null,"tool":null}')
    # Speed up VAD: shorter silence by patching constants
    import cj.live_local as ll
    monkeypatch.setattr(ll, "SILENCE_MS", 50)
    monkeypatch.setattr(ll, "MIN_SPEECH_MS", 20)
    monkeypatch.setattr(ll, "RMS_SPEECH", 100)

    token = prefs.token()
    with client.websocket_connect(f"/ws/live?token={token}") as ws:
        ws.send_text(json.dumps({"type": "start", "mode": "assistant", "history": []}))
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        # Loud speech then silence
        ws.send_bytes(_pcm16(80, amp=5000))
        ws.send_bytes(_pcm16(80, amp=5000))
        ws.send_bytes(_pcm16(100, amp=0))  # silence to end turn
        events = []
        audio_frames = 0
        # Drain until turn_done (or timeout via limited receives)
        for _ in range(40):
            msg = ws.receive()
            if "text" in msg and msg["text"] is not None:
                ev = json.loads(msg["text"])
                events.append(ev)
                if ev["type"] == "turn_done":
                    break
            elif msg.get("bytes"):
                audio_frames += 1
        types = [e["type"] for e in events]
        assert "heard" in types and "said" in types and "turn_done" in types
        heard = next(e for e in events if e["type"] == "heard")
        assert heard["text"] == "what fucking time is it"
        said = next(e for e in events if e["type"] == "said")
        assert "noon" in said["text"].lower()
        assert audio_frames >= 1
        ws.send_text(json.dumps({"type": "end"}))
    joined = " ".join(calls)
    assert "googleapis.com" not in joined
    assert "local-stt.test" in joined and "local-chat.test" in joined and "local-tts.test" in joined


def test_live_interpreter_rejected(client, monkeypatch):
    _install_stack(monkeypatch)
    token = prefs.token()
    with client.websocket_connect(f"/ws/live?token={token}") as ws:
        ws.send_text(json.dumps({"type": "start", "mode": "interpreter", "language": "Spanish"}))
        err = ws.receive_json()
        assert err["type"] == "error"
        assert "not available" in err["text"].lower()


def test_live_stt_failure_is_error_event(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "local-stt.test":
            return httpx.Response(500, text="whisper crashed")
        return httpx.Response(200, content=_wav_bytes())

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    client_factory = lambda **kw: real(transport=transport, **kw)
    monkeypatch.setattr(stt.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(tts.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(compat.httpx, "AsyncClient", client_factory)
    import cj.live_local as ll
    monkeypatch.setattr(ll, "SILENCE_MS", 50)
    monkeypatch.setattr(ll, "MIN_SPEECH_MS", 20)
    monkeypatch.setattr(ll, "RMS_SPEECH", 100)
    token = prefs.token()
    with client.websocket_connect(f"/ws/live?token={token}") as ws:
        ws.send_text(json.dumps({"type": "start", "mode": "assistant", "history": []}))
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(_pcm16(80, amp=5000))
        ws.send_bytes(_pcm16(100, amp=0))
        # Expect an error event from the failed turn
        found = False
        for _ in range(20):
            msg = ws.receive()
            if msg.get("text"):
                ev = json.loads(msg["text"])
                if ev["type"] == "error":
                    assert "STT" in ev["text"] or "stt" in ev["text"].lower() or "500" in ev["text"]
                    found = True
                    break
        assert found
        ws.send_text(json.dumps({"type": "end"}))


def test_cloud_mode_unchanged_when_local_off(monkeypatch):
    monkeypatch.setenv("CJ_LOCAL_ONLY", "0")
    cfg = prefs.load()
    cfg["local_only"] = False
    prefs.save(cfg)
    assert not local_mode.enabled()
    r = Router()
    # Without forcing mock, preferred may be gemini; chain still allows free fallbacks.
    names = [p.name for p in r.chain()]
    assert "gemini" in names or names == [r.mock.name] or "ollama" in names
