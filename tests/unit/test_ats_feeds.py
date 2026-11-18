"""Parsing the three public ATS feeds (F5, slice S12.1/S12.2).

Fixtures are **real responses**, trimmed to five postings each, captured on 2026-10-31. The
shapes are recorded in `docs/ATS_FEEDS.md`; a parser written against an assumed shape is one that
breaks on first contact, which is why the endpoints were tested before any of this was written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.jobposts import feeds

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"


def raw(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("source", ["greenhouse", "lever", "ashby"])
def test_every_source_parses_into_postings(source):
    postings = feeds.parse_feed(source, raw(source))

    assert postings
    for posting in postings:
        assert posting.external_id
        assert posting.title
        assert posting.apply_url.startswith("http")
        assert posting.source == source


def test_lever_returns_a_bare_array_and_is_still_parsed():
    """**The difference that would have broken exactly one source.**

    Lever's top level is a list; Greenhouse and Ashby wrap theirs in `jobs`. A parser assuming
    one shape fails for Lever alone, which reads like a bad slug rather than a bad assumption.
    """
    assert isinstance(json.loads(raw("lever")), list)
    assert feeds.parse_feed("lever", raw("lever"))


def test_lever_timestamps_are_epoch_milliseconds():
    """The other two are ISO 8601. Treating these as seconds dates every posting to 1970."""
    postings = feeds.parse_feed("lever", raw("lever"))
    dated = [p for p in postings if p.posted_at]

    assert dated
    assert all(p.posted_at.year > 2000 for p in dated)


def test_greenhouse_carries_body_text_when_the_list_was_asked_for_content():
    """**Written because fit scoring was impossible for every Greenhouse board.**

    The list response carries no description *unless* asked, and `feed_url` never asked. So
    every posting polled from a Greenhouse board was stored with `body_text` NULL, and
    `fit_store.compute` refused with "this posting has no description stored" -- correctly, but
    for a reason the system had imposed on itself. `docs/ATS_FEEDS.md` had recorded
    `?content=true` as the answer since the day the shapes were captured.

    The fixture is a real response captured 2026-11-18 with that parameter, trimmed to five
    postings like its neighbours.
    """
    postings = feeds.parse_feed("greenhouse", raw("greenhouse_content"))

    assert len(postings) == 5
    assert all(p.body_text for p in postings)
    #: Readable text, not the escaped markup the field actually contains.
    body = postings[0].body_text
    assert "&lt;" not in body
    assert "&nbsp;" not in body
    assert "<p>" not in body


def test_a_greenhouse_response_without_content_still_parses():
    """The older fixture, captured before the parameter existed, and a board that returns none.

    `None` rather than an empty string, because `intake` only overwrites stored text *with*
    text -- so a feed that stops carrying descriptions must not blank what is already there.
    """
    assert all(p.body_text is None for p in feeds.parse_feed("greenhouse", raw("greenhouse")))


@pytest.mark.parametrize("source", ["lever", "ashby"])
def test_the_other_two_do_carry_body_text(source):
    assert any(p.body_text for p in feeds.parse_feed(source, raw(source)))


def test_greenhouse_location_is_nested():
    postings = feeds.parse_feed("greenhouse", raw("greenhouse"))

    assert any(p.location for p in postings)


class TestWhatItRefuses:
    def test_a_non_json_body_names_the_source(self):
        with pytest.raises(feeds.FeedError) as caught:
            feeds.parse_feed("greenhouse", "<html>404</html>")

        assert "greenhouse" in str(caught.value)

    def test_the_wrong_shape_is_refused_rather_than_silently_empty(self):
        """An empty result and a misread response must not look the same."""
        with pytest.raises(feeds.FeedError):
            feeds.parse_feed("greenhouse", json.dumps([{"id": "1"}]))
        with pytest.raises(feeds.FeedError):
            feeds.parse_feed("lever", json.dumps({"jobs": []}))

    def test_an_unknown_source_is_refused(self):
        with pytest.raises(feeds.FeedError):
            feeds.parse_feed("workday", json.dumps({"jobs": []}))

    def test_a_response_that_is_too_large_is_refused(self):
        """One real Ashby board was 2.8 MB, so a cap is a size guard and not a theory."""
        with pytest.raises(feeds.FeedError):
            feeds.parse_feed("ashby", b"x" * (feeds.MAX_FEED_BYTES + 1))

    def test_a_malformed_row_is_skipped_rather_than_failing_the_batch(self):
        """One bad posting must not cost the other two hundred from a board we do not control."""
        payload = json.dumps(
            {"jobs": [{"id": "1"}, {"id": "2", "title": "Real", "absolute_url": "https://x.test"}]}
        )

        postings = feeds.parse_feed("greenhouse", payload)

        assert [p.external_id for p in postings] == ["2"]

    def test_an_unlisted_ashby_posting_is_skipped(self):
        payload = json.dumps(
            {
                "jobs": [
                    {"id": "1", "title": "Hidden", "jobUrl": "https://x.test", "isListed": False},
                    {"id": "2", "title": "Shown", "jobUrl": "https://y.test"},
                ]
            }
        )

        assert [p.external_id for p in feeds.parse_feed("ashby", payload)] == ["2"]


class TestFeedUrls:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (
                "greenhouse",
                "https://boards-api.greenhouse.io/v1/boards/vercel/jobs?content=true",
            ),
            ("lever", "https://api.lever.co/v0/postings/vercel"),
            ("ashby", "https://api.ashbyhq.com/posting-api/job-board/vercel"),
        ],
    )
    def test_the_urls_match_what_was_tested(self, source, expected):
        assert feeds.feed_url(source, "vercel") == expected

    def test_a_slug_is_cleaned_rather_than_pasted_in_raw(self):
        assert "/boards/vercel/jobs" in feeds.feed_url("greenhouse", " /vercel/ ")

    def test_greenhouse_always_asks_for_the_descriptions(self):
        """The whole point of the fix: without this parameter no Greenhouse posting has a body,
        and nothing from such a board can be scored."""
        assert "content=true" in feeds.feed_url("greenhouse", "vercel")

    def test_an_empty_slug_is_refused(self):
        with pytest.raises(feeds.FeedError):
            feeds.feed_url("greenhouse", "  ")


class TestFlatteningGreenhousesEscapedHtml:
    """The one piece of real logic in the fix, so it is tested on its own.

    What it is *for* is a requirements list a model can read. A flattener that produced one
    unbroken paragraph would pass "no tags remain" and still lose the only structure that
    matters, which is why the list test below checks for newlines rather than for words.
    """

    def test_a_bulleted_list_keeps_one_requirement_per_line(self):
        escaped = (
            "&lt;p&gt;What you will do&lt;/p&gt;&lt;ul&gt;"
            "&lt;li&gt;Own the pipeline&lt;/li&gt;"
            "&lt;li&gt;Write Python&lt;/li&gt;"
            "&lt;li&gt;Talk to customers&lt;/li&gt;&lt;/ul&gt;"
        )

        lines = [ln for ln in feeds._plain_from_escaped_html(escaped).split("\n") if ln]

        assert lines == [
            "What you will do",
            "Own the pipeline",
            "Write Python",
            "Talk to customers",
        ]

    def test_entities_inside_the_markup_are_decoded_too(self):
        """`&amp;nbsp;` survives one unescape as `&nbsp;`, which reads as a parsing bug."""
        escaped = "&lt;p&gt;Rust&amp;nbsp;and&amp;nbsp;Go&lt;/p&gt;"

        assert feeds._plain_from_escaped_html(escaped) == "Rust and Go"

    def test_script_and_style_contents_are_dropped_whole(self):
        escaped = (
            "&lt;style&gt;.a{color:red}&lt;/style&gt;"
            "&lt;p&gt;Real words&lt;/p&gt;"
            "&lt;script&gt;window.x = 1;&lt;/script&gt;"
        )

        assert feeds._plain_from_escaped_html(escaped) == "Real words"

    def test_a_br_breaks_a_line(self):
        assert feeds._plain_from_escaped_html("a&lt;br /&gt;b") == "a\nb"

    @pytest.mark.parametrize("value", [None, "", "   ", 7, {"a": 1}, []])
    def test_anything_that_is_not_text_is_no_body_at_all(self, value):
        """`None`, never `""`: `intake` overwrites stored text only *with* text, so an empty
        string here would be the one shape that could blank a description somebody pasted."""
        assert feeds._plain_from_escaped_html(value) is None

    def test_a_posting_longer_than_the_cap_is_truncated_not_refused(self):
        escaped = "&lt;p&gt;" + ("word " * 30_000) + "&lt;/p&gt;"

        assert len(feeds._plain_from_escaped_html(escaped)) == feeds.MAX_BODY_CHARS
