"""Unit tests for the Hunter's scorer and ranking."""

import pytest

from hunter.scorer import rank, score_all, score_listing


def _listing(**kw):
    base = {
        "source": "mock",
        "external_id": "t1",
        "url": "https://example.com/1",
        "brand": "BMW",
        "model": "M340i",
        "trim": "M340i xDrive",
        "model_year": 2021,
        "mileage_km": 50000,
        "asking_price_eur": 45000,
        "options": [],
        "location": "Munich, DE",
        "posted_date": "2026-01-01",
    }
    base.update(kw)
    return base


class TestScoreListing:
    def test_basic_score_adds_verdict(self):
        r = score_listing(_listing(asking_price_eur=40000))
        assert r["verdict"] in {"STEAL", "GOOD DEAL", "FAIR", "OVERPRICED", "RIP-OFF"}
        assert r["fair_value_eur"] > 0

    def test_priced_below_fair_gets_positive_savings_score(self):
        low = score_listing(_listing(asking_price_eur=30000))
        high = score_listing(_listing(asking_price_eur=60000))
        assert low["score"] > high["score"]

    def test_unknown_model_returns_error(self):
        r = score_listing(_listing(brand="", model="FakeRocket", trim="FakeRocket"))
        assert r["verdict"] == "UNKNOWN"
        assert "scoring_error" in r

    def test_fallback_to_bare_model(self):
        r = score_listing(_listing(model="M340i", trim="Some Trim That Does Not Exist"))
        assert r["verdict"] != "UNKNOWN"

    def test_mercedes_scored_correctly(self):
        r = score_listing(_listing(brand="Mercedes-Benz", model="C 300", trim="C 300",
                                   model_year=2022, mileage_km=35000, asking_price_eur=45000))
        assert r["verdict"] != "UNKNOWN"
        assert r["brand_matched"] == "Mercedes-Benz"
        assert r["fair_value_eur"] > 0

    def test_porsche_perf_bonus_applied(self):
        r = score_listing(_listing(brand="Porsche", model="911 Carrera S",
                                   trim="911 Carrera S", model_year=2021,
                                   mileage_km=40000, asking_price_eur=100000))
        # 911 Carrera S should get perf bonus → retention adjusted up
        assert r["brand_matched"] == "Porsche"

    def test_audi_ev_penalty(self):
        r = score_listing(_listing(brand="Audi", model="e-tron GT",
                                   trim="e-tron GT", model_year=2022,
                                   mileage_km=35000, asking_price_eur=75000))
        assert r["brand_matched"] == "Audi"
        assert r["fair_value_eur"] > 0


class TestRank:
    def test_rank_orders_best_first(self):
        scored = [
            score_listing(_listing(external_id="a", asking_price_eur=60000)),
            score_listing(_listing(external_id="b", asking_price_eur=30000)),
            score_listing(_listing(external_id="c", asking_price_eur=45000)),
        ]
        top = rank(scored, top_n=3)
        assert top[0]["external_id"] == "b"
        assert top[-1]["external_id"] == "a"

    def test_rank_respects_limit(self):
        scored = [score_listing(_listing(external_id=str(i))) for i in range(5)]
        assert len(rank(scored, top_n=2)) == 2

    def test_rank_drops_unscoreable(self):
        scored = [
            score_listing(_listing(model="Nope", trim="Nope")),
            score_listing(_listing(external_id="good")),
        ]
        top = rank(scored, top_n=5)
        assert all(l["verdict"] != "UNKNOWN" for l in top)

    def test_score_all_handles_empty(self):
        assert score_all([]) == []
