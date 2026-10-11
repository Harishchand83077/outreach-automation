"""One HTTP client for every provider in the chain.

All of them expose OpenAI-compatible `/chat/completions`, so a single adapter serves the lot and
adding a provider stays configuration rather than code. `httpx` directly rather than a vendor
SDK, because we need control over error classification (§11, issue E5) and over exactly which
headers are read to decide a cool-down.

LLM output is untrusted data (hard rule 7). This module parses and validates it; it never acts
on it, and a response that does not match the requested schema is an error rather than something
to pass along and hope about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.services.llm.entries import LLMEntry
from app.services.llm.errors import (
    InvalidOutputError,
    PermanentError,
    RetryableError,
    TimeoutError_,
    classify_response,
)


@dataclass
class Completion:
    """One successful call, with the numbers needed for accounting."""

    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    entry_name: str
    model: str

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


def _extract_usage(payload: dict[str, Any]) -> tuple[int, int]:
    """Token counts, defaulting to 0 when a provider omits them.

    Missing usage must not crash a call — but it does mean the daily cap under-counts for that
    provider, which is why `/admin/usage` reports measured totals rather than trusting estimates.
    """
    usage = payload.get("usage") or {}
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def _extract_text(payload: dict[str, Any], entry_name: str) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise InvalidOutputError(f"{entry_name}: response had no choices", entry_name=entry_name)
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise InvalidOutputError(f"{entry_name}: response content was empty", entry_name=entry_name)
    return content


class LLMClient:
    """Calls one entry. Stateless — the router owns cool-downs and ordering."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http
        self._owns_http = http is None

    async def __aenter__(self) -> LLMClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def complete(
        self,
        entry: LLMEntry,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
        json_schema: dict[str, Any] | None = None,
        max_output_tokens: int | None = None,
    ) -> Completion:
        """One chat completion against one entry.

        Raises an `LLMError` subclass on failure. The router decides what that means for the
        chain; this function never retries or falls over by itself, so retry policy lives in
        exactly one place.
        """
        if self._http is None:
            raise RuntimeError("LLMClient must be used as an async context manager")

        api_key = entry.api_key
        if api_key is None:
            raise PermanentError(
                f"{entry.name}: {entry.api_key_env} is not set", entry_name=entry.name
            )

        body: dict[str, Any] = {
            "model": entry.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_output_tokens or entry.max_output_tokens,
        }
        if json_schema is not None:
            # Structured output, so a subject line is a field rather than something parsed out of
            # prose. The prototype took line 1 if it started with "Subject:" and produced a
            # generic subject the moment the model added a preamble (issue B13).
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "result",
                    "strict": True,
                    "schema": _strictify(json_schema),
                },
            }

        try:
            response = await self._http.post(
                entry.completions_url,
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=entry.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError_(
                f"{entry.name}: timed out after {entry.timeout_seconds}s", entry_name=entry.name
            ) from exc
        except httpx.HTTPError as exc:
            raise RetryableError(
                f"{entry.name}: connection failed ({type(exc).__name__})", entry_name=entry.name
            ) from exc

        latency_ms = int(response.elapsed.total_seconds() * 1000) if response.elapsed else 0

        if response.status_code != 200:
            raise classify_response(
                response.status_code,
                dict(response.headers),
                response.text,
                entry_name=entry.name,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise InvalidOutputError(
                f"{entry.name}: response was not JSON", entry_name=entry.name
            ) from exc

        tokens_in, tokens_out = _extract_usage(payload)
        return Completion(
            text=_extract_text(payload, entry.name),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            entry_name=entry.name,
            model=entry.model,
        )


def _strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Set `additionalProperties: false` on every object in a JSON schema.

    Strict structured output requires it on **every** object, including the nested models
    Pydantic emits under `$defs` — and Pydantic only sets it on the top level. Groq rejects the
    whole request with:

        invalid JSON schema for response_format: /$defs/Claim:
        `additionalProperties:false` must be set on every object

    which the router records as a permanent error, so two perfectly good entries were skipped on
    every structured call and the chain fell through to whichever free provider was left.

    A copy is returned; the caller's schema is not mutated, because it comes from
    `model_json_schema()` and is shared.
    """
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if isinstance(value, dict):
            out[key] = _strictify(value)
        elif isinstance(value, list):
            out[key] = [_strictify(item) if isinstance(item, dict) else item for item in value]
        else:
            out[key] = value

    if out.get("type") == "object":
        out["additionalProperties"] = False
        # Strict mode also demands that `required` name *every* key in `properties`. Pydantic
        # omits fields that have defaults, and the provider answers:
        #
        #     `required` is required to be supplied and to be an array
        #     including every key in properties
        #
        # Listing them all is safe because the real enforcement is local: `parse_structured`
        # validates the reply against the Pydantic model, which still applies defaults, bounds
        # and types. The wire schema is a hint to the model, not the contract.
        properties = out.get("properties")
        if isinstance(properties, dict):
            out["required"] = list(properties)
    return out


def _unfence(text: str) -> str:
    """Strip a markdown code fence, if the model wrapped its JSON in one.

        Several providers honour `response_format` loosely and answer with
        ```json
    {...}
    ``` — correct JSON inside a wrapper that `json.loads` cannot read. Treating
        that as "the model failed" cools down an entry that in fact did the right thing, and on a
        free chain that can mean no provider is left.

        Only an outer fence is removed. Anything else is left exactly as it came back, so a
        genuinely malformed answer still fails loudly rather than being coaxed into shape.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text

    without_open = stripped.removeprefix("```")
    # ```json, ```JSON or a bare ``` — drop the language tag on the first line, if there is one.
    first_line, separator, remainder = without_open.partition("\n")
    if separator and first_line.strip().isalpha():
        without_open = remainder

    closing = without_open.rfind("```")
    return without_open[:closing] if closing != -1 else without_open


def parse_structured[M: BaseModel](completion: Completion, schema: type[M]) -> M:
    """Validate a completion's body against a Pydantic model.

    A schema failure is the *model's* failure (§11), so it raises `InvalidOutputError` and the
    router cools that entry down. The alternative — accepting whatever came back — is how
    ungrounded claims reach a recipient.
    """
    try:
        loaded = json.loads(_unfence(completion.text))
    except ValueError as exc:
        raise InvalidOutputError(
            f"{completion.entry_name}: expected JSON, got {completion.text[:120]!r}",
            entry_name=completion.entry_name,
        ) from exc

    try:
        return schema.model_validate(loaded)
    except ValidationError as exc:
        raise InvalidOutputError(
            f"{completion.entry_name}: output did not match {schema.__name__}: {exc.errors()[:3]}",
            entry_name=completion.entry_name,
        ) from exc
