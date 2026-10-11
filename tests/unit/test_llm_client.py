"""The HTTP layer, against mocked transports rather than the network.

`respx` intercepts httpx, so the real request-building and response-parsing code runs — including
which headers are read to decide a cool-down — without a single packet leaving the machine.
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.services.llm.client import LLMClient
from app.services.llm.entries import LLMEntry
from app.services.llm.errors import (
    InvalidOutputError,
    PermanentError,
    RateLimitedError,
    RetryableError,
    TimeoutError_,
)

BASE = "https://api.example.com/v1"
URL = f"{BASE}/chat/completions"


@pytest.fixture
def entry(monkeypatch) -> LLMEntry:
    monkeypatch.setenv("KEY_TEST", "sk-secret-value")
    return LLMEntry(
        name="test-entry",
        provider="test",
        base_url=BASE,
        model="test-model",
        api_key_env="KEY_TEST",
        timeout_seconds=5,
    )


def _completion_body(content: str = "hello", prompt: int = 120, output: int = 34) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": output},
    }


pytestmark = pytest.mark.asyncio


class TestSuccessfulCall:
    @respx.mock
    async def test_the_response_is_parsed_with_its_token_counts(self, entry):
        respx.post(URL).mock(return_value=httpx.Response(200, json=_completion_body()))

        async with LLMClient() as client:
            result = await client.complete(entry, system="sys", user="usr")

        assert result.text == "hello"
        assert result.tokens_in == 120
        assert result.tokens_out == 34
        assert result.total_tokens == 154
        assert result.entry_name == "test-entry"

    @respx.mock
    async def test_the_request_carries_the_key_and_the_messages(self, entry):
        route = respx.post(URL).mock(return_value=httpx.Response(200, json=_completion_body()))

        async with LLMClient() as client:
            await client.complete(entry, system="be helpful", user="a question")

        request = route.calls[0].request
        assert request.headers["authorization"] == "Bearer sk-secret-value"
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert body["messages"] == [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "a question"},
        ]

    @respx.mock
    async def test_a_json_schema_is_requested_as_structured_output(self, entry):
        """Structured output is why a subject line is a field rather than something parsed out
        of prose — the prototype took line 1 if it began "Subject:" (issue B13)."""
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json=_completion_body('{"a": 1}'))
        )
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}

        async with LLMClient() as client:
            await client.complete(entry, system="s", user="u", json_schema=schema)

        body = json.loads(route.calls[0].request.content)
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["response_format"]["json_schema"]["schema"] == schema

    @respx.mock
    async def test_missing_usage_does_not_crash_the_call(self, entry):
        """Some providers omit usage. That under-counts the cap, which is why /admin/usage
        reports measured totals rather than trusting estimates — but it must not fail."""
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        )
        async with LLMClient() as client:
            result = await client.complete(entry, system="s", user="u")
        assert result.tokens_in == 0
        assert result.text == "ok"


class TestErrorMapping:
    @respx.mock
    async def test_a_429_with_retry_after_becomes_a_rate_limit(self, entry):
        respx.post(URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "42"}, text="slow down")
        )
        async with LLMClient() as client:
            with pytest.raises(RateLimitedError) as exc:
                await client.complete(entry, system="s", user="u")
        assert exc.value.retry_after == timedelta(seconds=42)
        assert exc.value.daily_quota is False

    @respx.mock
    async def test_a_daily_quota_message_is_recognised(self, entry):
        respx.post(URL).mock(
            return_value=httpx.Response(429, text="Rate limit reached: tokens per day (TPD)")
        )
        async with LLMClient() as client:
            with pytest.raises(RateLimitedError) as exc:
                await client.complete(entry, system="s", user="u")
        assert exc.value.daily_quota is True

    @respx.mock
    async def test_a_401_is_permanent_not_retried(self, entry):
        """The prototype retried this three times and then failed, hiding a bad key."""
        respx.post(URL).mock(return_value=httpx.Response(401, text="invalid api key"))
        async with LLMClient() as client:
            with pytest.raises(PermanentError, match="API key"):
                await client.complete(entry, system="s", user="u")

    @respx.mock
    async def test_a_500_is_retryable(self, entry):
        respx.post(URL).mock(return_value=httpx.Response(503, text="overloaded"))
        async with LLMClient() as client:
            with pytest.raises(RetryableError):
                await client.complete(entry, system="s", user="u")

    @respx.mock
    async def test_a_timeout_is_its_own_outcome(self, entry):
        respx.post(URL).mock(side_effect=httpx.ReadTimeout("too slow"))
        async with LLMClient() as client:
            with pytest.raises(TimeoutError_, match="timed out"):
                await client.complete(entry, system="s", user="u")

    @respx.mock
    async def test_a_connection_failure_is_retryable(self, entry):
        respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
        async with LLMClient() as client:
            with pytest.raises(RetryableError, match="connection failed"):
                await client.complete(entry, system="s", user="u")


class TestMalformedSuccess:
    """200 responses that are not usable. LLM output is untrusted data (hard rule 7)."""

    @respx.mock
    async def test_a_body_that_is_not_json_is_invalid_output(self, entry):
        respx.post(URL).mock(return_value=httpx.Response(200, text="<html>502</html>"))
        async with LLMClient() as client:
            with pytest.raises(InvalidOutputError, match="not JSON"):
                await client.complete(entry, system="s", user="u")

    @respx.mock
    async def test_no_choices_is_invalid_output(self, entry):
        respx.post(URL).mock(return_value=httpx.Response(200, json={"choices": []}))
        async with LLMClient() as client:
            with pytest.raises(InvalidOutputError, match="no choices"):
                await client.complete(entry, system="s", user="u")

    @respx.mock
    async def test_empty_content_is_invalid_output(self, entry):
        respx.post(URL).mock(return_value=httpx.Response(200, json=_completion_body(content="   ")))
        async with LLMClient() as client:
            with pytest.raises(InvalidOutputError, match="empty"):
                await client.complete(entry, system="s", user="u")


class TestMissingKey:
    async def test_an_entry_with_no_key_fails_permanently_without_a_request(self, monkeypatch):
        monkeypatch.delenv("KEY_ABSENT", raising=False)
        entry = LLMEntry(
            name="absent",
            provider="test",
            base_url=BASE,
            model="m",
            api_key_env="KEY_ABSENT",
        )
        async with LLMClient() as client:
            with pytest.raises(PermanentError, match="KEY_ABSENT"):
                await client.complete(entry, system="s", user="u")


class TestFencedJsonIsAccepted:
    """Several providers honour `response_format` loosely and wrap correct JSON in a markdown
    fence. Treating that as "the model failed" cools down an entry that in fact did the right
    thing — and on a free chain that can leave no provider standing at all.

    Found live: `google/gemma-4-31b-it:free` answered ```json {...} ``` to a strict json_schema
    request, and the whole drafting stage reported capacity exhausted.
    """

    def _completion(self, text: str):
        from app.services.llm.client import Completion

        return Completion(
            text=text,
            entry_name="test-entry",
            model="test-model",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )

    def test_plain_json_still_works(self):
        from app.services.llm.client import parse_structured

        assert (
            parse_structured(self._completion('{"subject": "s", "body": "b"}'), _Draft).subject
            == "s"
        )

    def test_a_json_fence_is_accepted(self):
        from app.services.llm.client import parse_structured

        text = '```json\n{"subject": "s", "body": "b"}\n```'
        assert parse_structured(self._completion(text), _Draft).body == "b"

    def test_a_bare_fence_is_accepted(self):
        from app.services.llm.client import parse_structured

        text = '```\n{"subject": "s", "body": "b"}\n```'
        assert parse_structured(self._completion(text), _Draft).subject == "s"

    def test_genuinely_malformed_output_still_fails_loudly(self):
        """The fence is removed; nothing is coaxed into shape. A model that returns prose has
        failed, and must still cool down."""
        import pytest

        from app.services.llm.client import parse_structured
        from app.services.llm.errors import InvalidOutputError

        with pytest.raises(InvalidOutputError):
            parse_structured(self._completion("Sure! Here is your email."), _Draft)

    def test_a_fence_around_prose_still_fails(self):
        import pytest

        from app.services.llm.client import parse_structured
        from app.services.llm.errors import InvalidOutputError

        with pytest.raises(InvalidOutputError):
            parse_structured(self._completion("```\nnot json\n```"), _Draft)


class _Draft(BaseModel):
    subject: str
    body: str
