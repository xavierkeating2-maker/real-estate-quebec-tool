from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import centris, duproprio, kijiji, llm_extract, logisquebec, market, notify as notify_mod, proprio_direct, registre_foncier, regions as regions_mod, schl, storage
from .analyzer import DealInputs, analyze, estimate_expense_ratio
from .config import LepineCriteria, LocationFilter, NotifyConfig
from .geo import haversine_km
from .lepine import compute_metrics, screen
from .models import Listing
from rich.panel import Panel

SOURCES = {
    "duproprio": duproprio,
    "propriodirect": proprio_direct,
    "centris": centris,
}
RENT_SOURCES = {
    "kijiji": kijiji,
    "logisquebec": logisquebec,
}

app = typer.Typer(help="Screener multilogement Quebec (methode Lepine)")
console = Console()

DEFAULT_DB = Path("data/screener.db")


FULL_CRAWL_MAX_PAGES = 200  # sentinelle — chaque source s'arrete auto sur page vide


@app.command()
def crawl(
    source: str = typer.Option("all", help=f"Source: {', '.join(SOURCES)} ou 'all'"),
    max_pages: int = typer.Option(5, help="Pages a parcourir par source (ignore si --full)"),
    full: bool = typer.Option(
        False, "--full",
        help="Walk the FULL catalog (auto-stop sur page vide). Requis pour un pruning fiable.",
    ),
    region: str = typer.Option(None, help="Filtre region (utilise par DuProprio uniquement pour l'instant)"),
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
) -> None:
    """Telecharge les annonces multilogement dans la base.

    Chaque annonce vue est marquee `last_seen_at = now()` et `is_active = 1`.
    Le pruning (commande separee) marquera comme retirees les annonces non
    revues depuis N jours — pour que ce signal soit fiable, faire un
    `crawl --full` a intervalle regulier (nightly).
    """
    conn = storage.connect(db)
    pages = FULL_CRAWL_MAX_PAGES if full else max_pages
    targets = list(SOURCES) if source == "all" else [source]
    for src in targets:
        if src not in SOURCES:
            console.print(f"[red]Source inconnue: {src}[/red]")
            raise typer.Exit(1)
        mod = SOURCES[src]
        console.print(f"[bold cyan]==> {src}[/bold cyan]"
                      + (f"  [dim](FULL walk, max {pages} pages)[/dim]" if full else ""))
        count = 0
        for listing in mod.crawl_listings(max_pages=pages, region=region):
            storage.upsert_listing(conn, listing)
            count += 1
            console.print(f"[green]ok[/green] {src}/{listing.source_id}  "
                          f"{(listing.title or '')[:70]}")
        console.print(f"[dim]{src}: {count} annonces stockees[/dim]")


@app.command()
def prune(
    days: int = typer.Option(7, help="Marque comme retirees les annonces non revues depuis N jours."),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Affiche ce qui serait retire sans rien modifier.",
    ),
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
) -> None:
    """Soft-delete des annonces disparues (last_seen_at < now - N jours).

    Utilise le signal `last_seen_at` stampe par `crawl` (particulierement `crawl --full`).
    Une annonce reactivee par un crawl ulterieur reprendra is_active=1 automatiquement
    (voir storage.upsert_listing).

    Garde-fou: refuse si >50% des annonces actives ont last_seen_at=NULL — signale
    qu'aucun crawl --full n'a encore tourne apres la migration du schema.
    """
    from datetime import timedelta

    conn = storage.connect(db)
    total_active = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE is_active = 1"
    ).fetchone()[0]
    null_seen = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE is_active = 1 AND last_seen_at IS NULL"
    ).fetchone()[0]

    if total_active == 0:
        console.print("[yellow]Aucune annonce active — rien a faire.[/yellow]")
        raise typer.Exit(0)
    if null_seen / total_active > 0.5:
        console.print(
            f"[red]Garde-fou:[/red] {null_seen}/{total_active} annonces actives n'ont "
            f"pas de last_seen_at. Lancer `crawl --source all --full` d'abord."
        )
        raise typer.Exit(1)

    # Garde-fou #2: le crawl lui-meme est-il recent? Si le max(last_seen_at) date
    # d'avant la fenetre `--days`, le pruning est probablement en train de refleter
    # un catalogue perime plutot que des annonces vraiment disparues.
    now = datetime.now(timezone.utc)
    latest_seen_str = conn.execute(
        "SELECT MAX(last_seen_at) FROM listings WHERE is_active = 1"
    ).fetchone()[0]
    if latest_seen_str:
        latest_seen = datetime.fromisoformat(latest_seen_str)
        crawl_age_days = (now - latest_seen).total_seconds() / 86400
        if crawl_age_days > days and not dry_run:
            console.print(
                f"[red]Garde-fou:[/red] le crawl le plus recent date d'il y a "
                f"{crawl_age_days:.1f}j (> {days}j). Relancer `crawl --source all --full` "
                f"avant de prune, sinon le catalogue entier serait retire.\n"
                f"[dim]Utiliser `--dry-run` pour voir les candidats sans appliquer.[/dim]"
            )
            raise typer.Exit(1)

    cutoff = (now - timedelta(days=days)).isoformat()

    # Detail par source pour le rapport
    per_source = conn.execute(
        """
        SELECT source, COUNT(*)
        FROM listings
        WHERE is_active = 1
          AND (last_seen_at IS NULL OR last_seen_at < ?)
        GROUP BY source
        ORDER BY source
        """,
        (cutoff,),
    ).fetchall()

    total_to_prune = sum(n for _, n in per_source)

    table = Table(title=f"Candidats prune (non vus depuis {days}j, avant {cutoff[:10]})")
    for col in ("Source", "A retirer", "Actives avant", "Restantes"):
        table.add_column(col, justify="right")
    for src, n in per_source:
        active_before = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE is_active = 1 AND source = ?", (src,)
        ).fetchone()[0]
        table.add_row(src, str(n), str(active_before), str(active_before - n))
    console.print(table)

    if total_to_prune == 0:
        console.print("[green]Rien a retirer — catalogue frais.[/green]")
        raise typer.Exit(0)

    if dry_run:
        console.print(
            f"\n[cyan]DRY-RUN:[/cyan] {total_to_prune} annonces seraient retirees. "
            f"Relancer sans `--dry-run` pour appliquer."
        )
        raise typer.Exit(0)

    cur = conn.execute(
        """
        UPDATE listings
        SET is_active = 0
        WHERE is_active = 1
          AND (last_seen_at IS NULL OR last_seen_at < ?)
        """,
        (cutoff,),
    )
    conn.commit()
    console.print(
        f"\n[bold green]{cur.rowcount}[/bold green] annonces marquees inactives. "
        f"Un futur `crawl --full` les reactivera si elles reviennent."
    )


