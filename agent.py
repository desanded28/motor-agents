"""Agent 1 — Used-Car Deal Checker.

Input: a used-car listing URL (or free-text listing details) for BMW, Mercedes-Benz,
Audi, Porsche, Volkswagen, or Mini.
Output: verdict of whether the asking price is fair vs original MSRP after depreciation.

Usage:
    python agent.py "https://www.autoscout24.de/.../some-listing"
    python agent.py "2021 Audi RS 6 Avant, 38000 km, 95000 EUR, S line"
    python agent.py  # interactive mode
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

from tools.depreciation import estimate_fair_value, verdict
from tools.msrp_lookup import lookup_msrp
from tools.scraper import fetch_listing
from utils import cli
from utils.agent_loop import run_tool_loop

load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path(__file__).parent.parent / ".env")


SYSTEM_INSTRUCTION = """You are a used-car deal analyst covering BMW, Mercedes-Benz,
Audi, Porsche, Volkswagen, and Mini. Given a listing URL or description, produce an
honest verdict on whether the asking price is fair vs fair market value.

Workflow:
1. If the user gives a URL, call fetch_listing. If the listing is blocked or empty, say so.
2. Read the listing. Extract:
   - brand (e.g. "BMW", "Mercedes-Benz", "Audi", "Porsche", "Volkswagen", "Mini")
   - model/trim (e.g. "M340i", "C 300", "RS 6 Avant", "911 Carrera S", "Golf GTI", "Cooper S")
   - model_year (int)
   - mileage_km (int)
   - asking_price_eur (int)
   - option packages if visible (each brand has its own naming — "M Sport", "AMG Line",
     "S line", "Sport Chrono", "R-Line", "JCW trim", etc.)
3. Call lookup_msrp(model, year, options, brand). Passing `brand` narrows the search;
   the tool also auto-detects brand from the model string if brand is omitted.
4. Call estimate_fair_value with the MSRP total, model_year, mileage_km, model_name.
   The tool auto-detects performance trims (M / AMG / RS / S·Turbo Porsche / JCW / etc.)
   and EVs (i-series / EQ / e-tron / Taycan / ID. / Cooper SE) for depreciation tuning.
5. Call compute_verdict with the asking price and the fair value.
6. Return a concise final report (6–10 lines):
   - One-line car summary (brand model · year · km · key options)
   - Original MSRP (with options), fair value today, asking price, +/-% delta
   - Verdict + short commentary (1–2 sentences) on whether to buy
   - If any field was assumed/missing, list assumptions

Rules:
- Prices in euros with thousands separators (€1.234 style).
- If the MSRP lookup returns a fuzzy match, use it but mention the match confidence.
- If a URL is totally blocked, ask the user for the listing details as free text.
- Be concise. No fluff.
"""


def _tool_fetch_listing(url: str) -> dict:
    return fetch_listing(url)


def _tool_lookup_msrp(
    model: str, year: int, options: list[str] | None = None, brand: str | None = None
) -> dict:
    return lookup_msrp(model, year, options or [], brand=brand)


def _tool_estimate_fair_value(
    msrp_eur: int, model_year: int, mileage_km: int, model_name: str = ""
) -> dict:
    return estimate_fair_value(msrp_eur, model_year, mileage_km, model_name)


def _tool_compute_verdict(asking_price_eur: int, fair_value_eur: int) -> dict:
    return verdict(asking_price_eur, fair_value_eur)


TOOL_IMPLS = {
    "fetch_listing": _tool_fetch_listing,
    "lookup_msrp": _tool_lookup_msrp,
    "estimate_fair_value": _tool_estimate_fair_value,
    "compute_verdict": _tool_compute_verdict,
}


FUNCTION_DECLARATIONS = [
    {
        "name": "fetch_listing",
        "description": "Fetch a car listing URL and return its extracted text for analysis.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL of the car listing."}},
            "required": ["url"],
        },
    },
    {
        "name": "lookup_msrp",
        "description": "Look up original new-car MSRP in EUR for any supported brand (BMW, Mercedes-Benz, Audi, Porsche, Volkswagen, Mini). Fuzzy-matches trim names; brand auto-detected from model string if not passed.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "e.g. 'M340i', 'C 300', 'RS 6 Avant', '911 Carrera S', 'Golf GTI', 'Cooper S'"},
                "year": {"type": "integer", "description": "Model year, e.g. 2022"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of option package names (brand-specific).",
                },
                "brand": {"type": "string", "description": "Optional brand hint to narrow search. One of: BMW, Mercedes-Benz, Audi, Porsche, Volkswagen, Mini."},
            },
            "required": ["model", "year"],
        },
    },
    {
        "name": "estimate_fair_value",
        "description": "Estimate fair market value today for a used BMW from MSRP, year, mileage.",
        "parameters": {
            "type": "object",
            "properties": {
                "msrp_eur": {"type": "integer"},
                "model_year": {"type": "integer"},
                "mileage_km": {"type": "integer"},
                "model_name": {"type": "string", "description": "Used to tune the curve (M-cars hold value; EVs drop faster)."},
            },
            "required": ["msrp_eur", "model_year", "mileage_km"],
        },
    },
    {
        "name": "compute_verdict",
        "description": "Compare an asking price to a fair value and return a deal verdict.",
        "parameters": {
            "type": "object",
            "properties": {
                "asking_price_eur": {"type": "integer"},
                "fair_value_eur": {"type": "integer"},
            },
            "required": ["asking_price_eur", "fair_value_eur"],
        },
    },
]


def run(user_input: str, on_event=None, verbose: bool = True) -> tuple[str, str]:
    return run_tool_loop(
        agent_name="deal_checker",
        system_instruction=SYSTEM_INSTRUCTION,
        function_declarations=FUNCTION_DECLARATIONS,
        tool_impls=TOOL_IMPLS,
        user_input=user_input,
        max_iters=10,
        verbose=verbose,
        on_event=on_event,
    )


def main() -> None:
    user_input = " ".join(sys.argv[1:]).strip()
    if not user_input:
        print(cli.banner("BMW Deal Checker", "paste a listing URL or describe a car"))
        print("Example: '2021 M340i, 48000 km, asking 49500 EUR, M Sport Package'")
        user_input = input(cli.c("> ", "teal", bold=True)).strip()
        if not user_input:
            return

    print(cli.banner("BMW Deal Checker", user_input[:80]))
    final, trace = run(user_input)
    print()
    print(cli.c("─" * 60, "teal"))
    print(final)
    print(cli.c("─" * 60, "teal"))
    print(cli.dim(f"Trace saved: {trace}"))


if __name__ == "__main__":
    main()
