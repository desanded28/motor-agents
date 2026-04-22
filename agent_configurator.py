"""Agent 2 — Configurator Recreator (BMW · Mercedes-Benz · Audi · Porsche · VW · Mini).

Given a used-car listing URL (or free-text description), this agent:
  1. Extracts a structured config (brand/model/trim/year/color/options) via Gemini JSON mode.
  2. Opens the matching brand's real regional configurator in headless Chromium.
  3. Navigates via LLM-driven click decisions with a 3-tier strategy:
       a. click_by_text (text labels from DOM snapshot)
       b. click_link_by_href_contains (slug-based anchors)
       c. vision_click (Gemini vision on a grid-annotated screenshot)
  4. Screenshots each step and reports how close it matched.

Usage:
    python agent_configurator.py "https://www.autoscout24.de/angebote/..."
    python agent_configurator.py "2022 Audi RS 6 Avant, Nardo Grey, S line, Germany"
    python agent_configurator.py --headed "..."   # show the browser window
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from tools.browser_session import BrowserSession
from tools.listing_extractor import extract_config
from tools.scraper import fetch_listing
from tools.vision_picker import pick_element
from utils import cli
from utils.agent_loop import run_tool_loop

load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path(__file__).parent.parent / ".env")


SYSTEM_INSTRUCTION = """You are an agent that recreates a used-car configuration on
the matching brand's official configurator website. Supported brands: BMW,
Mercedes-Benz, Audi, Porsche, Volkswagen, Mini.

WORKFLOW:
1. Call fetch_and_extract_config(url) OR parse_freetext(text) to get a spec:
   {brand, model_family, trim, model_year, body_color, interior_color, options, country}
2. Call open_configurator(brand, country) — opens the correct brand's "All Models" or
   model-range page with cookies dismissed.
3. Call take_screenshot("landing").
4. Loop: get_page_snapshot() → pick a target → CLICK USING THE BEST STRATEGY:
     a) click_by_text("…") — e.g. "3er" on BMW, "C-Class" on Mercedes, "RS 6" on Audi,
        "911" on Porsche, "Golf" on VW, "Cooper" on Mini.
     b) click_link_by_href_contains("…") — slug hits like "3er", "c-class", "a6",
        "rs6", "911", "golf", "cooper".
     c) vision_click("the <thing>") — last-resort vision pick when a/b fail twice.
   Use scroll() if target is below the fold. Screenshot each milestone:
   model_selected / trim_selected / color_page / options_page / summary.
5. Stop when you reach a price-bearing summary page OR had 3 failed clicks in a row.
6. Final report (6–8 lines):
   - Target config (brand + model/trim from listing)
   - What you reached (URL + title)
   - New-car starting price if found (vs used asking price)
   - Screenshot paths
   - Match quality: exact / close / model-family only / failed

SHORTCUT — direct model URLs (use navigate() when landing-page filters hide your target).
These patterns have been VERIFIED against the real sites:

- BMW (petrol/M models need this — landing page filters to EVs by default):
    https://www.bmw.de/de/neufahrzeuge/3er/bmw-3-er-limousine/bmw-3er-limousine.html   (→ 3 Series, incl. M340i)
    https://www.bmw.de/de/neufahrzeuge/3er/bmw-3-er-touring/bmw-3er-touring.html        (→ 3 Series Touring)
    https://www.bmw.de/de/neufahrzeuge/m/bmw-3er-m-modelle/bmw-m3-limousine.html        (→ M3)
    https://www.bmw.de/de/neufahrzeuge/5er/...                                           (→ 5 Series, etc.)
    https://www.bmw.de/de/neufahrzeuge/x5/...                                            (→ X5)
    Filter view by series number: https://www.bmw.de/de/neufahrzeuge.html?series=3
- Mercedes:   https://www.mercedes-benz.de/passengercars/models/<body>/<class>/overview.html
              e.g. /models/saloon/c-class/overview.html, /models/suv/glc/overview.html
- Porsche:    https://www.porsche.com/germany/models/<family>/
              e.g. /models/911/, /models/cayenne/, /models/taycan/, /models/macan/