@app.command()
def run(
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
    top: int = typer.Option(15, help="Limite d'affichage (annonces triees par score)"),
    min_checks: int = typer.Option(2, help="Min de checks evaluables pour afficher"),
    max_km: float = typer.Option(
        LocationFilter().max_km,
        help="Distance max (km, vol d'oiseau) depuis la maison configuree dans config.py. 0 = pas de filtre.",
    ),
    include_inactive: bool = typer.Option(
        False, "--include-inactive",
        help="Inclut les annonces soft-deletees (retirees par `prune`).",
    ),
) -> None:
    """Applique les filtres Lepine; affiche les meilleures annonces (tri score desc)."""
    criteria = LepineCriteria()
    loc = LocationFilter()
    conn = storage.connect(db)
    where = "" if include_inactive else " WHERE is_active = 1"
    rows = conn.execute(f"SELECT payload FROM listings{where}").fetchall()
    if not rows:
        console.print("[yellow]Aucune annonce. Faire `crawl` d'abord.[/yellow]")
        raise typer.Exit(1)

    results = []
    passed = 0
    n_skipped_distance = 0
    for (payload,) in rows:
        listing = Listing.model_validate_json(payload)
        if max_km > 0 and listing.lat is not None and listing.lon is not None:
            dist = haversine_km(loc.home_lat, loc.home_lon, listing.lat, listing.lon)
            if dist > max_km:
                n_skipped_distance += 1
                continue
        elif max_km > 0:
            # Pas de coords → exclus quand le filtre distance est actif.
            n_skipped_distance += 1
            continue
        verdict = screen(listing, criteria)
        storage.save_verdict(conn, listing.source, verdict)
        n_decided = sum(1 for v in verdict.checks.values() if v is not None)
        if verdict.status == "pass":
            passed += 1
        results.append((listing, verdict, n_decided))

    # Tri: score desc, puis nombre de checks evaluables desc (= verdict plus fiable).
    results.sort(key=lambda r: (r[1].score, r[2]), reverse=True)
    shown = [r for r in results if r[2] >= min_checks][:top]

    status_badge = {
        "pass":         "[green]✓ oui[/green]",
        "pass_partial": "[yellow]~ partiel[/yellow]",
        "fail":         "[red]✗ non[/red]",
    }
    table = Table(title=f"Screener Lepine (top {len(shown)}/{len(rows)})")
    for col in ("ID", "Logts", "Prix", "MRB", "$/porte", "CF/porte", "Eval%", "Score", "Statut"):
        table.add_column(col)
    for listing, verdict, n in shown:
        m = verdict.metrics
        table.add_row(
            listing.source_id,
            str(listing.units or "-"),
            f"{listing.asking_price:,.0f}" if listing.asking_price else "-",
            f"{m.mrb:.1f}" if m.mrb else "-",
            f"{m.price_per_door:,.0f}" if m.price_per_door else "-",
            f"{m.estimated_cashflow_per_door_month:,.0f}"
            if m.estimated_cashflow_per_door_month is not None else "-",
            f"{m.price_to_eval:.2f}" if m.price_to_eval else "-",
            f"{verdict.score:.2f}({n})",
            status_badge[verdict.status],
        )
    console.print(table)
    n_partial = sum(1 for _, v, _ in results if v.status == "pass_partial")
    suffix = ""
    if max_km > 0 and n_skipped_distance:
        suffix = f" ({n_skipped_distance} hors rayon {max_km:.0f}km)"
    console.print(
        f"\n[bold green]{passed}[/bold green] passent / "
        f"[bold yellow]{n_partial}[/bold yellow] partiels (revenu non divulgue) "
        f"/ [dim]{len(rows) - n_skipped_distance}[/dim] analyses{suffix}. "
        f"Score format: pct-pass(nb-checks-evaluables)."
    )


