"""Thin OpenAI wrapper with a deterministic offline fallback.

`complete_json` is the only entry point the pipeline uses, so the rest of the
system is testable without network access or an API key.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.core.config import settings

log = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    data: dict[str, Any]
    raw: str
    model: str
    offline: bool = False


class LLMUnavailable(RuntimeError):
    pass


class LLMClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.model = model or settings.openai_model
        self._client = None
        if self.api_key:
            try:
                from openai import OpenAI

                self._client = OpenAI(api_key=self.api_key)
            except Exception as exc:  # pragma: no cover
                log.warning("OpenAI client init failed: %s", exc)
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def complete_json(
        self, system: str, user: str, *, fallback: dict[str, Any] | None = None
    ) -> LLMResponse:
        if not self.available:
            if fallback is not None and settings.allow_offline_llm:
                return LLMResponse(fallback, json.dumps(fallback), "offline-rules", offline=True)
            raise LLMUnavailable("No OPENAI_API_KEY configured and no offline fallback available")
        try:
            resp = self._client.chat.completions.create(  # type: ignore[union-attr]
                model=self.model,
                temperature=settings.llm_temperature,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            raw = resp.choices[0].message.content or "{}"
            return LLMResponse(json.loads(raw), raw, self.model)
        except Exception as exc:
            log.warning("LLM call failed (%s); using fallback", exc)
            if fallback is not None and settings.allow_offline_llm:
                return LLMResponse(fallback, json.dumps(fallback), "offline-rules", offline=True)
            raise

    def complete_text(self, system: str, user: str, *, fallback: str = "") -> str:
        if not self.available:
            return fallback
        try:
            resp = self._client.chat.completions.create(  # type: ignore[union-attr]
                model=self.model,
                temperature=settings.llm_temperature,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            return (resp.choices[0].message.content or fallback).strip()
        except Exception as exc:
            log.warning("LLM text call failed (%s); using fallback", exc)
            return fallback


_default: LLMClient | None = None


def get_llm() -> LLMClient:
    global _default
    if _default is None:
        _default = LLMClient()
    return _default