- Volkswagen: https://www.volkswagen.de/de/modelle/<family>.html
              e.g. /modelle/golf.html, /modelle/tiguan.html, /modelle/id4.html
- Mini:       https://www.mini.de/de_DE/home/range/mini-<body>.html
              e.g. /range/mini-3-tuerer.html, /range/mini-5-tuerer.html, /range/mini-cabrio.html
- Audi:       /de/brand/de/neuwagen/<family>/... (exact pattern varies; expect 403 from cloud IPs)

WHEN TO USE navigate(): if click_by_text AND click_link_by_href_contains both fail on
the landing page (the filters may be hiding the right model), construct the direct URL
using the pattern above and call navigate(url). Then screenshot + report.

COUNTRY-SELECTOR TRAP: If any tool result contains `country_selector_detected: true`
(the URL became /countries/ or a language picker), DO NOT keep clicking — immediately
call navigate() with the previous model URL. Porsche in particular tends to bounce to
the country selector when you click generic nav elements.

RULES:
- Text you click must appear in the most recent snapshot. Never invent.
- Be decisive. 1–5 clicks to a model page is ideal.
- Each brand's site differs. If the UI fights for 3+ steps, screenshot and report honestly.

STOP CRITERIA — read carefully:
- If the SAME click_by_text call fails TWICE in a row, stop retrying and either (a) try
  click_link_by_href_contains with a URL-slug, (b) try vision_click, or (c) write the
  final report.
- If THREE different click attempts for the same target all fail, DO NOT KEEP TRYING.
  Take one last screenshot, then produce the final report with match="failed".
- Reaching the model family page with a starting price is a valid "close" match —
  you do NOT need to pick every option. Stop there and report success.
- If you call the same tool with the same args 3 times, the system will force you to
  stop. Don't waste turns — write the report when progress stalls.