@app.command()
def value(
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
    percentile: float = typer.Option(5.0, help="Percentile du bas a montrer (defaut 5%)"),
    top: int = typer.Option(0, help="Override le percentile et montre N annonces"),
    min_units: int = typer.Option(2, help="Min logements"),
    no_macro: bool = typer.Option(False, help="Desactive la ponderation par signal macro regional"),
    yoy_weight: float = typer.Option(0.02, help="Poids du YoY transferts (par %point)"),
    distress_weight: float = typer.Option(1.0, help="Poids du ratio distress (en fraction)"),
    max_km: float = typer.Option(
        LocationFilter().max_km,
        help="Distance max (km, vol d'oiseau) depuis la maison. 0 = pas de filtre.",
    ),
    include_inactive: bool = typer.Option(
        False, "--include-inactive",
        help="Inclut les annonces soft-deletees (retirees par `prune`).",
    ),
) -> None:
    """Classe les annonces par ratio prix/evaluation municipale ascendant.

    Par defaut: pondere le score par les signaux macro regionaux (Registre
    foncier QC). Les regions avec un ratio distress eleve et un YoY transferts
    bas obtiennent un coup de pouce.
    """
    criteria = LepineCriteria()
    loc = LocationFilter()
    conn = storage.connect(db)
    where = "" if include_inactive else " WHERE is_active = 1"
    rows = conn.execute(f"SELECT payload FROM listings{where}").fetchall()

    macro: dict[str, dict] = {}
    if not no_macro:
        try:
            for r in registre_foncier.region_stats(months=12):
                macro[r["region"]] = r
        except Exception as e:
            console.print(f"[yellow]Macro non dispo ({e}). Lance `macro refresh`.[/yellow]")
            no_macro = True

    ranked = []
    for (payload,) in rows:
        listing = Listing.model_validate_json(payload)
        if listing.units and listing.units < min_units:
            continue
        if max_km > 0:
            if listing.lat is None or listing.lon is None:
                continue
            if haversine_km(loc.home_lat, loc.home_lon, listing.lat, listing.lon) > max_km:
                continue
        m = compute_metrics(listing, criteria)
        if m.price_to_eval is None:
            continue
        canonical_region = regions_mod.normalize_region(listing.region, listing.city)
        macro_row = macro.get(canonical_region) if canonical_region else None
        distress = (macro_row or {}).get("ratio_distress") or 0.0
        yoy = (macro_row or {}).get("transferts_yoy_pct") or 0.0
        if no_macro or macro_row is None:
            weighted = m.price_to_eval
        else:
            multiplier = 1.0 + yoy_weight * yoy - distress_weight * distress
            multiplier = max(0.1, multiplier)
            weighted = m.price_to_eval * multiplier
        ranked.append((listing, m, canonical_region, distress, yoy, weighted))

    if not ranked:
        console.print("[yellow]Aucune annonce avec prix ET evaluation municipale.[/yellow]")
        raise typer.Exit(1)

    ranked.sort(key=lambda x: x[5])
    n_total = len(ranked)
    n_show = top if top > 0 else max(3, round(n_total * percentile / 100))

    title = (
        f"Top {n_show} (bottom {percentile}%) "
        + ("score pondere macro" if not no_macro else "prix/eval brut")
        + f" — {n_total} annonces"
    )
    table = Table(title=title)
    cols = ["ID", "Ville", "Région", "Logts", "Prix", "Prix/Éval", "MRB"]
    if not no_macro:
        cols += ["Distress", "YoY", "Score pondere"]
    for col in cols:
        table.add_column(col)
    for listing, m, region, distress, yoy, weighted in ranked[:n_show]:
        row = [
            listing.source_id,
            (listing.city or "-")[:18],
            (region or "—")[:16],
            str(listing.units or "-"),
            f"{listing.asking_price:,.0f}",
            f"{m.price_to_eval:.2f}",
            f"{m.mrb:.1f}" if m.mrb else "-",
        ]
        if not no_macro:
            row += [
                f"{distress*100:.2f}%" if distress else "—",
                f"{yoy:+.1f}%" if yoy else "—",
                f"{weighted:.2f}",
            ]
        table.add_row(*row)
    console.print(table)


@app.command()
def extract(
    source_id: str = typer.Argument(None, help="ID d'une annonce specifique"),
    all_: bool = typer.Option(False, "--all", help="Traiter toutes les annonces avec description"),
    source: str = typer.Option(None, help="Filtre par source (duproprio/propriodirect/centris)"),
    limit: int = typer.Option(0, help="Max d'annonces a traiter en batch (0 = illimite)"),
    refresh: bool = typer.Option(False, help="Bypasse le cache (re-extrait meme si deja fait)"),
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
) -> None:
    """Extrait les champs structures depuis la description via LLM (Claude Haiku)."""
    import json as json_mod
    conn = storage.connect(db)

    if source_id:
        rows = conn.execute(
            "SELECT source, source_id, payload FROM listings WHERE source_id=?",
            (source_id,),
        ).fetchall()
    elif all_:
        where = ""
        params: tuple = ()
        if source:
            where = "WHERE source=?"
            params = (source,)
        sql = f"SELECT source, source_id, payload FROM listings {where}"
        rows = conn.execute(sql, params).fetchall()
    else:
        console.print("[red]Utiliser --all ou specifier un source_id.[/red]")
        raise typer.Exit(1)

    targets = []
    skipped_short = 0
    skipped_done = 0
    for src, sid, payload in rows:
        data = json_mod.loads(payload)
        desc = data.get("description")
        if not desc or len(desc) < 50:
            skipped_short += 1
            continue
        if not refresh and data.get("extracted_at"):
            skipped_done += 1
            continue
        targets.append((src, sid, data, desc))

    if limit > 0:
        targets = targets[:limit]
    console.print(
        f"À traiter: [bold]{len(targets)}[/bold]  "
        f"(deja extraits: {skipped_done}, description trop courte: {skipped_short})"
    )

    try:
        client = llm_extract._client()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    ok = err = 0
    for src, sid, data, desc in targets:
        try:
            extracted = llm_extract.extract(desc, units_hint=data.get("units"), client=client,
                                            use_cache=not refresh)
        except Exception as e:
            console.print(f"[red]echec[/red] {src}/{sid}: {e}")
            err += 1
            continue
        if not extracted:
            console.print(f"[yellow]vide[/yellow] {src}/{sid}")
            err += 1
            continue
        merged = llm_extract.apply_to_listing(data, extracted)
        conn.execute(
            "UPDATE listings SET payload=? WHERE source=? AND source_id=?",
            (json_mod.dumps(merged), src, sid),
        )
        conn.commit()
        rents = extracted.get("per_unit_rents") or []
        reno = len(extracted.get("renovations_done") or [])
        pot = extracted.get("rent_reset_potential") or "?"
        conf = extracted.get("confidence")
        rents_str = f"{rents}" if rents else "[]"
        console.print(
            f"[green]ok[/green] {src}/{sid}  rents={rents_str}  reno={reno}  "
            f"reset={pot}  conf={conf:.2f}"
        )
        ok += 1
    console.print(f"\n[bold]{ok}[/bold] OK · {err} erreurs")


