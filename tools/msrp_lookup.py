"""Brand-aware MSRP lookup.

Given an optional brand + model description + year + options, returns original new-car
price in EUR. The matcher:

  1. If brand is provided, searches within that brand only.
  2. Otherwise scans all brands, prefers the brand whose name appears in the query
     (e.g. "BMW M340i" → BMW; "mercedes-benz C300" → Mercedes-Benz).
  3. Falls back to scanning every brand and picking the best fuzzy hit across all.

Model matching itself uses a tiered strategy so the LLM can be imprecise:
exact → token subset → substring → token overlap → SequenceMatcher ≥ 0.72.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

_DATA = json.loads((Path(__file__).parent.parent / "data" / "msrp.json").read_text())
_BRANDS: dict = _DATA["brands"]

# Common aliases users might type — normalize to canonical brand name
_BRAND_ALIASES = {
    "bmw": "BMW",
    "mercedes": "Mercedes-Benz",
    "mercedes-benz": "Mercedes-Benz",
    "mercedes benz": "Mercedes-Benz",
    "merc": "Mercedes-Benz",
    "benz": "Mercedes-Benz",
    "mb": "Mercedes-Benz",
    "audi": "Audi",
    "porsche": "Porsche",
    "vw": "Volkswagen",
    "volkswagen": "Volkswagen",
    "mini": "Mini",
}

_LETTER_RE = re.compile(r"[a-z]+")
_DIGIT_RE = re.compile(r"\d+[a-z]*")


def _tokens(s: str) -> set[str]:
    low = s.lower()
    tokens: set[str] = set()
    tokens.update(_LETTER_RE.findall(low))
    tokens.update(_DIGIT_RE.findall(low))
    return tokens


def _detect_brand_from_query(query: str) -> str | None:
    """Sniff the query for a brand name. Returns canonical brand or None."""
    low = query.lower()
    for alias in sorted(_BRAND_ALIASES, key=len, reverse=True):
        if alias in low:
            return _BRAND_ALIASES[alias]
    for brand in _BRANDS:
        if brand.lower() in low:
            return brand
    return None


def _find_in_models(query: str, models: dict) -> tuple[str | None, float, str]:
    """Match a model within one brand's model dict. Returns (name, confidence, reason)."""
    q_low = query.strip().lower()
    if not q_low:
        return None, 0.0, "empty"

    for name in models:
        if name.lower() == q_low:
            return name, 1.0, "exact"

    q_tokens = _tokens(query)

    token_subset = []
    for name in models:
        n_tokens = _tokens(name)
        if n_tokens and n_tokens.issubset(q_tokens):
            token_subset.append((name, len(n_tokens)))
    if token_subset:
        token_subset.sort(key=lambda x: (-x[1], -len(x[0])))
        return token_subset[0][0], 0.9, "token-subset"

    for name in models:
        if name.lower() in q_low:
            return name, 0.85, "substring"
        if q_low in name.lower():
            return name, 0.75, "reverse-substring"

    overlap_hits = []
    for name in models:
        n_tokens = _tokens(name)
        if not n_tokens:
            continue
        shared = q_tokens & n_tokens
        if len(shared) >= 2 or (len(shared) >= 1 and len(q_tokens) <= 2):
            score = len(shared) / max(len(n_tokens), len(q_tokens))
            overlap_hits.append((name, score, len(shared)))
    if overlap_hits:
        overlap_hits.sort(key=lambda x: (-x[2], -x[1], -len(x[0])))
        best = overlap_hits[0]
        if best[2] >= 2 or best[1] >= 0.5:
            return best[0], 0.8, f"token-overlap({best[2]})"

    best_name, best_score = None, 0.0
    for name in models:
        score = SequenceMatcher(None, name.lower(), q_low).ratio()
        if score > best_score:
            best_name, best_score = name, score
    if best_name and best_score >= 0.72:
        return best_name, best_score, f"fuzzy({best_score:.2f})"

    return None, best_score, "no-match"


def _match_option(query: str, option_prices: dict) -> str | None:
    q_low = query.strip().lower()
    for name in option_prices:
        if name.lower() == q_low or q_low in name.lower() or name.lower() in q_low:
            return name
    best, best_score = None, 0.0
    for name in option_prices:
        s = SequenceMatcher(None, name.lower(), q_low).ratio()
        if s > best_score:
            best, best_score = name, s
    return best if best_score >= 0.78 else None


