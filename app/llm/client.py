"""LLM client: OpenAI, any OpenAI-compatible local server, or offline rules.

Three providers, one interface:

  openai   api.openai.com with OPENAI_API_KEY
  local    any OpenAI-compatible endpoint - Ollama, vLLM, LM Studio,
           llama.cpp, TGI - selected by setting LLM_BASE_URL
  none     no model reachable; callers fall back to the deterministic planner

The local path exists because a large class of buyers - public sector, health,
anything holding personal data - cannot send prompts to a third-party API at
all. Data residency is a hard constraint, not a preference, so "works fully
offline" has to be a first-class mode rather than a degraded one.

Local models are also less obedient than hosted ones: many ignore
`response_format`, wrap JSON in prose, or fence it in markdown. `complete_json`
therefore negotiates JSON mode once, remembers whether it worked, and extracts
JSON defensively from whatever comes back.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.core.config import settings

log = logging.getLogger(__name__)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


@dataclass
class LLMResponse:
    data: dict[str, Any]
    raw: str
    model: str
    offline: bool = False


class LLMUnavailable(RuntimeError):
    pass


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response.

    Handles: bare JSON, ```json fenced blocks, and prose wrapped around an
    object. Returns None if nothing parseable is found.
    """
    if not text:
        return None
    candidates: list[str] = []

    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text.strip())

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            continue
    return None


class LLMClient:
    """Thin wrapper over the OpenAI SDK, pointed wherever the config says."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ):
        self.base_url = (base_url if base_url is not None else settings.llm_base_url).strip()
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.model = model or settings.llm_model or settings.openai_model
        self.provider = "none"
        self._client = None
        self._json_mode_supported: bool | None = None

        if settings.llm_provider == "none":
            return

        if self.base_url:
            # Local servers usually ignore the key, but the SDK requires one.
            self.provider = "local"
            self._init_client(self.api_key or "local-no-key", self.base_url)
        elif self.api_key and settings.llm_provider in ("auto", "openai"):
            self.provider = "openai"
            self._init_client(self.api_key, None)

    def _init_client(self, key: str, base_url: str | None) -> None:
        try:
            from openai import OpenAI

            kwargs: dict[str, Any] = {"api_key": key, "timeout": settings.llm_timeout_seconds}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = OpenAI(**kwargs)
        except Exception as exc:  # pragma: no cover - import/config failure
            log.warning("LLM client init failed (%s); falling back to offline rules", exc)
            self._client = None
            self.provider = "none"

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._client is not None

    def describe(self) -> str:
        """Human-readable provider label for /health."""
        if not self.available:
            return "offline-rules"
        if self.provider == "local":
            return f"{self.model} (local)"
        return self.model

    # ------------------------------------------------------------------
    def _chat(self, messages: list[dict[str, str]], json_mode: bool) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "temperature": settings.llm_temperature,
            "messages": messages,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)  # type: ignore[union-attr]
        return resp.choices[0].message.content or ""

    def complete_json(
        self, system: str, user: str, *, fallback: dict[str, Any] | None = None
    ) -> LLMResponse:
        if not self.available:
            if fallback is not None and settings.allow_offline_llm:
                return LLMResponse(fallback, json.dumps(fallback), "offline-rules", offline=True)
            raise LLMUnavailable("No LLM configured and no offline fallback available")

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        raw = ""
        try:
            if self._json_mode_supported is not False:
                try:
                    raw = self._chat(messages, json_mode=True)
                    self._json_mode_supported = True
                except Exception as exc:
                    # Many local servers reject response_format outright.
                    log.info("JSON mode unsupported by %s (%s); retrying without it",
                             self.model, type(exc).__name__)
                    self._json_mode_supported = False
                    raw = ""
            if not raw:
                nudge = messages[0]["content"] + "\n\nRespond with a single valid JSON object and nothing else."
                raw = self._chat(
                    [{"role": "system", "content": nudge}, messages[1]], json_mode=False
                )
        except Exception as exc:
            log.warning("LLM call failed (%s); using fallback", exc)
            if fallback is not None and settings.allow_offline_llm:
                return LLMResponse(fallback, str(exc), "offline-rules", offline=True)
            raise

        parsed = extract_json(raw)
        if parsed is None:
            log.warning("LLM returned unparseable JSON (%.120r); using fallback", raw)
            if fallback is not None and settings.allow_offline_llm:
                return LLMResponse(fallback, raw, "offline-rules", offline=True)
            raise LLMUnavailable("LLM returned no parseable JSON")
        return LLMResponse(parsed, raw, self.model)

    def complete_text(self, system: str, user: str, *, fallback: str = "") -> str:
        if not self.available:
            return fallback
        try:
            text = self._chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                json_mode=False,
            )
            return text.strip() or fallback
        except Exception as exc:
            log.warning("LLM text call failed (%s); using fallback", exc)
            return fallback


_default: LLMClient | None = None


def get_llm() -> LLMClient:
    global _default
    if _default is None:
        _default = LLMClient()
    return _default


def reset_llm() -> None:
    """Test hook: rebuild the client from current settings."""
    global _default
    _default = None
