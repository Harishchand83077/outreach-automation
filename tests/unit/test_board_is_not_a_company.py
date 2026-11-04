"""A job board's host is not a company's domain (ADR-047).

`job_posts.company_domain` was `urlsplit(apply_url).hostname`, which for every posting arriving
through a feed is the applicant tracking system's host. The fixtures in
`tests/fixtures/feeds/` have carried those URLs since F5 landed, so these assertions could have
been written then -- the parser was tested, the storage was tested, and nothing checked what the
stored value *meant*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.jobposts import boards, feeds

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"


class TestTheRealFeedsAllCarryTheirOwnHost:
    """Driven off the same real trimmed responses the parser is tested with, because the point
    is that the evidence was already in the repository."""

    @pytest.mark.parametrize(
        ("source", "expected_slug"),
        [("greenhouse", "vercel"), ("lever", "leverdemo"), ("ashby", "ramp")],
    )
    def test_every_apply_url_is_the_boards_and_the_slug_is_the_company(
        self, source: str, expected_slug: str
    ) -> None:
        raw = (FIXTURES / f"{source}.json").read_text(encoding="utf-8")
        postings = feeds.parse_feed(source, raw)

        assert postings, "the fixture should carry postings"
        for posting in postings:
            assert boards.board_vendor(posting.apply_url) == source, (
                "the apply URL is the board's own host, which is the whole defect"
            )
            assert boards.company_domain_of(posting.apply_url) is None, (
                "so it must never be stored as the company's domain"
            )
            assert boards.slug_from_url(posting.apply_url) == expected_slug


class TestWhatCountsAsABoard:
    @pytest.mark.parametrize(
        "url",
        [
            "https://boards.greenhouse.io/acme/jobs/1",
            "https://job-boards.greenhouse.io/acme/jobs/1",
            "https://jobs.lever.co/acme/abc",
            "https://jobs.ashbyhq.com/acme/abc",
            "https://acme.recruitee.com/o/engineer",
            "https://apply.workable.com/acme/j/ABC",
            "https://www.naukri.com/job-listings-engineer-acme-123",
            "https://www.linkedin.com/jobs/view/123456",
        ],
    )
    def test_a_board_url_yields_no_company_domain(self, url: str) -> None:
        assert boards.is_board_url(url)
        assert boards.company_domain_of(url) is None

    @pytest.mark.parametrize(
        ("url", "domain"),
        [
            ("https://acme.example/careers/1", "acme.example"),
            ("https://www.acme.example/jobs", "acme.example"),
            ("https://careers.acme.example/role", "careers.acme.example"),
        ],
    )
    def test_a_companys_own_site_yields_its_host(self, url: str, domain: str) -> None:
        assert not boards.is_board_url(url)
        assert boards.company_domain_of(url) == domain

    def test_the_suffix_match_is_not_a_substring_match(self) -> None:
        """`greenhouse.io.example.test` is somebody else's domain, and treating it as Greenhouse
        would blank a real company's domain. Suffix, anchored on a dot."""
        assert boards.board_vendor("https://greenhouse.io.example.test/jobs") is None
        assert boards.board_vendor("https://notgreenhouse.io/jobs") is None
        assert boards.board_vendor("https://greenhouse.io/acme/jobs/1") == "greenhouse"

    @pytest.mark.parametrize(
        "url",
        [
            "https://boards.greenhouse.io/embed/job_board?for=acme",
            "https://boards.greenhouse.io/",
            "https://jobs.lever.co",
        ],
    )
    def test_a_board_url_with_no_company_segment_yields_no_slug(self, url: str) -> None:
        """A slug is a company's name on a board. `embed` is the board's own routing, and
        inventing a company from it would put "embed" on a screen as an employer."""
        assert boards.slug_from_url(url) is None

    def test_an_apply_page_keeps_the_companys_segment(self) -> None:
        """Lever's apply URL adds a trailing segment, which must not shift the slug."""
        assert boards.slug_from_url("https://jobs.lever.co/acme/abc/apply") == "acme"

    def test_a_companys_own_url_has_no_slug(self) -> None:
        assert boards.slug_from_url("https://acme.example/careers/1") is None


class TestTheBackfillAndTheParserAreOneRule:
    def test_the_migration_imports_the_exclusion_list_rather_than_restating_it(self) -> None:
        """The backfill is SQL and the parser is Python, applied to the same strings. Two copies
        of the rule would be this project's signature defect with its halves in two languages,
        so the migration imports `NOT_A_SLUG` and a grep proves it does not spell one out."""
        path = (
            Path(__file__).resolve().parents[2]
            / "app"
            / "db"
            / "migrations"
            / "versions"
            / "20261104_1100_board_slug.py"
        )
        source = path.read_text(encoding="utf-8")

        assert "from app.services.jobposts.boards import NOT_A_SLUG" in source
        # The literal that would mean a second copy. Named in a comment there, so the check is
        # for the tuple syntax that would make it a definition.
        assert "NOT_A_SLUG = (" not in source

    def test_the_hosts_the_backfill_blanks_are_all_recognised_by_the_parser(self) -> None:
        """The migration's list is deliberately narrower -- only hosts the old code could have
        written -- but nothing in it may be a host the parser would now call a company's."""
        import importlib.util

        path = (
            Path(__file__).resolve().parents[2]
            / "app"
            / "db"
            / "migrations"
            / "versions"
            / "20261104_1100_board_slug.py"
        )
        spec = importlib.util.spec_from_file_location("board_slug_migration", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for host in module.STORED_BOARD_HOSTS:
            assert boards.board_vendor(f"https://{host}/acme/jobs/1") is not None, host
