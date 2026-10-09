"""Regression tests for B4 and NEW-2.

The prototype's decision engine sent a "book a meeting" email and set MEETING_BOOKED purely on an
LLM label, with no human gate — while the UI claimed "Nothing is sent without your approval".
Combined with substring classification (B17), a reply reading "not positive" reached this path.

Hard rules: an LLM label can never trigger a send, and MEETING_BOOKED is set only by a human.
"""

import pytest

import decision_engine
from conftest import executable_source

LEAD = {
    "name": "Asha",
    "email": "lead@example.com",
    "company": "Acme",
    "reply": "Sounds good, send me a time",
    "insights": "something",
    "email_draft": "Subject: Hi\n\nBody",
}


@pytest.fixture
def spy(clean_env):
    """Record sends and DB writes instead of performing them."""
    sent = []
    writes = []

    async def fake_send_email(**kwargs):
        sent.append(kwargs)
        raise AssertionError("decision_engine must not send mail without human approval")

    async def fake_update_lead(email, **fields):
        writes.append(fields)

    async def fake_llm_call(system_prompt, user_prompt):
        return "a generated document"

    clean_env.setattr(decision_engine, "send_email", fake_send_email)
    clean_env.setattr(decision_engine, "update_lead", fake_update_lead)
    clean_env.setattr(decision_engine, "llm_call", fake_llm_call)
    return {"sent": sent, "writes": writes}


class TestPositiveReplyIsNotActedOnAutomatically:
    @pytest.mark.asyncio
    async def test_no_email_is_sent(self, spy):
        await decision_engine.handle_positive(dict(LEAD))
        assert spy["sent"] == []

    @pytest.mark.asyncio
    async def test_meeting_is_not_marked_booked(self, spy):
        await decision_engine.handle_positive(dict(LEAD))
        for fields in spy["writes"]:
            assert fields.get("status") != "MEETING_BOOKED"
            assert fields.get("meeting_booked") != 1

    @pytest.mark.asyncio
    async def test_classification_is_persisted_for_a_human(self, spy):
        """B17's other half: persist the classification before acting on it."""
        await decision_engine.handle_positive(dict(LEAD))
        assert any(f.get("classification") == "positive" for f in spy["writes"])

    @pytest.mark.asyncio
    async def test_lead_lands_in_a_state_that_needs_a_human(self, spy):
        await decision_engine.handle_positive(dict(LEAD))
        statuses = [f.get("status") for f in spy["writes"] if f.get("status")]
        assert statuses, "the lead's state must be recorded"
        assert statuses[-1] == "REPLIED_NEEDS_REVIEW"


class TestSourceLevelGuards:
    def test_meeting_booked_is_not_set_anywhere_in_the_decision_engine(self):
        src = executable_source(decision_engine)
        assert "MEETING_BOOKED" not in src
        assert "meeting_booked=1" not in src

    def test_calendar_link_is_not_mailed_from_the_decision_engine(self):
        """The auto-reply body embedded CALENDAR_LINK; the whole body is gone."""
        assert "CALENDAR_LINK" not in executable_source(decision_engine)


class TestNoInterestStillClosesTheLead:
    @pytest.mark.asyncio
    async def test_no_interest_is_recorded(self, spy):
        await decision_engine.handle_no_interest(dict(LEAD))
        assert any(f.get("classification") == "no_interest" for f in spy["writes"])
        assert spy["sent"] == []
