# IDEAS — parking lot for unpursued directions

Suggestions and side-paths surfaced during work but not pursued in the turn they came up. Revisit when picking the next thing to build.

Each entry: a *filed date* (when the idea came up) and, if pursued, a *done date* + one-line of what shipped.

---

## 🔲 To do

### High-value data we already have but haven't parsed

- **Per-unit rents from listing prose** *(filed 2026-06-21, partial 2026-06-22)*
  We now capture the full description text — so the data is ready. A regex pass across 100 cached DuProprio listings showed only ~3% yield (most sellers write annual totals, not per-unit). Real extraction needs an LLM (filed separately) or domain-tuned prompts; the regex-only approach isn't worth the maintenance burden. Description is in the model + Streamlit so the user can read it directly.

### Polish on existing tools

- **Rent-comp coverage for small cities** *(filed 2026-06-19)*
  Trois-Rivières (n=1), Lévis (n=5), Drummondville (n=0): the 250-listing LogisQuébec sample spreads too thin across 8K apartments. Options: (a) raise crawl to 1000+ listings (~50 min); (b) targeted crawl by region — walk `/a-louer/<region>` or filter sitemap URLs by city slug; (c) cross-city interpolation (nearest metro's cohort as fallback). Option (b) is probably cleanest.

- **Full 44-page DuProprio crawl** *(filed 2026-06-18)*
  Current crawl is 15 pages (~165 listings). Full catalog is ~482 listings across 44 pages. Worth doing once the data model is stable so percentile-based `value` ranking has meaningful denominators.

- **Deal-analyzer — TAL/régie constraints** *(filed 2026-06-21)*
  Quebec's Tribunal administratif du logement caps rent increases at ~2-4%/year and renters have strong tenure; the rent-reset thesis can only realize as units turn over. Add a `--rent-reset-years` flag (default 1 = immediate, 5 = phased over turnover horizon) that ramps stabilized revenue linearly from listing-reported to target. Refines PAT 1128857-style cases.

### New tools / sources

- **General assistant — the third tool** *(filed 2026-06-18)*
  Q&A surface that knows Lépine's method and the user's deal context. Useful once enough structured data flows through the screener and analyzer to ground answers.

- **LLM extraction of revenue from listing prose** *(filed 2026-06-18, done 2026-06-22)* — moved to Done

- **Densification / zoning-arbitrage angle** *(filed 2026-06-18)*
  Scan municipal urbanism documents to identify properties where zoning changes could unlock value. Off-target for vanilla Lépine but a genuinely different alpha source. Stack reference: `rhanka/radar-immobilier`.

---

## ✅ Done

### 2026-06-22

- **LLM extraction of structured fields from description** *(filed 2026-06-18, done 2026-06-22)*
  `qc_screener/llm_extract.py` calls Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) with a French few-shot system prompt (cached via `cache_control: ephemeral`) and extracts: `per_unit_rents`, `per_unit_sizes`, `total_annual_revenue`, `renovations_done`, `renovations_needed`, `units_occupied`, `vacant_unit_notes`, `seller_motivation`, `rent_reset_potential` (low/medium/high/unknown), plus a `confidence` score. Results cache to `data/cache/llm_extract/<sha-of-desc>.json` so re-runs are free. CLI: `qc-screener extract <id>`, `extract --all --source centris --limit N`, `--refresh` to bust. `analyze-deal` and the Streamlit Analyseur tab now auto-prefer the LLM-extracted rents (rents × 12) over cohort medians when available. Streamlit Analyseur tab shows a "🤖 Faits extraits" panel with per-unit rents, rent-reset potential emoji, renos done/needed, seller motivation, and the confidence score. Requires `ANTHROPIC_API_KEY` env var. Estimated cost: <$1 for the full ~700-listing catalog.

- **Listing description extraction** *(filed 2026-06-22)*
  Added `Listing.description` and wired the full text capture across all three sources. DuProprio + ProprioDirect pull from the JSON-LD `description` field (unescaped); Centris from `div[itemprop="description"]`. Coverage: DuProprio 159/159, ProprioDirect 236/238, Centris 284/306. Useful for Streamlit display and as the input for the future LLM-based per-unit-rent extraction.

- **Characteristics dict — ProprioDirect** *(filed 2026-06-22)*
  Extended PD's regex-based label/value extraction to capture flexible characteristics (Équipement, Sous-sol, Toiture, Zonage, Garage, Revêtements, etc.). 236/238 coverage. Symmetric with DuProprio + Centris.

- **DuProprio characteristics — selector bug fix** *(filed 2026-06-22)*
  selectolax's `row.css("div")` returns the outer container plus the inner label/separator/value divs (4 total, not 3). Switched to `divs[1]` for label / `divs[-1]` for value, which makes the characteristics dict actually usable instead of garbled "Prix demandé534 900 $" composite keys.

