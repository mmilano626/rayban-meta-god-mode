# Strict local-only voice (STT → chat → TTS)

Opt-in mode that keeps **listening and speaking on your own machines**: local speech-to-text, a local OpenAI-compatible chat model, and local text-to-speech. In this mode CJ **never** calls Gemini Live, Azure Speech, or cloud chat fallbacks (Gemini / Groq / OpenRouter / Claude / GPT / DeepSeek), even when those API keys are present.

This does **not** promise perfect transcription accuracy or a refusal-free model. It only avoids adding content filters, bleeping, or cloud rewrites in the voice path. Use an abliterated / uncensored local chat weights if that is what you want from the LLM.

## Enable

```bash
export CJ_LOCAL_ONLY=1
# or in ~/.cj/config.json:  "local_only": true
```

Point the three services at reachable OpenAI-compatible endpoints. Defaults assume the brain runs **on the same host** as the speech/chat servers (`127.0.0.1`). For a remote Dell or similar, use SSH tunnels or your private network URLs — **do not commit personal LAN/Tailscale addresses into the repo**.

| Variable | Default | Meaning |
|----------|---------|---------|
| `CJ_LOCAL_ONLY` | off | `1` / `true` / `yes` / `on` enables strict mode |
| `CJ_STT_URL` | `http://127.0.0.1:8178` | Root URL; CJ posts to `{root}/v1/audio/transcriptions` |
| `CJ_TTS_URL` | `http://127.0.0.1:8179` | Root URL; CJ posts to `{root}/v1/audio/speech` |
| `CJ_TTS_VOICE` | `af_heart` | Kokoro-style voice name |
| `CJ_TTS_MODEL` | `kokoro` | TTS model id |
| `CJ_OLLAMA_URL` | `http://localhost:11434` | Chat root **without** `/v1` (adapter appends `/v1/chat/completions`) |
| `CJ_OLLAMA_MODEL` / `ollama_model` | (required) | Local chat model id |
| `CJ_LOCAL_TIMEOUT` | `60` | HTTP timeout seconds for STT/TTS/chat |
| `CJ_HOME` | `~/.cj` | Config + pairing token directory |

Also set `provider` to `ollama` (or rely on local-only forcing the ollama adapter). Example config fragment:

```json
{
  "local_only": true,
  "provider": "ollama",
  "ollama_model": "your-local-model-id",
  "stt_url": "http://127.0.0.1:8178",
  "tts_url": "http://127.0.0.1:8179",
  "tts_voice": "af_heart",
  "tts_model": "kokoro"
}
```

### SSH forward example (chat bound to loopback on another machine)

```bash
ssh -N -L 8080:127.0.0.1:8080 -L 8178:127.0.0.1:8178 -L 8179:127.0.0.1:8179 user@dell
export CJ_LOCAL_ONLY=1
export CJ_OLLAMA_URL=http://127.0.0.1:8080
export CJ_OLLAMA_MODEL=your-local-model-id
export CJ_STT_URL=http://127.0.0.1:8178
export CJ_TTS_URL=http://127.0.0.1:8179
cd cj-mac && ./run.sh
```

## What works

- **`POST /api/ask`** with `audio_b64`: local STT → local chat → optional local TTS when `voice: true`
- **`POST /api/hear`**, **`POST /api/speak`**, note dictation audio: local STT / TTS
- **`/ws/live`** (Android live conversation): same phone protocol as Gemini Live
  - Uplink: 16 kHz PCM frames + JSON (`start` / `end` / `photo`)
  - Downlink: 24 kHz PCM + JSON (`ready`, `heard`, `said`, `interrupted`, `turn_done`, `phone`, `end`, `error`)
  - Turn-based VAD → STT → chat (including existing tool / confirmation JSON path) → TTS
  - Barge-in emits `interrupted` and stops remaining TTS audio
- Pairing auth, action allowlists, confirmations, and cancellation behavior are unchanged
- Fail-closed errors (`503` / live `error` events) when STT, TTS, or chat is down

## Limitations (honest)

- **Not Gemini Live**: first audio after you stop talking waits for a full utterance + STT + chat + TTS. Expect higher latency than ~0.6 s streaming.
- **Interpreter / live translation mode** is rejected with an error in local-only mode for now.
- **Vision is unsupported**: glasses photos, `/api/glance`, and screen “what’s on my Mac” do **not** fall back to a cloud vision model. You get a clear unsupported / `503` response. Do not assume the local chat model can see images.
- **STT reliability** depends on your Whisper (or compatible) build; GPU/runtime crashes are outside this app.
- **“Uncensored”** depends on your STT, TTS, and chat model. CJ does not add profanity filters; it also cannot guarantee ASR accuracy or that the LLM never refuses.
- Hardware glasses integration still needs a paired phone + Meta DAT setup on a machine that can reach these endpoints.

## API contracts expected of local services

**STT** `POST {CJ_STT_URL}/v1/audio/transcriptions`  
multipart `file` (WAV), `language=en`, `response_format=json` → `{"text":"..."}` (wording preserved as returned).

**TTS** `POST {CJ_TTS_URL}/v1/audio/speech`  
JSON `{"input","model","voice","response_format":"wav","speed":1}` → raw **24 kHz 16-bit mono WAV** (required sample rate for the Android live player).

**Chat** `POST {CJ_OLLAMA_URL}/v1/chat/completions`  
OpenAI-compatible JSON mode (`response_format: json_object`, `max_tokens: 800`). No auth header required by the ollama adapter.

## Tests

```bash
cd cj-mac
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest tests/test_local_voice.py tests/test_live.py tests/test_models.py tests/test_api.py -q
```

`tests/test_local_voice.py` uses mock STT/chat/TTS HTTP services: word preservation, no cloud calls with cloud keys set, fail-closed errors, disabled vision, live auth, and `/ws/live` event protocol.
