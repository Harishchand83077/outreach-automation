"""The router: pick a provider, call it, fall over, account for it.

`ARCHITECTURE_TARGET.md` §11. Everything stateful about provider health lives here, so retry and
failover policy exists in exactly one place rather than at every call site.

What it guarantees, and why each one matters:

- **PII never reaches a provider that may train on prompts.** Enforced by task, not by trust: a
  caller cannot accidentally route a lead's reply to `allow_pii=false` (§11, D6).
- **A permanent error disables an entry and alerts.** The prototype retried a 401 three times
  and then failed, hiding a bad key behind what looked like a transient problem (E5).
- **Exhaustion pauses work and says when it resets.** Never a silent downgrade to a worse model
  and never a skipped validation step, because grounding is the point.
- **Every call is recorded**, successes and failures alike, so capacity is measured.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from app.services.llm.client import Completion, LLMClient, parse_structured
from app.services.llm.entries import (
    TASK_HAS_PII,
    TASK_TIER,
    LLMChain,
    LLMEntry,
    Tier,
)
from app.services.llm.errors import (
    CapacityExhausted,
    LLMError,
    Outcome,
    PermanentError,
    cooldown_for,
)
from app.services.llm.usage import UsageRecord, UsageStore


@dataclass
class EntryHealth:
    """Per-entry state. In-process: the worker is a single process, and a cool-down that is
    forgotten on restart is safe — the next call simply re-discovers the limit."""

    consecutive_failures: int = 0
    cooldown_until: datetime | None = None
    disabled_reason: str | None = None

    def is_available(self, now: datetime) -> bool:
        if self.disabled_reason is not None:
            return False
        return self.cooldown_until is None or self.cooldown_until <= now


@dataclass
class RouterDecision:
    """Why an entry was or was not chosen. Surfaced in `/admin/usage` so "why is nothing
    happening?" has an answer that does not require reading logs."""

    entry_name: str
    available: bool
    reason: str = ""


