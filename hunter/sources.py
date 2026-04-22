"""Listing source adapters. Each source takes a Criteria and returns a list of Listing dicts.

Sources provided:
- MockSource: reads data/mock_listings.json. Always works, used for demos and tests.
- AutoScoutSource: best-effort real scrape of AutoScout24.de search results via Playwright.
  Bot detection may block this on some networks; the hunter treats it as optional.

All sources return the same Listing shape:
    {
      "source": str, "external_id": str, "url": str,
      "model": str, "trim": str, "model_year": int,
      "mileage_km": int, "asking_price_eur": int,
      "options": list[str], "location": str, "posted_date": str
    }
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Criteria:
    model_contains: str | None = None
    brand: str | None = None
    min_year: int | None = None
    max_price_eur: int | None = None
    max_mileage_km: int | None = None
    country: str = "de"
    limit_per_source: int = 30


@dataclass
class Listing:
    source: str
    external_id: str
    url: str
    brand: str
    model: str
    trim: str
    model_year: int
    mileage_km: int
    asking_price_eur: int
    options: list[str] = field(default_factory=list)
    location: str = ""
    posted_date: str = ""


def _passes(criteria: Criteria, listing: dict) -> bool:
    if criteria.brand:
        if criteria.brand.strip().lower() not in (listing.get("brand", "") or "").lower():
            return False
    if criteria.model_contains and criteria.model_contains.lower() not in (listing.get("model", "") + " " + listing.get("trim", "")).lower():
        return False
    if criteria.min_year and listing.get("model_year", 0) < criteria.min_year:
        return False
    if criteria.max_price_eur and listing.get("asking_price_eur", 0) > criteria.max_price_eur:
        return False
    if criteria.max_mileage_km and listing.get("mileage_km", 0) > criteria.max_mileage_km:
        return False
    return True


class MockSource:
    name = "mock"

    def __init__(self, path: Path | None = None):
        self.path = path or Path(__file__).parent.parent / "data" / "mock_listings.json"

    def search(self, criteria: Criteria) -> list[dict]:
        data = json.loads(self.path.read_text())
        out = [l for l in data["listings"] if _passes(criteria, l)]
        return out[: criteria.limit_per_source]


class AutoScoutSource:
    name = "autoscout24"

    # Maps brand alias → AutoScout24 make slug
    _BRAND_SLUGS = {
        "bmw": "bmw",
        "mercedes": "mercedes-benz", "mercedes-benz": "mercedes-benz",
        "audi": "audi",
        "porsche": "porsche",
        "vw": "volkswagen", "volkswagen": "volkswagen",
        "mini": "mini",
    }

    # Keyword → (brand slug, model slug) hints for narrowing search
    _MODEL_HINT_MAP = {
        "m340": ("bmw", "3-series"), "m3": ("bmw", "m3"), "m4": ("bmw", "m4"), "m5": ("bmw", "m5"),
        "x3": ("bmw", "x3"), "x5": ("bmw", "x5"), "i4": ("bmw", "i4"), "ix": ("bmw", "ix"),
        "330": ("bmw", "3-series"), "320": ("bmw", "3-series"),
        "c 63": ("mercedes-benz", "c-class"), "c63": ("mercedes-benz", "c-class"),
        "c 43": ("mercedes-benz", "c-class"), "c 300": ("mercedes-benz", "c-class"),
        "e 63": ("mercedes-benz", "e-class"), "e63": ("mercedes-benz", "e-class"),
        "glc": ("mercedes-benz", "glc"), "gle": ("mercedes-benz", "gle"),
        "eqe": ("mercedes-benz", "eqe"), "eqs": ("mercedes-benz", "eqs"),
        "rs 6": ("audi", "rs6"), "rs6": ("audi", "rs6"), "rs 4": ("audi", "rs4"),
        "rs 3": ("audi", "rs3"), "rs3": ("audi", "rs3"), "rs 7": ("audi", "rs7"),
        "a4": ("audi", "a4"), "a6": ("audi", "a6"),
        "q5": ("audi", "q5"), "q7": ("audi", "q7"), "q8": ("audi", "q8"),
        "e-tron": ("audi", "e-tron"), "etron": ("audi", "e-tron"),
        "911": ("porsche", "911"), "cayenne": ("porsche", "cayenne"),
        "macan": ("porsche", "macan"), "taycan": ("porsche", "taycan"),
        "panamera": ("porsche", "panamera"), "718": ("porsche", "718"),
        "golf": ("volkswagen", "golf"), "tiguan": ("volkswagen", "tiguan"),
        "passat": ("volkswagen", "passat"), "touareg": ("volkswagen", "touareg"),
        "id.3": ("volkswagen", "id.3"), "id.4": ("volkswagen", "id.4"),
        "cooper": ("mini", "cooper"), "countryman": ("mini", "countryman"),
    }

    def _build_url(self, criteria: Criteria) -> str:
        make, model = "bmw", "3-series"

        # Brand hint comes first
        if criteria.brand:
            slug = self._BRAND_SLUGS.get(criteria.brand.strip().lower())
            if slug:
                make = slug
                model = ""

        # Model keyword refinement
        if criteria.model_contains:
            low = criteria.model_contains.lower()
            for key, (mk, md) in self._MODEL_HINT_MAP.items():
                if key in low:
                    make, model = mk, md
                    break

        params: dict[str, str] = {"sort": "age", "desc": "0", "size": "20"}
        if criteria.min_year:
            params["fregfrom"] = str(criteria.min_year)
        if criteria.max_price_eur:
            params["priceto"] = str(criteria.max_price_eur)
        if criteria.max_mileage_km:
            params["kmto"] = str(criteria.max_mileage_km)

        path = f"{make}/{model}" if model else make
        return f"https://www.autoscout24.de/lst/{path}?{urllib.parse.urlencode(params)}"

    def search(self, criteria: Criteria) -> list[dict]:
        from tools.browser_session import BrowserSession

        url = self._build_url(criteria)
        session = BrowserSession.get(headless=True)
        try:
            session.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                session.page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            session._dismiss_cookies()
            time.sleep(1.2)
            html = session.page.content()
        except Exception as e:
            return [{"_error": f"navigation failed: {e}", "source": self.name}]

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        listings: list[dict] = []

        article_like = soup.select("article") or soup.select("[data-testid='list-item']")
        for a in article_like:
            try:
                text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
                link = a.find("a", href=True)
                href = link["href"] if link else ""
                full_url = href if href.startswith("http") else f"https://www.autoscout24.de{href}"

                price_m = re.search(r"€\s*([\d\.]{3,})", text)
                year_m = re.search(r"\b(20\d{2})\b", text)
                km_m = re.search(r"([\d\.]{3,})\s*km", text)

                if not (price_m and year_m and km_m):
                    continue

                # Try to pick off <Brand> <model> from the listing text
                brand_pattern = r"(BMW|Mercedes(?:-Benz)?|Audi|Porsche|Volkswagen|VW|Mini)"
                brand_m = re.search(brand_pattern, text, re.I)
                brand_detected = brand_m.group(1).title() if brand_m else make.title()
                if brand_detected.lower() == "vw":
                    brand_detected = "Volkswagen"

                trim_m = re.search(brand_pattern + r"\s+([A-Za-z0-9 ]+?)(?=\s+\d)", text, re.I)
                trim = (trim_m.group(2) if trim_m else "").strip()

                listings.append({
                    "source": self.name,
                    "external_id": (href.split("/")[-1] or full_url)[:120],
                    "url": full_url,
                    "brand": brand_detected,
                    "model": trim.split()[0] if trim else "",
                    "trim": trim,
                    "model_year": int(year_m.group(1)),
                    "mileage_km": int(km_m.group(1).replace(".", "")),
                    "asking_price_eur": int(price_m.group(1).replace(".", "")),
                    "options": [],
                    "location": "",
                    "posted_date": "",
                })
                if len(listings) >= criteria.limit_per_source:
                    break
            except Exception:
                continue

        return listings


def get_source(name: str):
    name = name.lower()
    if name == "mock":
        return MockSource()
    if name in ("autoscout", "autoscout24"):
        return AutoScoutSource()
    raise ValueError(f"unknown source: {name}")


def criteria_from_dict(d: dict) -> Criteria:
    return Criteria(**{k: v for k, v in d.items() if k in Criteria.__annotations__})


def listing_to_dict(l: Listing) -> dict:
    return asdict(l)