- **Kijiji extras (parking, date dispo, amenities)** *(filed 2026-06-22)*
  Added `RentComp.characteristics: dict[str, str]` and populated from each ad's `attributes.all` Apollo entries (`numberparkingspots`, `dateavailable`, `petsallowed`, `yard`, `hydro`, `visualaids`, etc.). Improves rent-comp matching quality once we wire features-based filtering.

- **Ventes par plage de prix (Registre foncier)** *(filed 2026-06-21)*
  Found the band labels in the Données Québec XLSX summary workbook (only 3 codes: 1=<250K, 2=250K-500K, 3=>500K) and hardcoded them as `registre_foncier.PRICE_BANDS`. `region_stats()` now returns `share_band1/2/3` (price-band share of recent sales) + `share_band1_yoy_pp`. CLI `macro regions` and the Streamlit Signal régional tab both surface these new columns. **Concrete Lépine insight**: regions where >40% of recent sales are under $250K (Gaspésie 75%, Bas-Saint-Laurent 59%, Saguenay-LSJ 49%, Chaudière-Appalaches 48%, Abitibi 47%, Mauricie 43%) are where his entry-price range matches market reality — natural hunting grounds for first multilog.

- **Capture taxes municipales + scolaires** *(filed 2026-06-21)*
  Added `Listing.municipal_tax` + `Listing.school_tax` and wired extraction across all three buying sources. Centris pulls from `.financial-details-table-yearly`; ProprioDirect from labelled rows; DuProprio from the Desjardins mortgage calc's "Sommaire des dépenses". Coverage: PD 237/238, Centris 294/300, DuProprio 152/159. `analyzer.DealInputs.known_taxes` now feeds the expense model — when present, base expenses = `taxes + 25% of revenue` (rather than the flat 40% guess). Saint-Hilaire 22564119 shifted NOI $19K → $20.5K, TGA 4.95% → 5.34%, cashflow –$86 → –$44/porte. Streamlit Annonces table + analyzer caption surface the values.

- **Évaluation Terrain vs Bâtiment** *(filed 2026-06-21)*
  Added `Listing.eval_land` + `Listing.eval_building`. PD + Centris expose both (PD: 229/238, Centris 288/300, with the `Bâtiment` HTML-entity bug `&#xE2;` discovered and fixed via `html.unescape`); DuProprio shows only the aggregate so it stays None for that source. Streamlit Annonces table shows `land_share = eval_land / municipal_evaluation`, useful for spotting densification candidates (high land share).

- **Date posted** *(filed 2026-06-21)*
  Added `Listing.date_posted`. ProprioDirect has it in the search API (`inscriptionDate`) and we now parse it: 238/238 covered. DuProprio + Centris don't expose listing date in the public detail HTML; left at None.

- **Characteristics dict (flexible)** *(filed 2026-06-21)*
  Added `Listing.characteristics: dict[str, str]` to capture all the side fields we don't want as first-class columns (heating type, parking count, certificat de localisation, lot area, etc.). DuProprio + Centris populate it from their characteristic-row sections (159 + 300 listings respectively). ProprioDirect wiring deferred.

### 2026-06-21

- **Weight macro signal into the `value` ranking** *(filed 2026-06-21)*
  `qc_screener/regions.py:normalize_region()` maps slug/free-text region labels (DuProprio slugs like `monteregie-rive-sud-montreal`, ProprioDirect names, Centris title segments) to the 17 canonical admin regions of QC used by the Registre foncier. `value` CLI now defaults to a macro-weighted score: `price_to_eval × (1 + α·yoy_transferts − β·ratio_distress)` with α=0.02, β=1.0 (tunable, `--no-macro` disables). Centris detail-parse also now extracts region from the page `<title>`. Result: Montréal listings dominate the new top-10 thanks to 6.31% distress + –4% YoY tail-wind.

- **Deal-analyzer polish** *(filed 2026-06-20)*
  Major upgrade in `qc_screener/analyzer.py`:
  - **Year-by-year projection** over a configurable horizon (default 10 years) instead of just Y1+Y5.
  - **True IRR 5 ans** via Newton's method, including the simulated exit (sale at year 5: value × (1−sell_costs_pct) − loan_balance − VTB).
  - **Quebec welcome tax** (taxe de bienvenue / droits de mutation) computed per-listing via the standard 0.5/1.0/1.5% provincial brackets — replaces the old flat 2.5% closing cost.
  - **CMHC premium** auto-added to the mortgage principal when MdF < 20% (4 logements et moins): 5%→4.0%, 10%→3.1%, 15%→2.8%. Skipped for 5+ units (commercial CMHC out of scope).
  - **Sensitivity matrix** (price × stabilized revenue grid) exposed in the Streamlit Analyseur tab with a 7×7 heatmap colorable by IRR / cashflow / TGA.
  Streamlit Analyseur tab now shows year-by-year table, cashflow line chart, capital breakdown with welcome tax + SCHL, and the sensitivity heatmap.