@app.command()
def analyze_deal(
    source_id: str = typer.Argument(..., help="ID DuProprio (ex: 1133401)"),
    offer: float = typer.Option(0.0, help="Prix d'offre (defaut: prix demande)"),
    stabilized_revenue: float = typer.Option(0.0, help="Revenus stabilises annuels (defaut: marche ou annonce)"),
    unit_mix: str = typer.Option(None, help="Mix de chambres, ex '2,2,1,2'. Defaut: tous 2ch (4 1/2)."),
    no_market: bool = typer.Option(False, help="Desactive l'estimation par cohortes de loyer"),
    expenses: float = typer.Option(0.0, help="Depenses annuelles (defaut: 40% des revenus)"),
    capex: float = typer.Option(0.0, help="Renos initiales avant stabilisation"),
    down_pct: float = typer.Option(20.0, help="Mise de fonds %"),
    rate: float = typer.Option(5.5, help="Taux hypothecaire %"),
    am_years: int = typer.Option(25, help="Amortissement (annees)"),
    vtb_pct: float = typer.Option(0.0, help="Balance de vente % du prix d'achat"),
    vtb_rate: float = typer.Option(7.0, help="Taux balance de vente %"),
    appreciation: float = typer.Option(
        -1.0,
        help="Appreciation annuelle %. -1 = auto (regional Registre foncier), sinon override.",
    ),
    vacancy: float = typer.Option(
        -1.0,
        help="Taux d'inoccupation %. -1 = auto (SCHL par ville), sinon override.",
    ),
    rent_growth: float = typer.Option(
        -1.0,
        help="Croissance annuelle des loyers %. -1 = auto (SCHL CAGR 3a), sinon override.",
    ),
    expense_ratio: float = typer.Option(
        -1.0,
        help="Ratio depenses/revenus %. -1 = auto (Lepine tiere par nb logts + age), sinon override.",
    ),
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
) -> None:
    """Analyse complete d'un deal: TGA, cashflow, mise de fonds, projection 5 ans."""
    conn = storage.connect(db)
    row = conn.execute(
        "SELECT payload FROM listings WHERE source_id=?", (source_id,)
    ).fetchone()
    if not row:
        console.print(f"[red]Annonce {source_id} introuvable dans la base.[/red]")
        raise typer.Exit(1)

    listing = Listing.model_validate_json(row[0])
    if not listing.units:
        console.print("[red]Nombre de logements inconnu.[/red]")
        raise typer.Exit(1)

    # Priorite si pas de revenu fourni par l'utilisateur:
    #   (1) per_unit_rents extraits par LLM × 12  (donnees seller-specifiques)
    #   (2) cohorte de loyer du marche par ville × bedrooms
    #   (3) revenus annonces par le vendeur (annual_gross_revenue)
    llm_used = False
    if stabilized_revenue <= 0 and listing.per_unit_rents:
        llm_total = sum(listing.per_unit_rents) * 12
        if llm_total >= 5000:
            stabilized_revenue = llm_total
            llm_used = True
            console.print(
                f"[green]✓ Revenus extraits par LLM:[/green] "
                f"{listing.per_unit_rents} × 12 = {llm_total:,.0f} $/an "
                f"(confiance {listing.extraction_confidence or 0:.2f})"
            )

    # Auto-fill stabilized revenue from rent-comp cohort medians when caller didn't supply.
    market_revenue: float | None = None
    market_breakdown: list[tuple[int, float | None, int]] = []
    market_city: str | None = None
    market_used = False
    if stabilized_revenue <= 0 and not no_market:
        mix = (
            [int(x) for x in unit_mix.split(",")] if unit_mix
            else [2] * listing.units
        )
        if len(mix) != listing.units:
            console.print(f"[red]--unit-mix doit avoir {listing.units} valeurs[/red]")
            raise typer.Exit(1)
        market_revenue, market_breakdown, market_city = market.estimate_market_revenue(
            conn, listing.city, mix
        )
        if market_revenue:
            stabilized_revenue = market_revenue
            market_used = True

    known_taxes = None
    tax_total = (listing.municipal_tax or 0) + (listing.school_tax or 0)
    if tax_total > 0:
        known_taxes = tax_total

    # Appreciation regionale (Registre foncier) — override si l'utilisateur en fournit une.
    canonical_region = regions_mod.normalize_region(listing.region, listing.city)
    appreciation_source = "defaut 2.5%"
    if appreciation >= 0:
        appreciation_frac = appreciation / 100
        appreciation_source = f"override CLI ({appreciation:.1f}%)"
    else:
        try:
            regional = registre_foncier.region_appreciation_estimate(canonical_region)
        except Exception:
            regional = None
        if regional is not None:
            appreciation_frac = regional
            appreciation_source = f"Registre foncier {canonical_region} 12mo ({regional*100:.1f}%)"
        else:
            appreciation_frac = 0.025

    # Vacance SCHL par ville — override si l'utilisateur en fournit une.
    vacancy_source = "defaut 5.0%"
    if vacancy >= 0:
        vacancy_frac = vacancy / 100
        vacancy_source = f"override CLI ({vacancy:.1f}%)"
    else:
        try:
            v = schl.vacancy_rate_estimate(listing.city)
        except Exception:
            v = None
        if v is not None:
            vacancy_frac, vacancy_source = v
        else:
            vacancy_frac = 0.05

    # Croissance des loyers (SCHL CAGR 3a) — override si l'utilisateur en fournit un.
    rent_growth_source = "defaut 2.5%"
    if rent_growth >= 0:
        rent_growth_frac = rent_growth / 100
        rent_growth_source = f"override CLI ({rent_growth:.1f}%)"
    else:
        try:
            g = schl.rent_growth_estimate(listing.city)
        except Exception:
            g = None
        if g is not None:
            rent_growth_frac, rent_growth_source = g
        else:
            rent_growth_frac = 0.025

    # Ratio depenses/revenus (Lepine tiere + ajustement age) — override CLI si fourni.
    if expense_ratio >= 0:
        expense_ratio_frac = expense_ratio / 100
        expense_ratio_source = f"override CLI ({expense_ratio:.1f}%)"
    else:
        expense_ratio_frac, expense_ratio_source = estimate_expense_ratio(
            listing.units, listing.year_built
        )

    inputs = DealInputs(
        purchase_price=offer or listing.asking_price or 0.0,
        units=listing.units,
        gross_annual_revenue=stabilized_revenue or listing.annual_gross_revenue or 0.0,
        annual_expenses=expenses if expenses > 0 else None,
        known_taxes=known_taxes,
        down_payment_pct=down_pct / 100,
        mortgage_rate=rate / 100,
        amortization_years=am_years,
        vtb_pct=vtb_pct / 100,
        vtb_rate=vtb_rate / 100,
        initial_capex=capex,
        annual_appreciation=appreciation_frac,
        vacancy_rate=vacancy_frac,
        annual_rent_growth=rent_growth_frac,
        expense_ratio=expense_ratio_frac,
    )
    if inputs.purchase_price <= 0 or inputs.gross_annual_revenue <= 0:
        console.print("[red]Prix d'achat et revenus stabilises requis.[/red]")
        raise typer.Exit(1)

    a = analyze(inputs)

    # Panneau hypotheses
    listed_diff = ""
    if listing.asking_price and inputs.purchase_price != listing.asking_price:
        delta = inputs.purchase_price - listing.asking_price
        listed_diff = f"  (vs demande {listing.asking_price:,.0f} $, ecart {delta:+,.0f} $)"
    rev_diff = ""
    if listing.annual_gross_revenue and inputs.gross_annual_revenue != listing.annual_gross_revenue:
        rev_diff = f"  (vs annonce {listing.annual_gross_revenue:,.0f} $)"
    rev_label = "Revenus stab."
    if market_used:
        rev_label = f"Revenus marche"
    elif stabilized_revenue == 0 and listing.annual_gross_revenue:
        rev_label = "Revenus annonce"

    header = (
        f"[bold]{source_id}[/bold]  {listing.city or '?'}  ({listing.units} logts, "
        f"bati {listing.year_built or '?'})\n"
        f"Prix d'offre:    {inputs.purchase_price:>12,.0f} $   "
        f"[dim]{listed_diff}[/dim]\n"
        f"{rev_label}:   {inputs.gross_annual_revenue:>12,.0f} $   [dim]{rev_diff}[/dim]\n"
        f"Capex initial:   {inputs.initial_capex:>12,.0f} $\n"
        f"Financement:     MdF {down_pct:.0f}%  |  Hypo {rate:.2f}% am{am_years}"
        + (f"  |  BdV {vtb_pct:.0f}% @ {vtb_rate:.2f}%" if vtb_pct > 0 else "")
        + f"\nApreciation:     {appreciation_frac*100:>+5.2f} %/an  [dim]({appreciation_source})[/dim]"
        + f"\nVacance:         {vacancy_frac*100:>+5.2f} %/an  [dim]({vacancy_source})[/dim]"
        + f"\nLoyer +/an:      {rent_growth_frac*100:>+5.2f} %/an  [dim]({rent_growth_source})[/dim]"
        + f"\nDep./Revenus:    {expense_ratio_frac*100:>+5.2f} %    [dim]({expense_ratio_source})[/dim]"
    )
    console.print(Panel(header, title="Hypotheses", border_style="cyan"))

    if market_used or market_breakdown:
        # Ventilation du marche par logement
        lines = [f"Ville canonique: [bold]{market_city or '?'}[/bold]"]
        for i, (br, rent, n) in enumerate(market_breakdown, 1):
            if rent:
                lines.append(f"  Logt {i} ({br}ch): {rent:>7,.0f} $/mois  (n={n})")
            else:
                lines.append(f"  Logt {i} ({br}ch): [red]pas assez de comparables[/red] (n={n})")
        if market_used and listing.annual_gross_revenue:
            gap = market_revenue - listing.annual_gross_revenue
            pct = gap / listing.annual_gross_revenue * 100
            lines.append("")
            lines.append(
                f"Annonce: {listing.annual_gross_revenue:,.0f} $/an  "
                f"-> Marche: {market_revenue:,.0f} $/an  "
                f"({'+' if gap >= 0 else ''}{gap:,.0f} $, {pct:+.0f}%)"
            )
        console.print(Panel("\n".join(lines), title="Loyers de marche", border_style="magenta"))
    elif stabilized_revenue == 0 and not no_market:
        console.print("[yellow]Pas de cohorte de loyer pour cette ville+mix.[/yellow]")

    # Panneau capital
    cmhc_line = (
        f"Prime SCHL ({(a.cmhc_premium / (inputs.purchase_price * (1 - inputs.down_payment_pct - inputs.vtb_pct)) * 100):.1f}%): "
        f"{a.cmhc_premium:>12,.0f} $   [dim](roulee dans l'hypotheque)[/dim]\n"
        if a.cmhc_premium > 0 else ""
    )
    cash_breakdown = (
        f"Mise de fonds:           {inputs.purchase_price * inputs.down_payment_pct:>12,.0f} $\n"
        f"Taxe de bienvenue:       {a.welcome_tax:>12,.0f} $\n"
        f"Autres frais ({inputs.other_closing_pct*100:.1f}%):  {inputs.purchase_price * inputs.other_closing_pct:>12,.0f} $   "
        f"[dim](notaire, inspection)[/dim]\n"
        f"Capex initial:           {inputs.initial_capex:>12,.0f} $\n"
        f"[bold]Capital total requis:    {a.cash_invested:>12,.0f} $[/bold]\n\n"
        f"Hypotheque:              {a.mortgage_principal:>12,.0f} $   "
        f"(paiement annuel {a.annual_mortgage_payment:,.0f} $)\n"
        + cmhc_line
        + (f"Balance de vente:        {inputs.purchase_price * inputs.vtb_pct:>12,.0f} $   "
           f"(interets annuels {a.annual_vtb_payment:,.0f} $)\n" if inputs.vtb_pct > 0 else "")
    )
    console.print(Panel(cash_breakdown, title="Capital", border_style="yellow"))

    # Tableau annee par annee
    table = Table(title=f"Projection annuelle ({len(a.yearly)} ans)")
    table.add_column("An", justify="right")
    table.add_column("Revenus", justify="right")
    table.add_column("NOI", justify="right")
    table.add_column("TGA", justify="right")
    table.add_column("Cashflow", justify="right")
    table.add_column("$/porte/mois", justify="right")
    table.add_column("Valeur", justify="right")
    table.add_column("Avoir net", justify="right")
    for s in a.yearly:
        table.add_row(
            str(s.year),
            f"{s.effective_revenue:,.0f}",
            f"{s.noi:,.0f}",
            f"{s.cap_rate:.2%}",
            f"{s.annual_cashflow:,.0f}",
            f"{s.cashflow_per_door_month:,.0f}",
            f"{s.property_value:,.0f}",
            f"{s.equity:,.0f}",
        )
    console.print(table)

    # Panneau retours (avec IRR)
    irr_str = f"{a.irr_5_year:>7.2%}" if a.irr_5_year is not None else "n/a (cashflows monotones)"
    returns = (
        f"Cash-on-cash Annee 1:    {a.cash_on_cash_year_1:>7.2%}\n"
        f"Cashflow cumule 5 ans:   {a.five_year_cf_cumulative:>10,.0f} $\n"
        f"IRR 5 ans (incl. sortie): {irr_str}\n"
        f"Rendement total 5 ans:   {a.five_year_total_return:>7.2%}  "
        f"[dim](cashflow + gain d'avoir / capital investi)[/dim]"
    )
    console.print(Panel(returns, title="Retours", border_style="green"))


