"""Terminal output helpers. Zero dependencies — just ANSI codes.

Auto-disables color if stdout isn't a TTY (e.g. piped to a file or SSE stream).
"""

from __future__ import annotations

import os
import sys
import shutil

_USE_COLOR = sys.stdout.isatty() and os.getenv("NO_COLOR") is None

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

FG = {
    "black": "\033[30m", "red": "\033[31m", "green": "\033[32m",
    "yellow": "\033[33m", "blue": "\033[34m", "magenta": "\033[35m",
    "cyan": "\033[36m", "white": "\033[37m", "gray": "\033[90m",
    "teal": "\033[38;5;37m", "orange": "\033[38;5;208m",
}


def c(text: str, color: str, bold: bool = False) -> str:
    if not _USE_COLOR:
        return text
    prefix = FG.get(color, "")
    if bold:
        prefix = BOLD + prefix
    return f"{prefix}{text}{RESET}"


def bold(text: str) -> str:
    return c(text, "white", bold=True)


def dim(text: str) -> str:
    return f"{DIM}{text}{RESET}" if _USE_COLOR else text


def banner(title: str, subtitle: str = "") -> str:
    width = min(shutil.get_terminal_size((72, 24)).columns, 80)
    bar = "═" * width
    lines = [c(bar, "teal"), c(f"  {title}", "teal", bold=True)]
    if subtitle:
        lines.append(dim(f"  {subtitle}"))
    lines.append(c(bar, "teal"))
    return "\n".join(lines)


def tool_call(name: str, args_preview: str) -> str:
    return f"  {c('→', 'teal')} {bold(name)}{dim('(' + args_preview + ')')}"


def tool_result(preview: str) -> str:
    return f"    {c('←', 'gray')} {dim(preview)}"


def verdict_color(verdict: str) -> str:
    palette = {
        "STEAL": "orange", "GOOD DEAL": "green", "FAIR": "cyan",
        "OVERPRICED": "yellow", "RIP-OFF": "red", "UNKNOWN": "gray",
    }
    return palette.get(verdict, "white")


def format_deal_line(i: int, listing: dict) -> list[str]:
    """Render one listing as a 3-line colored block."""
    km = int(listing.get("mileage_km", 0))
    asking = int(listing.get("asking_price_eur", 0))
    fair = listing.get("fair_value_eur")
    delta = listing.get("delta_eur")
    pct = listing.get("delta_pct", 0) or 0
    verdict = listing.get("verdict", "UNKNOWN")
    emoji = listing.get("verdict_emoji", "?")

    brand = listing.get("brand") or listing.get("brand_matched") or ""
    name = listing.get("trim") or listing.get("model", "?")
    title = f"#{i:2d}  {emoji}  {brand + ' ' if brand else ''}{name}"
    header = f"{bold(title)}  {dim('·')}  {listing.get('model_year','?')}  {dim('·')}  {km:,} km".replace(",", ".")
    savings = -(delta or 0)
    savings_str = f"€{savings:,}".replace(",", ".")
    asking_str = f"€{asking:,}".replace(",", ".")
    fair_str = f"€{int(fair or 0):,}".replace(",", ".") if fair else "?"
    price_line = (
        f"     Asking {bold(asking_str)}  {dim('|')}  Fair {fair_str}  {dim('|')}  "
        f"{c(verdict, verdict_color(verdict), bold=True)}  "
        f"{c(f'{savings_str} ({-pct:+.1f}%)', verdict_color(verdict))}"
    )
    meta_parts = []
    if listing.get("options"):
        meta_parts.append(", ".join(listing["options"][:3]))
    if listing.get("location"):
        meta_parts.append(listing["location"])
    meta = dim("     " + "  ·  ".join(meta_parts)) if meta_parts else ""
    url = dim(f"     {listing.get('url','')}")

    lines = [header, price_line]
    if meta:
        lines.append(meta)
    lines.append(url)
    return lines


def print_deals(top: list[dict], criteria_summary: str = "") -> None:
    print()
    print(banner(f"USED-CAR HUNTER — Top {len(top)} Deals", criteria_summary))
    for i, l in enumerate(top, 1):
        print()
        for line in format_deal_line(i, l):
            print(line)
    print()
