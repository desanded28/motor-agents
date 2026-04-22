"""Formatted reports for hunter output. Console-first; HTML email optional."""

from __future__ import annotations

import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _fmt_eur(v) -> str:
    if v is None:
        return "?"
    return f"€{int(v):,}".replace(",", ".")


def render_console(top: list[dict], criteria_summary: str = "") -> str:
    if not top:
        return "No deals found matching your criteria."

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f" BMW USED-CAR HUNTER — Top {len(top)} Deals")
    if criteria_summary:
        lines.append(f" Criteria: {criteria_summary}")
    lines.append(f" Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 72)

    for i, l in enumerate(top, 1):
        lines.append("")
        lines.append(
            f" #{i}  {l.get('verdict_emoji', '')}  "
            f"{l.get('trim') or l.get('model')} · {l.get('model_year')} · "
            f"{int(l.get('mileage_km', 0)):,} km".replace(",", ".")
        )
        lines.append(
            f"     Asking {_fmt_eur(l.get('asking_price_eur'))}  "
            f"| Fair {_fmt_eur(l.get('fair_value_eur'))}  "
            f"| Savings {_fmt_eur(-l.get('delta_eur', 0) if l.get('delta_eur') is not None else None)} "
            f"({-l.get('delta_pct', 0):+.1f}%)"
        )
        if l.get("options"):
            opts = ", ".join(l["options"][:3])
            lines.append(f"     Options: {opts}")
        if l.get("location"):
            lines.append(f"     Location: {l['location']}  |  {l.get('url', '')}")
        else:
            lines.append(f"     {l.get('url', '')}")

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def render_html(top: list[dict], criteria_summary: str = "") -> str:
    rows = []
    for i, l in enumerate(top, 1):
        savings = -l.get("delta_eur", 0) if l.get("delta_eur") is not None else 0
        savings_color = "#16a34a" if savings > 0 else "#dc2626" if savings < 0 else "#525252"
        rows.append(f"""
        <tr>
          <td style="padding:8px 12px; color:#737373;">#{i}</td>
          <td style="padding:8px 12px;">
            <div style="font-weight:600; color:#0f172a;">{l.get('verdict_emoji','')} {l.get('trim') or l.get('model')}</div>
            <div style="font-size:13px; color:#737373;">{l.get('model_year')} · {int(l.get('mileage_km',0)):,} km · {l.get('location','')}</div>
          </td>
          <td style="padding:8px 12px; text-align:right; color:#0f172a;">{_fmt_eur(l.get('asking_price_eur'))}</td>
          <td style="padding:8px 12px; text-align:right; color:#737373;">{_fmt_eur(l.get('fair_value_eur'))}</td>
          <td style="padding:8px 12px; text-align:right; color:{savings_color}; font-weight:600;">
            {_fmt_eur(savings)} ({-l.get('delta_pct',0):+.1f}%)
          </td>
          <td style="padding:8px 12px;"><a href="{l.get('url','#')}" style="color:#0284c7;">View</a></td>
        </tr>
        """)

    return f"""<!doctype html>
<html><body style="font-family: -apple-system, system-ui, sans-serif; color:#0f172a; max-width:760px; margin:0 auto; padding:24px;">
  <h1 style="font-size:20px; margin:0 0 8px;">BMW Hunter — Top {len(top)} Deals</h1>
  <div style="color:#737373; font-size:13px; margin-bottom:20px;">
    {criteria_summary} · {datetime.now().strftime('%Y-%m-%d %H:%M')}
  </div>
  <table cellpadding="0" cellspacing="0" style="width:100%; border-collapse:collapse; border:1px solid #e5e5e5; border-radius:8px; overflow:hidden;">
    <thead style="background:#f5f5f5; text-align:left; font-size:12px; text-transform:uppercase; color:#737373;">
      <tr>
        <th style="padding:10px 12px;">#</th>
        <th style="padding:10px 12px;">Car</th>
        <th style="padding:10px 12px; text-align:right;">Asking</th>
        <th style="padding:10px 12px; text-align:right;">Fair</th>
        <th style="padding:10px 12px; text-align:right;">Savings</th>
        <th style="padding:10px 12px;"></th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
  <p style="color:#a3a3a3; font-size:12px; margin-top:24px;">
    Fair values estimated using BMW MSRP + depreciation model (age curve + mileage adjustment + model-family tuning).
    Not financial advice — always verify service history before buying.
  </p>
</body></html>
"""


def send_email(html: str, subject: str, to_addr: str) -> dict:
    """Send the report via SMTP. Requires SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD in env."""
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    from_addr = os.getenv("SMTP_FROM", user or "")

    if not (host and user and password and to_addr):
        return {"ok": False, "error": "SMTP not configured (SMTP_HOST/USER/PASSWORD) or missing to_addr"}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            s.login(user, password)
            s.sendmail(from_addr, [to_addr], msg.as_string())
        return {"ok": True, "to": to_addr}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
