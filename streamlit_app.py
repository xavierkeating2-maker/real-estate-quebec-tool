"""Streamlit — vitrine du screener Lepine.

A lancer depuis la racine du projet:
    streamlit run streamlit_app.py

Lit la meme base SQLite que le CLI (`data/screener.db`).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from qc_screener.analyzer import DealInputs, analyze, sensitivity
from qc_screener.cities import normalize_city
from qc_screener.config import LepineCriteria, LocationFilter
from qc_screener.geo import haversine_km
from qc_screener.lepine import compute_metrics, screen
from qc_screener.market import estimate_market_revenue
from qc_screener.models import Listing
from qc_screener import registre_foncier

DB_PATH = Path("data/screener.db")

st.set_page_config(
    page_title="Lépine Screener QC",
    page_icon="🏠",
    layout="wide",
)


# ─────────────────────────────  Helpers  ─────────────────────────────


@st.cache_resource
def get_conn(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path, check_same_thread=False)


def db_present() -> bool:
    return DB_PATH.exists() and DB_PATH.stat().st_size > 0


def db_mtime() -> str:
    if not db_present():
        return "—"
    ts = datetime.fromtimestamp(DB_PATH.stat().st_mtime, tz=timezone.utc).astimezone()
    return ts.strftime("%Y-%m-%d %H:%M")


@st.cache_data(ttl=60)
def load_listings() -> pd.DataFrame:
    conn = get_conn(str(DB_PATH))
    rows = conn.execute("SELECT source, payload FROM listings").fetchall()
    records = []
    crit = LepineCriteria()
    loc = LocationFilter()
    for source, payload in rows:
        try:
            L = Listing.model_validate_json(payload)
        except Exception:
            continue
        m = compute_metrics(L, crit)
        v = screen(L, crit)
        records.append({
            "source": source,
            "source_id": L.source_id,
            "url": str(L.url),
            "title": L.title,
            "city": L.city,
            "region": L.region,
            "units": L.units,
            "year_built": L.year_built,
            "asking_price": L.asking_price,
            "municipal_evaluation": L.municipal_evaluation,
            "annual_gross_revenue": L.annual_gross_revenue,
            "mrb": m.mrb,
            "price_per_door": m.price_per_door,
            "price_to_eval": m.price_to_eval,
            "cf_per_door_month": m.estimated_cashflow_per_door_month,
            "score": v.score,
            "status": v.status,
            "fetched_at": L.fetched_at,
            "lat": L.lat,
            "lon": L.lon,
            "distance_km": (
                round(haversine_km(loc.home_lat, loc.home_lon, L.lat, L.lon), 1)
                if (L.lat is not None and L.lon is not None) else None
            ),
            "per_unit_rents": L.per_unit_rents,
            "renovations_done": L.renovations_done,
            "renovations_needed": L.renovations_needed,
            "rent_reset_potential": L.rent_reset_potential,
            "seller_motivation": L.seller_motivation,
            "extracted_revenue": (sum(L.per_unit_rents) * 12) if L.per_unit_rents else None,
            "extraction_confidence": L.extraction_confidence,
            "municipal_tax": L.municipal_tax,
            "school_tax": L.school_tax,
            "taxes_total": (L.municipal_tax or 0) + (L.school_tax or 0) or None,
            "eval_land": L.eval_land,
            "eval_building": L.eval_building,
            "land_share": (L.eval_land / L.municipal_evaluation)
                if (L.eval_land and L.municipal_evaluation) else None,
            "date_posted": L.date_posted,
        })
    df = pd.DataFrame(records)
    return df


@st.cache_data(ttl=60)
def load_rent_comps() -> pd.DataFrame:
    conn = get_conn(str(DB_PATH))
    rows = conn.execute(
        "SELECT source, source_id, city, bedrooms, monthly_rent FROM rent_comps "
        "WHERE monthly_rent IS NOT NULL"
    ).fetchall()
    return pd.DataFrame(rows, columns=["source", "source_id", "city", "bedrooms", "monthly_rent"])


def percentile(series, p):
    return float(series.quantile(p))


def fmt_money(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{v:,.0f} $"


def fmt_pct(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{v:.2%}"


# ─────────────────────────────  Sidebar  ─────────────────────────────


with st.sidebar:
    st.title("🏠 Lépine Screener")
    st.caption("Outils d'analyse multi-logement Québec")
    st.metric("Dernière mise à jour", db_mtime())

    if not db_present():
        st.warning("Aucune donnée. Lancer:\n```\nqc-screener crawl --source all --max-pages 15\nqc-screener rents fetch --max-pages 15\nqc-screener rents fetch --source logisquebec --max-listings 250\n```")
    else:
        ldf = load_listings()
        rdf = load_rent_comps()
        st.metric("Annonces", f"{len(ldf):,}")
        st.metric("Comparables loyer", f"{len(rdf):,}")

    st.divider()
    st.subheader("📍 Filtre géographique")
    loc_default = LocationFilter()
    max_km = st.slider(
        "Distance max de la maison (km, vol d'oiseau)",
        min_value=10, max_value=800,
        value=int(loc_default.max_km), step=10,
        help=f"Centre: {loc_default.home_lat:.3f}, {loc_default.home_lon:.3f} "
             f"(éditer dans qc_screener/config.py). "
             f"≈ 2h de route ≈ 150 km en ligne droite.",
    )
    include_no_coords = st.checkbox(
        "Inclure annonces sans coordonnées", value=False,
        help="Listings dont la latitude/longitude n'a pu être extraite.",
    )

    st.divider()
    st.markdown(
        "**Mise à jour:** relancer `qc-screener crawl ...` puis recharger cette page. "
        "L'app lit la même base SQLite que le CLI."
    )

if not db_present():
    st.title("Aucune donnée disponible")
    st.write("Voir la barre latérale pour les commandes d'initialisation.")
    st.stop()


def filter_by_distance(df: pd.DataFrame) -> pd.DataFrame:
    """Applique le filtre de distance global de la barre latérale."""
    if include_no_coords:
        mask = df["distance_km"].isna() | (df["distance_km"] <= max_km)
    else:
        mask = df["distance_km"].notna() & (df["distance_km"] <= max_km)
    return df[mask].copy()


# ─────────────────────────────  Tabs  ─────────────────────────────


tab_apercu, tab_annonces, tab_carte, tab_aubaines, tab_analyse, tab_loyers, tab_macro, tab_methode = st.tabs([
    "🏠 Aperçu",
    "🔍 Annonces",
    "🗺️ Carte",
    "💎 Aubaines",
    "📊 Analyseur de deal",
    "🏘️ Loyers",
    "📡 Signal régional",
    "📖 Méthode",
])


# ─────────────────────────────  Aperçu  ─────────────────────────────


with tab_apercu:
    st.header("Vue d'ensemble du catalogue")
    df_all = load_listings()
    df = filter_by_distance(df_all)
    st.caption(
        f"📍 Filtre actif: {len(df):,} / {len(df_all):,} annonces dans le rayon "
        f"de {max_km} km."
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Annonces", f"{len(df):,}")
    c2.metric("Avec évaluation municipale", int(df["price_to_eval"].notna().sum()))
    c3.metric("Avec revenus divulgués", int(df["mrb"].notna().sum()))
    c4.metric("✅ Passent Lépine", int((df["status"] == "pass").sum()))
    c5.metric("⚠️ Partiels (revenu inconnu)", int((df["status"] == "pass_partial").sum()))

    st.divider()

    cA, cB = st.columns(2)
    with cA:
        st.subheader("Top 5 par prix/évaluation")
        top_eval = df.dropna(subset=["price_to_eval"]).nsmallest(5, "price_to_eval")
        st.dataframe(
            top_eval[["source_id", "city", "units", "asking_price", "price_to_eval", "mrb"]],
            hide_index=True,
            use_container_width=True,
        )
    with cB:
        st.subheader("Top 5 par MRB (cashflow)")
        top_mrb = df.dropna(subset=["mrb"]).nsmallest(5, "mrb")
        st.dataframe(
            top_mrb[["source_id", "city", "units", "asking_price", "mrb", "cf_per_door_month"]],
            hide_index=True,
            use_container_width=True,
        )

    st.divider()
    st.subheader("Répartition par source")
    src_counts = df.groupby("source").size().reset_index(name="annonces")
    if not src_counts.empty:
        fig = px.bar(src_counts, x="source", y="annonces", text="annonces")
        fig.update_traces(textposition="outside")
        st.plotly_chart(fig, use_container_width=True)


# ─────────────────────────────  Annonces  ─────────────────────────────


with tab_annonces:
    st.header("Toutes les annonces multilogement")
    df = filter_by_distance(load_listings())

    f1, f2, f3, f4 = st.columns(4)
    sources = sorted(df["source"].dropna().unique().tolist())
    f_src = f1.multiselect("Source", sources, default=sources)
    units_range = (int(df["units"].min() or 0), int(df["units"].max() or 0))
    f_units = f2.slider("Nombre de logements", *units_range, units_range)
    price_max = float(df["asking_price"].max() or 0)
    f_price = f3.slider("Prix max ($)", 0.0, price_max, price_max, step=50000.0)
    only_eval = f4.checkbox("Seulement avec évaluation municipale", value=False)

    mask = (
        df["source"].isin(f_src)
        & df["units"].between(*f_units)
        & (df["asking_price"].fillna(0) <= f_price)
    )
    if only_eval:
        mask &= df["price_to_eval"].notna()
    view = df[mask].copy().sort_values("price_to_eval")

    st.caption(f"{len(view)} annonces correspondent aux filtres")
    STATUS_BADGE = {"pass": "✅ Pass", "pass_partial": "⚠️ Partiel", "fail": "❌"}
    view = view.assign(status_badge=view["status"].map(STATUS_BADGE).fillna("—"))
    cols = [
        "source", "source_id", "city", "distance_km", "units", "year_built",
        "asking_price", "municipal_evaluation", "price_to_eval",
        "annual_gross_revenue", "mrb", "cf_per_door_month",
        "taxes_total", "land_share", "date_posted", "status_badge", "url",
    ]
    st.dataframe(
        view[cols],
        column_config={
            "url": st.column_config.LinkColumn("URL"),
            "distance_km": st.column_config.NumberColumn("Distance (km)", format="%.0f"),
            "asking_price": st.column_config.NumberColumn("Prix", format="%.0f $"),
            "municipal_evaluation": st.column_config.NumberColumn("Éval", format="%.0f $"),
            "annual_gross_revenue": st.column_config.NumberColumn("Revenus an.", format="%.0f $"),
            "price_to_eval": st.column_config.NumberColumn("Prix/éval", format="%.2f"),
            "mrb": st.column_config.NumberColumn("MRB", format="%.1f"),
            "cf_per_door_month": st.column_config.NumberColumn("CF/porte/mois", format="%.0f $"),
            "taxes_total": st.column_config.NumberColumn("Taxes/an", format="%.0f $"),
            "land_share": st.column_config.NumberColumn("% Terrain", format="percent"),
            "date_posted": st.column_config.DatetimeColumn("Affichée"),
            "status_badge": st.column_config.TextColumn(
                "Lépine",
                help="✅ Pass = MRB + prix/éval OK · ⚠️ Partiel = prix/éval OK mais revenu non divulgué (MRB incalculable) · ❌ Fail",
            ),
        },
        hide_index=True,
        use_container_width=True,
        height=600,
    )


# ─────────────────────────────  Carte  ─────────────────────────────


with tab_carte:
    st.header("Carte des annonces multi-logement")
    df = filter_by_distance(load_listings())
    geo = df.dropna(subset=["lat", "lon"]).copy()
    st.caption(f"{len(geo)} / {len(df)} annonces géolocalisées")

    colA, colB, colC = st.columns(3)
    color_by = colA.selectbox(
        "Coloration",
        ["Prix/éval", "MRB", "Cashflow/porte", "Source", "Passe Lépine"],
    )
    only_with_eval = colB.checkbox("Seulement avec évaluation", value=False)
    min_units = colC.slider("Min logements", 2, 8, 2)

    plot = geo[geo["units"].fillna(0) >= min_units].copy()
    if only_with_eval:
        plot = plot.dropna(subset=["price_to_eval"])

    # Limite haut pour rendre la coloration lisible (clip outliers).
    color_kwargs: dict = {}
    if color_by == "Prix/éval":
        plot = plot.dropna(subset=["price_to_eval"])
        plot["color"] = plot["price_to_eval"].clip(upper=2.5)
        color_kwargs = dict(color="color", color_continuous_scale="RdYlGn_r",
                            range_color=(0.7, 2.5))
    elif color_by == "MRB":
        plot = plot.dropna(subset=["mrb"])
        plot["color"] = plot["mrb"].clip(upper=30)
        color_kwargs = dict(color="color", color_continuous_scale="RdYlGn_r",
                            range_color=(5, 25))
    elif color_by == "Cashflow/porte":
        plot = plot.dropna(subset=["cf_per_door_month"])
        plot["color"] = plot["cf_per_door_month"].clip(lower=-1500, upper=500)
        color_kwargs = dict(color="color", color_continuous_scale="RdYlGn",
                            range_color=(-1000, 200))
    elif color_by == "Source":
        color_kwargs = dict(color="source")
    elif color_by == "Passe Lépine":
        plot["color"] = plot["status"].map({
            "pass": "✅ Pass",
            "pass_partial": "⚠️ Partiel",
            "fail": "❌ Fail",
        }).fillna("❌ Fail")
        color_kwargs = dict(color="color",
                            color_discrete_map={
                                "✅ Pass": "#2ecc71",
                                "⚠️ Partiel": "#f1c40f",
                                "❌ Fail": "#e74c3c",
                            })

    if plot.empty:
        st.info("Aucune annonce à afficher avec ces filtres.")
    else:
        # Bornes carte: centroïde + zoom auto.
        center = {"lat": float(plot["lat"].mean()), "lon": float(plot["lon"].mean())}
        size_col = plot["asking_price"].fillna(plot["asking_price"].median()).clip(
            lower=200_000, upper=1_500_000
        )
        plot["size_norm"] = size_col
        fig = px.scatter_mapbox(
            plot,
            lat="lat", lon="lon",
            size="size_norm",
            hover_data={
                "title": True,
                "asking_price": ":,.0f",
                "units": True,
                "mrb": ":.1f",
                "price_to_eval": ":.2f",
                "cf_per_door_month": ":.0f",
                "url": True,
                "lat": False, "lon": False, "size_norm": False, "color": False,
            },
            zoom=6,
            center=center,
            height=650,
            **color_kwargs,
        )
        fig.update_layout(mapbox_style="open-street-map",
                          margin={"r": 0, "t": 0, "l": 0, "b": 0})
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Astuce: cliquer-glisser pour panner, molette pour zoomer. "
            "Survoler un point pour voir les détails."
        )


# ─────────────────────────────  Aubaines  ─────────────────────────────


with tab_aubaines:
    st.header("Aubaines — bottom-X% par prix/évaluation")
    df = filter_by_distance(load_listings())
    eligibles = df.dropna(subset=["price_to_eval"]).copy()
    st.caption(f"{len(eligibles)} annonces ont une évaluation municipale dans la base")

    pct = st.slider("Percentile du bas à montrer (%)", 1, 50, 10)
    min_units = st.slider("Min de logements", 2, 8, 2)
    eligibles = eligibles[eligibles["units"].fillna(0) >= min_units]
    n_show = max(3, round(len(eligibles) * pct / 100))
    view = eligibles.nsmallest(n_show, "price_to_eval")

    # On clippe les valeurs aberrantes (eval-placeholder côté vendeur, MRB
    # explose, etc.) pour que le nuage soit lisible — sans toucher au DataFrame
    # sous-jacent. Les vraies aubaines sont dans le coin bas-gauche.
    chart = eligibles.copy()
    chart = chart[(chart["price_to_eval"] > 0) & (chart["price_to_eval"] < 5)]
    if "mrb" in chart.columns:
        chart = chart[(chart["mrb"].isna()) | (chart["mrb"] < 40)]

    x_lo, x_hi = 0.5, 2.5
    y_lo, y_hi = 0, 25
    n_shown = len(chart)
    n_clipped = len(eligibles) - n_shown

    fig = px.scatter(
        chart,
        x="price_to_eval",
        y="mrb",
        size="asking_price",
        color="source",
        hover_data={
            "source_id": True, "city": True, "units": True,
            "asking_price": ":,.0f", "url": True,
        },
        title=f"Prix/éval vs MRB — {n_shown} annonces (taille = prix)",
        range_x=[x_lo, x_hi],
        range_y=[y_lo, y_hi],
    )
    # Zone "sweet spot Lépine": prix <= éval ET MRB <= 8.
    fig.add_shape(
        type="rect", x0=x_lo, y0=y_lo, x1=1.0, y1=8.0,
        fillcolor="rgba(46,204,113,0.10)", line_width=0, layer="below",
    )
    fig.add_vline(x=1.0, line_dash="dot", line_color="gray",
                  annotation_text="Prix = Éval", annotation_position="top")
    fig.add_hline(y=8.0, line_dash="dot", line_color="gray",
                  annotation_text="MRB 8 (cible Lépine)", annotation_position="right")
    fig.update_layout(xaxis_title="Prix / Évaluation municipale", yaxis_title="MRB")
    st.plotly_chart(fig, use_container_width=True)
    if n_clipped:
        st.caption(
            f"Note: {n_clipped} annonces hors plage (prix/éval > 5 ou MRB > 40) "
            "ont été clippées du nuage — souvent des valeurs placeholder côté vendeur."
        )

    st.subheader(f"Top {n_show}")
    st.dataframe(
        view[["source_id", "city", "units", "asking_price", "municipal_evaluation",
              "price_to_eval", "mrb", "cf_per_door_month", "url"]],
        column_config={
            "url": st.column_config.LinkColumn("URL"),
            "asking_price": st.column_config.NumberColumn("Prix", format="%.0f $"),
            "municipal_evaluation": st.column_config.NumberColumn("Éval", format="%.0f $"),
            "price_to_eval": st.column_config.NumberColumn("Prix/éval", format="%.2f"),
            "mrb": st.column_config.NumberColumn("MRB", format="%.1f"),
            "cf_per_door_month": st.column_config.NumberColumn("CF/porte/mois", format="%.0f $"),
        },
        hide_index=True,
        use_container_width=True,
    )


# ─────────────────────────────  Analyseur  ─────────────────────────────


with tab_analyse:
    st.header("Analyseur de deal")
    df = filter_by_distance(load_listings())
    if df.empty:
        st.info("Aucune annonce.")
    else:
        df_idx = df.set_index("source_id")
        choices = [
            f"{sid} · {row['city'] or '?'} · {row['units']}lgt · {fmt_money(row['asking_price'])}"
            for sid, row in df_idx.iterrows()
        ]
        idx = st.selectbox("Choisir une annonce", range(len(choices)),
                            format_func=lambda i: choices[i])
        sid = df_idx.index[idx]
        listing_row = df_idx.iloc[idx]

        # Panneau extraction LLM (si dispo)
        if pd.notna(listing_row.get("extraction_confidence")):
            with st.expander("🤖 Faits extraits de la description (LLM)", expanded=True):
                lc1, lc2 = st.columns(2)
                with lc1:
                    rents = listing_row.get("per_unit_rents") or []
                    if rents:
                        st.metric("Revenus extraits", f"{sum(rents)*12:,.0f} $/an")
                        st.write("**Loyers par logement:**", ", ".join(f"{r:,.0f} $" for r in rents))
                    reset = listing_row.get("rent_reset_potential")
                    if reset:
                        emoji = {"high": "🔥", "medium": "📈", "low": "📊", "unknown": "❓"}.get(reset, "")
                        st.write(f"**Potentiel rent-reset:** {emoji} {reset}")
                with lc2:
                    reno_done = listing_row.get("renovations_done") or []
                    reno_need = listing_row.get("renovations_needed") or []
                    if reno_done:
                        st.write("**Renos faites:**", ", ".join(reno_done))
                    if reno_need:
                        st.write("**Renos à faire:**", ", ".join(reno_need))
                    seller = listing_row.get("seller_motivation")
                    if seller:
                        st.write("**Motivation vendeur:**", seller)
                st.caption(f"Confiance LLM: {listing_row['extraction_confidence']:.2f}")

        with st.expander("Détails de l'annonce", expanded=False):
            st.json({
                "Titre": listing_row.get("title"),
                "Ville": listing_row.get("city"),
                "Région": listing_row.get("region"),
                "Logements": int(listing_row["units"]) if pd.notna(listing_row["units"]) else None,
                "Année": int(listing_row["year_built"]) if pd.notna(listing_row["year_built"]) else None,
                "Prix demandé": listing_row.get("asking_price"),
                "Évaluation": listing_row.get("municipal_evaluation"),
                "Éval Terrain": listing_row.get("eval_land"),
                "Éval Bâtiment": listing_row.get("eval_building"),
                "Revenus annoncés": listing_row.get("annual_gross_revenue"),
                "Taxes municipales": listing_row.get("municipal_tax"),
                "Taxes scolaires": listing_row.get("school_tax"),
                "Affichée": str(listing_row.get("date_posted")) if pd.notna(listing_row.get("date_posted")) else None,
                "URL": listing_row.get("url"),
            })

        units = int(listing_row["units"]) if pd.notna(listing_row["units"]) else 2

        col1, col2, col3 = st.columns(3)
        offer = col1.number_input(
            "Prix d'offre ($)", min_value=0,
            value=int(listing_row["asking_price"]) if pd.notna(listing_row["asking_price"]) else 500_000,
            step=10_000,
        )
        unit_mix_str = col2.text_input(
            "Mix de chambres (séparé par virgule)",
            value=",".join(["2"] * units),
            help="Ex pour un 4-plex avec deux 4½ et deux 3½: '2,2,1,1'",
        )
        use_market = col3.toggle("Auto-remplir avec loyers du marché", value=True)

        try:
            unit_mix = [int(x.strip()) for x in unit_mix_str.split(",") if x.strip()]
        except ValueError:
            st.error("Mix invalide. Utiliser des entiers séparés par des virgules.")
            unit_mix = [2] * units
        if len(unit_mix) != units:
            st.warning(f"Le mix devrait avoir {units} valeurs (a {len(unit_mix)}).")

        conn = get_conn(str(DB_PATH))
        market_rev, breakdown, canon = (None, [], None)
        if use_market and len(unit_mix) == units:
            market_rev, breakdown, canon = estimate_market_revenue(
                conn, listing_row.get("city"), unit_mix
            )

        col4, col5 = st.columns(2)
        # Priorite: rents extraits LLM > cohorte marche > revenus annonce
        extracted = listing_row.get("extracted_revenue")
        if extracted and not pd.isna(extracted):
            default_revenue = int(extracted)
            col4.success(
                f"🤖 Revenus extraits par LLM: **{fmt_money(extracted)}/an** "
                f"(loyers par logt: {listing_row.get('per_unit_rents')})"
            )
        elif use_market and market_rev:
            default_revenue = int(market_rev)
            col4.success(f"Loyer marché ({canon}): **{fmt_money(market_rev)}/an**")
        elif listing_row.get("annual_gross_revenue"):
            default_revenue = int(listing_row["annual_gross_revenue"])
            col4.info(f"Revenus annoncés: **{fmt_money(default_revenue)}/an**")
        else:
            default_revenue = 0
            col4.warning("Pas de revenus disponibles — entrer manuellement.")

        stabilized_revenue = col4.number_input(
            "Revenus stabilisés ($/an)", min_value=0, value=default_revenue, step=1000
        )
        capex = col5.number_input("Capex initial ($)", min_value=0, value=0, step=5000)

        st.subheader("Financement")
        f1, f2, f3, f4, f5 = st.columns(5)
        down_pct = f1.slider("Mise de fonds (%)", 5.0, 50.0, 20.0, step=0.5)
        rate = f2.slider("Taux hypo (%)", 1.0, 12.0, 5.5, step=0.05)
        am_years = f3.slider("Amortissement (ans)", 15, 30, 25)
        vtb_pct = f4.slider("Balance vente (%)", 0.0, 30.0, 0.0, step=1.0)
        vtb_rate = f5.slider("Taux balance (%)", 0.0, 12.0, 7.0, step=0.5)

        if breakdown:
            with st.expander("Ventilation par logement (marché)", expanded=False):
                rows = []
                for i, (br, rent, n) in enumerate(breakdown, 1):
                    rows.append({
                        "Logt": i, "Chambres": br,
                        "Loyer mois": rent if rent else None,
                        "n comparables": n,
                    })
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        if offer > 0 and stabilized_revenue > 0 and len(unit_mix) == units:
            known_taxes = None
            mt = listing_row.get("municipal_tax") or 0
            st_tax = listing_row.get("school_tax") or 0
            if (mt + st_tax) > 0:
                known_taxes = float(mt + st_tax)

            inputs = DealInputs(
                purchase_price=float(offer),
                units=units,
                gross_annual_revenue=float(stabilized_revenue),
                known_taxes=known_taxes,
                down_payment_pct=down_pct / 100,
                mortgage_rate=rate / 100,
                amortization_years=int(am_years),
                vtb_pct=vtb_pct / 100,
                vtb_rate=vtb_rate / 100,
                initial_capex=float(capex),
            )
            if known_taxes:
                st.caption(
                    f"✓ Taxes connues: {known_taxes:,.0f} $/an "
                    "(municipales + scolaires) — incluses dans le calcul des dépenses."
                )
            res = analyze(inputs, horizon=10)

            st.divider()
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("TGA (Année 1)", fmt_pct(res.year_1.cap_rate))
            m2.metric("MRB", f"{res.year_1.mrb:.1f}")
            m3.metric("Cashflow/porte/mois", fmt_money(res.year_1.cashflow_per_door_month))
            irr_str = fmt_pct(res.irr_5_year) if res.irr_5_year is not None else "n/a"
            m4.metric("IRR 5 ans (incl. sortie)", irr_str)

            st.subheader("Capital requis")
            cap_rows = [
                {"Élément": "Mise de fonds", "Montant": offer * down_pct / 100},
                {"Élément": "Taxe de bienvenue", "Montant": res.welcome_tax},
                {"Élément": f"Autres frais ({inputs.other_closing_pct*100:.1f}%)",
                 "Montant": offer * inputs.other_closing_pct},
                {"Élément": "Capex initial", "Montant": capex},
            ]
            if res.cmhc_premium > 0:
                cap_rows.append({"Élément": "Prime SCHL (dans hypo)", "Montant": res.cmhc_premium})
            cap_rows.append({"Élément": "Total capital comptant", "Montant": res.cash_invested})
            cap_df = pd.DataFrame(cap_rows)
            st.dataframe(
                cap_df,
                column_config={"Montant": st.column_config.NumberColumn(format="%.0f $")},
                hide_index=True, use_container_width=True,
            )

            st.subheader("Projection annuelle")
            yearly = pd.DataFrame([{
                "Année": s.year,
                "Revenus effectifs": s.effective_revenue,
                "NOI": s.noi,
                "TGA": s.cap_rate,
                "Cashflow annuel": s.annual_cashflow,
                "$/porte/mois": s.cashflow_per_door_month,
                "Valeur": s.property_value,
                "Avoir net": s.equity,
            } for s in res.yearly])
            st.dataframe(
                yearly,
                column_config={
                    "Revenus effectifs": st.column_config.NumberColumn(format="%.0f $"),
                    "NOI": st.column_config.NumberColumn(format="%.0f $"),
                    "TGA": st.column_config.NumberColumn(format="percent"),
                    "Cashflow annuel": st.column_config.NumberColumn(format="%.0f $"),
                    "$/porte/mois": st.column_config.NumberColumn(format="%.0f $"),
                    "Valeur": st.column_config.NumberColumn(format="%.0f $"),
                    "Avoir net": st.column_config.NumberColumn(format="%.0f $"),
                },
                hide_index=True, use_container_width=True,
            )

            fig = px.line(yearly, x="Année", y=["$/porte/mois"],
                          title="Cashflow par porte par mois sur 10 ans")
            fig.add_hline(y=0, line_dash="dot", line_color="gray")
            st.plotly_chart(fig, use_container_width=True)

            st.subheader("Retours 5 ans")
            r1, r2, r3 = st.columns(3)
            r1.metric("Cashflow cumulé 5 ans", fmt_money(res.five_year_cf_cumulative))
            r2.metric("Rendement total 5 ans", fmt_pct(res.five_year_total_return))
            r3.metric("Avoir net An.5", fmt_money(res.year_5.equity))

            with st.expander("📊 Sensibilité prix × revenus stabilisés", expanded=False):
                step_price = st.slider("Pas prix ($)", 5_000, 50_000, 25_000, step=5_000)
                step_rev = st.slider("Pas revenus ($)", 1_000, 10_000, 3_000, step=1_000)
                prices = [offer + i * step_price for i in range(-3, 4)]
                revs = [stabilized_revenue + i * step_rev for i in range(-3, 4)]
                grid = sensitivity(inputs, prices, revs)
                metric = st.selectbox("Métrique", ["IRR 5 ans", "Cashflow/porte/mois", "TGA"])
                rows = []
                for i, p in enumerate(prices):
                    row = {"Prix": p}
                    for j, r in enumerate(revs):
                        cell = grid[i][j]
                        if metric == "IRR 5 ans":
                            v = cell["irr"]
                            row[f"{r:,.0f}"] = round(v * 100, 2) if v is not None else None
                        elif metric == "Cashflow/porte/mois":
                            row[f"{r:,.0f}"] = round(cell["cf_door_mo"])
                        else:
                            row[f"{r:,.0f}"] = round(cell["tga"] * 100, 2)
                    rows.append(row)
                sdf = pd.DataFrame(rows).set_index("Prix")
                fmt_pct_cell = "%.2f%%" if metric != "Cashflow/porte/mois" else "%.0f $"
                st.dataframe(
                    sdf.style.background_gradient(cmap="RdYlGn", axis=None).format(fmt_pct_cell),
                    use_container_width=True,
                )
        else:
            st.info("Renseigner prix d'offre + revenus stabilisés pour lancer l'analyse.")


# ─────────────────────────────  Loyers  ─────────────────────────────


with tab_loyers:
    st.header("Loyers du marché par cohorte")
    rdf = load_rent_comps()
    if rdf.empty:
        st.info("Aucun comparable. Lancer `qc-screener rents fetch ...`.")
    else:
        min_n = st.slider("Min d'échantillons", 1, 30, 5)
        st.caption(f"{len(rdf):,} comparables au total — sources: {', '.join(sorted(rdf['source'].unique()))}")

        agg = (
            rdf.groupby(["city", "bedrooms"])
            .agg(n=("monthly_rent", "size"),
                 p25=("monthly_rent", lambda s: percentile(s, 0.25)),
                 mediane=("monthly_rent", lambda s: percentile(s, 0.50)),
                 p75=("monthly_rent", lambda s: percentile(s, 0.75)))
            .reset_index()
        )
        agg = agg[agg["n"] >= min_n].sort_values(["city", "bedrooms"])

        st.dataframe(
            agg,
            column_config={
                "p25": st.column_config.NumberColumn("P25", format="%.0f $"),
                "mediane": st.column_config.NumberColumn("Médiane", format="%.0f $"),
                "p75": st.column_config.NumberColumn("P75", format="%.0f $"),
            },
            hide_index=True,
            use_container_width=True,
        )

        st.subheader("Distribution des loyers (top villes)")
        top_cities = agg.groupby("city")["n"].sum().nlargest(8).index.tolist()
        plot_df = rdf[rdf["city"].isin(top_cities)].copy()
        plot_df = plot_df[plot_df["bedrooms"].notna() & plot_df["monthly_rent"].notna()]
        plot_df["bedrooms"] = plot_df["bedrooms"].astype(int).astype(str) + " ch"
        if not plot_df.empty:
            fig = px.box(
                plot_df, x="city", y="monthly_rent", color="bedrooms",
                category_orders={"city": top_cities},
                labels={"monthly_rent": "Loyer mensuel ($)", "city": ""},
            )
            st.plotly_chart(fig, use_container_width=True)


# ─────────────────────────────  Signal régional  ─────────────────────────────


@st.cache_data(ttl=3600)
def load_region_stats(months: int) -> pd.DataFrame:
    rows = registre_foncier.region_stats(months=months)
    return pd.DataFrame(rows)


with tab_macro:
    st.header("Signal macro régional — Registre foncier QC")
    st.caption(
        "Données ouvertes du gouvernement du Québec (CC-BY 4.0) — agrégés par "
        "région administrative et par mois. Pas de détail par propriété."
    )
    months = st.slider("Fenêtre (mois)", 3, 24, 12)
    try:
        rdf = load_region_stats(months)
    except Exception as e:
        st.error(f"Téléchargement des CSV échoué: {e}")
        st.info("Lancer `qc-screener macro refresh` une fois pour amorcer le cache disque.")
        rdf = pd.DataFrame()

    if not rdf.empty:
        window = f"{rdf.iloc[0]['fenetre_debut']} → {rdf.iloc[0]['fenetre_fin']}"
        st.caption(f"Fenêtre: **{window}**")

        rdf_sorted = rdf.dropna(subset=["ratio_distress"]).sort_values(
            "ratio_distress", ascending=False
        )

        st.subheader("Classement par ratio distress + plages de prix")
        st.dataframe(
            rdf_sorted[[
                "region", "transferts_recents", "transferts_yoy_pct",
                "ratio_distress", "share_band1", "share_band2", "share_band3",
                "hypotheques_recents", "difficulte_recents",
            ]],
            column_config={
                "region": "Région",
                "transferts_recents": st.column_config.NumberColumn("Transferts", format="%d"),
                "transferts_yoy_pct": st.column_config.NumberColumn("YoY %", format="%+.1f%%"),
                "ratio_distress": st.column_config.NumberColumn("Distress %", format="percent"),
                "share_band1": st.column_config.NumberColumn("% < 250K", format="percent"),
                "share_band2": st.column_config.NumberColumn("% 250-500K", format="percent"),
                "share_band3": st.column_config.NumberColumn("% > 500K", format="percent"),
                "hypotheques_recents": st.column_config.NumberColumn("Hypothèques", format="%d"),
                "difficulte_recents": st.column_config.NumberColumn("Difficulté", format="%d"),
            },
            hide_index=True,
            use_container_width=True,
        )

        c1, c2 = st.columns(2)
        with c1:
            fig = px.bar(
                rdf_sorted.head(12), x="region", y="ratio_distress",
                title="Ratio distress (difficulté/transferts) — top 12",
            )
            fig.update_yaxes(tickformat=".2%")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            rdf_yoy = rdf.dropna(subset=["transferts_yoy_pct"]).sort_values(
                "transferts_yoy_pct", ascending=False
            )
            fig = px.bar(
                rdf_yoy, x="region", y="transferts_yoy_pct",
                color="transferts_yoy_pct", color_continuous_scale="RdYlGn",
                title="Croissance YoY des transferts (%)",
            )
            st.plotly_chart(fig, use_container_width=True)

        st.info(
            "**Lecture Lépine** — un ratio distress élevé + une croissance des "
            "transferts faible/négative = davantage de vendeurs motivés et moins "
            "de concurrence à l'achat. Montréal est typiquement en tête."
        )


# ─────────────────────────────  Méthode  ─────────────────────────────


with tab_methode:
    st.header("Méthode Lépine — comment lire ces chiffres")
    st.markdown("""
