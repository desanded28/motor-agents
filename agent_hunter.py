"""Agent 3 — Used-Car Hunter (multi-brand).

Orchestrates a multi-step pipeline across any supported brand (BMW, Mercedes-Benz,
Audi, Porsche, Volkswagen, Mini):
  parse_criteria → search_source(s) → score_listings → save_to_db → rank_top → render_report → (optional) email

Every tool is deterministic Python; Gemini is the thinking layer that parses free-text
criteria and chains the stages. A --no-llm mode runs the same pipeline deterministically
(useful for CI, cron, and demos without an API key).

Usage:
    python agent_hunter.py "AMG C 63 under 80k EUR, 2020+, max 60k km"
    python agent_hunter.py "Audi RS 6 under 100k"
    python agent_hunter.py --real "Porsche 911 Carrera"   # also use AutoScout24
    python agent_hunter.py --every 60 "any M340i or AMG C 43"
    python agent_hunter.py --no-llm "X5 under 55k"        # no LLM, pure pipeline
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from hunter import database, report, scorer
from hunter.sources import Criteria, criteria_from_dict, get_source
from utils import cli
from utils.agent_loop import run_tool_loop

load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path(__file__).parent.parent / ".env")


SYSTEM_INSTRUCTION = """You are the Used-Car Hunter orchestrator for any supported
brand (BMW, Mercedes-Benz, Audi, Porsche, Volkswagen, Mini).

Your job: given a user's criteria, find the best used-car deals across enabled listing
sources, score each against original MSRP + depreciation, persist to SQLite, and return
a ranked top-N report.

Call tools in roughly this order:
  1. parse_criteria(user_input)        → structured Criteria dict.
  2. list_sources()                    → which sources are enabled this run.
  3. For each source: search_source(name, criteria).
  4. Concatenate all listings and call score_listings(listings).
  5. save_to_db(scored).
  6. rank_top(scored, n=10).
  7. render_console_report(top, summary).
  8. If user asked, send_email_report(top, summary, to_addr).

RULES:
- Always call score_listings and save_to_db on every result set.
- If the user gave no criteria, use defaults: any brand, max 60k EUR, max 100k km, min year 2019.
- If a source errors or returns [], try the next — don't give up unless all fail.
- Final message: "Found N deals; top 3 are <short list>. Full report above."
- Don't invent listings. Only report what tools returned.
"""


_state: dict = {"real_sources": False}


def _tool_parse_criteria(user_input: str) -> dict:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key or not user_input.strip():
        return {"ok": True, "criteria": Criteria().__dict__}

    client = genai.Client(api_key=api_key)
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    prompt = f"""Extract used-car search criteria from this text. Return ONLY JSON:

{{
  "model_contains": "e.g. 'M340i', 'C 300', 'RS 6', '911', 'Golf GTI', or null if any model",
  "brand": "BMW | Mercedes-Benz | Audi | Porsche | Volkswagen | Mini | null",
  "min_year": 2020,
  "max_price_eur": 50000,
  "max_mileage_km": 80000,
  "country": "de",
  "limit_per_source": 30
}}

