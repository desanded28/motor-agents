"""Persistent browser session for an LLM-driven browser-use agent.

Exposes a small, LLM-friendly API:
  - open_bmw_configurator(country)
  - get_page_snapshot()      → condensed list of visible clickable elements
  - click_by_text(text)
  - scroll(direction, amount)
  - screenshot(label)        → saves to bmw-agents/screenshots/
  - get_current_url()
  - close()

The session keeps a single Playwright browser alive across tool calls so the agent can
navigate a multi-step flow (configurator: model → trim → color → options → summary).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, Playwright

SCREENSHOT_DIR = Path(__file__).parent.parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

# Each (brand, country) maps to a LIST of candidate URLs. open_configurator tries them
# in order until one loads cleanly (no 404, non-empty title). This protects against
# individual brand-site restructures without requiring code changes.
CONFIG_START_URLS: dict[str, dict[str, list[str]]] = {
    "BMW": {
        "de":  ["https://www.bmw.de/de/neufahrzeuge.html",
                "https://www.bmw.de/"],
        "it":  ["https://www.bmw.it/it/all-models.html",
                "https://www.bmw.it/"],
        "com": ["https://www.bmw.com/en/all-models.html",
                "https://www.bmw.com/"],
        "us":  ["https://www.bmwusa.com/build-your-own.html",
                "https://www.bmwusa.com/"],
    },
    "Mercedes-Benz": {
        "de":  ["https://www.mercedes-benz.de/passengercars/models.html",
                "https://www.mercedes-benz.de/passengercars.html",
                "https://www.mercedes-benz.de/"],
        "it":  ["https://www.mercedes-benz.it/passengercars/models.html",
                "https://www.mercedes-benz.it/"],
        "com": ["https://www.mercedes-benz.com/en/vehicles/",
                "https://www.mercedes-benz.com/"],
        "us":  ["https://www.mbusa.com/en/vehicles/class",
                "https://www.mbusa.com/"],
    },
    "Audi": {
        "de":  ["https://www.audi.de/de/brand/de/neuwagen.html",
                "https://www.audi.de/de/brand/de/models.html",
                "https://www.audi.de/"],
        "it":  ["https://www.audi.it/it/web/it/modelli.html",
                "https://www.audi.it/"],
        "com": ["https://www.audi.com/en/models.html",
                "https://www.audi.com/"],
        "us":  ["https://www.audiusa.com/us/web/en/models.html",
                "https://www.audiusa.com/"],
    },
    "Porsche": {
        "de":  ["https://www.porsche.com/germany/models/",
                "https://www.porsche.com/germany/"],
        "it":  ["https://www.porsche.com/italy/models/",
                "https://www.porsche.com/italy/"],
        "com": ["https://www.porsche.com/international/models/",
                "https://www.porsche.com/international/"],
        "us":  ["https://www.porsche.com/usa/models/",
                "https://www.porsche.com/usa/"],
    },
    "Volkswagen": {
        "de":  ["https://www.volkswagen.de/de/modelle.html",
                "https://www.volkswagen.de/"],
        "it":  ["https://www.volkswagen.it/it/modelli.html",
                "https://www.volkswagen.it/"],
        "com": ["https://www.volkswagen.com/en/models.html",
                "https://www.volkswagen.com/"],
        "us":  ["https://www.vw.com/en/models.html",
                "https://www.vw.com/"],
    },
    "Mini": {
        "de":  ["https://www.mini.de/de_DE/home/range.html",
                "https://www.mini.de/de_DE/home.html",
                "https://www.mini.de/"],
        "it":  ["https://www.mini.it/it_IT/home/range.html",
                "https://www.mini.it/"],
        "com": ["https://www.mini.com/en_MS/home/range.html",
                "https://www.mini.com/"],
        "us":  ["https://www.miniusa.com/model/overview.html",
                "https://www.miniusa.com/"],
    },
}

# Signals a page 404'd even if HTTP returned 200. Brand sites often render SPA 404 pages.
COUNTRY_SELECTOR_MARKERS = [
    "/countries/",
    "/country-selector",
    "/language-selector",
    "currentlocale=",
    "?lang=",
]
COUNTRY_SELECTOR_TITLES = [
    "country selector", "country / language", "land wählen",
    "choose your country", "select your region",
]


def _is_country_selector(url: str, title: str = "") -> bool:
    low_url = (url or "").lower()
    low_title = (title or "").lower()
    return any(m in low_url for m in COUNTRY_SELECTOR_MARKERS) or \
           any(m in low_title for m in COUNTRY_SELECTOR_TITLES)


ERROR_PAGE_MARKERS = [
    "fehlerseite 404", "fehlerseite", "seite nicht gefunden",
    "404", "not found", "page not found", "cannot be found",
    "pagina non trovata", "non trovata",
    "pagina no encontrada", "page introuvable",
    "currently not available", "temporarily unavailable",
    "under maintenance", "try again later",
    "site nicht verfügbar", "nicht verfügbar",
    "access denied", "403 forbidden", "forbidden",
    "are you human", "unusual activity",
]

BRAND_ALIASES = {
    "bmw": "BMW",
    "mercedes": "Mercedes-Benz", "mercedes-benz": "Mercedes-Benz",
    "merc": "Mercedes-Benz", "benz": "Mercedes-Benz", "mb": "Mercedes-Benz",
    "audi": "Audi",
    "porsche": "Porsche",
    "vw": "Volkswagen", "volkswagen": "Volkswagen",
    "mini": "Mini",
}

COOKIE_ACCEPT_SELECTORS = [
    # Exact attribute hooks first (most reliable when they exist)
    'button[data-testid="uc-accept-all-button"]',
    'button[data-test="uc-accept-all-button"]',
    'button[data-testid="accept-all-button"]',
    '#onetrust-accept-btn-handler',
    '#onetrust-pc-btn-handler',
    '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
    'button#usercentrics-accept-all',
    'button.consent-accept',
    'button[aria-label*="Accept All" i]',
    'button[aria-label*="Alle akzeptieren" i]',
    'button[aria-label*="Accetta tutti" i]',

    # Text-based (works regardless of element tag via get_by_text fallback below too)
    'button:has-text("Accept All")',
    'button:has-text("Accept all")',
    'button:has-text("Alle akzeptieren")',
    'button:has-text("Accetta tutti")',
    'button:has-text("Akzeptieren")',
    'button:has-text("Accept")',
    'button:has-text("I Accept")',
    'button:has-text("Agree")',
    'button:has-text("Einverstanden")',
    'button:has-text("Zustimmen")',

    # Non-button tags some sites use (Mercedes/Usercentrics render divs with role=button)
    '[role="button"]:has-text("Alle akzeptieren")',
    '[role="button"]:has-text("Accept All")',
    '[role="button"]:has-text("Accetta tutti")',
    'a:has-text("Alle akzeptieren")',
    'a:has-text("Accept All")',
    'div:has-text("Alle akzeptieren"):not(:has(div))',
    'div:has-text("Accept All"):not(:has(div))',
]

# Text patterns for the last-resort any-element scan
COOKIE_ACCEPT_TEXTS = [
    "Alle akzeptieren",
    "Accept All",
    "Accept all",
    "Accetta tutti",
    "Einverstanden",
    "Akzeptieren",
    "Zustimmen",
    "I Accept",
    "Agree",
]


class BrowserSession:
    _instance: "BrowserSession | None" = None

    def __init__(self, headless: bool = True):
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._headless = headless
        self._screenshots_taken: list[str] = []

    @classmethod
    def get(cls, headless: bool = True) -> "BrowserSession":
        if cls._instance is None:
            cls._instance = cls(headless=headless)
            cls._instance._start()
        return cls._instance

    def _start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._ctx = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
            locale="de-DE",
        )
        self._page = self._ctx.new_page()

    @property
    def page(self) -> Page:
        if self._page is None:
            self._start()
        return self._page  # type: ignore[return-value]

    def _dismiss_cookies(self, max_rounds: int = 4) -> bool:
        """Try hard to dismiss the cookie banner. CMPs like Usercentrics (Mercedes),
        OneTrust, and custom shadow-DOM implementations each need a different approach:
          1. Selector list on main frame (works for OneTrust, simple banners).
          2. Selector list on each child iframe (some CMPs render in iframes).
          3. Text-based search tolerating <div role="button"> etc.
          4. JavaScript injection traversing shadow DOM (Usercentrics/Mercedes).
        We verify dismissal actually worked by re-checking banner visibility.
        """
        for round_idx in range(max_rounds):
            if self._try_dismiss_in_frame(self.page) and self._banner_gone():
                time.sleep(0.6)
                return True

            for frame in self.page.frames:
                if frame == self.page.main_frame:
                    continue
                try:
                    if self._try_dismiss_in_frame(frame) and self._banner_gone():
                        time.sleep(0.6)
                        return True
                except Exception:
                    continue

            if self._try_dismiss_by_text(self.page) and self._banner_gone():
                time.sleep(0.6)
                return True

            # Last resort: JS traversal through shadow DOMs
            if self._try_dismiss_via_js():
                time.sleep(0.8)
                if self._banner_gone():
                    return True

            time.sleep(0.9)
        return self._banner_gone()

    def _banner_gone(self) -> bool:
        """Cheap heuristic: no visible elements containing common consent phrasing."""
        try:
            count = self.page.evaluate(
                """() => {
                  const needles = ['Alle akzeptieren','Accept All','Accetta tutti','Einverstanden','Zustimmen'];
                  const walk = (root) => {
                    let found = 0;
                    const tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
                    let n;
                    while ((n = tw.nextNode())) {
                      const s = n.shadowRoot; if (s) found += walk(s);
                      const t = (n.innerText || '').trim();
                      if (!t) continue;
                      for (const nd of needles) {
                        if (t === nd || t.startsWith(nd + ' ') || t.endsWith(' ' + nd)) {
                          const r = n.getBoundingClientRect();
                          if (r.width > 0 && r.height > 0) { found++; break; }
                        }
                      }
                    }
                    return found;
                  };
                  return walk(document);
                }"""
            )
            return not count
        except Exception:
            return False

    def _try_dismiss_via_js(self) -> bool:
        """Inject a click into any shadow-DOM element whose text matches a consent phrase."""
        try:
            result = self.page.evaluate(
                """() => {
                  const needles = ['Alle akzeptieren','Accept All','Accept all','Accetta tutti','Einverstanden','Zustimmen','I Accept','Agree'];
                  const matchesNeedle = (t) => {
                    t = (t || '').trim();
                    return needles.some(nd => t === nd || t.startsWith(nd));
                  };
                  const click = (el) => {
                    try { el.click(); return true; } catch (e) { return false; }
                  };
                  const walk = (root) => {
                    const tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
                    let n;
                    while ((n = tw.nextNode())) {
                      if (n.shadowRoot) {
                        const hit = walk(n.shadowRoot);
                        if (hit) return true;
                      }
                      const tag = (n.tagName || '').toLowerCase();
                      const role = (n.getAttribute && n.getAttribute('role')) || '';
                      const clickable = tag === 'button' || tag === 'a' || role === 'button';
                      if (clickable && matchesNeedle(n.innerText)) {
                        const r = n.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 && click(n)) return true;
                      }
                    }
                    return false;
                  };
                  return walk(document);
                }"""
            )
            return bool(result)
        except Exception:
            return False

    def _try_dismiss_in_frame(self, frame) -> bool:
        for sel in COOKIE_ACCEPT_SELECTORS:
            try:
                loc = frame.locator(sel).first
                if loc.is_visible(timeout=400):
                    loc.click(timeout=1800)
                    return True
            except Exception:
                continue
        return False

    def _try_dismiss_by_text(self, page) -> bool:
        """Last-resort: click any visible element matching known consent texts."""
        for text in COOKIE_ACCEPT_TEXTS:
            try:
                loc = page.get_by_text(text, exact=False).first
                if loc.is_visible(timeout=400):
                    loc.click(timeout=1500)
                    return True
            except Exception:
                continue
        return False

    def open_configurator(self, brand: str = "BMW", country: str = "de") -> dict:
        """Open any supported brand's configurator/all-models page.

        Tries a list of candidate URLs per (brand, country), falling through to the brand
        homepage if deeper links 404. Detects SPA-rendered 404 pages (200 OK but "Fehlerseite"
        or "Page not found" in the title/body) and skips them.
        """
        brand_key = BRAND_ALIASES.get(brand.strip().lower(), brand)
        brand_urls = CONFIG_START_URLS.get(brand_key)
        if not brand_urls:
            return {
                "ok": False,
                "error": f"brand '{brand}' not supported",
                "supported_brands": list(CONFIG_START_URLS.keys()),
            }

        country_key = country.lower() if country.lower() in brand_urls else "de"
        # Primary country first, then cross-country fallbacks so a blocked regional
        # site (common with bot detection) can still resolve via .com/English.
        candidates: list[str] = list(brand_urls.get(country_key, []))
        fallback_order = ["com", "de", "it", "us"]
        for alt in fallback_order:
            if alt == country_key:
                continue
            for url in brand_urls.get(alt, []):
                if url not in candidates:
                    candidates.append(url)
        if not candidates:
            candidates = list(next(iter(brand_urls.values())))

        attempts: list[dict] = []
        for url in candidates:
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    self.page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass

                title = (self.page.title() or "").strip()
                title_low = title.lower()
                body_low = ""
                try:
                    body_low = self.page.inner_text("body")[:1500].lower()
                except Exception:
                    pass

                is_error = any(marker in title_low for marker in ERROR_PAGE_MARKERS) or \
                           any(marker in body_low[:600] for marker in ERROR_PAGE_MARKERS)
                attempts.append({"url": url, "title": title, "error_page": is_error})

                if not is_error and title:
                    dismissed = self._dismiss_cookies()
                    time.sleep(0.8)
                    return {
                        "ok": True,
                        "brand": brand_key,
                        "country": country_key,
                        "url": self.page.url,
                        "title": title,
                        "cookies_dismissed": dismissed,
                        "tried_urls": attempts,
                    }
            except Exception as e:
                attempts.append({"url": url, "error": f"{type(e).__name__}: {e}"})

        return {
            "ok": False,
            "brand": brand_key,
            "country": country_key,
            "error": "all candidate URLs failed or returned error pages",
            "tried_urls": attempts,
            "advice": "brand site may have changed — agent should screenshot and report, or try a different country.",
        }

    # Legacy alias for backward-compat with earlier agent prompts/code
    def open_bmw_configurator(self, country: str = "de") -> dict:
        return self.open_configurator(brand="BMW", country=country)

    def navigate(self, url: str) -> dict:
        """Jump directly to any URL on the current brand's domain. Useful when a
        landing page filters out the target model (e.g. BMW.de defaults to EVs only).
        Re-runs cookie dismissal after navigation. Also flags country-selector
        bounces so the agent knows to reroute.
        """
        if not url or not url.startswith("http"):
            return {"ok": False, "error": "url must start with http(s)://"}
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
            try:
                self.page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            dismissed = self._dismiss_cookies(max_rounds=2)
            time.sleep(0.6)
            title = (self.page.title() or "").strip()
            actual_url = self.page.url
            body = ""
            try:
                body = self.page.inner_text("body")[:600].lower()
            except Exception:
                pass
            is_error = any(m in title.lower() for m in ERROR_PAGE_MARKERS) or \
                       any(m in body[:400] for m in ERROR_PAGE_MARKERS)

            # Detect landing on a country/language selector (common Porsche quirk)
            is_country_selector = _is_country_selector(actual_url, title)

            result = {
                "ok": not is_error and not is_country_selector,
                "url": actual_url,
                "title": title,
                "error_page": is_error,
                "cookies_dismissed": dismissed,
            }
            if is_country_selector:
                result["country_selector_detected"] = True
                result["hint"] = (
                    "You landed on a country/language selector page. This is NOT the "
                    "target. Do NOT click anything here — call navigate() again with "
                    "the original model URL you were aiming at."
                )
            return result
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "url": url}

    def get_page_snapshot(self, max_items: int = 60) -> dict:
        """Return a compact snapshot: URL, page title, visible clickable elements.

        Each item reports its visible text AND its aria-label so the LLM can reliably
        pick a label that our click_by_text tool can actually match. Many modern
        sites (Mercedes, Porsche) use image-tile links where aria-label is the only
        real label.
        """
        page = self.page
        try:
            page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:
            pass

        clickable_selector = (
            "a[href], button, [role='button'], "
            "[role='link'], input[type='submit'], input[type='button']"
        )

        items: list[dict] = []
        seen: set[str] = set()
        for el in page.query_selector_all(clickable_selector):
            try:
                if not el.is_visible():
                    continue
                visible_text = re.sub(r"\s+", " ", (el.inner_text() or "").strip())[:120]
                aria = re.sub(r"\s+", " ", (el.get_attribute("aria-label") or "").strip())[:120]
                title_attr = re.sub(r"\s+", " ", (el.get_attribute("title") or "").strip())[:120]

                label = visible_text or aria or title_attr
                if not label or len(label) < 2:
                    continue
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)

                tag = el.evaluate("e => e.tagName.toLowerCase()")
                href = el.get_attribute("href") if tag == "a" else None

                entry = {"text": label, "tag": tag}
                if aria and aria != visible_text:
                    entry["aria"] = aria
                if href:
                    entry["href"] = href[:200]
                items.append(entry)
                if len(items) >= max_items:
                    break
            except Exception:
                continue

        body_text = ""
        try:
            body_text = re.sub(r"\s+", " ", page.inner_text("body"))[:1500]
        except Exception:
            pass

        return {
            "url": page.url,
            "title": page.title(),
            "clickable": items,
            "visible_text_preview": body_text,
        }

    def click_by_text(self, text: str, exact: bool = False) -> dict:
        """Click the first visible element whose label matches `text`.

        Fast-fail strategy (total budget ~3s for misses, not ~11s):
          1. Visible text (get_by_text)
          2. ARIA label (get_by_label / [aria-label])
          3. title attribute
          4. Token-overlap against the current snapshot (best effort)
        """
        page = self.page
        before_url = page.url
        attempts_log: list[dict] = []

        def _try(strategy: str, locator) -> dict | None:
            try:
                loc = locator.first
                # quick visibility probe with tight timeout — avoids 11s waits
                loc.wait_for(state="visible", timeout=1200)
                loc.scroll_into_view_if_needed(timeout=1200)
                loc.click(timeout=2000)
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                time.sleep(0.5)
                url_after = page.url
                result = {
                    "ok": True,
                    "clicked": text,
                    "strategy": strategy,
                    "url_before": before_url,
                    "url_after": url_after,
                    "navigated": before_url != url_after,
                }
                if _is_country_selector(url_after):
                    result["country_selector_detected"] = True
                    result["hint"] = (
                        "Click landed on a country/language selector. Do NOT continue "
                        "clicking here — call navigate() back to the model URL you had before."
                    )
                return result
            except Exception as e:
                attempts_log.append({"strategy": strategy, "error": f"{type(e).__name__}"})
                return None

        # 1. Visible text
        pattern = re.compile(rf"^{re.escape(text)}$" if exact else re.escape(text), re.I)
        r = _try("visible-text", page.get_by_text(pattern))
        if r:
            return r

        # 2. Exact aria-label match
        r = _try("aria-label-exact", page.locator(f'[aria-label="{text}"]'))
        if r:
            return r

        # 3. Partial aria-label match
        r = _try("aria-label-partial",
                 page.locator(f'[aria-label*="{text}" i]'))
        if r:
            return r

        # 4. title attribute
        r = _try("title-attr", page.locator(f'[title*="{text}" i]'))
        if r:
            return r

        # 5. Role-based (links/buttons) by partial name match
        try:
            r = _try("role-link-by-name", page.get_by_role("link", name=re.compile(re.escape(text), re.I)))
            if r:
                return r
        except Exception:
            pass
        try:
            r = _try("role-button-by-name", page.get_by_role("button", name=re.compile(re.escape(text), re.I)))
            if r:
                return r
        except Exception:
            pass

        return {
            "ok": False,
            "clicked": text,
            "error": "no matching element found via text / aria-label / title / role",
            "attempts": attempts_log,
        }

    def click_deep_by_text(self, text: str, exact: bool = False) -> dict:
        """Shadow-DOM-aware click. Walks the entire tree (including shadow roots),
        finds any button / link / [role=button] whose visible text contains `text`,
        invokes element.click() via JS. Use this for SPAs that render via Web Components
        (Mercedes' wb-*, BMW configurator, Porsche custom elements)."""
        page = self.page
        before_url = page.url
        needle = text.strip().lower()
        try:
            result = page.evaluate(
                """(args) => {
                  const needle = args.needle;
                  const exact = args.exact;
                  const match = (el) => {
                    const t = (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase();
                    if (!t) return false;
                    return exact ? t === needle : t.includes(needle);
                  };
                  const clickable = (el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    const role = el.getAttribute && el.getAttribute('role');
                    return tag === 'button' || tag === 'a' || role === 'button' || role === 'link';
                  };
                  const walk = (root) => {
                    const tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
                    let n;
                    while ((n = tw.nextNode())) {
                      if (n.shadowRoot) {
                        const hit = walk(n.shadowRoot);
                        if (hit) return hit;
                      }
                      if (clickable(n) && match(n)) {
                        const r = n.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                          try { n.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'}); } catch (e) {}
                          try { n.click(); return { ok: true, text: n.innerText.trim().slice(0, 80) }; } catch (e) { return { ok: false, error: String(e) }; }
                        }
                      }
                    }
                    return null;
                  };
                  return walk(document) || { ok: false, error: 'not found' };
                }""",
                {"needle": needle, "exact": exact},
            )
            if result and result.get("ok"):
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                time.sleep(0.6)
                return {
                    "ok": True,
                    "clicked": text,
                    "matched_text": result.get("text"),
                    "strategy": "shadow-deep-walk",
                    "url_before": before_url,
                    "url_after": page.url,
                    "navigated": before_url != page.url,
                }
            return {"ok": False, "clicked": text, "error": (result or {}).get("error", "no match in shadow tree")}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "clicked": text}

    def get_deep_text_everywhere(self, max_items: int = 80) -> dict:
        """Return all text nodes visible anywhere (including shadow DOMs + iframes).
        Used by pickers to 'see' what's on screen when regular snapshot misses shadow content."""
        try:
            items = self.page.evaluate(
                """(max) => {
                  const out = [];
                  const seen = new Set();
                  const walk = (root) => {
                    const tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
                    let n;
                    while ((n = tw.nextNode())) {
                      if (n.shadowRoot) walk(n.shadowRoot);
                      const tag = (n.tagName || '').toLowerCase();
                      if (!['button', 'a', 'span', 'h1', 'h2', 'h3', 'label', 'div'].includes(tag)) continue;
                      const role = n.getAttribute && n.getAttribute('role') || '';
                      const t = (n.innerText || n.getAttribute('aria-label') || '').trim();
                      if (!t || t.length < 2 || t.length > 80) continue;
                      const r = n.getBoundingClientRect();
                      if (r.width === 0 || r.height === 0) continue;
                      const key = t + '|' + tag;
                      if (seen.has(key)) continue;
                      seen.add(key);
                      out.push({ tag, role, text: t });
                      if (out.length >= max) return out;
                    }
                    return out;
                  };
                  return walk(document);
                }""",
                max_items,
            )
            return {"ok": True, "items": items, "url": self.page.url}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def click_link_by_href_contains(self, needle: str) -> dict:
        """Fallback click: find an <a> whose href contains the needle and navigate to it."""
        page = self.page
        before_url = page.url
        try:
            for a in page.query_selector_all("a[href]"):
                try:
                    href = a.get_attribute("href") or ""
                    if needle.lower() in href.lower() and a.is_visible():
                        a.scroll_into_view_if_needed(timeout=2000)
                        a.click(timeout=4000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=6000)
                        except Exception:
                            pass
                        time.sleep(0.7)
                        url_after = page.url
                        out = {"ok": True, "clicked_href_contains": needle, "url": url_after,
                               "navigated": before_url != url_after}
                        if _is_country_selector(url_after):
                            out["country_selector_detected"] = True
                            out["hint"] = (
                                "Landed on a country selector. Call navigate() back to your "
                                "target model URL instead of clicking anything here."
                            )
                        return out
                except Exception:
                    continue
            return {"ok": False, "error": f"no visible <a> with href containing {needle!r}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def click_at(self, x: int, y: int) -> dict:
        """Click at precise viewport coordinates. Used by the vision fallback."""
        page = self.page
        before_url = page.url
        try:
            page.mouse.click(int(x), int(y))
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            time.sleep(0.7)
            return {"ok": True, "at": [int(x), int(y)], "url_before": before_url,
                    "url_after": page.url, "navigated": before_url != page.url}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def screenshot_bytes(self) -> bytes:
        try:
            return self.page.screenshot(full_page=False)
        except Exception:
            return b""

    def scroll(self, direction: str = "down", amount: int = 800) -> dict:
        dy = amount if direction == "down" else -amount
        try:
            self.page.evaluate(f"window.scrollBy(0, {dy})")
            time.sleep(0.4)
            return {"ok": True, "direction": direction, "amount": amount}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def screenshot(self, label: str) -> dict:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", label)[:60]
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOT_DIR / f"{ts}_{safe}.png"
        try:
            self.page.screenshot(path=str(path), full_page=False)
            self._screenshots_taken.append(str(path))
            return {"ok": True, "path": str(path), "label": label}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_current_url(self) -> dict:
        try:
            return {"url": self.page.url, "title": self.page.title()}
        except Exception as e:
            return {"error": str(e)}

    def close(self) -> dict:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        finally:
            type(self)._instance = None
        return {"ok": True, "screenshots": self._screenshots_taken}
