<div align="center">

# ≋ surf-analytics

### Risk & market analytics for [Surf Lending](https://surflending.org) on Cardano

Sort markets by LTV. See which loans are underwater. Stress-test the protocol against a price crash — all in two fast, dependency-free dashboards.

[**▶ Open the Market Dashboard**](https://tywipi.github.io/surf-analytics/) · [**▶ Open the Risk Monitor**](https://tywipi.github.io/surf-analytics/positions.html)

![status](https://img.shields.io/badge/data-live%20via%20Surf%20API-4fd1c5)
![stack](https://img.shields.io/badge/stack-static%20HTML%20%2B%20Python%20stdlib-2a8076)
![units](https://img.shields.io/badge/units-ADA%20%2F%20USD-d29922)
![deps](https://img.shields.io/badge/dependencies-none-3fb950)

</div>

---

## What this is

A pair of tools that mirror Surf Lending's on-chain data and turn it into something you can actually read at a glance.

- **Market Dashboard** — every pool→collateral market in one sortable table. Sort by LTV, rates, utilization, or size.
- **Position Risk Monitor** — every borrow position across the protocol, ranked by how close it is to liquidation, with a live price-crash stress test.

Both run as plain static pages (no build step, no framework, no tracking) and both flip between **ADA** and **USD** with one click.

---

## The three tools

### 📊 Market Dashboard &nbsp;·&nbsp; `index.html`

The whole lending market, one table, sort any column.

- One row per **pool → collateral pair**, because in Surf each LTV belongs to a specific collateral, not the pool as a whole.
- Click any header to sort; click again to reverse. Defaults to **Max LTV, descending**.
- Color-graded LTV column (green → amber → red) so risky markets jump out.
- Filter box matches loan/collateral tickers.

### 🚨 Position Risk Monitor &nbsp;·&nbsp; `positions.html`

Find loans that are underwater or near liquidation — protocol-wide. Four linked views off one dataset:

| View | What it answers |
|---|---|
| **Health-factor table** | Which exact positions are closest to liquidation (worst-first, liquidatable rows in red) |
| **Position scatter** | Where the danger concentrates — buffer vs debt size, so a whale on the edge pops out |
| **Liquidation cliff heat map** | At what price drops cascades trigger, per pool |
| **Price-shock stress test** | "If collateral falls X%, how much debt gets liquidated?" — a global slider plus per-asset overrides (top collateral by debt), recomputing live |

> **Health factor** = liquidation LTV ÷ current LTV (≤ 1 = liquidatable). Surf loans are overcollateralized and non-recourse, so "at risk of default" really means *current LTV approaching the liquidation threshold* — driven mostly by collateral price, which is exactly what the stress test probes.

### 🎯 Pool Scorecard &nbsp;·&nbsp; `scorecard.html`

Which pools are good to **supply** to or **borrow** from. Three views:

| View | What it does |
|---|---|
| **Scores** | Ranks pools 0–100 with a composite supply or borrow score, component bars showing what drives each |
| **Weights** | Sliders to re-weight the score components to match how *you* judge a pool — ranking re-sorts live |
| **Raw metrics** | The underlying numbers, sortable, with no scoring opinion baked in |

> Scores are **opinionated and illustrative, not advice.** Supply favors yield earned against safe, healthy collateral (it penalizes pools where lots of debt sits near liquidation); borrow favors cheap, liquid pools with room before liquidation. The default weights are a starting point — tune them in the Weights view.

## Screenshots

> _Add screenshots here once Pages is live — drop images in a `docs/` folder and reference them:_
> `![Market Dashboard](docs/market.png)` · `![Risk Monitor](docs/risk.png)`

---

## Quick start

Pull live data, then open the dashboards locally:

```bash
# 1. fetch live data from Surf (writes data/*.json)
python scripts/fetch_surf.py --pretty
python scripts/fetch_positions.py --pretty

# 2. serve over http (not file://, or the browser blocks the fetch)
python -m http.server 8000
```

Then open **http://localhost:8000** (market) and **http://localhost:8000/positions.html** (risk).

No installs needed — the fetchers use only the Python standard library (3.8+).

---

## Deploy on GitHub Pages

1. Push this repo to GitHub.
2. **Settings → Pages → Source: Deploy from a branch** → `main` / root → **Save**.
3. Live within a minute or two at:
   - **Market:** https://tywipi.github.io/surf-analytics/
   - **Risk:** https://tywipi.github.io/surf-analytics/positions.html

The dashboards serve whatever `data/*.json` is committed, so the live site is as fresh as your last data push.

### Keep it fresh automatically

`.github/workflows/refresh.yml` re-runs both fetchers every 6 hours and commits the updated data — no local work. Enable it once: **Settings → Actions → General → Workflow permissions → Read and write → Save**. You can also trigger it manually from the **Actions** tab.

### History accumulation

Each run also appends a compact daily snapshot of per-pool metrics to `data/history/YYYY-MM-DD.json` (with a `data/history/index.json` listing available days). This quietly builds a time series so a future **trends view** (rate stability, APY/TVL over time) becomes possible — it isn't surfaced in the UI yet, it just accumulates. One small file per day keeps git diffs clean.

---

## ADA / USD toggle

Both dashboards switch units with one click (defaults to **ADA**). Surf prices everything in ADA internally, so USD is just ADA × the ADA/USD rate — each fetcher pulls that rate once per run from CoinGecko's free endpoint and bakes it into the JSON. No live browser call, works offline, and if the rate is ever unavailable the USD button simply greys out and everything stays in ADA.

The toggle only rescales **money** (supplied/borrowed/available, debt, liquidation price, stress-test totals). LTV, health factor, utilization, and APY are ratios — they never change with the unit. The Risk Monitor also has a manual rate-override box for hypotheticals.

---

## How it works

```
Surf API ──┐
           ├─ fetch_surf.py ──────► data/markets.json ───► index.html
CoinGecko ─┤
           └─ fetch_positions.py ─► data/positions.json ─► positions.html
```

- **Fetchers (Python)** run server-side, so they're immune to browser CORS. They normalize Surf's raw API into a clean schema and bake in the ADA/USD rate.
- **Dashboards (HTML/JS)** are static — they only read the committed JSON. All sorting, filtering, unit conversion, and stress-testing happens in the browser.

**One row per pool → collateral pair:** a Surf *pool* is one borrowable asset that accepts several collaterals, each with its own `maxBorrowLTV` / `recommendedBorrowLTV` / `liquidationThresholdLTV`. LTV is a property of the *pair*, so the fetcher emits a row per pair — that's what makes "sort by LTV" meaningful. Pool-level aggregates ride along on every row.

Amounts from the API are in each asset's smallest unit; the fetcher scales by `10**decimals`.

<details>
<summary><strong>Data fields per row</strong></summary>

| Field | Meaning |
|---|---|
| `loan_asset` | pool asset you supply / borrow |
| `collateral_asset` | accepted collateral for this row's LTV |
| `max_ltv` / `recommended_ltv` / `liq_threshold` | collateral LTVs (0–1) |
| `supplied` / `borrowed` / `available` | pool totals, human units |
| `utilization` | pool `u` (borrowed / supplied) |
| `supply_apy` / `borrow_apr` / `historical_apy` | rates as decimals |
| `loan_price` / `collateral_price` | asset prices (in ADA) |
| `total_volume` | pool lifetime volume |
| `ada_usd` | ADA/USD rate, baked into the snapshot (top-level) |

</details>

<details>
<summary><strong>Fetcher flags</strong></summary>

```bash
python scripts/fetch_surf.py --out data/markets.json   # output path
python scripts/fetch_surf.py --pretty                  # indent the JSON
python scripts/fetch_surf.py --dump-raw                # also save the raw API response for debugging
```
Same flags apply to `fetch_positions.py`.

</details>

---

## Notes

- If Surf's API shape changes, the only thing to edit is the normalizer in the relevant fetcher — the dashboards work off the normalized schema.
- LTV/health risk shading (green / amber / red) is **illustrative, not financial advice**.
- The repo ships with a simulated snapshot so the dashboards render before your first live fetch — run the fetchers to replace it with real data.
