"""Unit tests for MSRP lookup and fuzzy matching."""

import pytest

from tools.msrp_lookup import all_models, all_options, lookup_msrp


class TestExactMatch:
    def test_exact_trim_and_year(self):
        r = lookup_msrp("M340i", 2021)
        assert r["found"]
        assert r["matched_model"] == "M340i"
        assert r["base_msrp"] == 64900
        assert r["match_reason"] == "exact"

    def test_case_insensitive(self):
        r = lookup_msrp("m340i", 2021)
        assert r["found"]
        assert r["matched_model"] == "M340i"
        assert r["match_reason"] == "exact"

    def test_multi_word_trim(self):
        r = lookup_msrp("M3 Competition", 2022)
        assert r["matched_model"] == "M3 Competition"
        assert r["base_msrp"] == 96200


class TestFuzzyMatch:
    def test_token_subset_trim(self):
        r = lookup_msrp("M340i xDrive Touring", 2021)
        assert r["found"]
        assert r["matched_model"] == "M340i"
        assert r["match_reason"].startswith("token")

    def test_bmw_prefix_stripped(self):
        r = lookup_msrp("BMW 330i", 2020)
        assert r["found"]
        assert r["matched_model"] == "330i"

    def test_spaced_xdrive(self):
        r = lookup_msrp("X5 xDrive 40i", 2022)
        assert r["found"]
        assert r["matched_model"] == "X5 xDrive40i"

    def test_compact_xdrive(self):
        r = lookup_msrp("X5 xDrive40i", 2022)
        assert r["matched_model"] == "X5 xDrive40i"
        assert r["match_reason"] == "exact"

    def test_abbreviated_model(self):
        r = lookup_msrp("ix m60", 2023)
        assert r["found"]
        assert r["matched_model"] == "iX M60"


class TestMultiBrand:
    def test_bmw_model_resolves(self):
        r = lookup_msrp("M340i", 2021)
        assert r["brand"] == "BMW"
        assert r["matched_model"] == "M340i"

    def test_mercedes_with_brand_hint(self):
        r = lookup_msrp("C 300", 2022, brand="Mercedes-Benz")
        assert r["brand"] == "Mercedes-Benz"
        assert r["matched_model"] == "C 300"
        assert r["base_msrp"] == 55800

    def test_mercedes_brand_in_query(self):
        r = lookup_msrp("mercedes c300", 2022)
        assert r["brand"] == "Mercedes-Benz"
        assert r["matched_model"] == "C 300"

    def test_mercedes_alias_merc(self):
        r = lookup_msrp("merc E 220d", 2021)
        assert r["brand"] == "Mercedes-Benz"

    def test_amg_resolves(self):
        r = lookup_msrp("AMG C 63 S", 2023)
        assert r["brand"] == "Mercedes-Benz"
        assert "C 63 AMG S" in r["matched_model"]

    def test_audi_rs6(self):
        r = lookup_msrp("Audi RS6", 2022)
        assert r["brand"] == "Audi"
        assert "RS 6" in r["matched_model"]

    def test_porsche_911(self):
        r = lookup_msrp("911 Carrera S", 2022)
        assert r["brand"] == "Porsche"
        assert r["matched_model"] == "911 Carrera S"

    def test_porsche_taycan(self):
        r = lookup_msrp("porsche taycan 4s", 2022)
        assert r["brand"] == "Porsche"
        assert "Taycan" in r["matched_model"]

    def test_vw_golf_r(self):
        r = lookup_msrp("VW Golf R", 2022)
        assert r["brand"] == "Volkswagen"
        assert r["matched_model"] == "Golf R"

    def test_vw_id4_gtx(self):
        r = lookup_msrp("ID.4 GTX", 2023)
        assert r["brand"] == "Volkswagen"

    def test_mini_cooper(self):
        r = lookup_msrp("Cooper S", 2021, brand="Mini")
        assert r["brand"] == "Mini"
        assert "Cooper S" in r["matched_model"]

    def test_brand_hint_narrows_correctly(self):
        r = lookup_msrp("C 300", 2022, brand="Mercedes-Benz")
        assert r["brand"] == "Mercedes-Benz"

    def test_options_are_brand_scoped(self):
        # M Sport is BMW; lookup for Audi should NOT match BMW options
        r = lookup_msrp("A4 Avant 40 TDI", 2021, ["M Sport Package", "S line Exterior"])
        assert r["brand"] == "Audi"
        assert "S line Exterior" in r["matched_options"]
        assert "M Sport Package" not in r["matched_options"]


class TestMismatch:
    def test_nonsense_returns_not_found(self):
        r = lookup_msrp("definitely not a bmw xyz", 2022)
        assert not r["found"]
        assert "not found" in r["message"].lower()

    def test_year_missing_returns_not_found(self):
        r = lookup_msrp("M340i", 2015)
        assert not r["found"]

    def test_empty_model_not_found(self):
        r = lookup_msrp("", 2021)
        assert not r["found"]


class TestOptions:
    def test_options_add_to_total(self):
        r = lookup_msrp("330i", 2021, ["M Sport Package", "Harman Kardon Sound"])
        assert r["total_msrp"] == r["base_msrp"] + 3500 + 990
        assert sorted(r["matched_options"]) == ["Harman Kardon Sound", "M Sport Package"]

    def test_fuzzy_option_name_matches(self):
        r = lookup_msrp("330i", 2021, ["Harman Kardon"])
        assert "Harman Kardon Sound" in r["matched_options"]

    def test_unknown_option_is_unmatched(self):
        r = lookup_msrp("330i", 2021, ["Rocket Boosters"])
        assert "Rocket Boosters" in r["unmatched_options"]
        assert r["options_total"] == 0


class TestHelpers:
    def test_all_models_nonempty(self):
        models = all_models()
        assert len(models) >= 100
        # Brand-prefixed in the global list
        assert "BMW M340i" in models
        assert any("C 300" in m for m in models)
        assert any("RS 6 Avant" in m for m in models)

    def test_all_models_scoped_by_brand(self):
        bmw_models = all_models(brand="BMW")
        assert "M340i" in bmw_models
        assert "X5 xDrive40i" in bmw_models
        assert all(not m.startswith("BMW ") for m in bmw_models)

        audi_models = all_models(brand="Audi")
        assert "RS 6 Avant" in audi_models
        assert "e-tron GT" in audi_models

    def test_all_options_nonempty(self):
        options = all_options()
        # Multi-brand options now union together
        assert "M Sport Package" in options
        assert "AMG Line" in options
        assert "S line Exterior" in options
        assert "Sport Chrono Package" in options
        assert "R-Line Exterior" in options

    def test_all_options_scoped_by_brand(self):
        bmw_opts = all_options(brand="BMW")
        assert "M Sport Package" in bmw_opts
        assert "AMG Line" not in bmw_opts
        audi_opts = all_options(brand="Audi")
        assert "S line Exterior" in audi_opts
        assert "M Sport Package" not in audi_opts