def lookup_msrp(
    model: str,
    year: int,
    options: list[str] | None = None,
    brand: str | None = None,
) -> dict:
    """Look up the MSRP for a brand+model+year+options combo.

    `brand` is optional; if omitted, will be detected from the query or tried across
    all brands.
    """
    # Strip any embedded brand from the model query (e.g. "BMW M340i" → brand=BMW, model="M340i")
    query_brand = _detect_brand_from_query(model)
    if brand is None and query_brand:
        brand = query_brand
    if query_brand:
        low = model.lower()
        # Remove brand aliases from the model string so matching targets just the model
        for alias in sorted(_BRAND_ALIASES, key=len, reverse=True):
            low = low.replace(alias, " ")
        for b in _BRANDS:
            low = low.replace(b.lower(), " ")
        cleaned = re.sub(r"\s+", " ", low).strip()
        if cleaned:
            model = cleaned

    # Normalize brand alias if provided
    if brand:
        brand_key = brand.strip().lower()
        brand = _BRAND_ALIASES.get(brand_key, brand)
        # tolerate exact brand name in any case
        for b in _BRANDS:
            if b.lower() == brand.lower():
                brand = b
                break

    candidate_brands = [brand] if brand and brand in _BRANDS else list(_BRANDS.keys())

    # Try each brand; keep the best match
    best: dict | None = None
    for b in candidate_brands:
        models = _BRANDS[b]["models"]
        name, conf, reason = _find_in_models(model, models)
        if name is None:
            continue
        year_str = str(year)
        if year_str not in models[name]:
            continue
        base = int(models[name][year_str])

        matched, unmatched, options_total = [], [], 0
        for opt in options or []:
            hit = _match_option(opt, _BRANDS[b]["option_packages"])
            if hit:
                matched.append(hit)
                options_total += _BRANDS[b]["option_packages"][hit]
            else:
                unmatched.append(opt)

        total = base + options_total
        score = conf + (0.01 if b == brand else 0)
        candidate = {
            "found": True,
            "brand": b,
            "matched_model": name,
            "match_confidence": round(conf, 2),
            "match_reason": reason,
            "_internal_score": score,
            "base_msrp": base,
            "options_total": options_total,
            "total_msrp": total,
            "matched_options": matched,
            "unmatched_options": unmatched,
            "message": f"{b} {name} ({year}) base €{base:,} + €{options_total:,} options = €{total:,}  [{reason}]",
        }
        if best is None or candidate["_internal_score"] > best["_internal_score"]:
            best = candidate

    if best is not None:
        best.pop("_internal_score", None)
        return best

    candidates = []
    for b in candidate_brands:
        for name in _BRANDS[b]["models"]:
            ratio = SequenceMatcher(None, name.lower(), model.lower()).ratio()
            candidates.append((ratio, f"{b} {name}"))
    candidates.sort(reverse=True)
    top = [c[1] for c in candidates[:5]]

    return {
        "found": False,
        "brand": brand,
        "matched_model": None,
        "match_confidence": 0.0,
        "match_reason": "no-match",
        "base_msrp": None,
        "options_total": 0,
        "total_msrp": None,
        "matched_options": [],
        "unmatched_options": options or [],
        "message": f"Model '{model}'{' (brand=' + brand + ')' if brand else ''} not found. Closest candidates: {top}",
    }


def all_brands() -> list[str]:
    return sorted(_BRANDS.keys())


def all_models(brand: str | None = None) -> list[str]:
    if brand and brand in _BRANDS:
        return sorted(_BRANDS[brand]["models"].keys())
    out = []
    for b, bd in _BRANDS.items():
        out.extend(f"{b} {m}" for m in bd["models"])
    return sorted(out)


def all_options(brand: str | None = None) -> list[str]:
    if brand and brand in _BRANDS:
        return sorted(_BRANDS[brand]["option_packages"].keys())
    seen: set[str] = set()
    for bd in _BRANDS.values():
        seen.update(bd["option_packages"].keys())
    return sorted(seen)