TEXT: {user_input}"""

    try:
        resp = client.models.generate_content(
            model=gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0, response_mime_type="application/json"),
        )
        data = json.loads(resp.text or "{}")
        return {"ok": True, "criteria": data}
    except Exception as e:
        return {"ok": False, "error": str(e), "criteria": Criteria().__dict__}


def _tool_list_sources() -> dict:
    enabled = ["mock"]
    if _state.get("real_sources"):
        enabled.append("autoscout24")
    return {"enabled": enabled}


def _tool_search_source(name: str, criteria: dict | None = None) -> dict:
    try:
        src = get_source(name)
        c = criteria_from_dict(criteria or {})
        listings = src.search(c)
        return {"ok": True, "source": name, "count": len(listings), "listings": listings}
    except Exception as e:
        return {"ok": False, "source": name, "error": f"{type(e).__name__}: {e}"}


def _tool_score_listings(listings: list[dict]) -> dict:
    scored = scorer.score_all(listings or [])
    return {"ok": True, "count": len(scored), "listings": scored}


def _tool_save_to_db(scored: list[dict]) -> dict:
    res = database.upsert_scored(scored or [])
    res["db_total_rows"] = database.count_listings()
    res["ok"] = True
    return res


def _tool_rank_top(scored: list[dict], n: int = 10) -> dict:
    top = scorer.rank(scored or [], top_n=int(n))
    return {"ok": True, "count": len(top), "top": top}


def _tool_render_console_report(top: list[dict], summary: str = "") -> dict:
    cli.print_deals(top or [], summary)
    return {"ok": True, "rendered": len(top or [])}


def _tool_send_email_report(top: list[dict], summary: str = "", to_addr: str = "") -> dict:
    html = report.render_html(top or [], summary)
    return report.send_email(html, subject="BMW Hunter — daily deals", to_addr=to_addr)


TOOL_IMPLS = {
    "parse_criteria": _tool_parse_criteria,
    "list_sources": _tool_list_sources,
    "search_source": _tool_search_source,
    "score_listings": _tool_score_listings,
    "save_to_db": _tool_save_to_db,
    "rank_top": _tool_rank_top,
    "render_console_report": _tool_render_console_report,
    "send_email_report": _tool_send_email_report,
}


FUNCTION_DECLARATIONS = [
    {
        "name": "parse_criteria",
        "description": "Turn natural-language search text into a Criteria dict.",
        "parameters": {
            "type": "object",
            "properties": {"user_input": {"type": "string"}},
            "required": ["user_input"],
        },
    },
    {
        "name": "list_sources",
        "description": "Return enabled listing sources for this run.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_source",
        "description": "Search ONE listing source for BMWs matching the criteria.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "'mock' or 'autoscout24'"},
                "criteria": {
                    "type": "object",
                    "properties": {
                        "model_contains": {"type": "string"},
                        "min_year": {"type": "integer"},
                        "max_price_eur": {"type": "integer"},
                        "max_mileage_km": {"type": "integer"},
                        "country": {"type": "string"},
                        "limit_per_source": {"type": "integer"},
                    },
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "score_listings",
        "description": "Score listings against MSRP + depreciation. Adds fair_value_eur, delta, verdict.",
        "parameters": {
            "type": "object",
            "properties": {"listings": {"type": "array", "items": {"type": "object"}}},
            "required": ["listings"],
        },
    },
    {
        "name": "save_to_db",
        "description": "Upsert scored listings into SQLite (dedupes by source+external_id).",
        "parameters": {
            "type": "object",
            "properties": {"scored": {"type": "array", "items": {"type": "object"}}},
            "required": ["scored"],
        },
    },
    {
        "name": "rank_top",
        "description": "Rank scored listings by savings and return top N.",
        "parameters": {
            "type": "object",
            "properties": {
                "scored": {"type": "array", "items": {"type": "object"}},
                "n": {"type": "integer", "description": "Default 10"},
            },
            "required": ["scored"],
        },
    },
    {
        "name": "render_console_report",
        "description": "Print formatted top-deals report to the terminal.",
        "parameters": {
            "type": "object",
            "properties": {
                "top": {"type": "array", "items": {"type": "object"}},
                "summary": {"type": "string"},
            },
            "required": ["top"],
        },
    },
    {
        "name": "send_email_report",
        "description": "Email the top deals. Requires SMTP env vars + to_addr.",
        "parameters": {
            "type": "object",
            "properties": {
                "top": {"type": "array", "items": {"type": "object"}},
                "summary": {"type": "string"},
                "to_addr": {"type": "string"},
            },
            "required": ["top", "to_addr"],
        },
    },
]


def run(user_input: str, real_sources: bool = False, on_event=None, verbose: bool = True) -> tuple[str, str]:
    _state["real_sources"] = real_sources
    return run_tool_loop(
        agent_name="hunter",
        system_instruction=SYSTEM_INSTRUCTION,
        function_declarations=FUNCTION_DECLARATIONS,
        tool_impls=TOOL_IMPLS,
        user_input=user_input or "Find me the best used BMW deals.",
        max_iters=20,
        verbose=verbose,
        on_event=on_event,
    )


def run_pipeline_deterministic(user_input: str, real_sources: bool = False) -> str:
    """No-LLM fallback: same flow without Gemini. Used for CI and demos without a key."""
    _state["real_sources"] = real_sources

    parsed = _tool_parse_criteria(user_input)
    criteria = parsed.get("criteria") or Criteria().__dict__

    sources = ["mock"] + (["autoscout24"] if real_sources else [])

    all_scored: list[dict] = []
    for s in sources:
        res = _tool_search_source(s, criteria)
        if res.get("ok") and res.get("listings"):
            scored = _tool_score_listings(res["listings"])["listings"]
            all_scored.extend(scored)

    _tool_save_to_db(all_scored)
    top = _tool_rank_top(all_scored, n=10)["top"]
    summary = (
        f"{criteria.get('model_contains') or 'Any BMW'} · "
        f"≤€{criteria.get('max_price_eur') or '∞'} · "
        f"≤{criteria.get('max_mileage_km') or '∞'} km · "
        f"≥{criteria.get('min_year') or '?'}"
    )
    _tool_render_console_report(top, summary)
    return f"[deterministic] Scored {len(all_scored)} listings, top {len(top)} printed above."


def main() -> None:
    parser = argparse.ArgumentParser(description="BMW Used-Car Hunter Agent")
    parser.add_argument("input", nargs="*")
    parser.add_argument("--real", action="store_true", help="Also use AutoScout24 (may be blocked)")
    parser.add_argument("--every", type=int, metavar="MINUTES", help="Loop forever, run every N minutes")
    parser.add_argument("--no-llm", action="store_true", help="Run deterministic pipeline (no Gemini)")
    args = parser.parse_args()

    user_input = " ".join(args.input).strip()

    def _run_once() -> None:
        print(cli.banner(
            "Used-Car Hunter",
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ·  input: {user_input or '(defaults)'}",
        ))
        if args.no_llm:
            print(run_pipeline_deterministic(user_input, real_sources=args.real))
        else:
            final, trace = run(user_input, real_sources=args.real)
            print()
            print(cli.c("─" * 60, "teal"))
            print(final)
            print(cli.c("─" * 60, "teal"))
            print(cli.dim(f"Trace saved: {trace}"))

    if args.every:
        while True:
            try:
                _run_once()
            except Exception as e:
                print(cli.c(f"[error] {type(e).__name__}: {e}", "red"), file=sys.stderr)
            print(cli.dim(f"\nSleeping {args.every} min until next run..."))
            time.sleep(args.every * 60)
    else:
        _run_once()


if __name__ == "__main__":
    main()