**MRB — Multiplicateur de Revenu Brut**
Prix demandé ÷ revenus bruts annuels. Lépine cible **≤ 7-8** pour les petits multilogements.
Un MRB de 12+ signifie qu'on paie plus que la "valeur économique" du bâtiment.

**TGA — Taux Global d'Actualisation** (équivalent du *cap rate*)
NOI ÷ valeur. **≥ 7%** est l'objectif en marché secondaire québécois.

**Cashflow par porte par mois**
Revenu net après dépenses et service de la dette, divisé par le nombre de logements.
Lépine veut **positif dès le jour 1** — minimum 50-100 $/porte/mois selon le marché.

**Prix / Évaluation municipale**
Le ratio entre prix demandé et évaluation municipale. **< 1.0** = sous l'évaluation.
À nuancer: les évaluations municipales sont mises à jour aux 3 ans et peuvent retarder le marché.

**Mise de fonds**
- 4 logements et moins: 20% minimum (résidentiel)
- 5 logements et plus: 25%+ (commercial CMHC ou financement privé)

**Balance de vente (vendor take-back)**
Le vendeur finance une portion du prix d'achat. Permet de réduire la mise de fonds réelle
et le service de la dette principale.

---

### Comment fonctionne le screener

1. **Crawl** — récupère les annonces multi-logement de DuProprio (HTML scrape) et ProprioDirect (API JSON).
2. **Loyers du marché** — récupère les loyers de location de Kijiji (JSON Apollo) et LogisQuébec (sitemap + parse HTML).
3. **Screener** — applique les seuils Lépine et classe par score.
4. **Analyseur** — projette TGA / cashflow / valeur sur 5 ans pour une annonce donnée.

### Limites connues

- Les annonces FSBO (DuProprio/ProprioDirect) ne couvrent qu'**une fraction du marché** — Centris reste à intégrer.
- Les loyers du marché sont **plus solides à Montréal/Québec/Gatineau** qu'en région.
- Les chiffres reposent sur ce que le vendeur déclare; valider toujours sur place.
- Cette outil **ne remplace pas** un courtier, un inspecteur et un comptable.
""")
