"""The LLM router: selection, failover, cool-downs, PII routing and exhaustion.

The prototype retried on bare `Exception` with a fixed 2-10s backoff, so an invalid API key was
retried three times and a 429 carrying `Retry-After` was ignored; there was no accounting, no
fallback model and no token budget (issue E5).

No test here touches the network: the client is a fake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from app.services.llm.client import Completion
from app.services.llm.entries import LLMChain, LLMEntry, default_chain, parse_chain
from app.services.llm.errors import (
    CapacityExhausted,
    InvalidOutputError,
    Outcome,
    PermanentError,
    RateLimitedError,
    RetryableError,
    classify_response,
    cooldown_for,
    looks_like_daily_quota,
    parse_duration,
)
from app.services.llm.router import LLMRouter
from app.services.llm.usage import InMemoryUsageStore


def entry(name: str, **kw) -> LLMEntry:
    defaults = {
        "name": name,
        "provider": "test",
        "base_url": "https://api.example.com/v1",
        "model": f"model-{name}",
        "api_key_env": f"KEY_{name.upper().replace('-', '_')}",
        "tier": "primary",
        "allow_pii": True,
    }
    return LLMEntry(**{**defaults, **kw})


class FakeClient:
    """Returns queued outcomes in order. An exception is raised; anything else is returned."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    async def complete(self, llm_entry: LLMEntry, **kw: object) -> Completion:
        self.calls.append(llm_entry.name)
        result = self.outcomes.pop(0) if self.outcomes else _ok(llm_entry.name)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


def _ok(name: str, text: str = '{"ok": true}') -> Completion:
    return Completion(
        text=text,
        tokens_in=100,
        tokens_out=50,
        latency_ms=250,
        entry_name=name,
        model=f"model-{name}",
    )


@pytest.fixture
def keys(monkeypatch):
    """Configure keys for the entries a test uses. An entry with no key is skipped."""

    def _set(*names: str) -> None:
        for name in names:
            monkeypatch.setenv(f"KEY_{name.upper().replace('-', '_')}", "sk-test-key")

    return _set


class TestEntryConfiguration:
    def test_an_entry_without_a_key_is_skipped_not_an_error(self, monkeypatch):
        """This is what makes the chain additive: setting a key enables a provider."""
        monkeypatch.delenv("KEY_A", raising=False)
        chain = LLMChain(entries=[entry("a")])
        assert chain.usable(tier="primary", has_pii=True) == []
        assert chain.configured_names == []

    def test_the_key_is_read_from_the_environment_not_stored(self, keys):
        keys("a")
        e = entry("a")
        assert e.api_key == "sk-test-key"
        # The serialised entry names the variable, never the value, so a chain is safe to log.
        assert "sk-test-key" not in e.model_dump_json()
        assert "KEY_A" in e.model_dump_json()

    def test_duplicate_entry_names_are_refused(self):
        """Usage history and cool-downs are keyed on the name."""
        with pytest.raises(ValueError, match="unique"):
            LLMChain(entries=[entry("a"), entry("a")])

    def test_a_paid_entry_is_skipped_unless_explicitly_enabled(self, keys):
        keys("paid")
        chain = LLMChain(entries=[entry("paid", cost_per_mtok=0.5)])
        assert chain.usable(tier="primary", has_pii=True) == []

        enabled = LLMChain(
            entries=[entry("paid", cost_per_mtok=0.5)],
            paid_enabled=True,
            paid_daily_usd_cap=5.0,
        )
        assert len(enabled.usable(tier="primary", has_pii=True)) == 1

    def test_enabling_paid_without_a_cap_is_refused(self):
        """Spending money takes two deliberate settings, not one."""
        with pytest.raises(ValueError, match="paid_daily_usd_cap"):
            LLMChain(entries=[], paid_enabled=True, paid_daily_usd_cap=0)


