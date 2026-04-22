"""Tiny SQLite layer: dedupe listings across runs + store scoring history.

Table:
    listings(
      source, external_id, url, model, trim, model_year, mileage_km,
      asking_price_eur, options_json, location, posted_date,
      msrp_total, fair_value_eur, delta_eur, delta_pct, verdict,
      first_seen_at, last_seen_at,
      PRIMARY KEY(source, external_id)
    )

`upsert_scored()` is idempotent — running the hunter daily won't re-insert the same ad,
but it will update the last_seen_at + latest price/verdict (prices change, ads get re-listed).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "hunter.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT,
    brand TEXT,
    model TEXT,
    trim TEXT,
    model_year INTEGER,
    mileage_km INTEGER,
    asking_price_eur INTEGER,
    options_json TEXT,
    location TEXT,
    posted_date TEXT,
    msrp_total INTEGER,
    fair_value_eur INTEGER,
    delta_eur INTEGER,
    delta_pct REAL,
    verdict TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    PRIMARY KEY (source, external_id)
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def upsert_scored(scored: list[dict]) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inserted, updated = 0, 0
    with _conn() as c:
        for l in scored:
            row = (
                l.get("source"), l.get("external_id"), l.get("url"),
                l.get("brand"), l.get("model"), l.get("trim"), l.get("model_year"),
                l.get("mileage_km"), l.get("asking_price_eur"),
                json.dumps(l.get("options", [])), l.get("location"), l.get("posted_date"),
                l.get("msrp_total"), l.get("fair_value_eur"),
                l.get("delta_eur"), l.get("delta_pct"), l.get("verdict"),
                now, now,
            )
            cur = c.execute(
                "SELECT 1 FROM listings WHERE source = ? AND external_id = ?",
                (l.get("source"), l.get("external_id")),
            )
            existed = cur.fetchone() is not None
            c.execute(
                """
                INSERT INTO listings (
                    source, external_id, url, brand, model, trim, model_year, mileage_km,
                    asking_price_eur, options_json, location, posted_date,
                    msrp_total, fair_value_eur, delta_eur, delta_pct, verdict,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, external_id) DO UPDATE SET
                    url=excluded.url,
                    brand=excluded.brand,
                    asking_price_eur=excluded.asking_price_eur,
                    mileage_km=excluded.mileage_km,
                    msrp_total=excluded.msrp_total,
                    fair_value_eur=excluded.fair_value_eur,
                    delta_eur=excluded.delta_eur,
                    delta_pct=excluded.delta_pct,
                    verdict=excluded.verdict,
                    last_seen_at=excluded.last_seen_at
                """,
                row,
            )
            if existed:
                updated += 1
            else:
                inserted += 1
    return {"inserted": inserted, "updated": updated, "total": inserted + updated}


def get_best_deals(limit: int = 10) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT * FROM listings
            WHERE delta_eur IS NOT NULL
            ORDER BY delta_eur ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_listings() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
