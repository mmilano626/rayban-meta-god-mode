"""Picks a provider for each request and falls back down the chain when one fails or is rate limited.

Your main brain comes first (config "provider", or CJ_PROVIDER: gemini | claude | openai | deepseek | openrouter |
ollama), then the free ones in order: gemini -> groq -> openrouter -> ollama -> the offline stand-in. Paid models are
only used when you picked one as the main brain, and never past its monthly limit (spend.py). Speech is normally heard
by Gemini, the only built-in provider that takes audio.

Strict local-only mode (CJ_LOCAL_ONLY / config local_only): only the local OpenAI-compatible chat provider (ollama
adapter) is used. No Gemini, Groq, OpenRouter, or other cloud fallbacks — even if their keys exist. Audio and image
attachments are rejected here; callers must use local STT first, and vision is unsupported.
"""
from __future__ import annotations

import os

from .. import local_mode, prefs
from ..events import bus
from . import spend
from .claude import Claude
from .deepseek import DeepSeek
from .gemini import Gemini
from .groq import Groq
from .mock import Mock
from .ollama import Ollama
from .openai import OpenAI
from .openrouter import OpenRouter
from .provider import Attachment, Completion, Provider, ProviderError

AUDIO = "gemini"
FREE = ["gemini", "groq", "openrouter", "ollama"]


class Router:
    def __init__(self) -> None:
        self.providers: dict[str, Provider] = {p.name: p for p in (Gemini(), Claude(), OpenAI(), DeepSeek(), Groq(),
                                                                   OpenRouter(), Ollama())}
        self.mock = Mock()

    def preferred(self) -> str:
        """The main brain you picked, if its key is set and it's under its monthly limit; otherwise Gemini.

        In local-only mode the preferred brain is always the local chat adapter (ollama), never Gemini.
        """
        if local_mode.enabled():
            return "ollama"
        name = os.environ.get("CJ_PROVIDER") or prefs.load().get("provider") or AUDIO
        p = self.providers.get(name)
        return name if p and p.available() and not spend.over(name) else AUDIO

    def chain(self, attachment: Attachment | None = None) -> list[Provider]:
        if os.environ.get("CJ_FORCE_MOCK") == "1":
            return [self.mock]
        if local_mode.enabled():
            if attachment and attachment.mime.startswith("audio/"):
                raise ProviderError("local-only mode does not send audio to models; use local STT first")
            if attachment:
                raise ProviderError("vision is not supported in local-only mode")
            ollama = self.providers["ollama"]
            if not ollama.available():
                raise ProviderError(
                    "local-only mode requires a local chat model "
                    "(set CJ_OLLAMA_MODEL or config ollama_model; CJ_OLLAMA_URL root without /v1)")
            return [ollama]
        if attachment and attachment.mime.startswith("audio/"):
            names = [AUDIO]
        else:
            names = list(dict.fromkeys([self.preferred(), *FREE]))
            if attachment:
                names = [n for n in names if getattr(self.providers[n], "images", True)]
        usable = [self.providers[n] for n in names if self.providers[n].available()]
        return usable or [self.mock]

    def status(self) -> dict:
        return {name: p.available() for name, p in self.providers.items()} | {"mock": True}

    async def complete(self, task: str, system: str, messages: list[dict], image: Attachment | None = None,
                       json_mode: bool = True) -> Completion:
        last_error: Exception | None = None
        try:
            providers = self.chain(image)
        except ProviderError:
            raise
        if local_mode.enabled() and not providers:
            raise ProviderError("local-only mode: no local chat provider available")
        for provider in providers:
            try:
                result = await provider.complete(system, messages, image, json_mode)
                spend.record(result.provider, result.input_tokens, result.output_tokens)
                bus.emit("model_call", task=task, provider=result.provider, model=result.model,
                         latency_ms=result.latency_ms, tokens_in=result.input_tokens,
                         tokens_out=result.output_tokens)
                return result
            except Exception as e:  # network error, rate limit, bad response: try the next one
                last_error = e
                bus.emit("model_failed", task=task, provider=provider.name, error=f"{type(e).__name__}: {e}"[:200])
                if local_mode.enabled():
                    # Fail closed: never walk into a cloud provider after a local failure.
                    raise ProviderError(f"local chat failed: {type(e).__name__}: {e}") from e
        raise ProviderError(f"all providers failed: {type(last_error).__name__}: {last_error}")


router = Router()
