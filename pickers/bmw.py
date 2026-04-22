"""BMW deep-config picker.

BMW's configurator at configure.bmw.de is a shadow-DOM React SPA using custom elements:
- Tabs (Antriebe, Modellvarianten, Außenfarben, Räder, Polster, Pakete, ...) are deep-walkable
  buttons we can click via click_deep_by_text.
- Color swatches are <con-swatch> with data-test-swatch-name (human name) +
  data-node-code (BMW's internal paint code, e.g. P0C36).
- Package toggles are similarly tagged con-product-card with data-node-code.

This picker:
  1. Navigates to configure.bmw.de for the right chassis+model
  2. Dismisses cookies (the in-configurator UC banner re-appears)
  3. Clicks through tabs: model variant → color → packages
  4. Picks specific items by human-readable name (fuzzy match)
  5. Reads the configuration URL (BMW encodes the full spec in the URL) + final price
  6. Returns a structured result
"""

from __future__ import annotations

import re
import time
from typing import Any

from tools.browser_session import BrowserSession

# Chassis code per model family.
# These are BMW's internal platform codes — stable, used across BMW internal systems.
CHASSIS_CODES = {
    "3er": "G20", "3 series": "G20", "3-series": "G20",
    "318i": "G20", "320i": "G20", "330i": "G20", "330d": "G20", "330e": "G20",
    "m340i": "G20", "m340d": "G20", "m3": "G20",
    "4er": "G22", "4 series": "G22", "430i": "G22", "m440i": "G22", "m4": "G22",
    "5er": "G60", "5 series": "G60", "520i": "G60", "530i": "G60",
    "x1": "U11", "x3": "G45", "x5": "G05", "x7": "G07",
    "i4": "G26", "ix": "I20", "i5": "G68", "i7": "G70",
}