schl_app = typer.Typer(help="Vacance + croissance loyer (SCHL via StatCan)")
app.add_typer(schl_app, name="schl")


@schl_app.command("refresh")
def schl_refresh(force: bool = typer.Option(False, help="Force le re-telechargement")) -> None:
    """Telecharge les tables StatCan (vacance RMR + AR, loyers moyens)."""
    counts = schl.refresh(force=force)
    for tid, n in counts.items():
        console.print(f"[green]ok[/green] table {tid}: {n:,} lignes")
    last = schl.last_refresh_at()
    if last:
        console.print(f"[dim]Dernier refresh: {last}[/dim]")


@schl_app.command("lookup")
def schl_lookup(
    city: str = typer.Argument(..., help="Ville (ex 'Montréal', 'Rimouski')"),
) -> None:
    """Affiche la vacance et la croissance de loyer pour une ville."""
    v = schl.vacancy_rate_estimate(city)
    g = schl.rent_growth_estimate(city)
    if v:
        console.print(f"[cyan]Vacance:[/cyan]        {v[0]*100:.2f}%  ({v[1]})")
    else:
        console.print(f"[yellow]Vacance:[/yellow] aucune donnee (lancer `schl refresh`)")
    if g:
        console.print(f"[cyan]Croissance loyer:[/cyan] {g[0]*100:.2f}%/an  ({g[1]})")
    else:
        console.print(f"[yellow]Croissance loyer:[/yellow] aucune donnee")


