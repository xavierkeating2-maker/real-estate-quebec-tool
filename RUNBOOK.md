# RUNBOOK — qc-screener

Quick reference for running, refreshing, and operating the Lépine screener.
**Update this file whenever a CLI command, source, or workflow changes.**

Related docs:
- `IDEAS.md` — parking lot for unpursued ideas / future polish
- `streamlit_app.py` — UI for everything, runs locally

---

## 0. Setup (one time)

```bash
cd ~/projects/real-estate-quebec-tool
python3 -m venv .venv
.venv/bin/pip install -e .
```

After this, all commands below run from `~/projects/real-estate-quebec-tool/` with `.venv/bin/qc-screener`.

---

## 1. Daily flow — fastest

Incremental refresh + open the UI:

```bash
cd ~/projects/real-estate-quebec-tool
.venv/bin/qc-screener crawl --source all --max-pages 20   # ~10–20 min, search-pages always fresh + detail cache hit for already-seen listings
.venv/bin/streamlit run streamlit_app.py                  # browser opens at http://localhost:8501
```

What this captures:
- New listings on all three buying sources (DuProprio, ProprioDirect, Centris)
- Will NOT detect price changes on listings already in the cache — see §5 to bust

---

## 2. Weekly flow — rent comps + macro

Once a week, refresh the inputs to the deal analyzer:

```bash
.venv/bin/qc-screener rents fetch --source kijiji --max-pages 15           # ~1 min
.venv/bin/qc-screener rents fetch --source logisquebec --max-listings 250  # ~12 min (sitemap-based)
.venv/bin/qc-screener rents renormalize                                    # backfill canonical city names
```

Cohort medians should refresh — verify:

```bash
.venv/bin/qc-screener rents medians --min-samples 5
```

---

## 3. Monthly flow — macro signal

The Registre foncier CSVs (CC-BY, Données Québec) update ~monthly:

```bash
.venv/bin/qc-screener macro refresh --force   # force re-download
.venv/bin/qc-screener macro regions --months 12
```

---

## 4. Geographic filter (distance from home)

A global **distance-from-home filter** scopes the catalog to listings within driving range. Configured in `qc_screener/config.py` → `LocationFilter`:

```python
home_lat = 45.5019    # Grand Montréal centroid — edit to your address
home_lon = -73.5674
max_km   = 175.0      # haversine (straight-line); 175 km ≈ Gatineau
```

**To pin to your actual address:** Google Maps → right-click your house → "What's here?" → copy lat/lon into the config file. Restart Streamlit afterward.

**Reference distances from the default centroid:**

| City             | km  | In default (175 km) |
|------------------|----:|:-------------------:|
| Laval            |  16 | ✓                   |
| Trois-Rivières   | 123 | ✓                   |
| Sherbrooke       | 131 | ✓                   |
| Gatineau         | 166 | ✓                   |
| Québec ville     | 233 | ✗                   |
| Saguenay         | 377 | ✗                   |
| Rimouski         | 504 | ✗                   |

**Where it applies:**
- **Streamlit** — sidebar slider (📍 Filtre géographique) overrides the config default; every tab (Aperçu, Annonces, Carte, Aubaines, Analyseur) respects it.
- **CLI** — `run` and `value` accept `--max-km N` (use `--max-km 0` to disable).
- Listings without lat/lon are excluded by default (toggle in sidebar to include them).

---

## 5. Cheatsheet — common runs

```bash
# Browse the screener results in the terminal
.venv/bin/qc-screener run --top 15                      # Lépine-screened table (default --max-km from config.py)
.venv/bin/qc-screener run --top 15 --max-km 200         # widen radius
.venv/bin/qc-screener run --top 15 --max-km 0           # disable distance filter entirely
.venv/bin/qc-screener value --top 15                    # macro-weighted by default (distress + YoY tail-wind/headwind)
.venv/bin/qc-screener value --top 10 --percentile 3     # tighter bottom 3%
.venv/bin/qc-screener value --no-macro --top 15         # raw prix/eval, ignore region heat
.venv/bin/qc-screener value --distress-weight 2.0       # tune macro weighting

# Analyze a specific listing
.venv/bin/qc-screener analyze-deal 22564119                          # auto-fills offer + market rents (default 2br per unit)
.venv/bin/qc-screener analyze-deal 22564119 --offer 350000           # custom offer
.venv/bin/qc-screener analyze-deal 22564119 --unit-mix 2,2,1         # custom unit mix
.venv/bin/qc-screener analyze-deal 22564119 --vtb-pct 10 --vtb-rate 6.5  # vendor balance
.venv/bin/qc-screener analyze-deal 22564119 --no-market              # use listing's reported revenue, skip cohort lookup

# Inspect rent comps
.venv/bin/qc-screener rents medians --city Montréal --min-samples 5

# Macro / regional heat
.venv/bin/qc-screener macro regions --months 6                       # tighter window

# LLM extraction (requires ANTHROPIC_API_KEY env var)
export ANTHROPIC_API_KEY=sk-ant-...                                  # set once per shell
.venv/bin/qc-screener extract 22564119                               # one listing
.venv/bin/qc-screener extract --all --source centris --limit 50      # batch
.venv/bin/qc-screener extract --all                                  # everything not yet extracted (~$1 for ~700 listings on Haiku 4.5)
.venv/bin/qc-screener extract 22564119 --refresh                     # re-run, ignore cache

# Dump raw HTML of one URL (useful when a scraper breaks)
.venv/bin/qc-screener dump 'https://www.centris.ca/fr/...../12345678' --out scratch.html
```

