"""Opt-in strict local-only voice: STT → local OpenAI-compatible chat → local TTS.

Enable with CJ_LOCAL_ONLY=1 (or true/yes/on) or config.json "local_only": true.
In this mode CJ never calls Gemini Live, cloud speech, or cloud chat fallbacks.
Missing local services fail closed with honest errors. Vision is unsupported.
"""
from __future__ import annotations

import os

from . import prefs

_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    env = os.environ.get("CJ_LOCAL_ONLY", "").strip().lower()
    if env:
        return env in _TRUTHY
    return bool(prefs.load().get("local_only"))


def _cfg(name: str, env: str, default: str) -> str:
    return (os.environ.get(env) or prefs.load().get(name) or default).rstrip("/")


def stt_base() -> str:
    """Root of an OpenAI-compatible transcription server (Whisper). Appends /v1/audio/transcriptions."""
    return _cfg("stt_url", "CJ_STT_URL", "http://127.0.0.1:8178")


def tts_base() -> str:
    """Root of an OpenAI-compatible speech server (Kokoro). Appends /v1/audio/speech."""
    return _cfg("tts_url", "CJ_TTS_URL", "http://127.0.0.1:8179")


def tts_voice() -> str:
    return os.environ.get("CJ_TTS_VOICE") or prefs.load().get("tts_voice") or "af_heart"


def tts_model() -> str:
    return os.environ.get("CJ_TTS_MODEL") or prefs.load().get("tts_model") or "kokoro"


def timeout() -> float:
    raw = os.environ.get("CJ_LOCAL_TIMEOUT") or prefs.load().get("local_timeout") or 60
    try:
        return max(5.0, float(raw))
    except (TypeError, ValueError):
        return 60.0


def status() -> dict:
    """Safe status for /health: no secrets, no assumed LAN/Tailscale addresses."""
    return {
        "local_only": enabled(),
        "stt": stt_base() if enabled() else None,
        "tts": tts_base() if enabled() else None,
        "tts_voice": tts_voice() if enabled() else None,
        "tts_model": tts_model() if enabled() else None,
        "chat": "ollama" if enabled() else None,
        "vision": "unsupported" if enabled() else "cloud",
    }


class LocalVoiceError(RuntimeError):
    """A required local STT/TTS/chat call failed or is misconfigured."""