macro_app = typer.Typer(help="Signaux macro régionaux (Registre foncier QC)")
app.add_typer(macro_app, name="macro")


@macro_app.command("refresh")
def macro_refresh(force: bool = typer.Option(False, help="Force le re-telechargement")) -> None:
    """Telecharge les CSV du Registre foncier (Donnees Quebec, CC-BY)."""
    counts = registre_foncier.refresh(force=force)
    for name, n in counts.items():
        console.print(f"[green]ok[/green] {name:<20} {n:,} lignes")


@macro_app.command("regions")
def macro_regions(
    months: int = typer.Option(12, help="Taille de la fenetre en mois"),
) -> None:
    """Affiche les signaux macro par region administrative."""
    stats = registre_foncier.region_stats(months=months)
    if not stats:
        console.print("[yellow]Pas de donnees. Lancer `qc-screener macro refresh`.[/yellow]")
        raise typer.Exit(1)
    fenetre = f"{stats[0]['fenetre_debut'] or '?'}  -->  {stats[0]['fenetre_fin'] or '?'}"
    table = Table(title=f"Signal macro region ({months} mois: {fenetre})")
    for col in ("Région", "Transferts", "YoY %", "Difficulté %", "% <250K", "% 250-500K", "% >500K"):
        table.add_column(col)
    stats_sorted = sorted(stats, key=lambda r: r["ratio_distress"] or 0, reverse=True)
    for r in stats_sorted:
        table.add_row(
            r["region"],
            f"{r['transferts_recents']:,}",
            f"{r['transferts_yoy_pct']:+.1f}%" if r["transferts_yoy_pct"] is not None else "—",
            f"{r['ratio_distress']*100:.2f}%" if r["ratio_distress"] is not None else "—",
            f"{r['share_band1']*100:.0f}%" if r["share_band1"] is not None else "—",
            f"{r['share_band2']*100:.0f}%" if r["share_band2"] is not None else "—",
            f"{r['share_band3']*100:.0f}%" if r["share_band3"] is not None else "—",
        )
    console.print(table)


rents_app = typer.Typer(help="Comparables de loyer (Kijiji)")
app.add_typer(rents_app, name="rents")


