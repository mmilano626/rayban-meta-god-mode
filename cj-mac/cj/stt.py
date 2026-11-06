"""Local speech-to-text via an OpenAI-compatible /v1/audio/transcriptions endpoint.

Preserves the recognizer's text as returned (no profanity filtering or substitution here).
Recognition mistakes and model refusals elsewhere are separate; this module only forwards bytes.
"""
from __future__ import annotations

import io

import httpx

from . import local_mode
from .local_mode import LocalVoiceError
from .llm.provider import Attachment
import base64


def _wav_from_attachment(audio: Attachment) -> bytes:
    raw = base64.b64decode(audio.data_b64)
    if audio.mime.startswith("audio/wav") or raw[:4] == b"RIFF":
        return raw
    # Raw PCM is uncommon on /api/ask; wrap as 16 kHz mono WAV so Whisper accepts it.
    return pcm16_to_wav(raw, rate=16000)


def pcm16_to_wav(pcm: bytes, rate: int = 16000) -> bytes:
    import struct
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16,
        1, 1, rate, rate * 2, 2, 16, b"data", len(pcm),
    )
    return header + pcm


async def transcribe(audio: Attachment | bytes, *, language: str = "en",
                     mime: str = "audio/wav") -> str:
    """Return the exact transcript text from the local STT service. Fail closed on errors."""
    if isinstance(audio, Attachment):
        payload = _wav_from_attachment(audio)
        filename = "audio.wav"
    else:
        payload = audio if audio[:4] == b"RIFF" else pcm16_to_wav(audio, 16000)
        filename = "audio.wav"
    url = local_mode.stt_base() + "/v1/audio/transcriptions"
    try:
        async with httpx.AsyncClient(timeout=local_mode.timeout()) as client:
            r = await client.post(
                url,
                data={"language": language, "response_format": "json"},
                files={"file": (filename, io.BytesIO(payload), "audio/wav")},
            )
    except httpx.HTTPError as e:
        raise LocalVoiceError(f"local STT unreachable: {type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise LocalVoiceError(f"local STT {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError as e:
        raise LocalVoiceError("local STT returned non-JSON") from e
    text = data.get("text") if isinstance(data, dict) else None
    if text is None:
        raise LocalVoiceError("local STT response missing text")
    return str(text)  # preserve wording exactly; do not strip beyond what JSON gave
