"""The go-live gate's own tests (F1, slice S10.1).

Two kinds, and the structural half matters as much as the behavioural one. This script reads a
real database and prints to a terminal somebody will paste into a chat window, so what it must
never print is a property worth asserting rather than trusting -- the same bargain as
`test_mcp_smoke_script.py`.

The behavioural half is about the booking link and the gate's arithmetic, both pure functions,
because a decision that can only be checked by standing up a database is a decision nobody
checks.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.scripts import preflight
from app.services.deliverability.sender_requirements import Verdict

SCRIPT = Path(preflight.__file__)


class TestTheBookingLink:
    """A link that books nothing is worse than no link: it looks done.

    The live campaign's booking URL was `calendar.google.com/calendar/u/0/r`, which opens the
    **reader's own** calendar. A recipient clicking it sees their own week, and nothing is ever
    scheduled. That shipped and sat there, because every validator only asked whether the field
    was filled in.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://calendar.google.com/calendar/u/0/r?pli=1",
            "https://calendar.google.com/calendar/u/1/r",
            "https://calendar.google.com/calendar/r",
            "https://outlook.live.com/calendar/0/view/month",
        ],
    )
    def test_a_link_to_the_readers_own_calendar_fails(self, url):
        line = preflight.assess_booking_url(url, "Worth a call?")

        assert line.verdict == preflight.FAIL
        assert "own calendar" in line.detail

    @pytest.mark.parametrize(
        "url",
        [
            "https://cal.com/asha/20min",
            "https://calendly.com/asha/intro",
            "https://calendar.app.google/abc123",
            "https://zcal.co/asha",
        ],
    )
    def test_a_real_scheduling_page_passes(self, url):
        assert preflight.assess_booking_url(url, None).verdict == preflight.PASS

    def test_a_written_ask_with_no_link_is_a_warning_not_a_failure(self):
        """Allowed by `CampaignSettings` since Phase 2, so the gate must not contradict it.

        It is a warning because a reply then has to be read and answered by hand, which is a
        slower funnel rather than a broken one.
        """
        line = preflight.assess_booking_url(None, "Worth a 20-minute call next week?")

        assert line.verdict == preflight.WARN

    def test_neither_a_link_nor_an_ask_fails(self):
        assert preflight.assess_booking_url(None, None).verdict == preflight.FAIL

    def test_whitespace_is_not_a_booking_url(self):
        assert preflight.assess_booking_url("   ", "   ").verdict == preflight.FAIL


class TestTheGate:
    def test_a_warning_does_not_stop_the_gate(self):
        """Warnings are information. A gate that fails on them is a gate nobody can pass."""
        lines = [
            preflight.Line("a", preflight.PASS, ""),
            preflight.Line("b", preflight.WARN, ""),
        ]

        assert preflight.gate(lines) is True

    def test_one_failure_stops_it(self):
        lines = [preflight.Line("a", preflight.PASS, ""), preflight.Line("b", preflight.FAIL, "")]

        assert preflight.gate(lines) is False

    def test_an_unknown_dns_verdict_is_a_failure_here(self):
        """**The one place `UNKNOWN` must not be treated gently.**

        The admin screen shows the nuance -- "we could not tell" is honest and useful there. A
        go-live gate has to answer, and "we could not tell" is not a yes.
        """
        assert preflight.verdict_of(Verdict.UNKNOWN) == preflight.FAIL
        assert preflight.verdict_of(Verdict.WARN) == preflight.WARN
        assert preflight.verdict_of(Verdict.PASS) == preflight.PASS

    def test_the_summary_names_the_outcome_either_way(self):
        go = preflight.render([preflight.Line("a", preflight.PASS, "fine")])
        no_go = preflight.render([preflight.Line("a", preflight.FAIL, "broken")])

        assert "GO:" in go
        assert "NO-GO:" in no_go

    def test_a_pass_still_says_what_a_machine_cannot_check(self):
        """DKIM passing in DNS is not DKIM signing. Only a received message shows that.

        A gate that said GO without this would be the most expensive kind of wrong: confident.
        """
        assert "Show original" in preflight.render([preflight.Line("a", preflight.PASS, "fine")])


class TestWhatItMustNeverPrint:
    """Source-level, because the danger is an edit made later.

    This output is pasted into terminals and chat windows. Hard rule 2 has already been breached
    once in this project by a failing assertion printing every configured API key.
    """

    def test_it_never_prints_a_setting_that_holds_a_secret(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        forbidden = {
            "CREDENTIAL_KEY",
            "SUPABASE_JWT_SECRET",
            "DATABASE_URL",
            "GOOGLE_CLIENT_SECRET",
            "SMTP_PASSWORD",
            "IMAP_PASSWORD",
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute):
                    assert inner.attr not in forbidden, f"preflight prints {inner.attr}"

    def test_it_never_echoes_a_stored_invalid_reason(self):
        """That text can quote a provider's reply, and it reaches a screenshot (ADR-021).

        The script may *read* it to decide a mailbox is unusable -- which it does, through the
        shared `unavailable_reason` -- and may not repeat it.
        """
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "invalid_reason", (
                    "preflight reads invalid_reason directly; ask unavailable_reason instead"
                )

    def test_its_printed_output_is_plain_ascii(self):
        """A non-ASCII character arrives as a replacement glyph in a Windows console.

        The smoke check already learned this when a credential prefix printed with a Unicode
        ellipsis looked corrupted. Docstrings are exempt: they are read in an editor.
        """
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in docstrings:
                    continue
                assert node.value.isascii(), f"non-ascii in a printable string: {node.value!r}"


class TestItSharesItsRulesRatherThanRestatingThem:
    def test_it_uses_the_same_domain_checker_as_the_admin_screen(self):
        """A script and a screen that disagreed about readiness would be the worst instance
        of this project's signature defect, because this is where somebody acts on the answer.
        """
        source = SCRIPT.read_text(encoding="utf-8")

        assert "check_domain" in source
        assert "sender_requirements" in source

    def test_it_asks_unavailable_reason_rather_than_deciding_itself(self):
        source = SCRIPT.read_text(encoding="utf-8")

        assert "unavailable_reason" in source

    def test_it_reads_the_send_mode_through_the_one_function_that_decides_it(self):
        """Hard rule 1: `effective_send_mode` is the only thing that resolves a send mode."""
        source = SCRIPT.read_text(encoding="utf-8")

        assert "effective_send_mode" in source