def _chassis_for(model: str) -> str | None:
    low = (model or "").lower()
    # Exact trim lookups first
    for key in sorted(CHASSIS_CODES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", low):
            return CHASSIS_CODES[key]
    return None


def _click_deep(session: BrowserSession, text: str, exact: bool = False, wait: float = 1.5) -> dict:
    r = session.click_deep_by_text(text, exact=exact)
    time.sleep(wait)
    return r


def _pick_swatch_by_name(session: BrowserSession, target_name: str) -> dict:
    """Find and click a <con-swatch> whose data-test-swatch-name matches target_name (fuzzy).
    Returns {ok, matched_name, node_code, price_eur}."""
    result = session.page.evaluate(
        """(target) => {
          const want = target.toLowerCase();
          const tokens = want.split(/\\s+/).filter(Boolean);
          let best = null, bestScore = 0;
          const walk = (root) => {
            const tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
            let n;
            while ((n = tw.nextNode())) {
              if (n.shadowRoot) walk(n.shadowRoot);
              const tag = (n.tagName || '').toLowerCase();
              if (tag !== 'con-swatch') continue;
              const name = (n.getAttribute('data-test-swatch-name') || '').toLowerCase();
              if (!name) continue;
              let score = 0;
              for (const t of tokens) if (name.includes(t)) score++;
              if (name === want) score += 10;
              if (score > bestScore) { bestScore = score; best = n; }
            }
          };
          walk(document);
          if (!best) return { ok: false, error: 'no matching swatch' };
          const info = {
            matched_name: best.getAttribute('data-test-swatch-name'),
            node_code: best.getAttribute('data-node-code'),
            price: best.getAttribute('data-test-swatch-price'),
          };
          try {
            best.scrollIntoView({ block: 'center', inline: 'center' });
            best.click();
            return { ok: true, ...info };
          } catch (e) {
            return { ok: false, error: String(e), ...info };
          }
        }""",
        target_name,
    )
    return result or {"ok": False, "error": "unknown"}


def _pick_package_by_name(session: BrowserSession, target_name: str) -> dict:
    """Find and click a con-product-card / con-sgt-card whose text or aria-label matches."""
    result = session.page.evaluate(
        """(target) => {
          const want = target.toLowerCase();
          const walk = (root) => {
            const tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
            let n;
            while ((n = tw.nextNode())) {
              if (n.shadowRoot) {
                const hit = walk(n.shadowRoot);
                if (hit) return hit;
              }
              const tag = (n.tagName || '').toLowerCase();
              if (tag !== 'con-product-card' && tag !== 'con-sgt-card') continue;
              const aria = (n.getAttribute('aria-label') || '').toLowerCase();
              const txt = (n.innerText || '').toLowerCase();
              if (aria.includes(want) || txt.includes(want)) {
                const info = {
                  matched_name: (n.getAttribute('aria-label') || (n.innerText || '').trim().slice(0, 80)),
                  node_code: n.getAttribute('data-node-code') || n.getAttribute('data-pkg-code'),
                };
                try {
                  n.scrollIntoView({ block: 'center' });
                  n.click();
                  return { ok: true, ...info };
                } catch (e) {
                  return { ok: false, error: String(e), ...info };
                }
              }
            }
            return null;
          };
          const hit = walk(document);
          return hit || { ok: false, error: 'no matching package' };
        }""",
        target_name,
    )
    return result or {"ok": False, "error": "unknown"}


def _read_current_price(session: BrowserSession) -> int | None:
    """The price is rendered as a button with text like '49.090 €'. Read it from deep DOM."""
    try:
        txt = session.page.evaluate(
            """() => {
              const walk = (root) => {
                const tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
                let n;
                while ((n = tw.nextNode())) {
                  if (n.shadowRoot) { const h = walk(n.shadowRoot); if (h) return h; }
                  const t = (n.innerText || '').trim();
                  if (t && /^\\d{2,3}\\.\\d{3}\\s*€/.test(t)) return t;
                }
                return null;
              };
              return walk(document);
            }"""
        )
        if not txt:
            return None
        m = re.search(r"(\d{2,3})\.(\d{3})", txt)
        if m:
            return int(m.group(1)) * 1000 + int(m.group(2))
    except Exception:
        return None
    return None


def build(
    model: str,
    body_color: str | None = None,
    packages: list[str] | None = None,
    session: BrowserSession | None = None,
) -> dict:
    """Drive BMW's configurator to build a specific config.

    Args:
      model: model name or trim (e.g. "M340i", "3 Series", "X5")
      body_color: human name (e.g. "Alpinweiß", "Alpine White", "Black Sapphire")
      packages: list of package names (e.g. ["M Sport", "Innovation"])
      session: optional existing BrowserSession

    Returns:
      {
        ok: bool,
        chassis: "G20",
        model_variant: "...",
        color_picked: {name, code, price} | None,
        packages_picked: [{name, code}],
        final_url: "https://configure.bmw.de/de_DE/configure/G20/…",
        final_price_eur: int | None,
        screenshot_path: str,
        events: [...],       # step-by-step log
      }
    """
    owned_session = session is None
    s = session or BrowserSession.get(headless=True)
    events: list[dict] = []

    def _log(step: str, info: Any) -> None:
        events.append({"step": step, "info": info})

    try:
        chassis = _chassis_for(model)
        if not chassis:
            return {"ok": False, "error": f"no BMW chassis mapped for '{model}'", "events": events}

        url = f"https://configure.bmw.de/de_DE/configure/{chassis}"
        nav = s.navigate(url)
        _log("navigate", {"url": nav.get("url"), "title": nav.get("title")})
        time.sleep(6)
        s._dismiss_cookies(max_rounds=3)
        time.sleep(1)

        # Step 1: pick model variant if the model is a specific trim
        variant_picked = None
        low_model = model.lower()
        for trim_hint in ["m340i xdrive", "m340i", "m340d", "330i", "330e", "330d", "320i", "320d", "318i",
                           "m440i", "430i", "420i", "m4", "m3",
                           "x5 xdrive40i", "x5 m50i", "x5 m", "x3 m40i", "x3 xdrive20d",
                           "ix xdrive40", "ix xdrive50", "ix m60", "i4 edrive40", "i4 m50"]:
            if trim_hint in low_model:
                _click_deep(s, "Modellvarianten")
                rv = _click_deep(s, trim_hint.replace(" xdrive", " xDrive"))
                variant_picked = {"requested": trim_hint, "result": rv.get("ok"), "matched": rv.get("matched_text")}
                _log("model_variant", variant_picked)
                break

        # Step 2: pick exterior color
        color_result = None
        if body_color:
            _click_deep(s, "Außenfarben")
            time.sleep(2)
            r = _pick_swatch_by_name(s, body_color)
            color_result = r
            _log("color", r)
            time.sleep(1.5)

        # Step 3: pick packages
        pkg_results = []
        if packages:
            _click_deep(s, "Pakete")
            time.sleep(2)
            for pkg in packages:
                r = _pick_package_by_name(s, pkg)
                pkg_results.append({"requested": pkg, "result": r})
                _log("package", {"requested": pkg, "result": r})
                time.sleep(1.2)

        # Final snapshot
        s.screenshot("bmw_picker_final")
        final_url = s.page.url
        final_price = _read_current_price(s)

        return {
            "ok": True,
            "chassis": chassis,
            "model_variant": variant_picked,
            "color_picked": color_result,
            "packages_picked": pkg_results,
            "final_url": final_url,
            "final_price_eur": final_price,
            "events": events,
        }
    finally:
        if owned_session:
            try:
                s.close()
            except Exception:
                pass
