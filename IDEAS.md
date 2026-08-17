# IDEAS — parking lot for unpursued directions

Suggestions and side-paths surfaced during work but not pursued in the turn they came up. Revisit when picking the next thing to build.

Each entry: a *filed date* (when the idea came up) and, if pursued, a *done date* + one-line of what shipped.

---

## 🔲 To do

### HTTP-layer retry/backoff for transient timeouts *(filed 2026-08-15)*

The first nightly (2026-08-15) died mid-crawl on a single `ReadTimeout` and skipped ~292 extractions with `Connection error` — an overnight network drop (Mac likely asleep). The crawl per-source guard + extract per-listing guard (commit `0de3237`) contain the blast radius, but each transient network blip still loses that unit of work for the night. A small retry-with-backoff wrapper in the shared httpx client (crawlers + `llm_extract`) would ride out brief drops. Deferred: touches every source module for marginal gain on a personal tool that self-heals on the next nightly, and the launchd job already catches up on wake. Revisit if outages prove frequent.

### Automate the weekly/monthly refreshes *(filed 2026-07-31)*

The nightly automates the daily pipeline (crawl → prune → extract → notify), but weekly `rents fetch` and monthly `macro refresh` + `schl refresh` remain manual. A second launchd plist with `Weekday: 0` (Sunday) / `Day: 1` (monthly) would automate them. Left out of the nightly cut because they change slowly and running them nightly would waste requests.

### Personal Tax Considerations (ex. mortgage payment deductions)


### Polish on existing tools

- **Rent-comp coverage for small cities** *(filed 2026-06-19)*
  Still thin as of 2026-08-16 — 82 of 93 cities have <5 comps: Trois-Rivières (n=3), Lévis (n=8), Drummondville (n=1). The 882-comp sample spreads too thin across 8K apartments. Options: (a) raise crawl to 1000+ listings (~50 min); (b) targeted crawl by region — walk `/a-louer/<region>` or filter sitemap URLs by city slug; (c) cross-city interpolation (nearest metro's cohort as fallback). Option (b) is probably cleanest.

- **Deal-analyzer — TAL/régie constraints** *(filed 2026-06-21)*
  Quebec's Tribunal administratif du logement caps rent increases at ~2-4%/year and renters have strong tenure; the rent-reset thesis can only realize as units turn over. Add a `--rent-reset-years` flag (default 1 = immediate, 5 = phased over turnover horizon) that ramps stabilized revenue linearly from listing-reported to target. Refines PAT 1128857-style cases.

### New tools / sources

- **Scrape declared `annual_expenses` from listings** *(filed 2026-07-02)*
  Prerequisite for empirical expense-ratio cohorts. Still 0/5173 listings have `annual_expenses` populated as of 2026-08-16 (only revenues + taxes). Centris + PD typically list "Dépenses totales" or "Dépenses annuelles" in the financial detail table; DuProprio less consistent. Once 100+ listings have it, `analyzer.estimate_expense_ratio` can add an empirical branch (median declared_expenses / declared_revenue by units×region cohort) with fallback to the current Lépine tiered defaults.

- **StatCan / Teranet HPI for cleaner appreciation** *(filed 2026-07-02)*
  Current Registre foncier appreciation uses weighted band-midpoints (±1pp noise from the >500K band being unbounded). A cleaner alternative: pull the New Housing Price Index (StatCan Table 18-10-0205) or Teranet-NBC HPI by CMA. Would give a proper monthly HPI YoY per CMA. Adds ~1h of work for meaningfully lower noise. Only worth doing if the current signal proves too jumpy in practice.

- **General assistant — the third tool** *(filed 2026-06-18)*
  Q&A surface that knows Lépine's method and the user's deal context. Useful once enough structured data flows through the screener and analyzer to ground answers.

- **Densification / zoning-arbitrage angle** *(filed 2026-06-18)*
  Scan municipal urbanism documents to identify properties where zoning changes could unlock value. Off-target for vanilla Lépine but a genuinely different alpha source. Stack reference: `rhanka/radar-immobilier`.

---

## ✅ Done

### 2026-08-16

- **Automation initiative — nightly refresh + push notifications (went live)** *(filed 2026-07-02, code shipped 2026-07-31, live 2026-08-16)*
  All 6 steps of the automation initiative shipped and the nightly is now installed and running. Steps: schema (`is_active`/`last_seen_at`/`notified_at`/`last_price_notified` + `price_history` table), `crawl --full`, `prune`, price-history/drop-detection, `notify --scan` (ntfy.sh push for NEW passers + DROP events), and launchd nightly wiring (`scripts/nightly.sh` + `com.qc-screener.nightly.plist.template` + `install-launchd.sh` + `uninstall-launchd.sh` + RUNBOOK §11). Nightly at 03:00 runs `crawl --full && prune --days 7 && extract --all && notify --scan`, continuing past step failures so a broken crawl doesn't silence the notify. (`--full` = 200-page budget per source, auto-stopping on the first empty page, so it walks each source's *entire* catalog nightly — ~456 DuProprio, superseding the old "15-page crawl" limitation.) Secrets sourced from `~/.qc-screener.env`; logs to `data/logs/nightly-YYYY-MM-DD.log` (30-day rotation); failure alert pushed via ntfy.sh (high priority). Missed runs (Mac asleep) catch up on next wake — every step is idempotent.
  **Go-live setup (2026-08-16):** private ntfy topic generated (`openssl rand -hex 8`) + subscribed on phone; `~/.qc-screener.env` written (`chmod 600`) with topic + `ANTHROPIC_API_KEY` (verified valid + billing active via a minimal messages call); backlog absorbed with `notify --mark-only` (54 listings) so only genuinely-new listings/drops fire; launchd agent loaded + verified (`launchctl print`, scheduled 03:00). The first test-fire ran end-to-end and surfaced **4 bugs, all fixed + pushed**: (1) `17745a4` ntfy crash on accented/emoji titles — HTTP headers are latin-1 only → switched to ntfy's UTF-8 JSON API; (2) `a92ed9b` launchd installer false-FATAL — `launchctl list | grep -q` gave launchctl a SIGPIPE (141) that `pipefail` turned into failure → query the label directly; (3+4) `0de3237` extract aborting the whole batch on `float(None)` (apply/persist were outside the loop's try) and crawl aborting all sources on one `ReadTimeout` → per-listing and per-source guards. Real-path proof: the run delivered 4 correct notifications (1 drop + 3 new passers, accents/emoji intact). Final sign-off = first fully clean scheduled run (watch `nightly-2026-08-17.log`).