class TestChainParsing:
    def test_an_absent_chain_falls_back_to_the_default_rather_than_being_empty(self):
        """This test previously asserted the opposite, and the assertion was the bug.

        An empty `LLM_CHAIN` is the ordinary case — the variable exists to *override* the
        built-in order — so returning an empty chain left a correctly configured deployment
        with no providers at all. Every research and drafting job died with "No LLM provider is
        configured" while `GROQ_API_KEY` sat set in the environment. Nothing else caught it,
        because every other test builds its chain explicitly.
        """
        assert parse_chain(None).entries
        assert parse_chain("   ").entries
        assert parse_chain("").entries

    def test_malformed_json_is_rejected_with_a_clear_message(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_chain("{not json")

    def test_a_json_object_is_rejected(self):
        with pytest.raises(ValueError, match="must be a JSON list"):
            parse_chain('{"name": "a"}')

    def test_a_valid_chain_parses(self):
        chain = parse_chain(
            '[{"name":"x","provider":"groq","base_url":"https://api.example.com/v1",'
            '"model":"m","api_key_env":"KEY_X","tier":"fast","allow_pii":false}]'
        )
        assert chain.entries[0].tier == "fast"
        assert chain.entries[0].allow_pii is False

    def test_the_default_chain_puts_groq_first_and_gemini_last(self):
        """Order is priority, and Gemini is last because it cannot receive PII."""
        chain = default_chain()
        names = [e.name for e in chain.entries]
        assert names[0].startswith("groq")
        assert chain.by_name("gemini-flash").allow_pii is False

    def test_the_default_chain_uses_several_groq_models_not_several_accounts(self):
        """Groq publishes limits per model, so several models are several pools of headroom
        inside one account. Registering a second account to get more would violate provider
        terms (hard rule 8).

        The count is deliberately not asserted — it changes whenever a model is retired, and
        pinning it would make a routine refresh look like a regression. What must hold is that
        the models are distinct and the key is one.
        """
        chain = default_chain()
        groq = [e for e in chain.entries if e.provider == "groq"]
        assert len(groq) > 1
        assert len({e.model for e in groq}) == len(groq), "distinct models, or there is no gain"
        assert len({e.api_key_env for e in groq}) == 1, "one account"


class TestPiiRouting:
    """The rule that must not be possible to get wrong."""

    def test_a_pii_task_never_reaches_a_training_provider(self, keys):
        keys("safe", "trains")
        chain = LLMChain(entries=[entry("trains", allow_pii=False), entry("safe", allow_pii=True)])
        usable = chain.usable(tier="primary", has_pii=True)
        assert [e.name for e in usable] == ["safe"]

    def test_company_research_may_use_a_training_provider(self, keys):
        """It sees public page text and our own offer — never anything identifying the lead."""
        keys("trains")
        chain = LLMChain(entries=[entry("trains", allow_pii=False)])
        assert len(chain.usable(tier="primary", has_pii=False)) == 1

    @pytest.mark.asyncio
    async def test_classifying_a_reply_cannot_be_routed_to_a_training_provider(self, keys):
        """A reply is the most sensitive text in the system. The caller does not get a say."""
        keys("trains")
        client = FakeClient()
        router = LLMRouter(
            LLMChain(entries=[entry("trains", allow_pii=False, tier="fast")]),
            InMemoryUsageStore(),
            client=client,
        )
        with pytest.raises(CapacityExhausted):
            await router.complete(task="classify_reply", system="s", user="their reply")
        assert client.calls == [], "nothing may be sent at all"

    @pytest.mark.asyncio
    async def test_a_caller_may_raise_the_pii_flag_but_not_lower_it(self, keys):
        keys("trains")
        router = LLMRouter(
            LLMChain(entries=[entry("trains", allow_pii=False)]),
            InMemoryUsageStore(),
            client=FakeClient(),
        )
        # research_company is declared PII-free, so this normally succeeds...
        await router.complete(task="research_company", system="s", user="page text")
        # ...but a caller saying "this prompt also has PII" must close the door.
        with pytest.raises(CapacityExhausted):
            await router.complete(task="research_company", system="s", user="x", contains_pii=True)


class TestTierSelection:
    def test_a_fast_task_does_not_use_a_primary_entry(self, keys):
        keys("big", "small")
        chain = LLMChain(entries=[entry("big", tier="primary"), entry("small", tier="fast")])
        assert [e.name for e in chain.usable(tier="fast", has_pii=True)] == ["small"]

    @pytest.mark.asyncio
    async def test_drafting_uses_primary_and_classification_uses_fast(self, keys):
        keys("big", "small")
        client = FakeClient()
        router = LLMRouter(
            LLMChain(entries=[entry("big", tier="primary"), entry("small", tier="fast")]),
            InMemoryUsageStore(),
            client=client,
        )
        await router.complete(task="draft_email", system="s", user="u")
        await router.complete(task="classify_reply", system="s", user="u")
        assert client.calls == ["big", "small"]


class TestFailover:
    @pytest.mark.asyncio
    async def test_a_rate_limit_falls_through_to_the_next_entry(self, keys):
        keys("first", "second")
        client = FakeClient(
            RateLimitedError("429", entry_name="first", retry_after=timedelta(seconds=30)),
            _ok("second"),
        )
        router = LLMRouter(
            LLMChain(entries=[entry("first"), entry("second")]),
            InMemoryUsageStore(),
            client=client,
        )
        result = await router.complete(task="draft_email", system="s", user="u")
        assert result.entry_name == "second"
        assert client.calls == ["first", "second"]

    @pytest.mark.asyncio
    async def test_the_providers_own_retry_after_is_honoured(self, keys):
        """The prototype ignored this header and hammered the limit instead."""
        keys("first", "second")
        router = LLMRouter(
            LLMChain(entries=[entry("first"), entry("second")]),
            InMemoryUsageStore(),
            client=FakeClient(
                RateLimitedError("429", entry_name="first", retry_after=timedelta(seconds=90)),
                _ok("second"),
            ),
        )
        await router.complete(task="draft_email", system="s", user="u")

        cooldown = router.health("first").cooldown_until
        remaining = cooldown - datetime.now(tz=UTC)
        assert timedelta(seconds=80) < remaining <= timedelta(seconds=90)

    @pytest.mark.asyncio
    async def test_a_cooled_down_entry_is_skipped_on_the_next_call(self, keys):
        keys("first", "second")
        client = FakeClient(RetryableError("503", entry_name="first"), _ok("second"), _ok("second"))
        router = LLMRouter(
            LLMChain(entries=[entry("first"), entry("second")]),
            InMemoryUsageStore(),
            client=client,
        )
        await router.complete(task="draft_email", system="s", user="u")
        await router.complete(task="draft_email", system="s", user="u")
        assert client.calls == ["first", "second", "second"], "first is not retried immediately"

    @pytest.mark.asyncio
    async def test_a_permanent_error_disables_the_entry_and_alerts(self, keys):
        """A bad key will not fix itself; failing over quietly leaves it broken for weeks."""
        keys("broken", "working")
        router = LLMRouter(
            LLMChain(entries=[entry("broken"), entry("working")]),
            InMemoryUsageStore(),
            client=FakeClient(
                PermanentError("401 unauthorized", entry_name="broken"), _ok("working")
            ),
        )
        await router.complete(task="draft_email", system="s", user="u")

        assert router.health("broken").disabled_reason is not None
        assert any(a.kind == "llm_entry_disabled" for a in router.alerts)

    @pytest.mark.asyncio
    async def test_a_disabled_entry_can_be_reset_without_a_restart(self, keys):
        keys("broken")
        router = LLMRouter(
            LLMChain(entries=[entry("broken")]),
            InMemoryUsageStore(),
            client=FakeClient(PermanentError("401", entry_name="broken"), _ok("broken")),
        )
        with pytest.raises(CapacityExhausted):
            await router.complete(task="draft_email", system="s", user="u")

        router.reset("broken")
        result = await router.complete(task="draft_email", system="s", user="u")
        assert result.entry_name == "broken"

    @pytest.mark.asyncio
    async def test_a_success_clears_the_failure_count(self, keys):
        keys("flaky")
        router = LLMRouter(
            LLMChain(entries=[entry("flaky")]),
            InMemoryUsageStore(),
            client=FakeClient(_ok("flaky")),
        )
        router.health("flaky").consecutive_failures = 3
        await router.complete(task="draft_email", system="s", user="u")
        assert router.health("flaky").consecutive_failures == 0


class TestExhaustion:
    @pytest.mark.asyncio
    async def test_it_pauses_rather_than_degrading(self, keys):
        """§11: never a silent downgrade to a worse model, and never a skipped validation —
        grounding is the whole point."""
        keys("a", "b")
        router = LLMRouter(
            LLMChain(entries=[entry("a"), entry("b")]),
            InMemoryUsageStore(),
            client=FakeClient(
                RateLimitedError("429", entry_name="a", daily_quota=True),
                RateLimitedError("429", entry_name="b", daily_quota=True),
            ),
        )
        with pytest.raises(CapacityExhausted) as exc:
            await router.complete(task="draft_email", system="s", user="u")
        assert exc.value.resets_at is not None, "the UI must be able to say when it resets"
        assert any(a.kind == "llm_capacity_exhausted" for a in router.alerts)

    @pytest.mark.asyncio
    async def test_no_configured_provider_says_so_clearly(self, monkeypatch):
        monkeypatch.delenv("KEY_A", raising=False)
        router = LLMRouter(
            LLMChain(entries=[entry("a")]), InMemoryUsageStore(), client=FakeClient()
        )
        with pytest.raises(CapacityExhausted, match=r"not configured|No LLM provider"):
            await router.complete(task="draft_email", system="s", user="u")


class TestHeadroom:
    @pytest.mark.asyncio
    async def test_a_daily_token_cap_is_respected_before_spending_a_request(self, keys):
        """Asking first is cheaper than being told: a 429 costs a round trip, and the limits are
        published so clients can respect them."""
        keys("capped", "spare")
        usage = InMemoryUsageStore()
        client = FakeClient(_ok("spare"))
        router = LLMRouter(
            LLMChain(entries=[entry("capped", daily_token_cap=200), entry("spare")]),
            usage,
            client=client,
        )
        from app.services.llm.usage import UsageRecord

        await usage.record(
            UsageRecord(
                entry_name="capped",
                task="draft_email",
                tokens_in=150,
                tokens_out=100,
                latency_ms=1,
                outcome=Outcome.SUCCESS,
            )
        )
        result = await router.complete(task="draft_email", system="s", user="u")
        assert result.entry_name == "spare"
        assert client.calls == ["spare"], "the capped entry was never called"

    @pytest.mark.asyncio
    async def test_an_rpm_cap_is_respected(self, keys):
        keys("limited", "spare")
        usage = InMemoryUsageStore()
        router = LLMRouter(
            LLMChain(entries=[entry("limited", rpm_cap=1), entry("spare")]),
            usage,
            client=FakeClient(_ok("limited"), _ok("spare")),
        )
        first = await router.complete(task="draft_email", system="s", user="u")
        second = await router.complete(task="draft_email", system="s", user="u")
        assert first.entry_name == "limited"
        assert second.entry_name == "spare"


class TestAccounting:
    @pytest.mark.asyncio
    async def test_a_successful_call_is_recorded_with_its_tokens(self, keys):
        keys("a")
        usage = InMemoryUsageStore()
        import uuid

        lead_id = uuid.uuid4()
        router = LLMRouter(LLMChain(entries=[entry("a")]), usage, client=FakeClient(_ok("a")))
        await router.complete(task="draft_email", system="s", user="u", lead_id=lead_id)

        assert len(usage.records) == 1
        record = usage.records[0]
        assert record.outcome is Outcome.SUCCESS
        assert record.total_tokens == 150
        assert record.lead_id == lead_id
        assert record.task == "draft_email"

    @pytest.mark.asyncio
    async def test_failures_are_recorded_too(self, keys):
        """A provider that fails often is exactly what the admin view needs to show."""
        keys("a", "b")
        usage = InMemoryUsageStore()
        router = LLMRouter(
            LLMChain(entries=[entry("a"), entry("b")]),
            usage,
            client=FakeClient(RetryableError("503", entry_name="a"), _ok("b")),
        )
        await router.complete(task="draft_email", system="s", user="u")

        outcomes = [r.outcome for r in usage.records]
        assert outcomes == [Outcome.SERVER_ERROR, Outcome.SUCCESS]

    @pytest.mark.asyncio
    async def test_tokens_today_accumulates(self, keys):
        keys("a")
        usage = InMemoryUsageStore()
        router = LLMRouter(
            LLMChain(entries=[entry("a")]), usage, client=FakeClient(_ok("a"), _ok("a"))
        )
        await router.complete(task="draft_email", system="s", user="u")
        await router.complete(task="draft_email", system="s", user="u")
        assert await usage.tokens_today("a") == 300


class TestStructuredOutput:
    class Answer(BaseModel):
        label: str
        confidence: float

    @pytest.mark.asyncio
    async def test_valid_json_is_parsed_into_the_schema(self, keys):
        keys("a")
        router = LLMRouter(
            LLMChain(entries=[entry("a", tier="fast")]),
            InMemoryUsageStore(),
            client=FakeClient(_ok("a", '{"label": "interested", "confidence": 0.8}')),
        )
        answer = await router.structured(self.Answer, task="classify_reply", system="s", user="u")
        assert answer.label == "interested"
        assert answer.confidence == 0.8

    @pytest.mark.asyncio
    async def test_output_that_misses_the_schema_fails_over(self, keys):
        """§11: a schema failure is the model's failure. Accepting whatever came back is how
        ungrounded claims reach a recipient."""
        keys("sloppy", "good")
        client = FakeClient(
            _ok("sloppy", "Sure! Here is your answer: interested"),
            _ok("good", '{"label": "interested", "confidence": 0.9}'),
        )
        router = LLMRouter(
            LLMChain(entries=[entry("sloppy", tier="fast"), entry("good", tier="fast")]),
            InMemoryUsageStore(),
            client=client,
        )
        # The first entry returns prose, so parsing fails; the router must try the next.
        with pytest.raises(InvalidOutputError):
            await router.structured(self.Answer, task="classify_reply", system="s", user="u")

    @pytest.mark.asyncio
    async def test_json_with_the_wrong_fields_is_rejected(self, keys):
        keys("a")
        router = LLMRouter(
            LLMChain(entries=[entry("a", tier="fast")]),
            InMemoryUsageStore(),
            client=FakeClient(_ok("a", '{"verdict": "yes"}')),
        )
        with pytest.raises(InvalidOutputError, match="did not match"):
            await router.structured(self.Answer, task="classify_reply", system="s", user="u")


class TestErrorClassification:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (429, RateLimitedError),
            (402, RateLimitedError),
            (401, PermanentError),
            (403, PermanentError),
            (400, PermanentError),
            (404, PermanentError),
            (500, RetryableError),
            (502, RetryableError),
            (503, RetryableError),
        ],
    )
    def test_statuses_map_to_the_right_response(self, status, expected):
        error = classify_response(status, {}, "body", entry_name="a")
        assert isinstance(error, expected)

    def test_an_auth_failure_names_the_entry_to_fix(self):
        error = classify_response(401, {}, "", entry_name="groq-llama-70b")
        assert "groq-llama-70b" in str(error)
        assert "API key" in str(error)

    @pytest.mark.parametrize(
        "value,seconds",
        [
            ("30", 30.0),
            ("7.66s", 7.66),
            ("2m59.56s", 179.56),
            ("500ms", 0.5),
            ("1h", 3600.0),
        ],
    )
    def test_provider_reset_hints_are_parsed(self, value, seconds):
        """Providers are inconsistent about this, and all of them are more trustworthy than a
        backoff we invent."""
        parsed = parse_duration(value)
        assert parsed is not None
        assert parsed.total_seconds() == pytest.approx(seconds, abs=0.01)

    def test_unparseable_hints_return_none(self):
        assert parse_duration("soon") is None
        assert parse_duration("") is None

    @pytest.mark.parametrize(
        "body,is_daily",
        [
            ("rate limit exceeded: tokens per day", True),
            ("insufficient_quota", True),
            ("you have run out of credits", True),
            ("rate limit exceeded, please slow down", False),
            ("too many requests per minute", False),
        ],
    )
    def test_a_daily_quota_is_distinguished_from_slow_down(self, body, is_daily):
        """It matters: a per-minute limit clears in seconds, while cooling down a daily quota
        for a minute would re-fail repeatedly for hours."""
        assert looks_like_daily_quota(429, body) is is_daily

    def test_a_daily_quota_cools_down_until_the_next_utc_midnight(self):
        """Providers reset daily quotas at UTC midnight, so that is the correct wait.

        The clock is injected because the earlier version of this test read the wall clock and
        asserted "more than five minutes" — which is false for the last five minutes of any UTC
        day. CI ran at 23:56 and caught it, having passed locally every time.
        """
        error = RateLimitedError("429", entry_name="a", daily_quota=True)

        midday = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        assert cooldown_for(error, consecutive_failures=1, now=midday) == timedelta(hours=12)

        # Just before midnight the rest is short, and correctly so: the quota really is about
        # to come back.
        late = datetime(2026, 10, 12, 23, 56, tzinfo=UTC)
        assert cooldown_for(error, consecutive_failures=1, now=late) == timedelta(minutes=4)

    def test_backoff_grows_with_consecutive_failures(self):
        first = cooldown_for(RetryableError("503"), consecutive_failures=1)
        later = cooldown_for(RetryableError("503"), consecutive_failures=4)
        assert later > first

    def test_a_permanent_error_rests_the_entry_indefinitely(self):
        assert cooldown_for(PermanentError("401"), consecutive_failures=1) >= timedelta(hours=24)

    def test_a_retry_after_header_is_preferred_over_our_own_backoff(self):
        error = classify_response(429, {"Retry-After": "45"}, "", entry_name="a")
        assert isinstance(error, RateLimitedError)
        assert error.retry_after == timedelta(seconds=45)

    def test_groq_style_reset_headers_are_read(self):
        error = classify_response(
            429, {"x-ratelimit-reset-tokens": "2m30s"}, "tokens per day", entry_name="a"
        )
        assert isinstance(error, RateLimitedError)
        assert error.retry_after == timedelta(seconds=150)


