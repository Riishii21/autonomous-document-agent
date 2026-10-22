"""LLM client with structured (JSON) output.

The rest of the codebase only depends on the `LLM` protocol, so the agent can
be tested with a fake model and switched between Groq and Ollama via config.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Protocol, TypeVar

from openai import BadRequestError, OpenAI
from pydantic import BaseModel, ValidationError

log = logging.getLogger("agent.llm")
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model could not produce a valid response after all retries."""


class LLM(Protocol):
    def generate(self, system: str, user: str, schema: type[T]) -> T: ...


def _extract_json(raw: str) -> str:
    """Models sometimes wrap JSON in ```json fences or add a sentence around it."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start : end + 1] if start != -1 and end > start else raw


def _failed_generation(exc: BadRequestError) -> str | None:
    """Return the model's rejected text for a json_validate_failed error, else None."""
    body = exc.body if isinstance(exc.body, dict) else {}
    err = body.get("error", body)
    if not isinstance(err, dict) or err.get("code") != "json_validate_failed":
        return None
    return err.get("failed_generation") or ""


class OpenAICompatibleLLM:
    """Works with any OpenAI-compatible endpoint (Groq, Ollama, LM Studio...)."""

    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 0.3, json_retries: int = 2,
                 reasoning_effort: str | None = None):
        # max_retries here covers transport errors and 429 rate limits
        # (the SDK honours Groq's retry-after header with backoff).
        self._client = OpenAI(base_url=base_url, api_key=api_key, max_retries=4, timeout=90)
        self.model = model
        self.temperature = temperature
        self.json_retries = json_retries
        # Sent only when set, so non-reasoning models/providers are unaffected.
        self._extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else None

    def generate(self, system: str, user: str, schema: type[T]) -> T:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error: Exception | None = None

        for attempt in range(self.json_retries + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    response_format={"type": "json_object"},
                    extra_body=self._extra,
                )
                raw = resp.choices[0].message.content or ""
            except BadRequestError as exc:
                # Groq validates JSON mode server-side: malformed output comes back as a
                # 400 json_validate_failed (with the bad text attached) instead of a response.
                # Treat it like any other invalid output so the self-correction loop handles it.
                raw = _failed_generation(exc)
                if raw is None:
                    raise
                log.warning("Provider rejected malformed JSON for %s (attempt %d)",
                            schema.__name__, attempt + 1)
            try:
                return schema.model_validate(json.loads(_extract_json(raw)))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                log.warning("Invalid %s output (attempt %d): %s", schema.__name__, attempt + 1, exc)
                # Feed the validation error back so the model can self-correct.
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": (
                        f"That output was invalid for the required schema: {str(exc)[:800]}\n"
                        "Return ONLY corrected JSON that matches the schema exactly.")},
                ]

        raise LLMError(f"Model failed to return a valid {schema.__name__}: {last_error}")