- **Automate deletion of sold / no-longer-available listings (now fully automated)** *(filed 2026-07-02, storage+pruner 2026-07-30, automated 2026-08-16)*
  Storage layer + pruner shipped. `listings` gained `is_active` + `last_seen_at`; `crawl` (esp. `--full`) stamps `last_seen_at` on every seen listing; `qc-screener prune --days 7` soft-deletes actives whose `last_seen_at` is stale, with two safeguards (refuses if >50 % of actives have NULL `last_seen_at`, or if the most recent crawl itself is older than the window). CLI screener (`run`, `value`) excludes pruned rows by default (`--include-inactive` to include); Streamlit hides them by default (sidebar toggle). Resurrection is automatic — a listing reappearing in a later `crawl --full` flips back to `is_active = 1`. **Now runs untouched** via the launchd nightly (Step 6, above).

- **Detect sold / off-market listings — investigated, common case already handled** *(filed 2026-08-15, closed 2026-08-16)*
  Filed after a "sold" sainte-marguerite listing pinged the phone. Investigation showed the filed premise (a sold listing "stays published with a sold badge") was wrong for the actual listing, and the common case is already covered:
  - **The real culprit was `centris/18658459`** (not `15343216`, a different, active sainte-marguerite-du-lac listing that got conflated). Its DB row: `is_active=0`, `last_seen_at=None` — i.e. **already pruned**.
  - **A sold Centris listing delists**: it drops out of search → crawl stops seeing it → `prune --days 7` marks `is_active=0`. Its detail page 302-redirects to `…?listingnotfound=<id>` showing "Cette propriété n'est plus disponible."
  - **The nightly ordering already protects notify**: crawl → prune → extract → notify, and `notify --scan` only scans `is_active=1`, so a sold/delisted listing is deactivated *before* notify runs. The observed ping was a **one-time backlog artifact** — 18658459 was pre-automation backlog, notified by a manual `notify --scan --limit 1` test *before* the first crawl/prune cycle removed it. Won't recur in steady state.
  - **Residual gap (small, deferred):** a listing *sold-but-still-listed* (conditional sale / accepted offer still shown). Centris renders that badge client-side via JS (`badge-sold`/`badge-under-agreement` exist only in CSS; the server-rendered `badges-container` carries just a `d-none` hidden badge). Detecting it would need reverse-engineering Centris's status XHR — brittle, rare payoff. Cheap fallback if it ever matters: a manual `mark-sold <id>` CLI flag setting `is_active=0`. DuProprio already filters sold at search (`-vendu-` URL skip, `duproprio.py:74`).

### 2026-07-02

- **Streamlit Analyseur — expander with the 4 dynamic-default sliders** *(filed 2026-07-02, done 2026-07-02)*
  Added a "🎛️ Hypothèses de projection (défauts dérivés)" expander INSIDE the Analyseur tab (not the sidebar — sliders need per-listing dynamic defaults to work coherently). Four sliders (apprec, vacance, rent growth, expense ratio), each seeded from the same lookup as the CLI (`registre_foncier.region_appreciation_estimate`, `schl.vacancy_rate_estimate`, `schl.rent_growth_estimate`, `analyzer.estimate_expense_ratio`), with the source label shown as caption ("SCHL Montréal 2025", "Registre foncier Laurentides 12mo", etc.). Slider keys are namespaced by `sid` so switching listing resets the values to the new listing's regional defaults.