@rents_app.command("fetch")
def rents_fetch(
    source: str = typer.Option("kijiji", help=f"Source: {', '.join(RENT_SOURCES)}"),
    max_pages: int = typer.Option(3, help="Pages a parcourir (Kijiji)"),
    max_listings: int = typer.Option(300, help="Limite d'annonces (LogisQuebec)"),
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
) -> None:
    """Telecharge des annonces de location pour estimer les loyers du marche."""
    if source not in RENT_SOURCES:
        console.print(f"[red]Source inconnue: {source}[/red]")
        raise typer.Exit(1)
    conn = storage.connect(db)
    mod = RENT_SOURCES[source]
    kwargs = {"max_pages": max_pages}
    if source == "logisquebec":
        kwargs["max_listings"] = max_listings
    count = 0
    for comp in mod.crawl_rent_comps(**kwargs):
        storage.upsert_rent_comp(conn, comp)
        count += 1
        size = comp.size_label or "?"
        rent = f"{comp.monthly_rent:,.0f}$/mois" if comp.monthly_rent else "?"
        city = (comp.city or "?")[:25]
        console.print(f"[green]ok[/green] {comp.source_id}  {city:<25} {size:<8} {rent}")
    console.print(f"\n[bold]{count}[/bold] annonces stockees.")


@rents_app.command("renormalize")
def rents_renormalize(
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
) -> None:
    """Re-applique normalize_city() sur les comparables deja stockes."""
    conn = storage.connect(db)
    n = storage.renormalize_cities(conn)
    console.print(f"[green]{n}[/green] lignes mises a jour.")


@rents_app.command("medians")
def rents_medians(
    city: str = typer.Option(None, help="Filtre ville (substring, sensible a la casse)"),
    min_samples: int = typer.Option(3, help="Min d'echantillons par cohorte (city, ch)"),
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
) -> None:
    """Affiche la mediane des loyers par (ville, nb de chambres)."""
    conn = storage.connect(db)
    where = "WHERE monthly_rent IS NOT NULL AND bedrooms IS NOT NULL AND city IS NOT NULL"
    params: tuple = ()
    if city:
        where += " AND city LIKE ?"
        params = (f"%{city}%",)
    rows = conn.execute(
        f"SELECT city, bedrooms, monthly_rent FROM rent_comps {where}", params
    ).fetchall()
    if not rows:
        console.print("[yellow]Aucun comparable. `rents fetch` d'abord.[/yellow]")
        raise typer.Exit(1)
    cohorts: dict[tuple[str, int], list[float]] = {}
    for c, br, rent in rows:
        cohorts.setdefault((c, br), []).append(rent)

    table = Table(title=f"Loyers medians ({len(rows)} comparables, {len(cohorts)} cohortes)")
    for col in ("Ville", "Chambres", "n", "P25", "Mediane", "P75"):
        table.add_column(col)

    def percentile(xs: list[float], p: float) -> float:
        s = sorted(xs)
        k = (len(s) - 1) * p
        f, c = int(k), min(int(k) + 1, len(s) - 1)
        return s[f] + (s[c] - s[f]) * (k - f)

    rows_out = sorted(cohorts.items(), key=lambda x: (x[0][0], x[0][1]))
    for (c, br), rents in rows_out:
        if len(rents) < min_samples:
            continue
        table.add_row(
            c[:30], str(br), str(len(rents)),
            f"{percentile(rents, 0.25):,.0f}$",
            f"{percentile(rents, 0.50):,.0f}$",
            f"{percentile(rents, 0.75):,.0f}$",
        )
    console.print(table)


