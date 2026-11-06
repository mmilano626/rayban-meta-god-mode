"""CJ's voice.

Default: Microsoft Azure Speech (Olivia by default; pick another with tools/azure_voice_samples.py).
Strict local-only mode (CJ_LOCAL_ONLY): OpenAI-compatible local TTS (Kokoro-style /v1/audio/speech).
No cloud fallback when local-only is on; failures raise LocalVoiceError.
"""
from __future__ import annotations

import io
import os
import re
import struct
import wave
from xml.sax.saxutils import escape

import httpx

from . import local_mode, prefs
from .local_mode import LocalVoiceError

VOICE = os.environ.get("CJ_VOICE", "en-US-Olivia:MAI-Voice-2.1-Flash")
MULTILINGUAL = "en-US-AvaMultilingualNeural"

VOICES = {
    "en": (VOICE, "en-US"), "hinglish": (VOICE, "en-US"),
    "hi": (os.environ.get("CJ_VOICE_HINDI", "hi-IN-SwaraNeural"), "hi-IN"),
    "zh": ("zh-CN-XiaoxiaoNeural", "zh-CN"), "es": ("es-ES-ElviraNeural", "es-ES"), "fr": ("fr-FR-DeniseNeural", "fr-FR"),
    "ar": ("ar-SA-ZariyahNeural", "ar-SA"), "bn": ("bn-IN-TanishaaNeural", "bn-IN"), "pt": ("pt-BR-FranciscaNeural", "pt-BR"),
    "ru": ("ru-RU-SvetlanaNeural", "ru-RU"), "ur": ("ur-IN-GulNeural", "ur-IN"), "ja": ("ja-JP-NanamiNeural", "ja-JP"),
    "de": ("de-DE-KatjaNeural", "de-DE"), "ko": ("ko-KR-SunHiNeural", "ko-KR"), "it": ("it-IT-ElsaNeural", "it-IT"),
    "tr": ("tr-TR-EmelNeural", "tr-TR"), "id": ("id-ID-GadisNeural", "id-ID"), "ta": ("ta-IN-PallaviNeural", "ta-IN"),
    "te": ("te-IN-ShrutiNeural", "te-IN"), "mr": ("mr-IN-AarohiNeural", "mr-IN"), "gu": ("gu-IN-DhwaniNeural", "gu-IN"),
}

SCRIPTS = [(r"[ऀ-ॿ]", "hi"), (r"[一-鿿]", "zh"), (r"[ぁ-ヿ]", "ja"), (r"[가-힯]", "ko"),
           (r"[؀-ۿ]", "ar"), (r"[ঀ-৿]", "bn"), (r"[Ѐ-ӿ]", "ru"), (r"[஀-௿]", "ta"),
           (r"[ఀ-౿]", "te"), (r"[઀-૿]", "gu")]

OWN_SCRIPT = {"hi", "zh", "ar", "bn", "ru", "ur", "ja", "ko", "ta", "te", "mr", "gu"}

OUT_RATE = 24000  # phone LiveSession expects 24 kHz PCM


def available() -> bool:
    if local_mode.enabled():
        return True  # reachability is checked at speak time; config alone is enough to attempt
    return bool(prefs.key("azure_speech") and prefs.load().get("azure_region"))


def voice_for(text: str, language: str | None = None) -> tuple[str, str]:
    code = (language or "").lower().strip()
    if code in OWN_SCRIPT and text.isascii():
        return VOICES["hinglish"]
    if code in VOICES and not (code in ("en", "hinglish") and not text.isascii()):
        return VOICES[code]
    for pattern, script_code in SCRIPTS:
        if re.search(pattern, text):
            return VOICES[script_code]
    return VOICES["en"] if text.isascii() else (MULTILINGUAL, "en-US")


def wav_to_pcm24(data: bytes) -> bytes:
    """Extract little-endian 16-bit mono PCM from a WAV; resample is not done — caller must match 24 kHz."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() not in (1, 2):
            raise LocalVoiceError("local TTS WAV must be 16-bit mono or stereo")
        frames = w.readframes(w.getnframes())
        rate = w.getframerate()
        if w.getnchannels() == 2:
            # downmix
            samples = memoryview(frames).cast("h")
            mono = struct.pack(f"<{len(samples) // 2}h",
                               *(((samples[i] + samples[i + 1]) // 2) for i in range(0, len(samples), 2)))
            frames = mono
        if rate != OUT_RATE:
            # Honest limitation: we only accept 24 kHz from local TTS (Kokoro default).
            raise LocalVoiceError(f"local TTS sample rate {rate} Hz; need {OUT_RATE} Hz")
        return frames


async def speak(text: str, language: str | None = None) -> bytes | None:
    """24 kHz 16-bit mono WAV of [text].

    Local-only: returns WAV bytes or raises LocalVoiceError (never Azure, never silent None).
    Cloud mode: Azure WAV, or None if unset/failed (phone falls back to its own TTS).
    """
    if not text.strip():
        return None if not local_mode.enabled() else b""
    if local_mode.enabled():
        return await _local_speak(text)
    return await _azure_speak(text, language)


async def speak_pcm(text: str, language: str | None = None) -> bytes:
    """Raw 24 kHz PCM for the live WebSocket. Local-only only."""
    wav = await speak(text, language)
    if not wav:
        return b""
    if wav[:4] == b"RIFF":
        return wav_to_pcm24(wav)
    return wav  # already PCM if the server was asked for pcm


async def _local_speak(text: str) -> bytes:
    url = local_mode.tts_base() + "/v1/audio/speech"
    body = {
        "input": text[:4000],  # preserve wording; only bound length for safety
        "model": local_mode.tts_model(),
        "voice": local_mode.tts_voice(),
        "response_format": "wav",
        "speed": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=local_mode.timeout()) as client:
            r = await client.post(url, json=body)
    except httpx.HTTPError as e:
        raise LocalVoiceError(f"local TTS unreachable: {type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise LocalVoiceError(f"local TTS {r.status_code}: {r.text[:200]}")
    if not r.content:
        raise LocalVoiceError("local TTS returned empty audio")
    return r.content


async def _azure_speak(text: str, language: str | None) -> bytes | None:
    if not available():
        return None
    voice, lang = voice_for(text, language)
    ssml = (f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{lang}">'
            f'<voice name="{voice}">{escape(text[:2000])}</voice></speak>')
    region = prefs.load()["azure_region"]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
                                  content=ssml.encode(), headers={
                                      "Ocp-Apim-Subscription-Key": prefs.key("azure_speech"),
                                      "Content-Type": "application/ssml+xml",
                                      "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
                                      "User-Agent": "CJ"})
    except httpx.HTTPError:
        return None
    return r.content if r.status_code == 200 else None
