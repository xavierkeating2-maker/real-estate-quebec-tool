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
    last_seen_at TEXT,               -- touche a chaque crawl (mark-and-sweep pour pruning)
    is_active INTEGER DEFAULT 1,     -- 0 = soft-deleted (retiree du marche)
    notified_at TEXT,                -- ISO timestamp de la notif Lepine envoyee (evite double-push)
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
CREATE TABLE IF NOT EXISTS price_history (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    asking_price REAL NOT NULL,
    PRIMARY KEY (source, source_id, seen_at)
);
CREATE INDEX IF NOT EXISTS idx_price_history_lookup ON price_history(source, source_id, seen_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # Migrations ad-hoc: CREATE TABLE IF NOT EXISTS ne modifie pas les tables existantes.
    verdicts_cols = [r[1] for r in conn.execute("PRAGMA table_info(verdicts)").fetchall()]
    if "status" not in verdicts_cols:
        conn.execute("ALTER TABLE verdicts ADD COLUMN status TEXT")
    listings_cols = [r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()]
    if "last_seen_at" not in listings_cols:
        conn.execute("ALTER TABLE listings ADD COLUMN last_seen_at TEXT")
    if "is_active" not in listings_cols:
        conn.execute("ALTER TABLE listings ADD COLUMN is_active INTEGER DEFAULT 1")
        # Populer les listings existants comme actifs
        conn.execute("UPDATE listings SET is_active = 1 WHERE is_active IS NULL")
    if "notified_at" not in listings_cols:
        conn.execute("ALTER TABLE listings ADD COLUMN notified_at TEXT")
    if "last_price_notified" not in listings_cols:
        conn.execute("ALTER TABLE listings ADD COLUMN last_price_notified REAL")
    # Seed one-shot: si price_history est vide mais qu'on a des listings, insere le prix
    # actuel de chaque annonce comme baseline. Utilise fetched_at comme timestamp (approx
    # de la premiere observation). Idempotent — ne s'execute que quand la table est vide.
    ph_empty = conn.execute("SELECT NOT EXISTS(SELECT 1 FROM price_history)").fetchone()[0]
    listings_present = conn.execute("SELECT EXISTS(SELECT 1 FROM listings)").fetchone()[0]
    if ph_empty and listings_present:
        conn.execute(
            """
            INSERT OR IGNORE INTO price_history (source, source_id, seen_at, asking_price)
            SELECT source, source_id, fetched_at,
                   CAST(json_extract(payload, '$.asking_price') AS REAL)
            FROM listings
            WHERE json_extract(payload, '$.asking_price') IS NOT NULL
            """
        )
    conn.commit()
    return conn


def upsert_listing(conn: sqlite3.Connection, listing: Listing) -> None:
    """Insert ou mise a jour d'une annonce. Touche last_seen_at et remet is_active=1
    (une annonce ressuscitee apres un pruning garde son historique).
    Preserve notified_at existant pour ne pas re-notifier.
    Enregistre un point dans price_history si le prix demande a change."""
    now = datetime.now(timezone.utc).isoformat()
    _record_price_if_changed(conn, listing, now)
    conn.execute(
        """
        INSERT INTO listings (source, source_id, url, fetched_at, payload,
                              last_seen_at, is_active, notified_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, NULL)
        ON CONFLICT(source, source_id) DO UPDATE SET
            url          = excluded.url,
            fetched_at   = excluded.fetched_at,
            payload      = excluded.payload,
            last_seen_at = excluded.last_seen_at,
            is_active    = 1
        """,
        (
            listing.source,
            listing.source_id,
            str(listing.url),
            listing.fetched_at.isoformat(),
            listing.model_dump_json(),
            now,
        ),
    )
    conn.commit()


def _record_price_if_changed(
    conn: sqlite3.Connection, listing: Listing, now: str
) -> None:
    """Insere une ligne dans price_history si asking_price differe de la derniere
    observation. Silencieux quand le prix est None ou identique."""
    if listing.asking_price is None:
        return
    last = conn.execute(
        "SELECT asking_price FROM price_history "
        "WHERE source = ? AND source_id = ? "
        "ORDER BY seen_at DESC LIMIT 1",
        (listing.source, listing.source_id),
    ).fetchone()
    if last is not None and last[0] == listing.asking_price:
        return
    conn.execute(
        "INSERT OR IGNORE INTO price_history "
        "(source, source_id, seen_at, asking_price) VALUES (?, ?, ?, ?)",
        (listing.source, listing.source_id, now, listing.asking_price),
    )


def price_history(
    conn: sqlite3.Connection, source: str, source_id: str
) -> list[tuple[str, float]]:
    """Retourne la timeline des prix pour une annonce, du plus ancien au plus recent."""
    return conn.execute(
        "SELECT seen_at, asking_price FROM price_history "
        "WHERE source = ? AND source_id = ? ORDER BY seen_at",
        (source, source_id),
    ).fetchall()


def recent_price_drops(
    conn: sqlite3.Connection,
    since_days: int = 7,
    min_drop_pct: float = 0.0,
    active_only: bool = True,
) -> list[dict]:
    """Retourne les annonces avec une baisse de prix dans la fenetre (par defaut 7j).

    Utilise LAG() pour comparer chaque prix contre l'observation precedente puis
    filtre sur (1) la baisse est recente, (2) c'est la derniere observation pour
    cette annonce, (3) la baisse depasse le seuil min_drop_pct.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    active_filter = "AND l.is_active = 1" if active_only else ""
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT source, source_id, seen_at, asking_price,
                   LAG(asking_price) OVER (
                       PARTITION BY source, source_id ORDER BY seen_at
                   ) AS prev_price,
                   LAG(seen_at) OVER (
                       PARTITION BY source, source_id ORDER BY seen_at
                   ) AS prev_seen,
                   ROW_NUMBER() OVER (
                       PARTITION BY source, source_id ORDER BY seen_at DESC
                   ) AS rn
            FROM price_history
        )
        SELECT r.source, r.source_id, r.asking_price AS current_price,
               r.seen_at AS current_seen, r.prev_price, r.prev_seen
        FROM ranked r
        JOIN listings l ON l.source = r.source AND l.source_id = r.source_id
        WHERE r.rn = 1
          AND r.prev_price IS NOT NULL
          AND r.asking_price < r.prev_price
          AND r.seen_at >= ?
          {active_filter}
        ORDER BY (r.prev_price - r.asking_price) / r.prev_price DESC
        """,
        (cutoff,),
    ).fetchall()
    results = []
    for src, sid, curr, curr_seen, prev, prev_seen in rows:
        drop_pct = (prev - curr) / prev * 100
        if drop_pct < min_drop_pct:
            continue
        results.append({
            "source": src,
            "source_id": sid,
            "current_price": curr,
            "current_seen": curr_seen,
            "previous_price": prev,
            "previous_seen": prev_seen,
            "drop_pct": drop_pct,
            "drop_abs": prev - curr,
        })
    return results


def mark_seen(conn: sqlite3.Connection, source: str, source_id: str) -> None:
    """Touche last_seen_at + reactive une annonce sans re-parser le payload.
    Utilise quand on voit une annonce dans une page de recherche mais qu'on ne
    veut pas re-fetcher le detail (cache hit)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE listings SET last_seen_at = ?, is_active = 1 "
        "WHERE source = ? AND source_id = ?",
        (now, source, source_id),
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
