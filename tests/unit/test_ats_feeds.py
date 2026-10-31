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


def test_greenhouse_postings_have_no_body_text():
    """Its list response carries no description, so a body needs a second call per job.

    Asserted rather than assumed, because a model that required body text would have made
    Greenhouse unusable and the reason would not have been obvious.
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
            ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/vercel/jobs"),
            ("lever", "https://api.lever.co/v0/postings/vercel"),
            ("ashby", "https://api.ashbyhq.com/posting-api/job-board/vercel"),
        ],
    )
    def test_the_urls_match_what_was_tested(self, source, expected):
        assert feeds.feed_url(source, "vercel") == expected

    def test_a_slug_is_cleaned_rather_than_pasted_in_raw(self):
        assert feeds.feed_url("greenhouse", " /vercel/ ").endswith("/boards/vercel/jobs")

    def test_an_empty_slug_is_refused(self):
        with pytest.raises(feeds.FeedError):
            feeds.feed_url("greenhouse", "  ")
