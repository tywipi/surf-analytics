# surf-analytics

Tools for analyzing [Surf Lending](https://surflending.org) markets on Cardano.

## LTV Monitor

The first module: a two-part tool that mirrors Surf's market data and lets you **sort every column, including LTV**.

- **`scripts/fetch_surf.py`** — the data engine. Pulls live pool data from Surf's Flow Lending V1 API (`GET /api/getAllPoolInfos`), normalizes it, and writes `data/markets.json`. Runs server-side, so it is not affected by browser CORS.
- **`index.html`** — the view. A dependency-free static dashboard that reads `data/markets.json` and renders a sortable table. Click any column header to sort; click again to reverse. Defaults to **Max LTV, descending**. Filter box matches loan/collateral tickers.

## Position Risk Monitor

The second module: finds positions that are **underwater or at risk of liquidation**, protocol-wide.

- **`scripts/fetch_positions.py`** — joins `GET /api/getAllPositions` (every borrow) with `GET /api/getAllPoolInfos` (prices + liquidation thresholds), then computes per-position **health factor**, **distance-to-liquidation**, and **liquidation price**. Writes `data/positions.json`. Self-checks accuracy by recomputing LTV from amounts/prices and comparing to the API's `ltv` (the `ltv_check` block — `max_abs_diff` should be tiny).
- **`positions.html`** — four linked views off one dataset: (1) a **health-factor table**, worst-first, with liquidatable rows in red; (2) a **position scatter** of buffer vs debt where danger positions pop out; (3) a **liquidation cliff heat map** (pools × price drops, colored by debt liquidated) that exposes cascade cliffs; (4) a **price-shock stress test** with a global slider plus per-asset overrides that recompute liquidatable debt live across all views.

Health factor = liquidation LTV ÷ current LTV (≤1 = liquidatable). Since Surf loans are overcollateralized and non-recourse, "at risk of default" means current LTV approaching the liquidation threshold — driven mostly by collateral price moving, which is exactly what the stress test probes.

## Why one row per pool → collateral pair

In Surf, a *pool* is a single borrowable asset (e.g. supply/borrow iUSD) that accepts **several collaterals**, and each collateral carries its own `maxBorrowLTV`, `recommendedBorrowLTV`, and `liquidationThresholdLTV`. LTV is therefore a property of the **(loan asset, collateral asset) pair**, not the pool. The fetcher emits one row per pair so "sort by LTV" is meaningful and matches how lending dashboards present markets. Pool-level aggregates (supplied, borrowed, utilization, APYs) are carried on every row of that pool.

Amounts from the API are in each asset's smallest unit; the fetcher scales them by `10**decimals`.

## Quick start

```bash
# 1. pull live data
python scripts/fetch_surf.py --pretty

# 2. view it (must be over http, not file://, or the fetch() is blocked)
python -m http.server 8000
# open http://localhost:8000
```

No pip installs — the fetcher uses only the Python standard library (3.8+).

### Useful flags

```bash
python scripts/fetch_surf.py --out data/markets.json   # where to write
python scripts/fetch_surf.py --pretty                  # indent the JSON
python scripts/fetch_surf.py --dump-raw                # also save the untouched API response to data/_raw.json
```

## Deploy on GitHub Pages

1. Push this repo to GitHub.
2. **Settings → Pages → Source: Deploy from a branch**, pick `main` / root.
3. Live at `https://<user>.github.io/surf-analytics/` (market dashboard) and `/positions.html` (risk monitor). The two pages link to each other.

The committed `data/markets.json` and `data/positions.json` are what Pages serves. The workflow below refreshes both on a schedule.

## Auto-refresh (GitHub Actions)

`.github/workflows/refresh.yml` runs `fetch_surf.py` on a cron and commits the updated `data/markets.json`. Adjust the schedule to taste. You can also trigger it manually from the Actions tab.

## Data fields per row

| Field | Meaning |
|---|---|
| `loan_asset` | pool asset you supply / borrow |
| `collateral_asset` | accepted collateral for this row's LTV |
| `max_ltv` / `recommended_ltv` / `liq_threshold` | collateral LTVs (0–1) |
| `supplied` / `borrowed` / `available` | pool totals, human units |
| `utilization` | pool `u` (borrowed / supplied) |
| `supply_apy` / `borrow_apr` / `historical_apy` | rates as decimals |
| `loan_price` / `collateral_price` | asset prices |
| `total_volume` | pool lifetime volume |
| `raw_pool` | original `PoolInfo` (minus `collateralAssets`) for debugging |

## Notes

- If the API shape changes, the only file to touch is `scripts/fetch_surf.py` — specifically `normalize_pool_info()`. The dashboard works off the normalized schema.
- LTV risk shading on the dashboard (green / amber / red) is illustrative, not financial advice.
- `data/markets.json` ships with a simulated snapshot so the dashboard renders before your first live fetch. Run the fetcher to replace it.