@dataclass
class Alert:
    """Something a person needs to see: a bad key, or the chain running dry."""

    kind: str
    message: str
    entry_name: str | None = None
    at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class LLMRouter:
    """Routes one task to the best available entry, with failover and accounting."""

    def __init__(
        self,
        chain: LLMChain,
        usage: UsageStore,
        *,
        client: LLMClient | None = None,
        on_alert: Any = None,
    ) -> None:
        self._chain = chain
        self._usage = usage
        self._client = client
        self._health: dict[str, EntryHealth] = {}
        self._on_alert = on_alert
        self.alerts: list[Alert] = []

    # ---- health -----------------------------------------------------------------

    def health(self, entry_name: str) -> EntryHealth:
        return self._health.setdefault(entry_name, EntryHealth())

    def _alert(self, alert: Alert) -> None:
        self.alerts.append(alert)
        if self._on_alert is not None:
            self._on_alert(alert)

    def cool_down(self, entry: LLMEntry, error: LLMError, *, now: datetime | None = None) -> None:
        """Rest an entry after a failure, honouring the provider's own hint where given.

        One clock for both the delay and the deadline: computing the delay against a different
        instant than the one it is added to is how an off-by-a-few-seconds cool-down appears.
        """
        state = self.health(entry.name)
        state.consecutive_failures += 1
        moment = now or datetime.now(tz=UTC)
        delay = cooldown_for(error, consecutive_failures=state.consecutive_failures, now=moment)
        state.cooldown_until = moment + delay

        if isinstance(error, PermanentError):
            # Not transient: failing over quietly would leave a broken key in place for weeks.
            state.disabled_reason = str(error)
            self._alert(
                Alert(
                    kind="llm_entry_disabled",
                    message=f"{entry.name} disabled: {error}",
                    entry_name=entry.name,
                )
            )

    def mark_healthy(self, entry: LLMEntry) -> None:
        state = self.health(entry.name)
        state.consecutive_failures = 0
        state.cooldown_until = None

    def reset(self, entry_name: str) -> None:
        """Clear state after an operator fixes a key — no restart required."""
        self._health.pop(entry_name, None)

    # ---- selection --------------------------------------------------------------

    async def _has_headroom(self, entry: LLMEntry, now: datetime) -> tuple[bool, str]:
        """Check the entry's own published caps before spending a request on it.

        Asking first is cheaper than being told: a 429 costs a round trip and burns goodwill
        with the provider, and the limits are published precisely so clients can respect them.
        """
        if entry.daily_token_cap:
            used = await self._usage.tokens_today(entry.name, now=now)
            if used >= entry.daily_token_cap:
                return False, f"daily token cap reached ({used}/{entry.daily_token_cap})"

        if entry.tokens_per_min_cap:
            used = await self._usage.tokens_in_last_minute(entry.name, now=now)
            if used >= entry.tokens_per_min_cap:
                return False, f"per-minute token cap reached ({used}/{entry.tokens_per_min_cap})"

        if entry.rpm_cap:
            used = await self._usage.requests_in_last_minute(entry.name, now=now)
            if used >= entry.rpm_cap:
                return False, f"requests-per-minute cap reached ({used}/{entry.rpm_cap})"

        return True, ""

    async def candidates(
        self, *, tier: Tier, has_pii: bool, now: datetime | None = None
    ) -> list[tuple[LLMEntry, RouterDecision]]:
        """Every usable entry in priority order, each with a verdict and a reason."""
        now = now or datetime.now(tz=UTC)
        results: list[tuple[LLMEntry, RouterDecision]] = []

        for entry in self._chain.usable(tier=tier, has_pii=has_pii):
            state = self.health(entry.name)
            if state.disabled_reason is not None:
                results.append(
                    (entry, RouterDecision(entry.name, False, f"disabled: {state.disabled_reason}"))
                )
                continue
            if not state.is_available(now):
                results.append(
                    (
                        entry,
                        RouterDecision(
                            entry.name, False, f"cooling down until {state.cooldown_until:%H:%M:%S}"
                        ),
                    )
                )
                continue

            ok, why = await self._has_headroom(entry, now)
            results.append((entry, RouterDecision(entry.name, ok, why)))

        return results

    def _next_reset(self, now: datetime) -> datetime | None:
        """When the chain might work again, for the UI's "resets at …" message."""
        times = [
            state.cooldown_until
            for state in self._health.values()
            if state.cooldown_until is not None and state.disabled_reason is None
        ]
        return min(times) if times else None

    # ---- calling ----------------------------------------------------------------

    async def complete(
        self,
        *,
        task: str,
        system: str,
        user: str,
        lead_id: uuid.UUID | None = None,
        temperature: float = 0.3,
        json_schema: dict[str, Any] | None = None,
        contains_pii: bool | None = None,
    ) -> Completion:
        """Run `task`, trying each usable entry in order.

        `contains_pii` defaults to the task's declared value and may only be raised, never
        lowered: a caller may say "this particular prompt also carries PII", but cannot declare
        a reply-classification prompt PII-free to unlock a wider set of providers.
        """
        tier = TASK_TIER.get(task, "primary")
        has_pii = TASK_HAS_PII.get(task, True)
        if contains_pii is True:
            has_pii = True

        now = datetime.now(tz=UTC)
        decisions = await self.candidates(tier=tier, has_pii=has_pii, now=now)

        if not decisions:
            raise CapacityExhausted(
                f"No LLM provider is configured for task {task!r} "
                f"(tier={tier}, pii={has_pii}). Set a provider key - see env.example."
            )

        last_error: LLMError | None = None
        for entry, decision in decisions:
            if not decision.available:
                continue

            try:
                completion = await self._call(
                    entry,
                    system=system,
                    user=user,
                    temperature=temperature,
                    json_schema=json_schema,
                )
            except LLMError as error:
                last_error = error
                await self._usage.record(
                    UsageRecord(
                        entry_name=entry.name,
                        task=task,
                        tokens_in=0,
                        tokens_out=0,
                        latency_ms=0,
                        outcome=error.outcome,
                        lead_id=lead_id,
                    )
                )
                self.cool_down(entry, error)
                continue

            self.mark_healthy(entry)
            await self._usage.record(
                UsageRecord(
                    entry_name=entry.name,
                    task=task,
                    tokens_in=completion.tokens_in,
                    tokens_out=completion.tokens_out,
                    latency_ms=completion.latency_ms,
                    outcome=Outcome.SUCCESS,
                    lead_id=lead_id,
                )
            )
            return completion

        # Nothing left. Pause rather than degrade: a worse model or a skipped validation would
        # defeat the grounding this system exists to provide.
        resets_at = self._next_reset(now)
        blocked = "; ".join(f"{d.entry_name}: {d.reason}" for _, d in decisions if d.reason)
        self._alert(
            Alert(
                kind="llm_capacity_exhausted",
                message=f"All LLM providers unavailable for {task!r}. {blocked}",
            )
        )
        raise CapacityExhausted(
            f"LLM capacity exhausted for task {task!r}. {blocked}"
            + (f" Last error: {last_error}" if last_error else ""),
            resets_at=resets_at,
        )

    async def structured[M: BaseModel](
        self,
        schema: type[M],
        *,
        task: str,
        system: str,
        user: str,
        lead_id: uuid.UUID | None = None,
        temperature: float = 0.0,
        contains_pii: bool | None = None,
    ) -> M:
        """A completion validated against `schema`.

        A schema failure is treated as that model's failure (§11): the entry cools down and the
        next one is tried, rather than unvalidated output being accepted. Subject lines, labels
        and claims are all fields here — never something parsed out of prose (issue B13).
        """
        answer, _ = await self.structured_with_entry(
            schema,
            task=task,
            system=system,
            user=user,
            lead_id=lead_id,
            temperature=temperature,
            contains_pii=contains_pii,
        )
        return answer

    async def structured_with_entry[M: BaseModel](
        self,
        schema: type[M],
        *,
        task: str,
        system: str,
        user: str,
        lead_id: uuid.UUID | None = None,
        temperature: float = 0.0,
        contains_pii: bool | None = None,
    ) -> tuple[M, str]:
        """`structured`, plus the name of the entry that answered.

        Callers that store provenance need it: `drafts.llm_entry` and
        `classifications.llm_entry` exist so a decision can be traced to the model that made it
        (§11), and "some model in the chain" is not an answer when a label looks wrong.
        """
        completion = await self.complete(
            task=task,
            system=system,
            user=user,
            lead_id=lead_id,
            temperature=temperature,
            json_schema=schema.model_json_schema(),
            contains_pii=contains_pii,
        )
        return parse_structured(completion, schema), completion.entry_name

    async def _call(
        self,
        entry: LLMEntry,
        *,
        system: str,
        user: str,
        temperature: float,
        json_schema: dict[str, Any] | None,
    ) -> Completion:
        if self._client is not None:
            return await self._client.complete(
                entry,
                system=system,
                user=user,
                temperature=temperature,
                json_schema=json_schema,
            )
        async with LLMClient() as client:
            return await client.complete(
                entry,
                system=system,
                user=user,
                temperature=temperature,
                json_schema=json_schema,
            )

    # ---- reporting --------------------------------------------------------------

    async def status(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Per-entry state for `/admin/usage`."""
        now = now or datetime.now(tz=UTC)
        rows: list[dict[str, Any]] = []
        for entry in self._chain.entries:
            state = self.health(entry.name)
            available = entry.is_configured and state.is_available(now)
            headroom_ok, why = await self._has_headroom(entry, now) if available else (False, "")
            rows.append(
                {
                    "name": entry.name,
                    "provider": entry.provider,
                    "model": entry.model,
                    "tier": entry.tier,
                    "allow_pii": entry.allow_pii,
                    "configured": entry.is_configured,
                    "available": available and headroom_ok,
                    "reason": ("" if entry.is_configured else f"{entry.api_key_env} is not set")
                    or state.disabled_reason
                    or why,
                    "tokens_today": await self._usage.tokens_today(entry.name, now=now),
                    "consecutive_failures": state.consecutive_failures,
                }
            )
        return rows


def build_router(settings: Any, usage: UsageStore, *, client: LLMClient | None = None) -> LLMRouter:
    """Construct a router from settings, falling back to the default chain.

    `LLM_CHAIN` is read as JSON when set; otherwise the built-in chain is used, and entries
    whose key is absent are simply skipped. That means the system works with however many
    providers happen to be configured, with no code change to add one.
    """
    from app.services.llm.entries import default_chain, parse_chain

    raw = getattr(settings, "LLM_CHAIN", "") or ""
    paid_enabled = bool(getattr(settings, "PAID_LLM_ENABLED", False))
    cap = float(getattr(settings, "PAID_LLM_DAILY_USD_CAP", 0.0) or 0.0)

    chain = (
        parse_chain(raw, paid_enabled=paid_enabled, paid_daily_usd_cap=cap)
        if raw.strip()
        else default_chain(paid_enabled=paid_enabled, paid_daily_usd_cap=cap)
    )
    return LLMRouter(chain, usage, client=client)
