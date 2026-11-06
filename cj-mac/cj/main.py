"""CJ brain server. Run from cj-mac/:  uvicorn cj.main:app --host 0.0.0.0 --port 8765"""
from __future__ import annotations

import asyncio
import base64
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from . import access, coder, prefs, beacon, live, live_local, local_mode, recall, notes, nudges, phone_agent, lookups, stt, tts
from .access import Access
from .events import bus
from .llm.provider import Attachment
from .llm import spend
from .llm.chain import router
from .local_mode import LocalVoiceError
from .modes import assistant
from .modes.common import ask_json


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(beacon.announce())  # so the phone finds the brain after an address change
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="CJ brain", lifespan=lifespan)
CONSOLE_PAGE = Path(__file__).parent / "console" / "index.html"


def _valid(token: str) -> bool:
    return secrets.compare_digest(token.encode(), prefs.token().encode())


def require_auth(authorization: str = Header(default="")) -> None:
    if not _valid(authorization.removeprefix("Bearer ").strip()):
        raise HTTPException(401, "bad or missing pairing token")


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    text: str


class Location(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    acc: float | None = Field(default=None, ge=0)  # metres; network fixes can be 500 m off


class AskIn(BaseModel):
    """A typed question (text), or a spoken one (audio_b64, a WAV clip) that the model transcribes."""
    text: str = Field(default="", max_length=4000)
    mode: str = "assistant"
    history: list[Turn] = []
    image_b64: str | None = Field(default=None, max_length=8_000_000)
    mime: str = "image/jpeg"
    audio_b64: str | None = Field(default=None, max_length=4_000_000)  # about 90 s of 16 kHz mono
    audio_mime: str = "audio/wav"
    voice: bool = False  # also return the answer as speech (voice_b64 in the reply)
    location: Location | None = None  # the phone's position, for "how long to reach…"
    access: Access | None = None  # Setup → Accessibility on the phone
    apps: list[dict] | None = Field(default=None, max_length=400)  # the phone's installed apps, {label, package}

    @model_validator(mode="after")
    def text_or_audio(self):
        if not self.text.strip() and not self.audio_b64:
            raise ValueError("send text or audio")
        return self


@app.get("/health")
async def health():
    return {"name": "CJ", "ok": True, "providers": router.status(), "spend": spend.status(),
            "local": local_mode.status()}


@app.post("/api/ask", dependencies=[Depends(require_auth)])
async def ask(body: AskIn):
    started = time.monotonic()
    history = [t.model_dump() for t in body.history]
    lookups.LOCATION.set(body.location.model_dump(exclude_none=True) if body.location else None)
    access.SETTINGS.set(body.access.model_dump() if body.access else None)
    phone_agent.remember_apps(body.apps)
    try:
        if local_mode.enabled() and body.image_b64:
            raise LocalVoiceError("vision is not supported in local-only mode")
        if body.audio_b64 and not body.text.strip():
            out = await assistant.ask_spoken(Attachment(body.audio_b64, body.audio_mime), history)
            bus.emit("heard", mode=body.mode, text=out["heard"] or "(no clear speech)", has_image=False, spoken=True)
        else:
            bus.emit("heard", mode=body.mode, text=body.text, has_image=bool(body.image_b64))
            image = Attachment(body.image_b64, body.mime) if body.image_b64 else None
            out = await assistant.ask(body.text, history, image)
    except LocalVoiceError as e:
        bus.emit("error", where="ask", error=str(e)[:200])
        raise HTTPException(503, str(e)[:300]) from e
    except Exception as e:
        bus.emit("error", where="ask", error=str(e)[:200])
        raise HTTPException(502, f"no AI provider could answer: {e}"[:300]) from e
    if body.voice and out["spoken"]:
        try:
            audio = await tts.speak(out["spoken"], out.get("language"))
        except LocalVoiceError as e:
            raise HTTPException(503, str(e)[:300]) from e
        if audio:
            out["voice_b64"] = base64.b64encode(audio).decode()
        elif local_mode.enabled():
            raise HTTPException(503, "local TTS returned no audio")
    out["ms"] = int((time.monotonic() - started) * 1000)
    if out["needs_photo"]:
        bus.emit("needs_photo", mode=body.mode, text=out["heard"], ms=out["ms"])
    else:
        bus.emit("answer", mode=body.mode, spoken=out["spoken"], language=out["language"], ms=out["ms"])
    return out


class DictateIn(BaseModel):
    session: str = Field(min_length=1, max_length=40)
    audio_b64: str = Field(min_length=1, max_length=4_000_000)
    audio_mime: str = "audio/wav"


@app.post("/api/notes/dictate", dependencies=[Depends(require_auth)])
async def notes_dictate(body: DictateIn):
    """One chunk of "start taking notes". When the user says "stop taking notes", the reply has the summary."""
    try:
        out = await assistant.dictate(Attachment(body.audio_b64, body.audio_mime), body.session)
    except LocalVoiceError as e:
        bus.emit("error", where="dictate", error=str(e)[:200])
        raise HTTPException(503, str(e)[:300]) from e
    except Exception as e:
        bus.emit("error", where="dictate", error=str(e)[:200])
        raise HTTPException(502, f"couldn't transcribe: {e}"[:300]) from e
    bus.emit("dictation", heard=out["heard"][:200], stopped=out["stopped"])
    if out["stopped"] and out.get("spoken"):
        audio = await tts.speak(out["spoken"])
        if audio:
            out["voice_b64"] = base64.b64encode(audio).decode()
    return out


@app.get("/api/notes", dependencies=[Depends(require_auth)])
async def notes_list():
    return {"notes": notes.load()}


class GlanceIn(BaseModel):
    image_b64: str = Field(min_length=1, max_length=8_000_000)
    mime: str = "image/jpeg"


_last_glance = ""


@app.post("/api/glance", dependencies=[Depends(require_auth)])
async def glance(body: GlanceIn):
    """Visual memory (opt-in): one automatic photo. Only a short note is kept; the photo itself is not stored."""
    global _last_glance
    try:
        note = await assistant.glance(Attachment(body.image_b64, body.mime), _last_glance)
    except LocalVoiceError as e:
        raise HTTPException(503, str(e)[:300]) from e
    if note:
        _last_glance = note
        bus.emit("glance", note=note)
    return {"kept": bool(note)}


class HearIn(BaseModel):
    audio_b64: str = Field(min_length=1, max_length=4_000_000)
    audio_mime: str = "audio/wav"


@app.post("/api/hear", dependencies=[Depends(require_auth)])
async def hear(body: HearIn):
    """Just what was said (no answer): the phone agent's "should I?" questions."""
    try:
        if local_mode.enabled():
            heard = await stt.transcribe(Attachment(body.audio_b64, body.audio_mime))
            return {"heard": heard.strip()}
        data, _ = await ask_json("transcribe", assistant.TRANSCRIBE, [{"role": "user", "text": "(audio)"}],
                                 Attachment(body.audio_b64, body.audio_mime))
        return {"heard": str(data.get("heard", "")).strip()}
    except LocalVoiceError as e:
        raise HTTPException(503, str(e)[:300]) from e


class AgentStepIn(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    screen: list[dict] = Field(default_factory=list, max_length=400)
    history: list[str] = Field(default_factory=list, max_length=60)
    approved: str = Field(default="", max_length=200)  # the risky button the user just said yes to


@app.post("/api/agent/step", dependencies=[Depends(require_auth)])
async def agent_step(body: AgentStepIn):
    """Phone agent: the next tap, text or question for the goal, given what's on the phone's screen now."""
    out = await phone_agent.step(body.goal, body.screen, body.history, body.approved)
    bus.emit("phone_agent", goal=body.goal, step=out.get("do"), say=out.get("say", ""))
    if out.get("say") and out["do"] in ("done", "ask", "confirm"):
        audio = await tts.speak(out["say"])
        if audio:
            out["voice_b64"] = base64.b64encode(audio).decode()
    return out


class NudgeIn(BaseModel):
    location: Location


@app.post("/api/nudge", dependencies=[Depends(require_auth)])
async def nudge(body: NudgeIn):
    """The phone's position while hands-free is on. Returns something to say when the place makes it useful."""
    out = await nudges.NUDGER.check(body.location.model_dump(exclude_none=True))
    if out["say"]:
        bus.emit("nudge", text=out["say"])
        audio = await tts.speak(out["say"])
        if audio:
            out["voice_b64"] = base64.b64encode(audio).decode()
    return out


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@app.post("/api/speak", dependencies=[Depends(require_auth)])
async def speak(body: SpeakIn):
    """CJ's voice for things the phone says on its own, like a timer going off."""
    try:
        audio = await tts.speak(body.text)
    except LocalVoiceError as e:
        raise HTTPException(503, str(e)[:300]) from e
    if local_mode.enabled() and not audio:
        raise HTTPException(503, "local TTS returned no audio")
    return {"voice_b64": base64.b64encode(audio).decode() if audio else ""}


@app.get("/api/jobs/done", dependencies=[Depends(require_auth)])
async def jobs_done():
    """Claude Code jobs that finished since the phone last asked; the phone reads each summary aloud once."""
    return {"jobs": [{"project": j.project, "state": j.state, "summary": j.summary} for j in coder.take_finished()]}


@app.get("/api/memory", dependencies=[Depends(require_auth)])
async def memory_list():
    return {"notes": recall.load()}


@app.patch("/api/memory/{note_id}", dependencies=[Depends(require_auth)])
async def memory_edit(note_id: int, body: dict):
    if not str(body.get("text", "")).strip():
        raise HTTPException(400, "text is empty")
    if not recall.edit(note_id, str(body["text"])):
        raise HTTPException(404, "no such note")
    return {"ok": True}


@app.delete("/api/memory/{note_id}", dependencies=[Depends(require_auth)])
async def memory_delete(note_id: int):
    if not recall.delete(note_id):
        raise HTTPException(404, "no such note")
    return {"ok": True}


@app.get("/console")
async def console():
    return FileResponse(CONSOLE_PAGE)


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket, token: str = ""):
    """Real-time conversation. Default: Gemini Live (live.py). Strict local-only: STT→chat→TTS (live_local.py).
    The phone sends its pairing token as a header, so it doesn't show up in the server's request log."""
    token = ws.headers.get("authorization", "").removeprefix("Bearer ").strip() or token
    if not _valid(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        if local_mode.enabled():
            await live_local.LocalBridge(ws).run()
        else:
            await live.Bridge(ws).run()
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass  # the phone already hung up


@app.websocket("/ws/console")
async def ws_console(ws: WebSocket, token: str = ""):
    if not _valid(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    for event in list(bus.history):
        await ws.send_json(event)
    queue = bus.subscribe()
    try:
        while True:
            await ws.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)
