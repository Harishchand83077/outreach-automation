"""Regression tests for B2 and NEW-1.

The prototype turned an LLM exception into the email body: generate_email returned
"Error generating email: ..." *with a success status*, so the graph carried it to approval and,
under AUTO_APPROVE, sent it. runner.py wrote the raw exception into the same column on crash.
Error text must never be reachable as email content.
"""

import pytest

from conftest import executable_source


class TestNodesRaiseInsteadOfReturningErrorText:
    @pytest.mark.asyncio
    async def test_generate_email_raises_on_llm_failure(self, clean_env):
        import nodes

        async def boom(system_prompt, user_prompt):
            raise RuntimeError("groq exploded")

        writes = []

        async def fake_update_lead(email, **fields):
            writes.append(fields)

        clean_env.setattr(nodes, "llm_call", boom)
        clean_env.setattr(nodes, "update_lead", fake_update_lead)

        with pytest.raises(RuntimeError, match="groq exploded"):
            await nodes.generate_email({
                "name": "A", "email": "me@example.com", "company": "Acme",
                "insights": "x", "email_attempts": 0,
            })

        # Nothing may have been persisted as a sendable draft.
        for fields in writes:
            assert fields.get("status") != "EMAIL_DRAFTED"
            assert "groq exploded" not in str(fields.get("email_draft", ""))

    @pytest.mark.asyncio
    async def test_generate_insights_raises_on_llm_failure(self, clean_env):
        import nodes

        async def boom(system_prompt, user_prompt):
            raise RuntimeError("groq exploded")

        writes = []

        async def fake_update_lead(email, **fields):
            writes.append(fields)

        clean_env.setattr(nodes, "llm_call", boom)
        clean_env.setattr(nodes, "update_lead", fake_update_lead)

        with pytest.raises(RuntimeError):
            await nodes.generate_insights({
                "name": "A", "email": "me@example.com", "company": "Acme",
                "insights_attempts": 0,
            })

        for fields in writes:
            assert fields.get("status") != "INSIGHTS_GENERATED"

    def test_no_error_prefix_strings_remain_in_nodes(self):
        src = executable_source(__import__("nodes"))
        assert "Error generating email" not in src
        assert "Error generating insights" not in src


class TestLlmClientHasNoStubFallback:
    """B2 — the "(simulated LLM response)" fallback was a sendable string."""

    def test_stub_string_is_gone(self):
        import llm_client

        assert "(simulated LLM response)" not in executable_source(llm_client)

    @pytest.mark.asyncio
    async def test_missing_backend_raises(self, clean_env):
        import llm_client

        clean_env.setattr(llm_client, "_HAS_LANGCHAIN", False)
        clean_env.setattr(llm_client, "_llm", None)
        with pytest.raises(RuntimeError):
            await llm_client.llm_call("sys", "user")

    @pytest.mark.asyncio
    async def test_missing_backend_does_not_fake_a_classification(self, clean_env):
        """It used to return the literal 'question' for anything classification-shaped."""
        import llm_client

        clean_env.setattr(llm_client, "_HAS_LANGCHAIN", False)
        clean_env.setattr(llm_client, "_llm", None)
        with pytest.raises(RuntimeError):
            await llm_client.llm_call("Classification task", "Classification (positive / ...):")


class TestRunnerDoesNotStoreExceptionsAsDrafts:
    """NEW-1 — runner.py:88 wrote str(e) into email_draft."""

    def test_exception_is_not_written_to_email_draft(self):
        import runner

        src = executable_source(runner)
        assert "email_draft=str(e)" not in src.replace(" ", "")

    @pytest.mark.asyncio
    async def test_crashed_workflow_records_error_without_a_draft(self, clean_env):
        import runner

        writes = []

        async def fake_update_lead(email, **fields):
            writes.append(fields)

        def boom_graph():
            raise RuntimeError("secret internal detail")

        clean_env.setattr(runner, "update_lead", fake_update_lead)
        clean_env.setattr(runner, "get_graph", boom_graph)

        state = await runner.run_lead_workflow(
            {"name": "A", "email": "me@example.com", "company": "Acme"}
        )

        assert state["status"] == "ERROR"
        assert writes, "the failure should still be recorded"
        for fields in writes:
            assert "secret internal detail" not in str(fields.get("email_draft", ""))
