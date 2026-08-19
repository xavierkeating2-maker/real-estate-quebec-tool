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

## Refresh protocol — cheatsheet

Five independent pipelines feed the tool; each has its own cadence.

| # | Pipeline                | Command (prefix `.venv/bin/qc-screener`)                                    | Cadence                              | Wall-time        | 
|---|-------------------------|-----------------------------------------------------------------------------|--------------------------------------|------------------|
| A | Listings — quick        | `crawl --source all --max-pages 20`                                         | Daily / on demand                    | ~5 min           |
| A′| Listings — full walk ⭐ | `crawl --source all --full`                                                 | Weekly (or nightly when automated)   | ~20 min          |
| B | Rent comps — Kijiji     | `rents fetch --source kijiji --max-pages 15 && rents renormalize`           | Bi-weekly                            | ~1 min           |
| B′| Rent comps — LogisQC    | `rents fetch --source logisquebec --max-listings 250 && rents renormalize`  | Monthly                              | ~12 min          |
| C | Registre foncier        | `macro refresh`                                                             | Monthly                              | ~30 s            |
| D | SCHL / StatCan          | `schl refresh`                                                              | Monthly (updates ~yearly, cheap)     | ~30 s            |
| E | LLM extraction          | `extract --all`  *(requires `ANTHROPIC_API_KEY`)*                           | After every full crawl               | ~$0.10 / 100 new |
| F | Prune stale listings ⭐ | `prune --days 7`                                                            | After every full crawl               | <1 s             |
| G | Price history ⭐        | *(auto — populated on every `crawl`)*  · inspect with `price-drops --days 7`| Auto                                 | 0 s              |
| H | Push notifications ⭐   | `notify --scan`  *(requires `NTFY_TOPIC` env var — see §2.1)*               | After every full crawl               | ~15 s            |

Details for each cadence in §1–§3 below.

---

## 1. Daily / on-demand — "what's new today?"

```bash
cd ~/projects/real-estate-quebec-tool
.venv/bin/qc-screener crawl --source all --max-pages 20   # pipeline A
.venv/bin/streamlit run streamlit_app.py                  # browser opens at http://localhost:8501
```

Catches new listings surfacing at the top of each portal. Does NOT catch price changes on already-cached listings (see §6 to bust) and does NOT produce a reliable pruning signal (§2 is required for that).

---

## 2. Weekly — full walk + rent comps + LLM extraction

Run once a week (or wire into launchd once Step 6 of the automation lands):

```bash
.venv/bin/qc-screener crawl --source all --full           # pipeline A′: walks every page, auto-stops on empty
.venv/bin/qc-screener prune --days 7                      # pipeline F: soft-delete listings not seen in 7 days
.venv/bin/qc-screener rents fetch --source kijiji --max-pages 15
.venv/bin/qc-screener rents renormalize                   # pipeline B: fresh rent cohorts
.venv/bin/qc-screener extract --all                       # pipeline E: fill per-unit rents on new listings
.venv/bin/qc-screener notify --scan                       # pipeline H: push new Lépine passers + price drops
```

Why `--full` matters:
- Every listing seen is stamped `last_seen_at = now()`. Anything NOT seen becomes prune-eligible (`prune` will flip `is_active = 0`).
- Ceiling is `FULL_CRAWL_MAX_PAGES = 200`, but each source stops early on the first empty results page — no runaway.
- `--max-pages 20` was walking only ~20 % of Centris; the full walk brings the DB to full coverage (~4,500 listings vs. 861 previously).

Price-history notes:
- **Populated automatically** — every `crawl` compares the incoming `asking_price` against the last observation in `price_history` (per source_id). Only inserts a new row when the price actually changed. No config needed.
- **Baseline seed** — on schema migration, one row per existing listing is inserted with `seen_at = fetched_at`. So drops become detectable immediately, not just after two crawls.
- **Inspect:** `qc-screener price-history <source_id>` shows the timeline; `qc-screener price-drops --days 7` lists recent drops sorted by biggest % first.
- **Streamlit:** the Annonces tab has a "Δ prix 30j" column (empty for stable prices); the Analyseur tab shows a mini line-chart when a listing has ≥2 price points.

### 2.1 Notification setup (one-time, then automatic)

