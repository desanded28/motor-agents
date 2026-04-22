"""Vision fallback for Agent 2.

When text-based clicking can't find the target element (e.g. BMW's configurator uses
React-rendered image cards with no accessible text labels), this tool:

  1. Takes a screenshot of the current viewport.
  2. Overlays a 10×10 coordinate grid on it.
  3. Sends it to Gemini with a goal like "click the M340i model card".
  4. Parses the model's response into (x, y) pixel coordinates.
  5. Returns the coords, which the agent can feed to browser_session.click_at().

The idea is to give the LLM a labeled visual coordinate system it can point to, so
it can pick elements even when the underlying DOM is opaque (custom elements, shadow
DOM, React components without useful ARIA labels).
"""

from __future__ import annotations

import io
import json
import os
import re

from google import genai
from google.genai import types

from tools.browser_session import BrowserSession

GRID_COLS = 10
GRID_ROWS = 10
VIEWPORT_W = 1400
VIEWPORT_H = 900


def _overlay_grid(png_bytes: bytes) -> bytes:
    """Overlay a labelled grid on the screenshot so the model can reference cells."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return png_bytes

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img, "RGBA")

    step_x, step_y = w / GRID_COLS, h / GRID_ROWS
    grid_color = (255, 64, 64, 160)
    label_bg = (0, 0, 0, 180)
    label_fg = (255, 255, 255, 255)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except Exception:
        font = ImageFont.load_default()

    for c in range(1, GRID_COLS):
        x = int(c * step_x)
        draw.line([(x, 0), (x, h)], fill=grid_color, width=1)
    for r in range(1, GRID_ROWS):
        y = int(r * step_y)
        draw.line([(0, y), (w, y)], fill=grid_color, width=1)

    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            label = f"{chr(ord('A') + r)}{c + 1}"
            lx, ly = int(c * step_x) + 4, int(r * step_y) + 2
            tw = draw.textlength(label, font=font)
            draw.rectangle([lx - 2, ly - 1, lx + tw + 4, ly + 16], fill=label_bg)
            draw.text((lx, ly), label, fill=label_fg, font=font)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _cell_center(cell: str) -> tuple[int, int] | None:
    m = re.match(r"^([A-Za-z])(\d+)$", cell.strip())
    if not m:
        return None
    row = ord(m.group(1).upper()) - ord("A")
    col = int(m.group(2)) - 1
    if not (0 <= row < GRID_ROWS and 0 <= col < GRID_COLS):
        return None
    x = int((col + 0.5) * (VIEWPORT_W / GRID_COLS))
    y = int((row + 0.5) * (VIEWPORT_H / GRID_ROWS))
    return x, y


def pick_element(goal: str) -> dict:
    """Ask Gemini vision: given the current page, which grid cell contains the target?

    Returns {ok, cell, x, y, reasoning} or {ok: False, error}.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return {"ok": False, "error": "GEMINI_API_KEY not set"}

    session = BrowserSession.get()
    raw = session.screenshot_bytes()
    if not raw:
        return {"ok": False, "error": "failed to capture screenshot"}

    annotated = _overlay_grid(raw)

    prompt = f"""This screenshot of a car-manufacturer configurator page has a 10×10 red
grid overlay. Each cell is labeled (A1–J10, row letter + column number).

GOAL: Find the element that best matches "{goal}".

Respond with ONLY valid JSON:
{{"cell": "C4", "confidence": 0.0-1.0, "reasoning": "one-sentence why"}}

If nothing matches, return {{"cell": null, "confidence": 0.0, "reasoning": "..."}}.
"""

    try:
        client = genai.Client(api_key=api_key)
        # Vision requires a multimodal model — 2.0-flash and 2.5-flash both support images.
        gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
        resp = client.models.generate_content(
            model=gemini_model,
            contents=[
                types.Part.from_bytes(data=annotated, mime_type="image/png"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        data = json.loads((resp.text or "{}").strip())
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    cell = data.get("cell")
    if not cell:
        return {"ok": False, "error": "model returned no cell", "reasoning": data.get("reasoning", "")}

    coords = _cell_center(cell)
    if coords is None:
        return {"ok": False, "error": f"invalid cell label {cell!r}"}

    return {
        "ok": True,
        "cell": cell,
        "x": coords[0],
        "y": coords[1],
        "confidence": data.get("confidence", 0.0),
        "reasoning": data.get("reasoning", ""),
    }
