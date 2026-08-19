"""Push notifications via ntfy.sh pour les Lepine passers + baisses de prix.

Scanne la DB pour deux types d'evenements:
  1. NEW: annonce active passant Lepine (`status in {pass, pass_partial}`) qui
     n'a jamais ete notifiee (`notified_at IS NULL`).
  2. DROP: annonce passante dont le prix a baisse de >= `drop_threshold_pct`
     vs. la derniere valeur notifiee (`last_price_notified`).

Une fois notifiee, une annonce est gated par (`notified_at`, `last_price_notified`).
Un `crawl --full` ulterieur qui la ressuscite ne re-notifie pas.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx

from .config import LepineCriteria, LocationFilter, NotifyConfig
from .geo import haversine_km
from .lepine import screen
from .models import Listing

NotificationKind = Literal["new", "drop"]

# propriodirect syndique ses annonces vers Centris sous le MEME identifiant
# numerique. Deux lignes (source, source_id) distinctes decrivent alors le meme
# immeuble physique. On les dedoublonne au moment de notifier via une cle
# (source_id, ville normalisee) — la ville sert de garde-fou contre une
# hypothetique collision d'ID entre deux annonces non liees. Quand plusieurs
# jumeaux sont candidats, on prefere Centris (donnees plus completes).
_SOURCE_PRIORITY = {"centris": 0}


def _phys_key(source_id: str, city: str | None) -> tuple[str, str]:
    """Cle d'identite physique pour collapser les cross-postings pd/centris."""
    return (source_id, re.sub(r"[^0-9a-z]", "", (city or "").lower()))


@dataclass
class Notification:
    kind: NotificationKind
    listing: Listing
    title: str
    body: str
    priority: str  # ntfy: min/low/default/high/urgent
    tags: list[str]
    click_url: str
    # Prix a stocker dans last_price_notified apres envoi reussi.
    price_to_record: float


def _format_new(listing: Listing, verdict) -> Notification:
    m = verdict.metrics
    status_emoji = "✅" if verdict.status == "pass" else "⚠️"
    label_status = "Pass" if verdict.status == "pass" else "Partiel"
    units = listing.units or "?"
    city = listing.city or "?"
    price = f"{listing.asking_price:,.0f}$" if listing.asking_price else "?"
    title = f"{status_emoji} Nouveau Lépine ({label_status}) · {city} {units}-plex {price}"
    parts = []
    if m.mrb:
        parts.append(f"MRB {m.mrb:.1f}")
    if m.price_per_door:
        parts.append(f"{m.price_per_door/1000:,.0f}k$/porte")
    if m.estimated_cashflow_per_door_month is not None:
        parts.append(f"CF {m.estimated_cashflow_per_door_month:,.0f}$/porte/mois")
    if m.price_to_eval:
        parts.append(f"prix/éval {m.price_to_eval:.2f}")
    body = " · ".join(parts) if parts else "Métriques indisponibles"
    return Notification(
        kind="new",
        listing=listing,
        title=title,
        body=body,
        priority="default",
        tags=["house", listing.source, verdict.status],
        click_url=str(listing.url),
        price_to_record=listing.asking_price or 0.0,
    )


def _format_drop(
    listing: Listing, verdict, previous_price: float, current_price: float
) -> Notification:
    drop_pct = (previous_price - current_price) / previous_price * 100
    drop_abs = previous_price - current_price
    units = listing.units or "?"
    city = listing.city or "?"
    title = (
        f"📉 Baisse {drop_pct:.1f}% · {city} {units}-plex "
        f"{current_price:,.0f}$"
    )
    m = verdict.metrics
    parts = [f"{previous_price:,.0f}$ → {current_price:,.0f}$ ({drop_abs:+,.0f}$)"]
    if m.mrb:
        parts.append(f"MRB {m.mrb:.1f}")
    if m.price_to_eval:
        parts.append(f"prix/éval {m.price_to_eval:.2f}")
    parts.append("passe Lépine" if verdict.status == "pass" else "Lépine partiel")
    body = " · ".join(parts)
    priority = "high" if drop_pct >= 5.0 else "default"
    return Notification(
        kind="drop",
        listing=listing,
        title=title,
        body=body,
        priority=priority,
        tags=["chart_with_downwards_trend", listing.source, verdict.status],
        click_url=str(listing.url),
        price_to_record=current_price,
    )