@app.command("notify")
def notify_cmd(
    scan: bool = typer.Option(
        True, "--scan/--no-scan",
        help="Balaye la DB pour les Lepine passers + baisses de prix.",
    ),
    topic: str = typer.Option(
        None,
        help="Topic ntfy.sh. Defaut: env `NTFY_TOPIC`. Non requis avec --dry-run.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Affiche les notifications qui seraient envoyees sans rien pousser.",
    ),
    max_km: float = typer.Option(
        LocationFilter().max_km,
        help="Distance max (km) depuis la maison. 0 = pas de filtre.",
    ),
    only_full_pass: bool = typer.Option(
        False, "--only-full-pass",
        help="N'inclut que les status=='pass' (exclut 'pass_partial').",
    ),
    drop_threshold_pct: float = typer.Option(
        NotifyConfig().drop_threshold_pct,
        help="Seuil de baisse minimum (%) pour declencher une notif de drop.",
    ),
    limit: int = typer.Option(
        0, help="Max de notifications par execution (0 = illimite). Utile au premier scan.",
    ),
    mark_only: bool = typer.Option(
        False, "--mark-only",
        help="Marque les candidats comme notifies sans envoyer. Utile pour absorber "
             "le backlog initial et ne recevoir que les VRAIS nouveaux au prochain scan.",
    ),
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
) -> None:
    """Scanne la DB et pousse des notifications ntfy.sh pour les Lepine passers.

    Deux evenements declencheurs:
      - NEW  : annonce active passant Lepine, jamais notifiee auparavant
      - DROP : annonce passante dont le prix demande a baisse >= seuil % vs
               la valeur au dernier envoi

    Setup une fois:
      export NTFY_TOPIC=qc-screener-<slug-aleatoire>
      # Abonnez-vous sur https://ntfy.sh/<topic> ou l'app mobile ntfy.
    """
    if not scan:
        console.print("[yellow]Rien a faire — utiliser --scan (defaut).[/yellow]")
        raise typer.Exit(0)

    if not dry_run and not mark_only:
        topic = topic or NotifyConfig.topic_from_env()
        if not topic:
            console.print(
                "[red]Aucun topic ntfy.sh configure.[/red] "
                "Definir `NTFY_TOPIC` dans l'env ou passer `--topic ...`.\n"
                "[dim]Astuce: choisir un slug aleatoire, ex `qc-screener-8f3a2b1c`.[/dim]"
            )
            raise typer.Exit(1)

    conn = storage.connect(db)
    cfg = NotifyConfig(drop_threshold_pct=drop_threshold_pct)

    # En mode mark_only ou dry_run, on scanne sans envoyer.
    notifs = notify_mod.scan(
        conn, notify_cfg=cfg, max_km=max_km,
        include_partial=not only_full_pass,
    )
    if limit > 0:
        notifs = notifs[:limit]

    if not notifs:
        console.print("[green]Aucune notification a envoyer (tout est deja notifie).[/green]")
        raise typer.Exit(0)

    if mark_only:
        for n in notifs:
            notify_mod.mark_notified(conn, n)
        console.print(
            f"[bold cyan]{len(notifs)}[/bold cyan] annonces marquees comme notifiees "
            f"(sans envoi). Prochaine execution ne verra que les vrais nouveaux."
        )
        raise typer.Exit(0)

    if not dry_run and len(notifs) > 20:
        console.print(
            f"[yellow]Attention:[/yellow] {len(notifs)} notifications a envoyer d'un coup. "
            f"Pour un premier scan, considerer `--mark-only` (absorber le backlog "
            f"sans spam) ou `--limit 5` (test).\n[dim]Continue dans 3s... Ctrl-C pour "
            f"annuler.[/dim]"
        )
        import time as _t
        _t.sleep(3)

    sent: list = []
    errors: list = []
    if dry_run:
        sent = notifs
    else:
        for i, n in enumerate(notifs):
            if i > 0:
                import time as _t
                _t.sleep(0.2)
            try:
                notify_mod.send(n, topic=topic, base_url=cfg.base_url)
                notify_mod.mark_notified(conn, n)
                sent.append(n)
            except Exception as e:
                errors.append((n, e))

    kind_emoji = {"new": "🆕", "drop": "📉"}
    table = Table(title=f"Notifications {'a envoyer (DRY-RUN)' if dry_run else 'envoyees'}")
    for col in ("Type", "ID", "Titre", "Corps"):
        table.add_column(col)
    for n in sent:
        table.add_row(
            f"{kind_emoji[n.kind]} {n.kind}",
            n.listing.source_id,
            n.title[:60],
            n.body[:70],
        )
    console.print(table)
    console.print(f"[bold green]{len(sent)}[/bold green] notifications {'affichees' if dry_run else 'envoyees'}")
    if errors:
        console.print(f"[red]{len(errors)} echecs:[/red]")
        for n, e in errors[:5]:
            console.print(f"  {n.listing.source_id}: {e}")


@app.command("price-history")
def price_history_cmd(
    source_id: str = typer.Argument(..., help="ID de l'annonce"),
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
) -> None:
    """Affiche la timeline des prix demandes pour une annonce."""
    conn = storage.connect(db)
    row = conn.execute(
        "SELECT source, url, payload FROM listings WHERE source_id = ?", (source_id,)
    ).fetchone()
    if not row:
        console.print(f"[red]Annonce {source_id} introuvable.[/red]")
        raise typer.Exit(1)
    src, url, _payload = row
    history = storage.price_history(conn, src, source_id)
    if not history:
        console.print(f"[yellow]Aucun historique de prix pour {src}/{source_id}.[/yellow]")
        raise typer.Exit(0)
    table = Table(title=f"Historique prix — {src}/{source_id}")
    for col in ("Date", "Prix demande", "Δ vs precedent"):
        table.add_column(col, justify="right")
    prev = None
    for seen_at, price in history:
        delta = ""
        if prev is not None:
            diff = price - prev
            pct = diff / prev * 100
            color = "green" if diff < 0 else ("red" if diff > 0 else "dim")
            delta = f"[{color}]{diff:+,.0f} $ ({pct:+.1f}%)[/{color}]"
        table.add_row(seen_at[:10], f"{price:,.0f} $", delta or "—")
        prev = price
    console.print(table)
    console.print(f"[dim]{url}[/dim]")


@app.command("price-drops")
def price_drops_cmd(
    days: int = typer.Option(7, help="Fenetre (jours) pour considerer une baisse recente."),
    min_drop_pct: float = typer.Option(1.0, help="Seuil de baisse minimum (%)."),
    include_inactive: bool = typer.Option(
        False, "--include-inactive",
        help="Inclut aussi les annonces soft-deletees.",
    ),
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
) -> None:
    """Liste les annonces avec une baisse de prix dans les N derniers jours."""
    conn = storage.connect(db)
    drops = storage.recent_price_drops(
        conn, since_days=days, min_drop_pct=min_drop_pct,
        active_only=not include_inactive,
    )
    if not drops:
        console.print(
            f"[green]Aucune baisse >={min_drop_pct:.1f}% dans les {days} derniers jours.[/green]"
        )
        raise typer.Exit(0)
    table = Table(title=f"Baisses recentes ({days}j, seuil {min_drop_pct:.1f}%) — {len(drops)} annonces")
    for col in ("Source", "ID", "Ancien", "Nouveau", "Δ %", "Δ $", "Date"):
        table.add_column(col, justify="right")
    for d in drops[:50]:
        table.add_row(
            d["source"],
            d["source_id"],
            f"{d['previous_price']:,.0f}",
            f"{d['current_price']:,.0f}",
            f"[green]{d['drop_pct']:.1f}%[/green]",
            f"{d['drop_abs']:,.0f}",
            d["current_seen"][:10],
        )
    console.print(table)
    if len(drops) > 50:
        console.print(f"[dim]…et {len(drops) - 50} de plus.[/dim]")


@app.command()
def dump(url: str, out: Path = typer.Option(Path("scratch.html"), help="Fichier de sortie")) -> None:
    """Recupere l'URL hors-cache et ecrit le HTML — utile pour valider les selecteurs."""
    p = duproprio.dump_html(url, out)
    console.print(f"HTML ecrit dans {p}")


if __name__ == "__main__":
    app()