`notify --scan` pushes to your phone/browser via [ntfy.sh](https://ntfy.sh) — free, no signup, no app-store account needed.

**One-time setup:**

1. Pick a private topic slug (must be hard to guess — ntfy.sh topics are public):
   ```bash
   echo "export NTFY_TOPIC=qc-screener-$(openssl rand -hex 6)" >> ~/.zshrc
   source ~/.zshrc
   ```
2. Subscribe on your phone: install the **ntfy** app (iOS / Android), tap `+`, paste your topic slug. OR just open `https://ntfy.sh/<your-topic>` in a browser tab.
3. Absorb the current backlog (one-time — otherwise the first real scan floods you with ~50 existing Lépine passers):
   ```bash
   .venv/bin/qc-screener notify --mark-only
   ```
   This marks every current passer as `notified_at = now()` without sending anything. From now on you only get pings for genuinely new listings and price drops.

**Verify it works before automating:**

```bash
.venv/bin/qc-screener notify --dry-run                    # preview candidates (no send)
.venv/bin/qc-screener notify --dry-run --limit 3          # narrow preview
.venv/bin/qc-screener notify --scan --limit 1             # send one real ping to your phone
```

**Notification triggers:**
- **NEW** — active listing passing Lépine that has never been notified (`notified_at IS NULL`). Sent at default priority.
- **DROP** — Lépine passer whose `asking_price < last_price_notified` by ≥ `--drop-threshold-pct` (default 1 %). Sent at **high** priority when drop ≥ 5 %.

**Gates:**
- `notified_at` (set on first send, never cleared) suppresses re-notification of existing passers.
- `last_price_notified` (updated on each send) is the reference for future drop detection.
- Resurrection of a soft-deleted listing does NOT re-notify — the gates stay set.

**Common flags:**
- `--only-full-pass` — skip `pass_partial` (revenue undisclosed), notify only on `pass`
- `--limit N` — cap number of notifications per invocation (safety valve)
- `--max-km N` — override the default radius from `LocationFilter`
- `--drop-threshold-pct 2.5` — only notify drops ≥ 2.5 %

Pruning notes:
- Order matters: **prune must run after `--full`**, otherwise a partial crawl would prune everything Centris didn't cover.
- Soft-delete only: `is_active = 0`. A future `crawl --full` automatically resurrects a listing that returns.
- Safeguard: `prune` refuses if >50 % of active rows have `last_seen_at = NULL` (signals no full-crawl has run yet after the schema migration).
- Preview first with `prune --dry-run` before applying, especially the first time.
- CLI screener (`run`, `value`) already excludes pruned listings; add `--include-inactive` to include them.
- Streamlit hides pruned listings by default; toggle **"Inclure annonces retirées du marché"** in the sidebar to include them.

Verify cohort medians shifted:

```bash
.venv/bin/qc-screener rents medians --min-samples 5
```

---

## 3. Monthly — macro inputs to the analyzer

```bash
.venv/bin/qc-screener macro refresh                                                # pipeline C
.venv/bin/qc-screener schl refresh                                                 # pipeline D
.venv/bin/qc-screener rents fetch --source logisquebec --max-listings 250          # pipeline B′
.venv/bin/qc-screener rents renormalize

# sanity checks
.venv/bin/qc-screener macro regions --months 12
.venv/bin/qc-screener schl lookup Montréal
```

Where each feeds the analyzer:
- **Registre foncier** → per-region appreciation default in `analyze-deal` + `value --top` macro scoring
- **SCHL** → per-city vacancy + 3-year rent-growth CAGR defaults in `analyze-deal`
- **LogisQuébec** → rent comps for cities Kijiji covers poorly (Saguenay, Rimouski, small-town Estrie)

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

# Analyze a specific listing (dynamic defaults: apprec/vacance/loyer/dépenses per city)
.venv/bin/qc-screener analyze-deal 22564119                          # all auto: region → apprec, city → vac + rent growth, units+age → expense ratio
.venv/bin/qc-screener analyze-deal 22564119 --offer 350000           # custom offer
.venv/bin/qc-screener analyze-deal 22564119 --unit-mix 2,2,1         # custom unit mix
.venv/bin/qc-screener analyze-deal 22564119 --vtb-pct 10 --vtb-rate 6.5  # vendor balance
.venv/bin/qc-screener analyze-deal 22564119 --no-market              # use listing's reported revenue, skip cohort lookup

# Override dynamic assumptions (each defaults to -1 = auto)
.venv/bin/qc-screener analyze-deal 22564119 --appreciation 3.0       # override regional Registre foncier YoY
.venv/bin/qc-screener analyze-deal 22564119 --vacancy 4.0            # override SCHL vacancy
.venv/bin/qc-screener analyze-deal 22564119 --rent-growth 3.0        # override SCHL rent CAGR
.venv/bin/qc-screener analyze-deal 22564119 --expense-ratio 45       # override Lépine tiered ratio

# Price history + recent drops
.venv/bin/qc-screener price-history 22564119                          # timeline of a listing's asking prices
.venv/bin/qc-screener price-drops --days 7 --min-drop-pct 1           # every drop >=1% in last 7 days
.venv/bin/qc-screener price-drops --days 30                           # widen window
.venv/bin/qc-screener price-drops --days 7 --include-inactive         # include soft-deleted listings

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

Caches are URL-hashed files under `data/cache/<source>/`. **No TTL** — they persist until explicitly removed. This means a `crawl` re-uses the cached HTML instead of re-fetching, so **price changes on already-seen listings are invisible until you bust the cache.**

Nuking cache files does NOT touch the SQLite DB. The next crawl re-parses the fresh HTML and upserts back into `listings` (preserving `notified_at` per §5 of storage.py).

### Bust everything (force full re-fetch on next crawl)

```bash
rm -rf data/cache/*/
.venv/bin/qc-screener crawl --source all --full           # ~20 min — re-fetches every page + every listing
```

Pair with `--full` (not `--max-pages 20`): if you're paying the cost of nuking the cache, you want a full walk so every listing gets fresh data AND `last_seen_at` gets stamped.

### Bust one source

```bash
rm -rf data/cache/duproprio/
.venv/bin/qc-screener crawl --source duproprio --full
```

### Bust one listing (targeted — for price-change check on a specific ID)

```bash
.venv/bin/python -c "
import hashlib
url = 'https://duproprio.com/...full-url-here...'
print(hashlib.sha1(url.encode()).hexdigest()[:16])
"
rm data/cache/duproprio/<that-hash>.html
.venv/bin/qc-screener crawl --source duproprio --max-pages 1   # search page 1 will re-fetch the listing detail
```

Note: this only works if the listing is still on page 1 of the search results. If it has scrolled off, run `crawl --source duproprio --full` after the `rm` instead.

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
| 🔍 Annonces          | Filterable table (source, units, price, distance), Lépine badge, Δ prix 30j |
| 🗺️ Carte             | OpenStreetMap with all geolocated listings, colorable by metric |
| 💎 Aubaines          | Scatter prix/éval × MRB, Lépine sweet-spot shaded               |
| 📊 Analyseur de deal | Pick a listing, slide offer/financing/unit-mix → live projection; mini price-history chart when ≥2 observations |
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

---

## 11. Automated nightly job (launchd)

Runs the weekly full-refresh sequence (§2) every night at 03:00 without you touching anything. Sends an ntfy.sh push if any step fails.

### One-time install

```bash
# 1. Create the secrets file (launchd doesn't source ~/.zshrc)
cat > ~/.qc-screener.env <<'EOF'
export NTFY_TOPIC=qc-screener-<your-random-slug>
export ANTHROPIC_API_KEY=sk-ant-...
EOF
chmod 600 ~/.qc-screener.env

# 2. Install the launchd agent
./scripts/install-launchd.sh
```

The installer copies `scripts/com.qc-screener.nightly.plist.template` to `~/Library/LaunchAgents/com.qc-screener.nightly.plist`, substituting the project path, then `launchctl load`s it. It refuses to install if `.venv/bin/qc-screener` is missing or `~/.qc-screener.env` doesn't exist.

### What it runs (in `scripts/nightly.sh`)

```
crawl --source all --full   # ~20 min
prune --days 7              # <1 s
extract --all               # ~15 s + LLM cost
notify --scan               # ~15 s
```

Each step runs independently — a failed `crawl` doesn't skip `notify` (the notify step works on the last-known DB state, and silent failure is worse than a slightly stale notification). Overall exit code = failure count.

### Verify + test-run

```bash
# Confirm launchd registered it
launchctl list | grep qc-screener

# Fire it manually right now (does NOT wait for 03:00)
launchctl start com.qc-screener.nightly

# Watch the log
tail -f data/logs/nightly-$(date +%Y-%m-%d).log
```

### Failure notification

If any step fails AND `NTFY_TOPIC` is set, the runner pushes a **high-priority** ntfy notification titled *"qc-screener nightly failed"* listing which steps errored and where the log is. You'll know immediately even if you're not watching.

### Log rotation

`nightly.sh` deletes `data/logs/nightly-*.log` files older than 30 days on each run. `launchd-stdout.log` and `launchd-stderr.log` (launchd's own capture) are append-only and NOT rotated — truncate them manually if they grow (they should stay tiny — everything real goes to the dated log).

### Missed runs (laptop asleep at 03:00)

launchd's `StartCalendarInterval` fires at the next wake-up if the Mac was asleep at the scheduled time. Only one catch-up run happens even if you missed several days. Each step is idempotent so this is safe.

### Disable / uninstall

```bash
./scripts/uninstall-launchd.sh     # unload + remove plist; keeps ~/.qc-screener.env and logs

# Or temporarily pause without removing:
launchctl unload ~/Library/LaunchAgents/com.qc-screener.nightly.plist
# Resume:
launchctl load ~/Library/LaunchAgents/com.qc-screener.nightly.plist
```

### Not automated (still manual — see §3)

The nightly job intentionally omits weekly/monthly data pipelines:
- `rents fetch --source kijiji` / `rents fetch --source logisquebec` — rent comps
- `macro refresh` / `schl refresh` — Registre foncier + StatCan

These change slowly enough that a nightly run would be wasted requests. If you want them automated too, add a second launchd plist with `StartCalendarInterval` set to a specific day/hour (e.g. `Weekday: 0` for Sunday, `Day: 1` for monthly on the 1st).