def scan(
    conn: sqlite3.Connection,
    *,
    criteria: LepineCriteria | None = None,
    loc: LocationFilter | None = None,
    notify_cfg: NotifyConfig | None = None,
    max_km: float | None = None,
    include_partial: bool = True,
) -> list[Notification]:
    """Balaye les listings actifs et retourne les notifications a envoyer.

    Ne modifie pas la DB — c'est `mark_notified()` qui persiste apres envoi reussi.
    """
    criteria = criteria or LepineCriteria()
    loc = loc or LocationFilter()
    notify_cfg = notify_cfg or NotifyConfig()
    max_km = max_km if max_km is not None else loc.max_km

    rows = conn.execute(
        "SELECT source, source_id, payload, notified_at, last_price_notified "
        "FROM listings WHERE is_active = 1"
    ).fetchall()

    parsed = []
    for source, source_id, payload, notified_at, last_price_notified in rows:
        try:
            listing = Listing.model_validate_json(payload)
        except Exception:
            continue
        parsed.append((listing, source_id, notified_at, last_price_notified))

    # On prefere Centris quand deux jumeaux sont tous deux candidats: en le
    # placant en tete, c'est lui qui remplit `seen_keys` en premier.
    parsed.sort(key=lambda p: _SOURCE_PRIORITY.get(p[0].source, 1))

    # Cles d'immeubles deja notifiees sous n'importe quelle source: un jumeau
    # cross-poste (pd/centris partagent source_id) ne doit pas re-alerter.
    notified_keys = {
        _phys_key(sid, listing.city)
        for (listing, sid, notified_at, _) in parsed
        if notified_at is not None
    }

    notifications: list[Notification] = []
    seen_keys: set[tuple[str, str]] = set()
    passing_statuses = {"pass", "pass_partial"} if include_partial else {"pass"}

    for listing, source_id, notified_at, last_price_notified in parsed:
        # Filtre distance (meme convention que run/value).
        if max_km > 0:
            if listing.lat is None or listing.lon is None:
                continue
            if haversine_km(loc.home_lat, loc.home_lon, listing.lat, listing.lon) > max_km:
                continue

        verdict = screen(listing, criteria)
        if verdict.status not in passing_statuses:
            continue

        key = _phys_key(source_id, listing.city)

        if notified_at is None:
            # NEW — jamais notifiee. On saute si un jumeau a deja ete notifie
            # (autre source) ou deja emis dans ce meme scan.
            if key in notified_keys or key in seen_keys:
                continue
            seen_keys.add(key)
            notifications.append(_format_new(listing, verdict))
        elif (
            last_price_notified is not None
            and listing.asking_price is not None
            and listing.asking_price < last_price_notified
        ):
            drop_pct = (last_price_notified - listing.asking_price) / last_price_notified * 100
            if drop_pct >= notify_cfg.drop_threshold_pct:
                # Un jumeau peut aussi baisser: ne pas dupliquer le drop.
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                notifications.append(_format_drop(
                    listing, verdict, last_price_notified, listing.asking_price,
                ))
    return notifications


# ntfy: les priorites nommees ne sont valides qu'en header; l'API JSON attend
# un entier 1-5. On passe par le JSON pour supporter l'UTF-8 (accents + emoji)
# dans le titre, qu'un header HTTP (latin-1 uniquement) ne peut pas transporter.
_PRIORITY_TO_INT = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def send(
    notif: Notification, topic: str, base_url: str = "https://ntfy.sh",
    client: httpx.Client | None = None,
) -> None:
    """Envoie via l'API JSON de ntfy.sh (UTF-8 natif). Leve en cas d'echec."""
    _client = client or httpx.Client(timeout=15)
    try:
        payload = {
            "topic": topic,
            "title": notif.title,
            "message": notif.body,
            "priority": _PRIORITY_TO_INT.get(notif.priority, 3),
            "tags": notif.tags,
            "click": notif.click_url,
        }
        r = _client.post(base_url, json=payload)
        r.raise_for_status()
    finally:
        if client is None:
            _client.close()


def mark_notified(conn: sqlite3.Connection, notif: Notification) -> None:
    """Persiste la notification comme envoyee — apres send() reussi.

    Marque aussi tout jumeau cross-poste (meme `source_id`, autre source — le
    meme immeuble syndique sur l'autre portail) pour qu'il ne re-alerte jamais.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE listings SET notified_at = ?, last_price_notified = ? "
        "WHERE source_id = ?",
        (now, notif.price_to_record, notif.listing.source_id),
    )
    conn.commit()


def scan_and_send(
    conn: sqlite3.Connection, topic: str, *,
    notify_cfg: NotifyConfig | None = None,
    max_km: float | None = None,
    include_partial: bool = True,
    throttle_s: float = 0.2,
    dry_run: bool = False,
) -> tuple[list[Notification], list[tuple[Notification, Exception]]]:
    """Scan + envoi + persistance. Retourne (envoyees, erreurs)."""
    notify_cfg = notify_cfg or NotifyConfig()
    notifs = scan(
        conn, notify_cfg=notify_cfg, max_km=max_km, include_partial=include_partial,
    )
    if dry_run:
        return notifs, []
    sent: list[Notification] = []
    errors: list[tuple[Notification, Exception]] = []
    with httpx.Client(timeout=15) as client:
        for i, n in enumerate(notifs):
            if i > 0 and throttle_s > 0:
                time.sleep(throttle_s)
            try:
                send(n, topic, base_url=notify_cfg.base_url, client=client)
                mark_notified(conn, n)
                sent.append(n)
            except Exception as e:
                errors.append((n, e))
    return sent, errors