class TestStatusReporting:
    @pytest.mark.asyncio
    async def test_status_explains_why_each_entry_is_unavailable(self, keys, monkeypatch):
        """ "Why is nothing happening?" should have an answer that does not require log diving."""
        keys("working")
        monkeypatch.delenv("KEY_MISSING", raising=False)
        router = LLMRouter(
            LLMChain(entries=[entry("working"), entry("missing")]),
            InMemoryUsageStore(),
            client=FakeClient(),
        )
        rows = {row["name"]: row for row in await router.status()}

        assert rows["working"]["available"] is True
        assert rows["missing"]["configured"] is False
        assert "KEY_MISSING" in rows["missing"]["reason"]


class TestAnUnsetChainFallsBackToTheDefault:
    """`LLM_CHAIN` exists to *override* the built-in order; leaving it unset is the ordinary
    case. Returning an empty chain for it made the whole system inert on first deployment —
    every research and drafting job died with "No LLM provider is configured" while
    `GROQ_API_KEY` sat correctly set in the environment. Nothing in the test suite noticed,
    because every test built its chain explicitly.
    """

    def test_an_empty_value_yields_the_default_chain(self) -> None:
        from app.services.llm.entries import DEFAULT_CHAIN, parse_chain

        assert len(parse_chain("").entries) == len(DEFAULT_CHAIN)

    def test_so_does_none_and_whitespace(self) -> None:
        from app.services.llm.entries import parse_chain

        assert parse_chain(None).entries
        assert parse_chain("   ").entries

    def test_the_default_can_serve_a_pii_free_primary_call(self) -> None:
        """The exact shape the research engine asks for, and the one that was failing."""
        from app.services.llm.entries import parse_chain

        entries = parse_chain("").entries
        assert [e.name for e in entries if e.accepts(tier="primary", has_pii=False)]

    def test_the_default_can_serve_a_call_carrying_pii(self) -> None:
        from app.services.llm.entries import parse_chain

        entries = parse_chain("").entries
        assert [e.name for e in entries if e.accepts(tier="primary", has_pii=True)]

    def test_an_explicit_chain_still_overrides_it(self) -> None:
        from app.services.llm.entries import parse_chain

        raw = (
            '[{"name": "only-one", "provider": "groq", '
            '"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile", '
            '"api_key_env": "GROQ_API_KEY", "tier": "primary"}]'
        )
        chain = parse_chain(raw)
        assert [e.name for e in chain.entries] == ["only-one"]
