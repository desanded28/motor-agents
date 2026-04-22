"""Tests for source adapters and the SQLite layer."""

import tempfile
from pathlib import Path

import pytest

from hunter import database
from hunter.sources import Criteria, MockSource, criteria_from_dict


class TestMockSource:
    def test_loads_full_dataset(self):
        results = MockSource().search(Criteria())
        assert len(results) >= 15
        assert all("asking_price_eur" in l for l in results)

    def test_filter_by_model_contains(self):
        results = MockSource().search(Criteria(model_contains="M340i"))
        assert len(results) >= 1
        for l in results:
            combined = (l.get("model", "") + " " + l.get("trim", "")).lower()
            assert "m340i" in combined

    def test_filter_by_max_price(self):
        results = MockSource().search(Criteria(max_price_eur=40000))
        assert all(l["asking_price_eur"] <= 40000 for l in results)

    def test_filter_by_min_year(self):
        results = MockSource().search(Criteria(min_year=2022))
        assert all(l["model_year"] >= 2022 for l in results)

    def test_filter_by_max_mileage(self):
        results = MockSource().search(Criteria(max_mileage_km=50000))
        assert all(l["mileage_km"] <= 50000 for l in results)

    def test_combined_filters(self):
        c = Criteria(model_contains="X5", min_year=2020, max_price_eur=80000)
        results = MockSource().search(c)
        for l in results:
            assert "x5" in (l["model"] + l["trim"]).lower()
            assert l["model_year"] >= 2020
            assert l["asking_price_eur"] <= 80000

    def test_limit_per_source(self):
        results = MockSource().search(Criteria(limit_per_source=3))
        assert len(results) <= 3

    def test_mock_has_multiple_brands(self):
        results = MockSource().search(Criteria())
        brands = {l.get("brand") for l in results}
        # The mock set should cover 6 brands (BMW, Mercedes-Benz, Audi, Porsche, VW, Mini)
        assert len(brands) >= 5
        assert "BMW" in brands and "Mercedes-Benz" in brands and "Audi" in brands

    def test_filter_by_brand(self):
        results = MockSource().search(Criteria(brand="Porsche"))
        assert len(results) > 0
        assert all(l.get("brand") == "Porsche" for l in results)

    def test_filter_brand_case_insensitive(self):
        results = MockSource().search(Criteria(brand="audi"))
        assert len(results) > 0
        assert all(l.get("brand", "").lower() == "audi" for l in results)


class TestCriteriaFromDict:
    def test_unknown_keys_dropped(self):
        c = criteria_from_dict({"model_contains": "M3", "unknown_key": "ignored"})
        assert c.model_contains == "M3"

    def test_empty_dict_uses_defaults(self):
        c = criteria_from_dict({})
        assert c.country == "de"
        assert c.limit_per_source == 30


class TestDatabase:
    @pytest.fixture
    def tmp_db(self, monkeypatch, tmp_path):
        db_path = tmp_path / "test_hunter.db"
        monkeypatch.setattr(database, "DB_PATH", db_path)
        yield db_path

    def _row(self, external_id="e1", asking=40000, verdict="FAIR", brand="BMW"):
        return {
            "source": "mock", "external_id": external_id, "url": "https://x.com/1",
            "brand": brand, "model": "M340i", "trim": "M340i", "model_year": 2021,
            "mileage_km": 50000, "asking_price_eur": asking,
            "options": [], "location": "Berlin", "posted_date": "2026-01-01",
            "msrp_total": 70000, "fair_value_eur": 45000,
            "delta_eur": asking - 45000, "delta_pct": 0.0, "verdict": verdict,
        }

    def test_upsert_inserts_first_time(self, tmp_db):
        r = database.upsert_scored([self._row("a"), self._row("b")])
        assert r["inserted"] == 2
        assert r["updated"] == 0

    def test_upsert_updates_existing(self, tmp_db):
        database.upsert_scored([self._row("a")])
        r = database.upsert_scored([self._row("a", asking=50000)])
        assert r["updated"] == 1
        assert r["inserted"] == 0
        assert database.count_listings() == 1

    def test_get_best_deals_orders_by_savings(self, tmp_db):
        database.upsert_scored([
            self._row("a", asking=50000),  # delta +5000
            self._row("b", asking=30000),  # delta -15000 → best deal
            self._row("c", asking=45000),  # delta 0
        ])
        best = database.get_best_deals(3)
        assert best[0]["external_id"] == "b"