- **Streamlit map view** *(filed 2026-06-19)*
  Added `lat`/`lon` fields to the `Listing` model and wired extraction across all three sources: DuProprio (regex on embedded `"latitude":X` JSON), Centris (regex on JS vars `latitude = X; longitude = Y;`), ProprioDirect (`geoLocation.lat/lon` from the API entry). New "🗺️ Carte" tab in Streamlit uses `plotly.express.scatter_mapbox` with OpenStreetMap tiles (no token required). Colorings: Prix/éval, MRB, Cashflow/porte, Source, Passe Lépine; min-units filter; marker size scaled to asking price; hover shows title + URL. Geo coverage: 100% across the existing 517 stored listings.

- **Centris scraper** *(filed 2026-06-18)*
  Akamai turned out to be a paper tiger for read-only multilogement scraping. `qc_screener/centris.py` primes session cookies via `GET /fr/plex~a-vendre`, then paginates via `POST /Property/GetInscriptions` (the page's internal XHR). The response is `{d: {Result: {html, count, ...}}}`; we parse listing URLs from the HTML fragment, then fetch each detail page directly. Detail parsing pulls `meta[itemprop=price]`, `.carac-container` rows (Année / Nombre d'unités / Revenus bruts potentiels) and the "Évaluation municipale" table total via regex anchored on the section header. **Catalog: 4,401 plex** — 6× our combined DuProprio + ProprioDirect inventory. First-page smoke test shipped 20 clean Listings; immediately reshaped the `value` leaderboard with multiple sub-0.85× éval candidates.

- **Cleaner h1 title parsing** *(filed 2026-06-18)*
  Both `duproprio.parse_listing` and `proprio_direct._build_listing` now compose titles as `"<Type> — <adresse>, <ville>"` (e.g. *"Triplex — 10927 avenue de Rome, Montreal-Nord"*). DuProprio: pulls `property_type` from the h1 `<a>` text minus the " à vendre" suffix + first span of `p.listing-location__address`. ProprioDirect: combines `genreName` + `addressLine` + `cityName` from the API entry. All 397 stored listings re-parsed from cached HTML — no fresh fetches.

- **Registre foncier macro signal** *(filed 2026-06-18)*
  `qc_screener/registre_foncier.py` downloads 4 CC-BY CSVs from donneesquebec.ca (CKAN API), caches to disk, aggregates by region × month. CLI: `macro refresh` then `macro regions [--months 12]`. Streamlit "Signal régional" tab shows distress-ratio + YoY growth bars. **Headline**: Montréal ratio distress 6.31% (highest in QC) with –4% YoY transfer volume = best buyer's market in the province.

### 2026-06-20

- **Deal analyzer — the second tool** *(filed 2026-06-18)*
  `qc_screener/analyzer.py` + `qc-screener analyze-deal` CLI + Streamlit "Analyseur" tab. Inputs: prix d'offre, unit mix, financement (MdF/taux/am./balance de vente), capex. Auto-fills stabilized rents from rent-comp cohort medians via `market.estimate_market_revenue`. Outputs: Hypothèses, ventilation par logt (marché), capital requis, projection Année 1 / Année 5 (TGA, MRB, NOI, cashflow/porte, valeur, solde hypo, avoir net), retours 5 ans.

### 2026-06-19

- **Kijiji + LogisQuébec as rent-comps sources** *(filed 2026-06-18)*
  - *Kijiji* (`kijiji.py`): listings extracted from `__NEXT_DATA__.props.pageProps.__APOLLO_STATE__` Apollo cache (~46/page, 11,425 in QC).
  - *LogisQuébec* (`logisquebec.py`): search page is JS-hydrated, but the XML sitemap exposes 8,213 apartment URLs. Crawl walks the sitemap then fetches each detail page for Twitter card meta tags + characteristic blocks (chambres / pieces / pi²). Sampling is evenly-spaced for broad geographic coverage.

- **Rent-comp city normalization** *(filed 2026-06-19)*
  `qc_screener/cities.py:normalize_city()` does accent-strip + alias map + paren extraction + " / suffix" split + substring fallback. Wired into `storage.upsert_rent_comp` (canonical → indexed column, raw → JSON payload). Backfill via `qc-screener rents renormalize`. **Impact**: Montréal 2ch went from 3 noisy cohorts ($1,588 / $1,749 / $1,850) → 1 clean cohort n=125, median $1,794.

- **Wire rent comps into the deal analyzer** *(filed 2026-06-19)*
  `market.estimate_market_revenue()` queries (canonical city, bedrooms) cohort and returns per-logement breakdown + total stabilized annual revenue. `analyze-deal` auto-fills `stabilized_revenue` when not user-supplied, with `--unit-mix '2,2,1,2'` and `--no-market`. PAT 1128857 surfaced a **+140% rent gap** ($26,880 → $64,584) — confirms rent-reset thesis quantitatively but shows it's not enough alone to clear Lépine's bar.

- **ProprioDirect scraper** *(filed 2026-06-18)*
  Public JSON API (`POST /fr/api/searchListings` with `filter.genre=multiplex`) + HTML scrape of detail pages for eval/revenue/year. ~10× faster than HTML-only scraping. Catalog: 238 multiplexes across 8 pages.
