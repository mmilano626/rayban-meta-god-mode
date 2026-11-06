"""Local-only real-time voice bridge for /ws/live (same phone protocol as live.py).

Phone protocol (unchanged): 16 kHz PCM uplink, 24 kHz PCM downlink, JSON events
ready / heard / said / interrupted / turn_done / need_photo / phone / end / error.

Pipeline: energy VAD on PCM → local STT → local chat (assistant.ask) → local TTS → PCM chunks.
Never opens Gemini Live or any cloud speech/chat API. Interpreter mode is rejected honestly.
Vision (look / photos) reports unsupported rather than calling a cloud vision model.

Latency is higher than Gemini Live (full utterance before STT); interruption cancels remaining TTS
audio and emits "interrupted". This is turn-based continuous voice, not a byte-streaming Live API clone.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from . import access, local_mode, lookups, phone_agent, stt, tts
from .access import Access
from .events import bus
from .local_mode import LocalVoiceError
from .modes import assistant

log = logging.getLogger("cj.live_local")

def _rms_pcm16(pcm: bytes) -> int:
    """Root-mean-square of signed 16-bit little-endian mono PCM (no audioop; removed in 3.13)."""
    if len(pcm) < 2:
        return 0
    import array
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0
    acc = 0
    for s in samples:
        acc += s * s
    return int((acc / len(samples)) ** 0.5)


IN_RATE = 16000
OUT_RATE = 24000
# Silence after speech ends a turn. Matches Gemini Live's ~0.5–1.5 s idea, slightly longer for local STT.
SILENCE_MS = 700
MIN_SPEECH_MS = 280
MAX_UTTERANCE_S = 45
RMS_SPEECH = 400  # absolute PCM RMS; phone already gates echo somewhat
CHUNK_PCM = OUT_RATE // 10 * 2  # ~100 ms of 16-bit mono for downlink pacing


class LocalBridge:
    def __init__(self, phone: WebSocket) -> None:
        self.phone = phone
        self.ending = False
        self.closed = asyncio.Event()
        self.mode = "assistant"
        self.start: dict = {}
        self.buf = bytearray()
        self.speaking = False  # TTS currently streaming
        self.cancel_tts = asyncio.Event()
        self.busy = asyncio.Lock()  # one turn at a time
        self._speech_ms = 0
        self._silence_ms = 0
        self._in_utterance = False
        self._last_frame_ms = 20

    async def _event(self, kind: str, **data) -> None:
        try:
            await self.phone.send_text(json.dumps({"type": kind, **data}, ensure_ascii=False))
        except (WebSocketDisconnect, RuntimeError):
            self.closed.set()

    async def run(self) -> None:
        self.start = json.loads(await self.phone.receive_text())
        self.mode = "interpreter" if self.start.get("mode") == "interpreter" else "assistant"
        if self.mode == "interpreter":
            await self._event("error", text="Live translation is not available in local-only mode yet.")
            return
        if not local_mode.enabled():
            await self._event("error", text="Local live bridge requires CJ_LOCAL_ONLY.")
            return
        loc = self.start.get("location")
        lookups.LOCATION.set(loc if isinstance(loc, dict) and "lat" in loc else None)
        apps = self.start.get("apps")
        phone_agent.remember_apps(apps if isinstance(apps, list) else None)
        settings = self.start.get("access")
        access.SETTINGS.set(Access.model_validate(settings).model_dump() if isinstance(settings, dict) else None)
        history = [t for t in self.start.get("history", []) if isinstance(t, dict)]
        self._history = [{"role": t.get("role", "user"), "text": t.get("text", "")} for t in history][-8:]
        await self._event("ready")
        try:
            await self._loop()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.warning("local live failed: %s", e)
            await self._event("error", text=f"Local voice dropped: {e}"[:200])
        self.closed.set()

    async def _loop(self) -> None:
        while not self.closed.is_set() and not self.ending:
            msg = await self.phone.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                await self._on_pcm(msg["bytes"])
            elif msg.get("text"):
                ctl = json.loads(msg["text"])
                if ctl.get("type") == "end":
                    break
                if ctl.get("type") == "photo":
                    # Vision unsupported: ignore payload; any in-flight look already failed closed.
                    pass

    async def _on_pcm(self, pcm: bytes) -> None:
        if not pcm:
            return
        # ~20 ms frames from the phone (MicStream); estimate from size if odd.
        frame_ms = max(10, int(len(pcm) / 2 / IN_RATE * 1000))
        self._last_frame_ms = frame_ms
        rms = _rms_pcm16(pcm)

        if self.speaking and rms >= max(RMS_SPEECH * 2, 800):
            # Barge-in: cancel remaining TTS and notify the phone (same event as Gemini Live).
            self.cancel_tts.set()
            await self._event("interrupted")
            return

        if rms >= RMS_SPEECH:
            if not self._in_utterance:
                self._in_utterance = True
                self._speech_ms = 0
                self._silence_ms = 0
                self.buf.clear()
            self.buf.extend(pcm)
            self._speech_ms += frame_ms
            self._silence_ms = 0
        elif self._in_utterance:
            self.buf.extend(pcm)
            self._silence_ms += frame_ms
            if self._silence_ms >= SILENCE_MS and self._speech_ms >= MIN_SPEECH_MS:
                utterance = bytes(self.buf)
                self.buf.clear()
                self._in_utterance = False
                self._speech_ms = self._silence_ms = 0
                asyncio.create_task(self._turn(utterance))
            elif self._speech_ms + self._silence_ms > MAX_UTTERANCE_S * 1000:
                utterance = bytes(self.buf)
                self.buf.clear()
                self._in_utterance = False
                self._speech_ms = self._silence_ms = 0
                asyncio.create_task(self._turn(utterance))

    async def _turn(self, pcm: bytes) -> None:
        if self.busy.locked():
            return
        async with self.busy:
            try:
                await self._turn_locked(pcm)
            except LocalVoiceError as e:
                await self._event("error", text=str(e)[:200])
            except Exception as e:
                log.warning("local turn failed: %s", e)
                await self._event("error", text=f"Local turn failed: {e}"[:200])

    async def _turn_locked(self, pcm: bytes) -> None:
        heard = (await stt.transcribe(pcm)).strip()
        # Preserve recognizer wording; only trim edges for empty checks.
        await self._event("heard", text=heard)
        if not heard or assistant._noise(heard):
            await self._event("turn_done", heard=heard, said="")
            return
        from . import live as live_cloud
        goodbye = live_cloud._goodbye(heard)

        out = await assistant.ask(heard, list(self._history))
        if out.get("needs_photo"):
            await self._event("need_photo")
            said = ("I can't use the glasses camera in local-only mode yet — "
                    "vision needs a local image model, which isn't configured.")
            await self._event("said", text=said)
            await self._play(said)
            await self._finish_turn(heard, said, goodbye=False)
            return

        said = (out.get("spoken") or "").strip()
        if out.get("phone"):
            phone = out["phone"]
            await self._event("phone", **phone)
            if phone.get("action") in ("dictation", "translate", "phone_task"):
                self.ending = True
        if out.get("timer"):
            t = out["timer"]
            await self._event("phone", action="timer", seconds=t["seconds"], label=t.get("label", ""))

        if said:
            await self._event("said", text=said)
            await self._play(said)

        self._history = (self._history + [{"role": "user", "text": heard},
                                          {"role": "assistant", "text": said}])[-8:]
        await self._finish_turn(heard, said, goodbye=goodbye or self.ending)

    async def _finish_turn(self, heard: str, said: str, *, goodbye: bool) -> None:
        bus.emit("answer", mode="live_local", heard=heard, spoken=said)
        await self._event("turn_done", heard=heard, said=said)
        if goodbye or self.ending:
            self.ending = True
            await self._event("end")
            self.closed.set()

    async def _play(self, text: str) -> None:
        self.cancel_tts.clear()
        self.speaking = True
        try:
            pcm = await tts.speak_pcm(text)
            if not pcm:
                return
            for i in range(0, len(pcm), CHUNK_PCM):
                if self.cancel_tts.is_set() or self.closed.is_set():
                    break
                chunk = pcm[i:i + CHUNK_PCM]
                try:
                    await self.phone.send_bytes(chunk)
                except (WebSocketDisconnect, RuntimeError):
                    self.closed.set()
                    break
                # Pace roughly real-time so barge-in has a window; cancel skips the rest.
                await asyncio.sleep(len(chunk) / 2 / OUT_RATE)
        finally:
            self.speaking = False
