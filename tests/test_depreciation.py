"""Unit tests for the depreciation model."""

import pytest

from tools.depreciation import estimate_fair_value, verdict


class TestAgeCurve:
    def test_new_car_retains_msrp(self):
        r = estimate_fair_value(50000, 2026, 0, current_year=2026)
        assert r["age_years"] == 0
        assert r["fair_value_eur"] >= 49000

    def test_five_year_old_retains_roughly_half(self):
        r = estimate_fair_value(50000, 2021, 75000, current_year=2026)
        assert r["age_years"] == 5
        assert 22000 <= r["fair_value_eur"] <= 35000


class TestMileageAdjustment:
    def test_low_miles_increases_value(self):
        low = estimate_fair_value(50000, 2022, 20000, current_year=2026)
        expected = estimate_fair_value(50000, 2022, 60000, current_year=2026)
        assert low["fair_value_eur"] > expected["fair_value_eur"]

    def test_high_miles_decreases_value(self):
        high = estimate_fair_value(50000, 2022, 150000, current_year=2026)
        expected = estimate_fair_value(50000, 2022, 60000, current_year=2026)
        assert high["fair_value_eur"] < expected["fair_value_eur"]


class TestModelFamily:
    def test_m_car_gets_bonus(self):
        m_car = estimate_fair_value(80000, 2021, 50000, "M340i", current_year=2026)
        regular = estimate_fair_value(80000, 2021, 50000, "320i", current_year=2026)
        assert m_car["fair_value_eur"] > regular["fair_value_eur"]
        assert m_car["model_adjustment"] > 0

    def test_ev_gets_penalty(self):
        ev = estimate_fair_value(70000, 2022, 40000, "i4 eDrive40", current_year=2026)
        ice = estimate_fair_value(70000, 2022, 40000, "330i", current_year=2026)
        assert ev["fair_value_eur"] < ice["fair_value_eur"]
        assert ev["model_adjustment"] < 0

    def test_m_ev_nets_closer_to_zero(self):
        """Performance EVs like i4 M50 get both the M-bonus and the EV-penalty; net adjustment
        should be smaller in magnitude than a pure M-car's bonus."""
        m_ev = estimate_fair_value(70000, 2022, 40000, "i4 M50", current_year=2026)
        m_ice = estimate_fair_value(70000, 2022, 40000, "M340i", current_year=2026)
        assert 0 <= m_ev["model_adjustment"] < m_ice["model_adjustment"]


class TestVerdict:
    def test_steal_verdict(self):
        v = verdict(30000, 40000)
        assert v["verdict"] == "STEAL"
        assert v["delta_pct"] < -10

    def test_good_deal_verdict(self):
        v = verdict(38000, 40000)
        assert v["verdict"] == "GOOD DEAL"

    def test_fair_verdict(self):
        v = verdict(40000, 40000)
        assert v["verdict"] == "FAIR"

    def test_overpriced_verdict(self):
        v = verdict(44000, 40000)
        assert v["verdict"] == "OVERPRICED"

    def test_ripoff_verdict(self):
        v = verdict(50000, 40000)
        assert v["verdict"] == "RIP-OFF"
        assert v["delta_pct"] > 15


class TestSanityBounds:
    def test_retention_floor(self):
        r = estimate_fair_value(50000, 2010, 300000, current_year=2026)
        assert r["final_retention"] >= 0.10

    def test_zero_mileage_ok(self):
        r = estimate_fair_value(50000, 2024, 0, current_year=2026)
        assert r["fair_value_eur"] > 0
