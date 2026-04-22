"""Structured listing extractor. Takes raw listing text and asks Gemini to pull out a
typed vehicle configuration spec across supported brands (BMW, Mercedes-Benz, Audi,
Porsche, Volkswagen, Mini)."""

import json
import os

from google import genai
from google.genai import types

EXTRACTION_PROMPT = """Extract the vehicle configuration from this car listing.
Return ONLY valid JSON matching this schema — no prose, no markdown fences:

{
  "brand": "BMW" | "Mercedes-Benz" | "Audi" | "Porsche" | "Volkswagen" | "Mini" | "other",
  "model_family": "e.g. '3 Series', 'C-Class', 'A6', '911', 'Golf', 'Countryman'",
  "trim": "e.g. 'M340i xDrive', 'C 300', 'RS 6 Avant', '911 Carrera S', 'Golf GTI', 'Cooper S'",
  "model_year": 2022,
  "body_color": "e.g. 'Alpine White', 'Nardo Grey', 'Guards Red'",
  "interior_color": "e.g. 'Black Vernasca', 'Cognac'",
  "wheel_description": "e.g. '19-inch M double-spoke 791M', '20-inch S-line'",
  "options": ["brand-specific option package names, e.g. 'M Sport Package', 'AMG Line', 'S line', 'Sport Chrono Package', 'R-Line', 'JCW Trim'"],
  "mileage_km": 48000,
  "asking_price_eur": 49500,
  "country": "de" | "it" | "com" | "us",
  "notes": "any important caveats the agent should know"
}

Rules:
- If a field is genuinely missing, use null (for strings/numbers) or [] (for options).
- Convert non-EUR prices to EUR at rough parity (USD≈0.92, GBP≈1.17). Note conversion in "notes".
- Convert miles to km (×1.609).
- `country` is the market the configurator should open in — infer from language/domain.
- Only include options that are likely top-level package names, not every sub-feature.

LISTING TEXT:
"""


def extract_config(listing_text: str, listing_url: str = "") -> dict:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return {"error": "GEMINI_API_KEY not set"}

    client = genai.Client(api_key=api_key)
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    prompt = EXTRACTION_PROMPT + (listing_text or "")[:8000]
    if listing_url:
        prompt += f"\n\nORIGINAL URL: {listing_url}"

    try:
        resp = client.models.generate_content(
            model=gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        text = (resp.text or "").strip()
        data = json.loads(text)
        return {"ok": True, "config": data}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"model did not return valid JSON: {e}", "raw": text[:500]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
