"""Brand-agnostic depreciation estimator.

Estimates fair used-car value from MSRP + age + mileage + model name. Model-family
signals are detected across marques — performance trims hold value, EVs depreciate harder.

Tokens that flag "performance / halo" trim (→ +8% retention bonus):
  M / AMG / RS / S (Porsche) / Turbo / GT / GTS / GT3 / JCW / John Cooper Works /
  Competition / Club Sport / Nismo / STI / Type R / R Line / Z06 / etc.

Tokens that flag "EV" (→ −5% retention penalty):
  i-series BMW, EQ-class Mercedes, e-tron Audi, Taycan Porsche, ID. VW, Cooper SE Mini,
  any "EV" / "electric" / "edrive" / "pure electric" string.
"""

from __future__ import annotations

import re
from datetime import datetime

AGE_CURVE = {
    0: 1.00, 1: 0.78, 2: 0.68, 3: 0.60, 4: 0.53, 5: 0.47,
    6: 0.42, 7: 0.37, 8: 0.33, 9: 0.30, 10: 0.27,
}

PERFORMANCE_BONUS = 0.08
EV_PENALTY = 0.05
AVG_KM_PER_YEAR = 15000
KM_ADJUSTMENT_PER_10K = 0.015

# Word-bounded regex patterns — each brand's performance naming lives here.
PERFORMANCE_PATTERNS = [
    r"\bm[2-8]\b",
    r"\bm340",
    r"\bm440",
    r"\bm550",
    r"\bm[5-7]0\b",
    r"\bm60\b",
    r"\bamg\b",
    r"\brs\s*\d",
    r"\brs\s*q",
    r"\brs\s*e[-\s]?tron",
    r"\bs[3-8]\b",
    r"\bturbo(\s+s)?\b",
    r"\bgt[23s]?\b",
    r"\bcompetition\b",
    r"\bcarrera\s+s\b",
    r"\bcarrera\s+4s\b",
    r"\bjcw\b",
    r"\bjohn\s+cooper\s+works\b",
    r"\bgti(\s+clubsport)?\b",
    r"\bgolf\s+r\b",
    r"\btiguan\s+r\b",
    r"\btouareg\s+r\b",
    r"\bt[-\s]?roc\s+r\b",
    r"\barteon\s+r\b",
    r"\bsq[3-9]\b",
    r"\bs\s*8\b",
    r"\bgtx\b",
    r"\bpolestar\b",
    r"\bcs\b",
    r"\bcsl\b",
]

EV_PATTERNS = [
    r"\bi[3-9]\b",
    r"\bix[123]?\b",
    r"\bedrive\b",
    r"\beqa\b", r"\beqb\b", r"\beqc\b", r"\beqe\b", r"\beqs\b",
    r"\be[-\s]?tron\b",
    r"\bq\d\s+e[-\s]?tron\b",
    r"\btaycan\b",
    r"\bid[.\s]\d",
    r"\bid\.buzz\b",
    r"\bcooper\s+se\b",
    r"\b(pure\s+)?electric\b",
    r"\bev\b",
]

_COMPILED_PERF = [re.compile(p, re.I) for p in PERFORMANCE_PATTERNS]
_COMPILED_EV = [re.compile(p, re.I) for p in EV_PATTERNS]


def _is_performance(name: str) -> bool:
    return any(p.search(name) for p in _COMPILED_PERF)


def _is_ev(name: str) -> bool:
    return any(p.search(name) for p in _COMPILED_EV)


def _age_factor(age_years: int) -> float:
    if age_years <= 0:
        return AGE_CURVE[0]
    if age_years >= 10:
        return max(AGE_CURVE[10] - (age_years - 10) * 0.02, 0.15)
    lo = AGE_CURVE[age_years]
    hi = AGE_CURVE[age_years - 1] if age_years - 1 in AGE_CURVE else lo
    return (lo + hi) / 2 if age_years < 10 else lo


def estimate_fair_value(
    msrp_eur: int,
    model_year: int,
    mileage_km: int,
    model_name: str = "",
    current_year: int | None = None,
) -> dict:
    current_year = current_year or datetime.now().year
    age = max(0, current_year - model_year)

    base_factor = _age_factor(age)

    expected_km = age * AVG_KM_PER_YEAR
    km_delta = mileage_km - expected_km
    mileage_adj = -(km_delta / 10000) * KM_ADJUSTMENT_PER_10K
    mileage_adj = max(-0.20, min(0.10, mileage_adj))

    model_adj = 0.0
    perf = _is_performance(model_name)
    ev = _is_ev(model_name)
    if perf:
        model_adj += PERFORMANCE_BONUS
    if ev:
        model_adj -= EV_PENALTY

    final_retention = max(0.10, base_factor + mileage_adj + model_adj)
    fair_value = int(msrp_eur * final_retention)

    return {
        "fair_value_eur": fair_value,
        "age_years": age,
        "base_age_factor": round(base_factor, 3),
        "mileage_adjustment": round(mileage_adj, 3),
        "model_adjustment": round(model_adj, 3),
        "is_performance": perf,
        "is_ev": ev,
        "final_retention": round(final_retention, 3),
        "breakdown": (
            f"MSRP €{msrp_eur:,} × {round(final_retention * 100, 1)}% retention "
            f"(age {age}y: {round(base_factor * 100)}%, mileage adj {round(mileage_adj * 100, 1)}%, "
            f"model adj {round(model_adj * 100, 1)}%) = €{fair_value:,}"
        ),
    }


def verdict(asking_price_eur: int, fair_value_eur: int) -> dict:
    delta_pct = (asking_price_eur - fair_value_eur) / fair_value_eur * 100
    if delta_pct <= -10:
        label, emoji = "STEAL", "🔥"
    elif delta_pct <= -3:
        label, emoji = "GOOD DEAL", "✅"
    elif delta_pct <= 5:
        label, emoji = "FAIR", "⚖️"
    elif delta_pct <= 15:
        label, emoji = "OVERPRICED", "⚠️"
    else:
        label, emoji = "RIP-OFF", "🚫"
    return {
        "verdict": label,
        "emoji": emoji,
        "delta_eur": asking_price_eur - fair_value_eur,
        "delta_pct": round(delta_pct, 1),
        "summary": f"{emoji} {label}: asking €{asking_price_eur:,} vs fair €{fair_value_eur:,} ({delta_pct:+.1f}%)",
    }