"""


def _tool_fetch_and_extract_config(url: str) -> dict:
    page = fetch_listing(url)
    if not page.get("ok") and not page.get("text"):
        return {"ok": False, "error": "could not fetch listing", "detail": page}
    combined = f"{page.get('title', '')}\n\n{page.get('text', '')}"
    extracted = extract_config(combined, listing_url=url)
    return {
        "ok": extracted.get("ok", False),
        "fetch_method": page.get("method"),
        "listing_blocked": page.get("blocked", False),
        "config": extracted.get("config"),
        "extract_error": extracted.get("error"),
    }


def _tool_parse_freetext(text: str) -> dict:
    r = extract_config(text)
    return {"ok": r.get("ok", False), "config": r.get("config"), "error": r.get("error")}


def _tool_open_configurator(brand: str = "BMW", country: str = "de") -> dict:
    return BrowserSession.get().open_configurator(brand=brand, country=country)


def _tool_navigate(url: str) -> dict:
    return BrowserSession.get().navigate(url)


def _tool_open_bmw_configurator(country: str = "de") -> dict:
    # Legacy alias kept for backward compatibility with older agent prompts
    return BrowserSession.get().open_configurator(brand="BMW", country=country)


def _tool_get_page_snapshot() -> dict:
    return BrowserSession.get().get_page_snapshot()


def _tool_click_by_text(text: str, exact: bool = False) -> dict:
    return BrowserSession.get().click_by_text(text, exact=exact)


def _tool_click_link_by_href_contains(needle: str) -> dict:
    return BrowserSession.get().click_link_by_href_contains(needle)


def _tool_vision_click(goal: str) -> dict:
    pick = pick_element(goal)
    if not pick.get("ok"):
        return pick
    click = BrowserSession.get().click_at(pick["x"], pick["y"])
    return {**pick, **{f"click_{k}": v for k, v in click.items()}}


def _tool_scroll(direction: str = "down", amount: int = 800) -> dict:
    return BrowserSession.get().scroll(direction, amount)


def _tool_take_screenshot(label: str) -> dict:
    return BrowserSession.get().screenshot(label)


def _tool_get_current_url() -> dict:
    return BrowserSession.get().get_current_url()


TOOL_IMPLS = {
    "fetch_and_extract_config": _tool_fetch_and_extract_config,
    "parse_freetext": _tool_parse_freetext,
    "open_configurator": _tool_open_configurator,
    "open_bmw_configurator": _tool_open_bmw_configurator,
    "navigate": _tool_navigate,
    "get_page_snapshot": _tool_get_page_snapshot,
    "click_by_text": _tool_click_by_text,
    "click_link_by_href_contains": _tool_click_link_by_href_contains,
    "vision_click": _tool_vision_click,
    "scroll": _tool_scroll,
    "take_screenshot": _tool_take_screenshot,
    "get_current_url": _tool_get_current_url,
}


FUNCTION_DECLARATIONS = [
    {
        "name": "fetch_and_extract_config",
        "description": "Fetch a listing URL and extract BMW configuration (model, trim, year, color, options).",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "parse_freetext",
        "description": "Extract BMW configuration from free-text (no URL needed).",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "open_configurator",
        "description": "Open a brand's all-models / configurator landing page. Supports BMW, Mercedes-Benz, Audi, Porsche, Volkswagen, Mini. Handles cookie banner automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "brand":   {"type": "string", "description": "BMW, Mercedes-Benz, Audi, Porsche, Volkswagen, or Mini. Default BMW."},
                "country": {"type": "string", "description": "'de', 'it', 'com', 'us'. Default 'de'."},
            },
            "required": ["brand"],
        },
    },
    {
        "name": "open_bmw_configurator",
        "description": "[legacy] Open BMW's all-models page. Prefer open_configurator for multi-brand use.",
        "parameters": {
            "type": "object",
            "properties": {"country": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "navigate",
        "description": "Jump directly to a specific model/trim URL. Use this when the landing page's filters are hiding your target (e.g. BMW.de defaults to EVs only). Handles cookies after navigation.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Absolute https:// URL on the brand's domain"}},
            "required": ["url"],
        },
    },
    {
        "name": "get_page_snapshot",
        "description": "Get compact snapshot of current page: URL, title, visible clickable labels, text preview.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "click_by_text",
        "description": "Click first visible element whose text matches. Use text from the snapshot.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "exact": {"type": "boolean", "description": "Exact match. Default false."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "click_link_by_href_contains",
        "description": "Click the first visible <a> whose href contains the substring (e.g. '3er', 'm340i').",
        "parameters": {"type": "object", "properties": {"needle": {"type": "string"}}, "required": ["needle"]},
    },
    {
        "name": "vision_click",
        "description": "Vision fallback: Gemini sees the current screen and clicks where the described element is.",
        "parameters": {
            "type": "object",
            "properties": {"goal": {"type": "string", "description": "e.g. 'the M340i model card'"}},
            "required": ["goal"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the page up or down.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
                "amount": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "take_screenshot",
        "description": "Save a screenshot of the current page with a label.",
        "parameters": {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]},
    },
    {
        "name": "get_current_url",
        "description": "Return current URL and title.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


def run(user_input: str, headed: bool = False, on_event=None, verbose: bool = True) -> tuple[str, str]:
    BrowserSession._instance = None
    BrowserSession.get(headless=not headed)
    try:
        return run_tool_loop(
            agent_name="configurator_recreator",
            system_instruction=SYSTEM_INSTRUCTION,
            function_declarations=FUNCTION_DECLARATIONS,
            tool_impls=TOOL_IMPLS,
            user_input=user_input,
            max_iters=18,
            verbose=verbose,
            on_event=on_event,
        )
    finally:
        try:
            BrowserSession.get().close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="BMW Configurator Recreator Agent")
    parser.add_argument("input", nargs="*", help="URL or free-text car description")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    args = parser.parse_args()

    user_input = " ".join(args.input).strip()
    if not user_input:
        print(cli.banner("BMW Configurator Recreator", "paste a listing URL or describe a car"))
        user_input = input(cli.c("> ", "teal", bold=True)).strip()
        if not user_input:
            return

    print(cli.banner("BMW Configurator Recreator", user_input[:80]))
    final, trace = run(user_input, headed=args.headed)
    print()
    print(cli.c("─" * 60, "teal"))
    print(final)
    print(cli.c("─" * 60, "teal"))
    print(cli.dim(f"Trace saved: {trace}"))


if __name__ == "__main__":
    main()
