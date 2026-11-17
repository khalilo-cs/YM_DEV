"""MOSAIC model adapters.

* No provider is hard-coded. Adapters implement ``ModelAdapter``.
* ``OpenAICompatibleAdapter`` speaks the ``/v1/chat/completions`` protocol, which
  llama.cpp's server, Ollama, vLLM and hosted APIs implement. API keys are read from
  an environment variable *named* in config, never stored.
* Model output is untrusted: ``structured()`` parses JSON and validates it against a
  Pydantic schema; invalid output is an error, not a best-effort guess.
* With no adapter available the registry reports UNAVAILABLE; callers must fall back
  to deterministic behaviour and say so.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Protocol, TypeVar

from pydantic import BaseModel, Field, ValidationError

from yemendev.core.config import ModelSettings
from yemendev.core.contracts import ExecutionFailedError, InvalidInputError, UnavailableError

T = TypeVar("T", bound=BaseModel)


class Availability(BaseModel):
    available: bool
    reason: str


class ModelRequest(BaseModel):
    system: str
    prompt: str
    max_tokens: int = Field(default=1024, ge=1, le=32768)
    temperature: float = Field(default=0.0, ge=0, le=2)


class ModelResponse(BaseModel):
    adapter: str
    model: str
    text: str
    latency_seconds: float
    usage: dict[str, int] = Field(default_factory=dict)


class ModelAdapter(Protocol):
    name: str

    def availability(self) -> Availability: ...

    def complete(self, req: ModelRequest) -> ModelResponse: ...


class OpenAICompatibleAdapter:
    name = "openai_compatible"

    def __init__(self, settings: ModelSettings) -> None:
        self.s = settings

    def availability(self) -> Availability:
        if not self.s.base_url or not self.s.model:
            return Availability(available=False, reason="no model endpoint configured")
        if not re.match(r"^https?://", self.s.base_url):
            return Availability(available=False, reason="base_url must be http(s)")
        if self.s.api_key_env and not os.environ.get(self.s.api_key_env):
            return Availability(available=False, reason=f"environment variable {self.s.api_key_env} is not set")
        return Availability(available=True, reason="configured (reachability checked on use)")

    def complete(self, req: ModelRequest) -> ModelResponse:
        av = self.availability()
        if not av.available:
            raise UnavailableError(av.reason)
        body = json.dumps({
            "model": self.s.model,
            "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": req.prompt}],
            "max_tokens": min(req.max_tokens, self.s.max_tokens),
            "temperature": req.temperature,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self.s.api_key_env:
            headers["Authorization"] = f"Bearer {os.environ[self.s.api_key_env]}"
        url = self.s.base_url.rstrip("/") + "/v1/chat/completions"  # type: ignore[union-attr]
        start = time.monotonic()
        try:
            r = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 - scheme checked
            with urllib.request.urlopen(r, timeout=self.s.timeout_seconds) as resp:  # noqa: S310
                raw = resp.read(5_000_000)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UnavailableError(f"model endpoint unreachable: {exc.__class__.__name__}") from exc
        try:
            data = json.loads(raw)
            text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise TypeError("content is not a string")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ExecutionFailedError("model endpoint returned an unexpected response shape") from exc
        usage = {k: int(v) for k, v in (data.get("usage") or {}).items() if isinstance(v, int)}
        return ModelResponse(adapter=self.name, model=str(self.s.model), text=text,
                             latency_seconds=round(time.monotonic() - start, 3), usage=usage)


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def structured(adapter: ModelAdapter, req: ModelRequest, schema: type[T]) -> tuple[T, ModelResponse]:
    """Call the model and validate its JSON output against ``schema``. No silent coercion."""
    resp = adapter.complete(req)
    text = resp.text.strip()
    if m := _FENCE.match(text):
        text = m.group(1)
    try:
        return schema.model_validate_json(text), resp
    except ValidationError as exc:
        raise InvalidInputError(f"model output failed schema validation ({exc.error_count()} error(s))") from exc


class ModelRegistry:
    def __init__(self, adapters: list[ModelAdapter]) -> None:
        self.adapters = adapters

    def status(self) -> list[dict[str, object]]:
        return [{"adapter": a.name, **a.availability().model_dump()} for a in self.adapters]

    def first_available(self) -> ModelAdapter | None:
        for a in self.adapters:
            if a.availability().available:
                return a
        return None
