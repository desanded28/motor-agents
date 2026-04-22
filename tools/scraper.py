"""Listing scraper. Fetches a car listing URL and returns raw text + HTML for the LLM to parse.

Strategy:
  1. Try a plain HTTP request with a browser UA (fast, works for many sites).
  2. If that returns a bot-block page or too-thin content, fall back to Playwright (real browser).

We deliberately keep parsing dumb here — the LLM does structured extraction in the agent loop.
Web scraping of AutoScout24/mobile.de breaks often (bot detection). This is a portfolio demo;
in production you'd use official APIs or a scraping service like Bright Data / ScrapingBee.
"""

import re
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BLOCK_MARKERS = [
    "access denied",
    "are you a human",
    "cloudflare",
    "captcha",
    "please verify",
    "unusual traffic",
]


@dataclass
class ListingPage:
    url: str
    title: str
    text: str
    method: str
    blocked: bool
    raw_html_len: int


def _extract_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "svg"]):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title else ""
    text = re.sub(r"\s+", " ", soup.get_text(separator=" ").strip())
    return title, text


def _looks_blocked(text: str) -> bool:
    low = text.lower()[:4000]
    return any(m in low for m in BLOCK_MARKERS) or len(text) < 400


def fetch_with_requests(url: str, timeout: int = 15) -> ListingPage:
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    r.raise_for_status()
    title, text = _extract_text(r.text)
    return ListingPage(
        url=url,
        title=title,
        text=text[:12000],
        method="requests",
        blocked=_looks_blocked(text),
        raw_html_len=len(r.text),
    )


def fetch_with_playwright(url: str, timeout_ms: int = 25000) -> ListingPage:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"], locale="de-DE")
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        html = page.content()
        browser.close()

    title, text = _extract_text(html)
    return ListingPage(
        url=url,
        title=title,
        text=text[:12000],
        method="playwright",
        blocked=_looks_blocked(text),
        raw_html_len=len(html),
    )


def fetch_listing(url: str) -> dict:
    """Main entry: fetch a listing URL, returning a dict the agent/LLM can consume.

    Tries fast HTTP first; if blocked or content is thin, falls back to Playwright.
    """
    try:
        page = fetch_with_requests(url)
        if page.blocked:
            page = fetch_with_playwright(url)
    except Exception as e:
        try:
            page = fetch_with_playwright(url)
        except Exception as e2:
            return {
                "ok": False,
                "url": url,
                "error": f"requests: {e}; playwright: {e2}",
                "title": "",
                "text": "",
                "method": "none",
            }

    return {
        "ok": not page.blocked,
        "url": page.url,
        "title": page.title,
        "text": page.text,
        "method": page.method,
        "blocked": page.blocked,
        "bytes": page.raw_html_len,
    }
