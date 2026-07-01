import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .cities import normalize_city
from .models import Listing, RentComp, ScreenVerdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE TABLE IF NOT EXISTS verdicts (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    passes INTEGER NOT NULL,        -- 1 si status='pass', sinon 0 (conserve pour les anciennes queries)
    score REAL NOT NULL,
    payload TEXT NOT NULL,
    status TEXT,                    -- 'pass' | 'pass_partial' | 'fail' (ajoute apres coup)
    PRIMARY KEY (source, source_id, run_at)
);
CREATE TABLE IF NOT EXISTS rent_comps (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    city TEXT,
    bedrooms INTEGER,
    monthly_rent REAL,
    payload TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_rent_comps_city_br ON rent_comps(city, bedrooms);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # Migration ad-hoc: ajouter `status` aux verdicts existants si absent.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(verdicts)").fetchall()]
    if "status" not in cols:
        conn.execute("ALTER TABLE verdicts ADD COLUMN status TEXT")
        conn.commit()
    return conn


def upsert_listing(conn: sqlite3.Connection, listing: Listing) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO listings (source, source_id, url, fetched_at, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            listing.source,
            listing.source_id,
            str(listing.url),
            listing.fetched_at.isoformat(),
            listing.model_dump_json(),
        ),
    )
    conn.commit()


def upsert_rent_comp(conn: sqlite3.Connection, comp: RentComp) -> None:
    # La colonne indexee `city` recoit la forme canonique pour fusionner les
    # cohortes; la valeur brute reste dans le payload JSON.
    conn.execute(
        "INSERT OR REPLACE INTO rent_comps "
        "(source, source_id, url, fetched_at, city, bedrooms, monthly_rent, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            comp.source,
            comp.source_id,
            str(comp.url),
            comp.fetched_at.isoformat(),
            normalize_city(comp.city),
            comp.bedrooms,
            comp.monthly_rent,
            comp.model_dump_json(),
        ),
    )
    conn.commit()


def renormalize_cities(conn: sqlite3.Connection) -> int:
    """Re-applique normalize_city() sur la colonne `city` des rent_comps
    a partir du raw stocke dans le payload JSON. Retourne le nb de lignes touchees."""
    import json
    rows = conn.execute(
        "SELECT source, source_id, payload FROM rent_comps"
    ).fetchall()
    updated = 0
    for source, source_id, payload in rows:
        try:
            raw_city = json.loads(payload).get("city")
        except (json.JSONDecodeError, AttributeError):
            continue
        canon = normalize_city(raw_city)
        cur = conn.execute(
            "UPDATE rent_comps SET city = ? WHERE source = ? AND source_id = ?",
            (canon, source, source_id),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


def save_verdict(conn: sqlite3.Connection, source: str, verdict: ScreenVerdict) -> None:
    conn.execute(
        "INSERT INTO verdicts (source, source_id, run_at, passes, score, payload, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            verdict.listing_source_id,
            datetime.now(timezone.utc).isoformat(),
            1 if verdict.status == "pass" else 0,
            verdict.score,
            verdict.model_dump_json(),
            verdict.status,
        ),
    )
    conn.commit()
