"""A page can say that it changed and never what changed (F22, ADR-054).

`pages.py` is the only thing in this project that fetches a page nobody pointed us at posting by
posting, so most of these tests are about what it refuses to do. `respx` throughout -- never the
network (hard rule 9's sibling).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.jobposts import pages, terms

pytestmark = pytest.mark.asyncio

CAREERS = "https://acme.example/careers"
ROBOTS = "https://acme.example/robots.txt"


class TestWhatIsHashed:
    def test_a_script_tag_does_not_change_the_hash(self) -> None:
        """The defect this guards. A build id or an analytics nonce in a `<script>` changes on
        every deploy, so hashing it would report a change every single day -- which is the same
        as reporting nothing, except that somebody would act on the first few."""
        first = pages.hash_of("<html><script>var build='a1'</script><h1>Jobs</h1></html>")
        second = pages.hash_of("<html><script>var build='b2'</script><h1>Jobs</h1></html>")
        assert first == second

    def test_a_style_tag_does_not_change_the_hash(self) -> None:
        first = pages.hash_of("<style>.a{color:red}</style><p>Backend Engineer</p>")
        second = pages.hash_of("<style>.a{color:blue}</style><p>Backend Engineer</p>")
        assert first == second

    def test_a_new_role_does_change_the_hash(self) -> None:
        before = pages.hash_of("<ul><li>Backend Engineer</li></ul>")
        after = pages.hash_of("<ul><li>Backend Engineer</li><li>SRE</li></ul>")
        assert before != after

    def test_whitespace_and_markup_changes_do_not(self) -> None:
        """A site that reflows its markup has not opened a role."""
        before = pages.hash_of("<div><p>Backend Engineer</p></div>")
        after = pages.hash_of("<section>\n  <h2>Backend   Engineer</h2>\n</section>")
        assert before == after

    def test_the_text_is_not_returned_by_the_state(self) -> None:
        """Storing the page would make this a crawler with a cache: somebody else's copy of
        their own site, in our database, for every company any workspace watches."""
        fields = pages.PageState.__dataclass_fields__
        assert "text" not in fields and "body" not in fields and "html" not in fields


class TestRobots:
    @respx.mock
    async def test_a_disallowed_page_is_refused_and_never_fetched(self) -> None:
        robots = respx.get(ROBOTS).mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /careers")
        )
        page = respx.get(CAREERS).mock(return_value=httpx.Response(200, text="<p>Jobs</p>"))

        with pytest.raises(pages.PageRefused):
            await pages.read_page(CAREERS)

        assert robots.called
        # The point: not merely reported as refused, actually not requested.
        assert not page.called

    @respx.mock
    async def test_an_allowed_page_is_read(self) -> None:
        respx.get(ROBOTS).mock(return_value=httpx.Response(200, text="User-agent: *\nAllow: /"))
        respx.get(CAREERS).mock(return_value=httpx.Response(200, text="<p>Backend Engineer</p>"))
        state = await pages.read_page(CAREERS)
        assert state.content_hash == pages.hash_of("<p>Backend Engineer</p>")

    @respx.mock
    async def test_a_missing_robots_file_is_permission(self) -> None:
        """The convention the standard describes, and the only workable reading: most sites
        serve no such file, and refusing every one of them would mean the feature never runs."""
        respx.get(ROBOTS).mock(return_value=httpx.Response(404))
        respx.get(CAREERS).mock(return_value=httpx.Response(200, text="<p>Jobs</p>"))
        state = await pages.read_page(CAREERS)
        assert state.length > 0

    @respx.mock
    async def test_an_unreachable_robots_file_is_permission_not_a_failure(self) -> None:
        respx.get(ROBOTS).mock(side_effect=httpx.ConnectError("nope"))
        respx.get(CAREERS).mock(return_value=httpx.Response(200, text="<p>Jobs</p>"))
        assert (await pages.read_page(CAREERS)).length > 0

    @respx.mock
    async def test_the_request_identifies_itself(self) -> None:
        """A fetch that cannot be identified is a fetch nobody can refuse."""
        respx.get(ROBOTS).mock(return_value=httpx.Response(404))
        route = respx.get(CAREERS).mock(return_value=httpx.Response(200, text="<p>Jobs</p>"))
        await pages.read_page(CAREERS)
        assert "outreach-automation" in route.calls[0].request.headers["User-Agent"]


class TestTwoClassesOfFailure:
    """A refusal is permanent until the site changes its mind; a timeout is worth trying again
    tomorrow. Collapsing them is how a site that said no gets asked again every day, and how one
    bad afternoon permanently stops a watch."""

    @respx.mock
    async def test_a_timeout_is_unavailable_not_refused(self) -> None:
        respx.get(ROBOTS).mock(return_value=httpx.Response(404))
        respx.get(CAREERS).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(pages.PageUnavailable):
            await pages.read_page(CAREERS)

    @respx.mock
    async def test_an_http_error_is_unavailable(self) -> None:
        respx.get(ROBOTS).mock(return_value=httpx.Response(404))
        respx.get(CAREERS).mock(return_value=httpx.Response(503))
        with pytest.raises(pages.PageUnavailable, match="503"):
            await pages.read_page(CAREERS)

    @respx.mock
    async def test_something_far_too_large_is_not_a_careers_page(self) -> None:
        respx.get(ROBOTS).mock(return_value=httpx.Response(404))
        respx.get(CAREERS).mock(
            return_value=httpx.Response(200, content=b"x" * (pages.MAX_PAGE_BYTES + 1))
        )
        with pytest.raises(pages.PageUnavailable, match="larger than"):
            await pages.read_page(CAREERS)

    async def test_a_non_http_url_is_refused_without_a_request(self) -> None:
        with pytest.raises(pages.PageUnavailable, match="http or https"):
            await pages.read_page("file:///etc/passwd")


class TestTerms:
    def test_a_source_we_have_not_read_is_refused(self) -> None:
        """`UNKNOWN` is a refusal here, unlike in the deliverability report. There, "we could
        not tell" goes to a person who then decides; here the next step is an automated fetch
        repeated daily for ever."""
        assert not terms.may_poll("workday")
        assert "have not read" in terms.terms_for("workday").basis

    def test_every_pollable_source_carries_its_basis_and_a_date(self) -> None:
        for name in terms.pollable_sources():
            entry = terms.terms_for(name)
            assert entry.basis.strip()
            assert entry.checked_on is not None

    def test_pasted_is_not_pollable(self) -> None:
        """There is nothing to poll: the user supplied the text, and nothing in this system
        fetches a pasted URL (hard rule 4, ADR-036)."""
        assert not terms.may_poll("pasted")

    def test_the_watchlist_asks_this_module_rather_than_deriving_it(self) -> None:
        """`POLLABLE` used to be "every source except pasted", which is a guess about what we
        may poll dressed as a fact -- it would have admitted any source added later, on nobody's
        authority."""
        from app.services.signals import watchlist

        assert watchlist.POLLABLE == terms.pollable_sources()