- **Smarter expense ratio (Lépine tiered + age adjustment)** *(filed 2026-07-02, done 2026-07-02)*
  New `analyzer.estimate_expense_ratio(units, year_built) -> (ratio, label)` implementing Lépine's own book progression: ≤6 units 40%, 7–12 35%, 13–24 30%, 25+ 27%, plus small age adjustments (pre-1980 +3pp, post-2010 −2pp), capped [25%, 50%]. CLI `analyze-deal` gained `--expense-ratio` (–1 = auto, positive % = override); Hypotheses panel prints the source. Also fixed a coherence bug: the "taxes + 25%" branch had a hardcoded 25% that was tied to the old 40% default — now derives non-tax portion from `expense_ratio - 15pp`, so the two branches move together when the ratio changes. **Not pursued: empirical from DB** (was the original plan). 0 of 861 listings have `annual_expenses` populated by our scrapers, so the empirical cohort has no data. Filed separately (below) as a prerequisite.

- **Dynamic rent growth from SCHL rents (analyzer)** *(filed 2026-07-02, done 2026-07-02)*
  Extended `qc_screener/schl.py` to also ingest StatCan Table 34-10-0133 (average rent by centre × bedroom × structure type). Filtered to "Row and apartment structures of 3 units and over" (matches Lépine multi-loger scope), averaged across bedroom types per (city, year), then computed a 3-year CAGR per city — smooths the 2024–2025 QC rent-shortage spike into something projectable. Function `rent_growth_estimate(city)` reuses the same satellite→CMA mapping as vacancy. Current CAGRs: Montréal +9.5%, Sherbrooke +9.6%, Trois-Rivières +15.5% (fastest CMA), Saguenay +2.8%, provincial median +8.5% (vs. old flat 2.5%). CLI `analyze-deal` gained `--rent-growth`; Hypotheses panel prints the source. **Bug caught & fixed:** initial `load_rates()` iterated every cached CSV including rents.csv (dollars), mixing rent $ into the vacancy dict — split by table_id.

- **Dynamic vacancy rate from SCHL/StatCan (analyzer)** *(filed 2026-07-02, done 2026-07-02)*
  New source module `qc_screener/schl.py` pulling StatCan Table 34-10-0127 (vacancy by RMR — Montréal 3.3%, Québec 2.6%, Sherbrooke 2.5%, Trois-Rivières 2.7%, Ottawa-Gatineau 4.6%, Saguenay 2.6%) and Table 34-10-0132 (vacancy by AR — 30 smaller centres). Both tables merged into a single `{city_norm: (year, rate)}` map covering 40 QC centres, plus a `SATELLITE_TO_CMA` table so Laval/Longueuil/Brossard inherit Montréal's rate, Lévis inherits Québec's, etc. Function `vacancy_rate_estimate(city)` returns `(fraction, source_label)` with fallback to province-wide average (~1.69%). CLI `analyze-deal` gained `--vacancy` (–1 = auto, positive % = override), Hypotheses panel shows the source. Deal 22564119 Saint-Hippolyte (no direct entry) now uses 1.69% instead of 5% — cashflow gains ~$1.5K/yr just from that change.

- **Dynamic appreciation from Registre foncier (analyzer)** *(filed 2026-07-02, done 2026-07-02)*
  Replaces the hardcoded `annual_appreciation=2.5%` with a region-specific YoY derived from `ventes_par_prix` band-share drift. Method: weighted midpoint per (region, 12-mo window) using band midpoints ($175K / $375K / $700K), YoY change = appreciation proxy. Added `price_proxy_recent`, `price_proxy_prior`, `price_yoy_pct` to `region_stats()` and a `region_appreciation_estimate(region_name)` helper. CLI `analyze-deal` now accepts `--appreciation` (–1 = auto-detect regional; positive % = override), and the Hypotheses panel prints which source was used. Current 12-mo signal: Mauricie +10.2%, Abitibi +8.1%, Laurentides +6.4%, Montréal +3.1% (vs. the old flat 2.5%). Deal 22564119 Saint-Hippolyte (Laurentides) now uses +6.4% instead of +2.5%. **Known limitation**: band 3 (>500K) is unbounded, so the midpoint anchor of $700K is a heuristic — the YoY has ±1pp of noise. See "SL slider for override" (below in To do). Reason we didn't use a cleaner source (StatCan HPI / Teranet): keeps everything from data we already ingest; can upgrade later.

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