---

## 6. Cache management

Caches are URL-hashed files under `data/cache/<source>/`. No TTL — they persist until explicitly removed.

### Bust everything (force full re-fetch on next crawl)

```bash
rm -rf data/cache/*/
.venv/bin/qc-screener crawl --source all --max-pages 20   # will re-fetch from scratch
```

### Bust one source

```bash
rm -rf data/cache/duproprio/
.venv/bin/qc-screener crawl --source duproprio --max-pages 15
```

### Bust one listing (for price-change check)

```bash
.venv/bin/python -c "
import hashlib
url = 'https://duproprio.com/...full-url-here...'
print(hashlib.sha1(url.encode()).hexdigest()[:16])
"
rm data/cache/duproprio/<that-hash>.html
.venv/bin/qc-screener crawl --source duproprio --max-pages 1
```

---

## 7. Sources & cadence

| Source         | Type            | Catalog size       | Throttle | Suggested cadence  |
|----------------|-----------------|--------------------|----------|--------------------|
| DuProprio      | HTML scrape     | ~482 plex          | 3 s      | weekly             |
| ProprioDirect  | JSON API + HTML | ~238 multiplex     | 3 s      | weekly             |
| Centris        | XHR + HTML      | ~4,400 plex        | 4 s      | weekly             |
| Kijiji rents   | NEXT_DATA scrape| ~11,400 rentals/QC | 4 s      | bi-weekly          |
| LogisQuébec    | sitemap + HTML  | ~8,200 apartments  | 3 s      | monthly (slow)     |
| Registre foncier| CC-BY CSV       | ~15K rows agrégés  | n/a      | monthly            |

Source modules: `qc_screener/{duproprio,proprio_direct,centris,kijiji,logisquebec,registre_foncier}.py`.

---

## 8. Streamlit — tabs and what they do

`.venv/bin/streamlit run streamlit_app.py` → http://localhost:8501

**Sidebar global filter:** the "📍 Filtre géographique" slider (default from `LocationFilter.max_km` in `config.py`, currently 175 km) applies to every tab. Toggle "Inclure annonces sans coordonnées" to include listings whose lat/lon couldn't be extracted. See §4 for full details.

| Tab                  | What it shows                                                   |
|----------------------|-----------------------------------------------------------------|
| 🏠 Aperçu            | Catalog totals (in-radius), top-5 by prix/éval and MRB          |
| 🔍 Annonces          | Filterable table (source, units, price, distance), Lépine badge |
| 🗺️ Carte             | OpenStreetMap with all geolocated listings, colorable by metric |
| 💎 Aubaines          | Scatter prix/éval × MRB, Lépine sweet-spot shaded               |
| 📊 Analyseur de deal | Pick a listing, slide offer/financing/unit-mix → live projection|
| 🏘️ Loyers            | Cohort medians + box-plots                                      |
| 📡 Signal régional   | Registre foncier: ratio distress + YoY transfers per region     |
| 📖 Méthode           | Lépine vocabulary explainer (for the sister)                    |

Data refreshes when the SQLite file changes. Reload the browser tab after a crawl.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `qc-screener: command not found` | venv not active or not installed | `cd ~/projects/real-estate-quebec-tool && .venv/bin/pip install -e .` |
| `Annonce <id> introuvable` from `analyze-deal` | Listing not in DB | Run `crawl` first |
| Aubaines chart axes look broken | Garbage placeholder values in DB | Look in §6 to bust the offending source's cache, then re-crawl |
| Streamlit shows old data after crawl | Browser cached the page | Hard refresh (⌘⇧R on macOS) |
| Kijiji crawler returns 0 listings | They updated their Next.js shape | `dump` a search URL and inspect `__NEXT_DATA__` |
| Centris returns 429 | Throttled by their server | Wait 10+ min, drop max-pages |
| Aucun comparable pour cette ville | No rent-comp cohort meets `min_samples` | Lower `--min-samples` or crawl more rents |
| Tabs show 0 listings but DB has many | Distance filter excluding everything | Bump sidebar "Distance max" slider, or edit `home_lat`/`home_lon` in `qc_screener/config.py` |

---

## 10. Adding a new scraper source

Checklist (so the new source plugs into everything cleanly):

1. New module `qc_screener/<source>.py` exposing `crawl_listings(max_pages, region=None) -> Iterator[Listing]` (and `dump_html`).
2. Register in `cli.py` → `SOURCES` dict.
3. If the source has lat/lon, populate `Listing.lat` / `Listing.lon`.
4. Add a row to the **Sources & cadence** table above.
5. Update the **Daily flow** example if cadence needs adjusting.
6. If it's a NEW *kind* of data (e.g. rent comps from a new portal), also register under `RENT_SOURCES` and expose via `rents fetch --source <name>`.
