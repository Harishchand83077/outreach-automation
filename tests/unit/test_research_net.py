"""The SSRF guard.

These URLs arrive from an uploaded spreadsheet, so they are untrusted input. The worker shares a
VM with Postgres, which makes `localhost` a real target rather than a theoretical one.

Every test here corresponds to an attack rather than a style preference.
"""

from __future__ import annotations

import pytest

from app.services.research.net import (
    CLOUD_METADATA_IPS,
    UnsafeTarget,
    host_is_blocked,
    ip_is_safe,
    same_registrable_domain,
    validate_url,
)


def fixed_resolver(*addresses: str):
    """Stand in for DNS, including hostile answers."""
    return lambda host: list(addresses)


class TestAddressClassification:
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "127.1.1.1",
            "::1",  # loopback
            "10.0.0.5",
            "172.16.0.1",
            "192.168.1.1",  # RFC 1918
            "169.254.169.254",  # cloud metadata
            "169.254.1.1",  # link-local
            "0.0.0.0",  # unspecified
            "224.0.0.1",  # multicast
            "100.64.0.1",  # carrier-grade NAT
            "fc00::1",
            "fd00::1",  # IPv6 unique-local
            "fe80::1",  # IPv6 link-local
        ],
    )
    def test_internal_addresses_are_refused(self, address):
        safe, reason = ip_is_safe(address)
        assert safe is False
        assert reason, "a refusal must say which rule applied"

    @pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1::1"])
    def test_public_addresses_are_allowed(self, address):
        assert ip_is_safe(address) == (True, "")

    def test_the_metadata_endpoint_is_named_explicitly(self):
        """It is already inside link-local, but it is the single most valuable SSRF target on a
        cloud VM — it serves instance credentials to anything that can make a request."""
        for address in CLOUD_METADATA_IPS:
            safe, reason = ip_is_safe(address)
            assert safe is False
            assert "metadata" in reason.lower() or "link-local" in reason.lower()

    def test_an_ipv4_mapped_ipv6_address_cannot_smuggle_a_private_ip(self):
        """`::ffff:127.0.0.1` is loopback wearing an IPv6 costume."""
        safe, reason = ip_is_safe("::ffff:127.0.0.1")
        assert safe is False
        assert "loopback" in reason

    def test_garbage_is_not_an_address(self):
        assert ip_is_safe("not-an-ip")[0] is False


class TestHostBlocking:
    @pytest.mark.parametrize(
        "host",
        ["linkedin.com", "www.linkedin.com", "lnkd.in", "facebook.com", "x.com", "localhost"],
    )
    def test_blocked_hosts_are_refused_before_any_lookup(self, host):
        blocked, reason = host_is_blocked(host)
        assert blocked is True
        assert reason

    def test_linkedin_is_blocked_because_it_is_never_fetched(self):
        """Hard rule 4: the URL is stored and shown for reference, never retrieved."""
        assert host_is_blocked("linkedin.com")[0] is True

    @pytest.mark.parametrize(
        "host", ["printer.local", "db.internal", "wiki.corp", "host.home.arpa", "app.lan"]
    )
    def test_internal_only_suffixes_are_refused(self, host):
        assert host_is_blocked(host)[0] is True

    def test_case_and_trailing_dot_do_not_evade_the_list(self):
        assert host_is_blocked("LinkedIn.COM")[0] is True
        assert host_is_blocked("linkedin.com.")[0] is True

    def test_an_ordinary_company_host_is_allowed(self):
        assert host_is_blocked("acme-analytics.example")[0] is False

    def test_an_empty_host_is_refused(self):
        assert host_is_blocked("")[0] is True


class TestUrlValidation:
    def test_a_normal_https_url_passes(self):
        target = validate_url(
            "https://acme.example/about", resolver=fixed_resolver("93.184.216.34")
        )
        assert target.host == "acme.example"
        assert target.port == 443
        assert target.addresses == ("93.184.216.34",)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://example.com/",
            "ftp://example.com/",
            "data:text/html,<script>",
            "redis://localhost:6379",
            "//example.com/no-scheme",
        ],
    )
    def test_only_http_and_https_are_fetched(self, url):
        """A naive fetcher will happily read a local file or speak to Redis."""
        with pytest.raises(UnsafeTarget):
            validate_url(url, resolver=fixed_resolver("93.184.216.34"))

    @pytest.mark.parametrize("port", [22, 3306, 5432, 6379, 8080, 11211])
    def test_only_ports_80_and_443_are_fetched(self, port):
        """5432 is our own Postgres, and the worker runs on the same machine."""
        with pytest.raises(UnsafeTarget, match="port"):
            validate_url(f"https://acme.example:{port}/", resolver=fixed_resolver("93.184.216.34"))

    def test_a_bare_private_ip_in_the_url_is_refused_without_dns(self):
        with pytest.raises(UnsafeTarget, match=r"private|loopback"):
            validate_url("http://127.0.0.1/admin")

    def test_the_metadata_url_is_refused(self):
        with pytest.raises(UnsafeTarget):
            validate_url("http://169.254.169.254/latest/meta-data/")

    def test_a_host_resolving_to_a_private_address_is_refused(self):
        """The attack: a public hostname whose A record points inside our network."""
        with pytest.raises(UnsafeTarget, match="private"):
            validate_url("https://evil.example/", resolver=fixed_resolver("10.1.2.3"))

    def test_one_unsafe_address_is_enough_to_refuse_the_host(self):
        """A hostile record can return a public address *and* a private one. Picking the safe
        one would be a gamble we would sometimes lose."""
        with pytest.raises(UnsafeTarget, match=r"private|loopback"):
            validate_url(
                "https://evil.example/",
                resolver=fixed_resolver("93.184.216.34", "127.0.0.1"),
            )

    def test_a_host_that_does_not_resolve_is_refused(self):
        def no_records(host: str) -> list[str]:
            raise UnsafeTarget(f"{host} does not resolve")

        with pytest.raises(UnsafeTarget, match="does not resolve"):
            validate_url("https://nonexistent.example/", resolver=no_records)

    def test_a_host_resolving_to_nothing_is_refused(self):
        with pytest.raises(UnsafeTarget):
            validate_url("https://empty.example/", resolver=fixed_resolver())

    def test_http_defaults_to_port_80(self):
        target = validate_url("http://acme.example/", resolver=fixed_resolver("93.184.216.34"))
        assert target.port == 80


class TestSameSite:
    """Research stays on the company's own pages: a CDN or a partner is not evidence about
    this company, and following links off-site also widens the fetch surface."""

    @pytest.mark.parametrize(
        "a,b",
        [
            ("acme.example", "www.acme.example"),
            ("acme.example", "blog.acme.example"),
            ("www.acme.example", "careers.acme.example"),
        ],
    )
    def test_subdomains_count_as_the_same_site(self, a, b):
        assert same_registrable_domain(a, b) is True

    @pytest.mark.parametrize(
        "a,b",
        [
            ("acme.example", "globex.example"),
            ("acme.example", "cdn.cloudfront.net"),
            ("acme.example", "acme.evil"),
        ],
    )
    def test_different_sites_are_distinguished(self, a, b):
        assert same_registrable_domain(a, b) is False

    def test_a_public_suffix_is_not_mistaken_for_a_site(self):
        """A naive last-two-labels rule calls a.co.uk and b.co.uk the same site, which would
        let research wander from one company onto another's pages. The public suffix list is
        what gets this right."""
        assert same_registrable_domain("a.co.uk", "b.co.uk") is False
        assert same_registrable_domain("shop.a.co.uk", "www.a.co.uk") is True
