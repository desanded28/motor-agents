"""Scoring: apply Agent 1's MSRP + depreciation math to every listing, return a deal score.

A "deal score" is how many euros below fair value a car is, expressed both as absolute
savings and a percentage. Bigger positive = better deal for the buyer.
"""

from tools.depreciation import estimate_fair_value, verdict as verdict_fn
from tools.msrp_lookup import lookup_msrp


def score_listing(listing: dict) -> dict:
    """Enrich a listing with MSRP lookup, fair-value estimate, and a verdict.

    Returns the listing dict plus:
      brand_matched, msrp_total, fair_value_eur, delta_eur, delta_pct, verdict,
      verdict_emoji, score, scoring_error (present only if pricing couldn't be computed).
    """
    brand = listing.get("brand")
    model = listing.get("model", "")
    trim = listing.get("trim", "")
    year = int(listing.get("model_year", 0) or 0)
    km = int(listing.get("mileage_km", 0) or 0)
    asking = int(listing.get("asking_price_eur", 0) or 0)
    options = listing.get("options", []) or []

    lookup_key = trim or model
    msrp = lookup_msrp(lookup_key, year, options, brand=brand)

    if not msrp.get("found") and model and model != lookup_key:
        msrp = lookup_msrp(model, year, options, brand=brand)

    if not msrp.get("found") and lookup_key:
        bare = lookup_key.split()[0]
        if bare and bare != lookup_key and bare != model:
            msrp = lookup_msrp(bare, year, options, brand=brand)

    enriched = dict(listing)

    if not msrp.get("found"):
        enriched.update({
            "msrp_total": None,
            "fair_value_eur": None,
            "delta_eur": None,
            "delta_pct": None,
            "verdict": "UNKNOWN",
            "verdict_emoji": "❓",
            "score": -1e9,
            "scoring_error": msrp.get("message", "model not in MSRP database"),
        })
        return enriched

    fv = estimate_fair_value(msrp["total_msrp"], year, km, model_name=lookup_key)
    v = verdict_fn(asking, fv["fair_value_eur"])

    enriched.update({
        "brand_matched": msrp.get("brand"),
        "msrp_total": msrp["total_msrp"],
        "fair_value_eur": fv["fair_value_eur"],
        "delta_eur": v["delta_eur"],
        "delta_pct": v["delta_pct"],
        "verdict": v["verdict"],
        "verdict_emoji": v["emoji"],
        "score": -v["delta_eur"],
    })
    if not enriched.get("brand") and msrp.get("brand"):
        enriched["brand"] = msrp["brand"]
    return enriched


def score_all(listings: list[dict]) -> list[dict]:
    return [score_listing(l) for l in listings]


def rank(scored: list[dict], top_n: int = 10) -> list[dict]:
    """Sort by best-deal-first. Drops listings that failed to price."""
    priceable = [l for l in scored if l.get("score", -1e9) > -1e9]
    priceable.sort(key=lambda l: l["score"], reverse=True)
    return priceable[:top_n]
