"""The hardened fetcher, against mocked transports.

The important test here is the redirect one: a public URL that redirects to localhost is the
standard SSRF bypass, and it defeats any check performed only on the original URL.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.research.fetcher import USER_AGENT, Fetcher

pytestmark = pytest.mark.asyncio

SAFE_IP = "93.184.216.34"
PAGE = "<html><head><title>Acme</title></head><body><p>We build things.</p></body></html>"


@pytest.fixture
def no_dns(monkeypatch):
    """Every hostname resolves to one public address, unless a test says otherwise."""
    monkeypatch.setattr("app.services.research.net.resolve", lambda host: [SAFE_IP])


@pytest.fixture
async def fetcher(no_dns):
    async with httpx.AsyncClient() as client:
        # No politeness delay in tests; the delay logic is covered separately.
        yield Fetcher(client=client, host_delay=0.0, respect_robots=False)


class TestSuccessfulFetch:
    @respx.mock
    async def test_a_page_is_returned(self, fetcher):
        respx.get("https://acme.example/about").mock(return_value=httpx.Response(200, html=PAGE))
        result = await fetcher.fetch("https://acme.example/about")
        assert result.ok
        assert "We build things" in result.html
        assert result.status == 200

    @respx.mock
    async def test_the_request_identifies_us_honestly(self, fetcher):
        """An operator who does not want us there should be able to act on it without having to
        work out who we are."""
        route = respx.get("https://acme.example/").mock(return_value=httpx.Response(200, html=PAGE))
        await fetcher.fetch("https://acme.example/")

        agent = route.calls[0].request.headers["user-agent"]
        assert agent == USER_AGENT
        assert "+https://" in agent, "the UA must carry a contact URL"
        assert "robots.txt" in agent


class TestRedirectsAreRevalidated:
    @respx.mock
    async def test_a_redirect_to_localhost_is_refused(self, fetcher):
        """The standard SSRF bypass: a perfectly innocent public URL that 302s inside."""
        respx.get("https://acme.example/").mock(
            return_value=httpx.Response(302, headers={"Location": "http://127.0.0.1:6379/"})
        )
        result = await fetcher.fetch("https://acme.example/")
        assert not result.ok
        assert "refused after redirect" in (result.error or "")

    @respx.mock
    async def test_a_redirect_to_the_metadata_endpoint_is_refused(self, fetcher):
        respx.get("https://acme.example/").mock(
            return_value=httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            )
        )
        result = await fetcher.fetch("https://acme.example/")
        assert not result.ok
        assert "refused" in (result.error or "")

    @respx.mock
    async def test_a_legitimate_redirect_is_followed(self, fetcher):
        respx.get("https://acme.example/old").mock(
            return_value=httpx.Response(301, headers={"Location": "/new"})
        )
        respx.get("https://acme.example/new").mock(return_value=httpx.Response(200, html=PAGE))
        result = await fetcher.fetch("https://acme.example/old")
        assert result.ok

    @respx.mock
    async def test_a_redirect_loop_terminates(self, fetcher):
        respx.get("https://acme.example/a").mock(
            return_value=httpx.Response(302, headers={"Location": "/b"})
        )
        respx.get("https://acme.example/b").mock(
            return_value=httpx.Response(302, headers={"Location": "/a"})
        )
        result = await fetcher.fetch("https://acme.example/a")
        assert "more than" in (result.error or "")

    @respx.mock
    async def test_a_redirect_with_no_location_is_an_error(self, fetcher):
        respx.get("https://acme.example/").mock(return_value=httpx.Response(302))
        result = await fetcher.fetch("https://acme.example/")
        assert "Location" in (result.error or "")


class TestLimits:
    @respx.mock
    async def test_an_oversized_body_is_abandoned_mid_stream(self, no_dns):
        """Content-Length is advisory. A server can send far more than it claims, so the cap is
        enforced while reading rather than from the header."""
        async with httpx.AsyncClient() as client:
            small = Fetcher(client=client, host_delay=0.0, respect_robots=False, max_bytes=500)
            respx.get("https://acme.example/").mock(
                return_value=httpx.Response(
                    200,
                    content=b"x" * 50_000,
                    headers={
                        "Content-Type": "text/html",
                        "Content-Length": "10",
                    },
                )
            )
            result = await small.fetch("https://acme.example/")

        assert not result.ok
        assert "larger than" in (result.error or "")

    @respx.mock
    @pytest.mark.parametrize(
        "content_type",
        ["application/pdf", "video/mp4", "application/zip", "image/png"],
    )
    async def test_non_html_is_refused(self, fetcher, content_type):
        """A video is not evidence, and decoding it wastes the token budget."""
        respx.get("https://acme.example/file").mock(
            return_value=httpx.Response(
                200, content=b"binary", headers={"Content-Type": content_type}
            )
        )
        result = await fetcher.fetch("https://acme.example/file")
        assert not result.ok
        assert "not HTML" in (result.error or "")

    @respx.mock
    async def test_xhtml_is_accepted(self, fetcher):
        respx.get("https://acme.example/").mock(
            return_value=httpx.Response(
                200, content=PAGE.encode(), headers={"Content-Type": "application/xhtml+xml"}
            )
        )
        assert (await fetcher.fetch("https://acme.example/")).ok


class TestFailuresAreRecordedNotRaised:
    """A company site being down is normal and must not fail the whole research job."""

    @respx.mock
    async def test_a_404_is_recorded(self, fetcher):
        respx.get("https://acme.example/missing").mock(return_value=httpx.Response(404))
        result = await fetcher.fetch("https://acme.example/missing")
        assert not result.ok
        assert result.status == 404

    @respx.mock
    async def test_a_connection_error_is_recorded(self, fetcher):
        respx.get("https://acme.example/").mock(side_effect=httpx.ConnectError("refused"))
        result = await fetcher.fetch("https://acme.example/")
        assert not result.ok
        assert "ConnectError" in (result.error or "")

    @respx.mock
    async def test_a_timeout_is_recorded(self, fetcher):
        respx.get("https://acme.example/").mock(side_effect=httpx.ReadTimeout("slow"))
        result = await fetcher.fetch("https://acme.example/")
        assert not result.ok
        assert "Timeout" in (result.error or "")

    async def test_an_unsafe_url_never_reaches_the_network(self, fetcher):
        with respx.mock:
            result = await fetcher.fetch("http://localhost/admin")
            assert not result.ok
            assert respx.calls.call_count == 0, "nothing may be requested at all"


class TestRobots:
    @respx.mock
    async def test_a_disallowed_path_is_not_fetched(self, no_dns):
        async with httpx.AsyncClient() as client:
            polite = Fetcher(client=client, host_delay=0.0, respect_robots=True)
            respx.get("https://acme.example/robots.txt").mock(
                return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
            )
            page = respx.get("https://acme.example/private/plans").mock(
                return_value=httpx.Response(200, html=PAGE)
            )
            result = await polite.fetch("https://acme.example/private/plans")

        assert not result.ok
        assert "robots" in (result.error or "")
        assert not page.called, "the page must not be requested"

    @respx.mock
    async def test_an_allowed_path_is_fetched(self, no_dns):
        async with httpx.AsyncClient() as client:
            polite = Fetcher(client=client, host_delay=0.0, respect_robots=True)
            respx.get("https://acme.example/robots.txt").mock(
                return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
            )
            respx.get("https://acme.example/about").mock(
                return_value=httpx.Response(200, html=PAGE)
            )
            result = await polite.fetch("https://acme.example/about")

        assert result.ok

    @respx.mock
    async def test_a_missing_robots_file_means_no_stated_preference(self, no_dns):
        """Treating a 404 as prohibition would make us unable to read most of the web."""
        async with httpx.AsyncClient() as client:
            polite = Fetcher(client=client, host_delay=0.0, respect_robots=True)
            respx.get("https://acme.example/robots.txt").mock(return_value=httpx.Response(404))
            respx.get("https://acme.example/about").mock(
                return_value=httpx.Response(200, html=PAGE)
            )
            result = await polite.fetch("https://acme.example/about")

        assert result.ok

    @respx.mock
    async def test_robots_is_fetched_once_per_host(self, no_dns):
        """A sheet usually has several people at one company, so several pages per host."""
        async with httpx.AsyncClient() as client:
            polite = Fetcher(client=client, host_delay=0.0, respect_robots=True)
            robots = respx.get("https://acme.example/robots.txt").mock(
                return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
            )
            respx.get("https://acme.example/a").mock(return_value=httpx.Response(200, html=PAGE))
            respx.get("https://acme.example/b").mock(return_value=httpx.Response(200, html=PAGE))

            await polite.fetch("https://acme.example/a")
            await polite.fetch("https://acme.example/b")

        assert robots.call_count == 1

    @respx.mock
    async def test_a_total_disallow_is_honoured(self, no_dns):
        async with httpx.AsyncClient() as client:
            polite = Fetcher(client=client, host_delay=0.0, respect_robots=True)
            respx.get("https://acme.example/robots.txt").mock(
                return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
            )
            result = await polite.fetch("https://acme.example/")

        assert not result.ok
        assert "robots" in (result.error or "")
