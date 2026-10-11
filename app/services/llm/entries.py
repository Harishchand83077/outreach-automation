"""The LLM chain: an ordered list of providers, configured rather than hardcoded.

`ARCHITECTURE_TARGET.md` §11. Every entry speaks the same OpenAI-compatible chat-completions
endpoint, so adding a provider is configuration, not code. The chain is ordered and tried
front-to-back, so "free first, paid last" is expressed by the order itself.

The prototype had one hardcoded Groq model, retried on *every* exception including auth
failures, used a fixed 2-10s backoff that ignored `Retry-After`, and had no token accounting or
fallback at all — so when its daily quota ran out, work simply failed (issue E5).

Two rules are enforced here rather than left to the caller:

- **`allow_pii=false` means public page text only.** Some free tiers may train on prompts, so a
  lead's name, address or reply text must never reach them (§11, D6). The router refuses, rather
  than trusting each call site to remember.
- **One account per provider.** Never rotate accounts or keys to evade a free-tier quota — that
  violates provider terms (hard rule 8). Extra headroom comes from more *providers*, which is
  exactly what this chain is for.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

#: `fast` for extraction and classification, `primary` for drafting and validation.
Tier = Literal["fast", "primary"]

#: What a call is for. Decides the tier and whether lead PII is involved.
Task = Literal[
    "research_company",
    "draft_email",
    "validate_claims",
    "draft_reply",
    "classify_reply",
]

#: Per §11's routing table. `research_company` sees company page text and our own offer, never
#: anything identifying the lead, so it may use a provider that might train on prompts.
TASK_TIER: dict[str, Tier] = {
    "research_company": "primary",
    "draft_email": "primary",
    "validate_claims": "fast",
    "draft_reply": "primary",
    "classify_reply": "fast",
}

#: True when a task's prompt necessarily contains lead PII.
TASK_HAS_PII: dict[str, bool] = {
    "research_company": False,
    "draft_email": True,
    "validate_claims": True,
    "draft_reply": True,
    "classify_reply": True,
}


class LLMEntry(BaseModel):
    """One provider/model pairing in the chain."""

    model_config = ConfigDict(extra="forbid")

    #: Stable identifier used in `llm_usage` rows, cool-down state and alerts. Must be unique
    #: within a chain, because usage history is keyed on it.
    name: str = Field(min_length=1, max_length=60)
    provider: str = Field(min_length=1, max_length=40)
    base_url: HttpUrl
    model: str = Field(min_length=1, max_length=120)
    #: The *name* of the environment variable holding the key — never the key itself, so a chain
    #: can be logged or committed safely.
    api_key_env: str = Field(min_length=1, max_length=60)
    tier: Tier = "primary"

    #: False for tiers that may train on prompts (Google AI Studio outside the EEA/UK, Mistral's
    #: Experiment tier). Those receive public website text only.
    allow_pii: bool = True

    #: Caps as published by the provider. 0 or None means "not capped by us".
    daily_token_cap: int | None = Field(default=None, ge=0)
    tokens_per_min_cap: int | None = Field(default=None, ge=0)
    rpm_cap: int | None = Field(default=None, ge=0)

    #: 0 for a free tier. A non-zero cost makes the entry paid, which is gated separately.
    cost_per_mtok: float = Field(default=0.0, ge=0.0)

    #: Request timeout. Drafting is slower than classification, so this is per entry.
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_output_tokens: int = Field(default=2048, ge=64, le=32_000)

    @property
    def is_paid(self) -> bool:
        return self.cost_per_mtok > 0

    @property
    def api_key(self) -> str | None:
        """Read the key at call time, never stored on the model."""
        value = os.environ.get(self.api_key_env, "").strip()
        return value or None

    @property
    def is_configured(self) -> bool:
        """An entry whose key is absent is skipped, not an error.

        That is what makes the chain additive: setting a key enables a provider, and unsetting it
        removes it, with no code change either way.
        """
        return self.api_key is not None

    @property
    def completions_url(self) -> str:
        return f"{str(self.base_url).rstrip('/')}/chat/completions"

    def accepts(self, *, tier: Tier, has_pii: bool) -> bool:
        """Whether this entry may serve a call of this shape."""
        if self.tier != tier:
            return False
        return self.allow_pii or not has_pii


class LLMChain(BaseModel):
    """The ordered chain. Order is priority: first healthy entry with headroom wins."""

    model_config = ConfigDict(extra="forbid")

    entries: list[LLMEntry] = Field(default_factory=list)
    #: A paid entry is skipped unless this is explicitly on, with a daily spend cap set.
    paid_enabled: bool = False
    paid_daily_usd_cap: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _names_are_unique(self) -> Self:
        names = [entry.name for entry in self.entries]
        if len(names) != len(set(names)):
            raise ValueError("LLM entry names must be unique — usage history is keyed on them")
        return self

    @model_validator(mode="after")
    def _paid_entries_need_a_cap(self) -> Self:
        """Spending money requires two deliberate settings, not one (hard rule 5)."""
        if self.paid_enabled and self.paid_daily_usd_cap <= 0:
            raise ValueError(
                "paid_enabled requires paid_daily_usd_cap > 0: set PAID_LLM_DAILY_USD_CAP"
            )
        return self

    def usable(self, *, tier: Tier, has_pii: bool) -> list[LLMEntry]:
        """Entries that could serve this call, in priority order.

        Filters on configuration, tier, PII policy and the paid gate. Cool-downs and usage
        headroom are the router's business, since they are stateful.
        """
        return [
            entry
            for entry in self.entries
            if entry.is_configured
            and entry.accepts(tier=tier, has_pii=has_pii)
            and (not entry.is_paid or self.paid_enabled)
        ]

    def by_name(self, name: str) -> LLMEntry | None:
        return next((entry for entry in self.entries if entry.name == name), None)

    @property
    def configured_names(self) -> list[str]:
        """For the admin view: which providers are actually available right now."""
        return [entry.name for entry in self.entries if entry.is_configured]


def default_chain(*, paid_enabled: bool = False, paid_daily_usd_cap: float = 0.0) -> LLMChain:
    return LLMChain(
        entries=[LLMEntry.model_validate(item) for item in DEFAULT_CHAIN],
        paid_enabled=paid_enabled,
        paid_daily_usd_cap=paid_daily_usd_cap,
    )


def parse_chain(
    raw: str | None, *, paid_enabled: bool = False, paid_daily_usd_cap: float = 0.0
) -> LLMChain:
    """Build a chain from the `LLM_CHAIN` environment value (a JSON list of entries).

    **An empty or absent value yields `DEFAULT_CHAIN`, not an empty chain.** `LLM_CHAIN` exists
    to *override* the built-in order, and leaving it unset is the ordinary case — so returning
    nothing made the whole system inert the moment it was deployed without one: every research
    and drafting job died with "No LLM provider is configured" while `GROQ_API_KEY` sat correctly
    set in the environment.

    Entries whose key is absent are skipped later by `is_configured`, which is what keeps this
    additive: setting a key enables a provider and unsetting it removes one, with no code change.
    """
    if not raw or not raw.strip():
        return default_chain(paid_enabled=paid_enabled, paid_daily_usd_cap=paid_daily_usd_cap)

    try:
        loaded: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM_CHAIN is not valid JSON: {exc}") from exc

    if not isinstance(loaded, list):
        raise ValueError("LLM_CHAIN must be a JSON list of entry objects")

    return LLMChain(
        entries=[LLMEntry.model_validate(item) for item in loaded],
        paid_enabled=paid_enabled,
        paid_daily_usd_cap=paid_daily_usd_cap,
    )


#: A sensible starting chain, used when `LLM_CHAIN` is not set. Entries whose key is absent are
#: skipped, so this works with however many providers happen to be configured.
#:
#: Groq appears twice on purpose: its limits are published *per model*, so two models give two
#: separate pools of headroom inside one account — which is the legitimate way to get more
#: capacity, unlike registering a second account.
DEFAULT_CHAIN: list[dict[str, Any]] = [
    {
        "name": "groq-llama-70b",
        "provider": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "api_key_env": "GROQ_API_KEY",
        "tier": "primary",
        "allow_pii": True,
    },
    {
        "name": "groq-llama-8b",
        "provider": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.1-8b-instant",
        "api_key_env": "GROQ_API_KEY",
        "tier": "fast",
        "allow_pii": True,
    },
    {
        "name": "cerebras-llama-70b",
        "provider": "cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "model": "llama-3.3-70b",
        "api_key_env": "CEREBRAS_API_KEY",
        "tier": "primary",
        "allow_pii": True,
    },
    {
        "name": "openrouter-free",
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "api_key_env": "OPENROUTER_API_KEY",
        "tier": "primary",
        "allow_pii": True,
    },
    {
        "name": "openrouter-free-fast",
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.1-8b-instruct:free",
        "api_key_env": "OPENROUTER_API_KEY",
        "tier": "fast",
        "allow_pii": True,
    },
    # Google AI Studio's free tier may train on prompts outside the EEA/UK, so it is restricted
    # to company page text: no lead name, address or reply content ever reaches it.
    {
        "name": "gemini-flash",
        "provider": "google",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.0-flash",
        "api_key_env": "GEMINI_API_KEY",
        "tier": "primary",
        "allow_pii": False,
    },
]
