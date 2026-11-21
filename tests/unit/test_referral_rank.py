"""S19.6 — referral ranking unit tests."""

from __future__ import annotations

from app.domain.referral_rank import rank_for_company


def _conn(full_name: str, company_norm: str, strength: int | None = None) -> dict[str, object]:
    return {"id": "uuid", "full_name": full_name, "company_norm": company_norm, "strength": strength}


class TestRankForCompany:
    def test_filters_by_company(self) -> None:
        conns = [_conn("Alice", "acme"), _conn("Bob", "other")]
        ranked = rank_for_company(conns, company_norm="acme")
        assert len(ranked) == 1
        assert ranked[0].full_name == "Alice"

    def test_moderate_tie_ranks_before_strong(self) -> None:
        conns = [_conn("Strong", "co", strength=5), _conn("Moderate", "co", strength=3)]
        ranked = rank_for_company(conns, company_norm="co")
        assert ranked[0].full_name == "Moderate"

    def test_weak_tie_ranks_before_strong(self) -> None:
        conns = [_conn("Strong", "co", strength=5), _conn("Weak", "co", strength=2)]
        ranked = rank_for_company(conns, company_norm="co")
        assert ranked[0].full_name == "Weak"

    def test_unrated_ranks_last(self) -> None:
        conns = [_conn("Unrated", "co", strength=None), _conn("Rated", "co", strength=5)]
        ranked = rank_for_company(conns, company_norm="co")
        assert ranked[-1].full_name == "Unrated"

    def test_reason_shown(self) -> None:
        conns = [_conn("Alice", "co", strength=3)]
        ranked = rank_for_company(conns, company_norm="co")
        assert "moderate" in ranked[0].reason.lower()

    def test_empty_when_no_match(self) -> None:
        conns = [_conn("Bob", "other")]
        ranked = rank_for_company(conns, company_norm="acme")
        assert ranked == []

    def test_no_io(self) -> None:
        import importlib.util
        spec = importlib.util.find_spec("app.domain.referral_rank")
        assert spec is not None
        source = open(spec.origin).read()
        for bad in ("httpx", "asyncpg", "socket", "subprocess"):
            assert bad not in source
