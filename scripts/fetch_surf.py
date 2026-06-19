#!/usr/bin/env python3
"""
fetch_surf.py
Pulls Surf Lending pool data from the Flow Lending V1 API and writes a normalized
snapshot to data/markets.json, which the dashboard reads. Runs server-side, so CORS
is irrelevant.

API: GET https://surflending.org/api/getAllPoolInfos
  -> { "poolInfos": { "<poolId>": PoolInfo, ... } }

Each PoolInfo is one borrowable asset with one or more accepted collaterals. LTV in
Surf is a property of the (pool asset, collateral asset) PAIR, not the pool alone.
So we emit ONE ROW PER pool->collateral pair, each carrying that collateral's
maxBorrowLTV / recommendedBorrowLTV / liquidationThresholdLTV, alongside the
pool-level supply/borrow/APY aggregates. That mirrors how lending dashboards display
"markets" and makes "sort by LTV" meaningful.

Amounts from the API are in each asset's SMALLEST unit; we divide by 10**decimals.

Usage:
    python scripts/fetch_surf.py
    python scripts/fetch_surf.py --pretty --dump-raw
    python scripts/fetch_surf.py --out data/markets.json
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
BASE_URL = "https://surflending.org"
POOLS_ENDPOINT = "/api/getAllPoolInfos"
HEADERS = {"User-Agent": "surf-ltv-dashboard/1.0", "Accept": "application/json"}
TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF = 2.0

# Refresh cadence the GitHub Actions workflow runs at. MUST match the cron in
# .github/workflows/refresh.yml. The dashboards use this to show "next update ~...".
REFRESH_HOURS = 6

# ADA/USD rate source (free, no key). Returns {"cardano":{"usd": <rate>}}.
ADAUSD_URL = "https://api.coingecko.com/api/v3/simple/price?ids=cardano&vs_currencies=usd"


def fetch_ada_usd():
    """Best-effort ADA/USD rate. Returns (rate, source) or (None, reason) on failure
    so the dashboards simply stay in ADA rather than crashing."""
    try:
        from urllib.request import Request, urlopen
        import json as _json
        req = Request(ADAUSD_URL, headers={"User-Agent": "surf-analytics/1.0", "Accept": "application/json"})
        with urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        rate = data.get("cardano", {}).get("usd")
        if isinstance(rate, (int, float)) and rate > 0:
            return float(rate), "coingecko"
        return None, "unexpected_response"
    except Exception as e:  # noqa: BLE001 - any failure just means no USD view
        return None, f"error:{type(e).__name__}"


# ---------------------------------------------------------------------------
# NORMALIZED SCHEMA (one row per pool -> collateral pair)
#   pool_id            str
#   loan_asset         str    pool asset ticker (what you supply / borrow)
#   collateral_asset   str    ticker of the accepted collateral (None = aggregate row)
#   max_ltv            float  collateral.maxBorrowLTV            (0..1)  <- sort key
#   recommended_ltv    float  collateral.recommendedBorrowLTV   (0..1)
#   liq_threshold      float  collateral.liquidationThresholdLTV(0..1)
#   collateral_price   float
#   loan_price         float  pool asset price
#   supplied           float  pool totalSupplied (human units)
#   borrowed           float  pool totalBorrowed (human units)
#   available          float  supplied - borrowed
#   utilization        float  pool `u`, else borrowed/supplied   (0..1)
#   supply_apy         float  decimal
#   borrow_apr         float  decimal
#   historical_apy     float  decimal
#   total_volume       float
#   raw_pool           dict   original PoolInfo (collateralAssets stripped to keep it small)
# ---------------------------------------------------------------------------


def _as_decimal(v):
    """Normalize a rate to decimal form. API LTVs are already 0..1; APYs may be
    decimal or percent. Heuristic: a value > 1.5 is treated as a percent."""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v / 100.0 if v > 1.5 else v


def _ticker(asset):
    if not isinstance(asset, dict):
        return None
    return asset.get("ticker") or asset.get("assetName")


def _decimals(asset, default=6):
    if isinstance(asset, dict) and isinstance(asset.get("decimals"), (int, float)):
        return int(asset["decimals"])
    return default  # ADA and most Cardano stables use 6


def _scaled(value, decimals):
    if value is None:
        return None
    try:
        return float(value) / (10 ** decimals)
    except (TypeError, ValueError):
        return None


def normalize_pool_info(pool_id: str, info: dict) -> list:
    """Turn one PoolInfo into a list of normalized pair-rows."""
    pool_asset = info.get("asset") or {}
    pool_ticker = _ticker(pool_asset)
    pool_dec = _decimals(pool_asset)
    loan_price = info.get("price")

    supplied = _scaled(info.get("totalSupplied"), pool_dec)
    borrowed = _scaled(info.get("totalBorrowed"), pool_dec)
    volume = _scaled(info.get("totalVolume"), pool_dec)
    available = (supplied - borrowed) if (supplied is not None and borrowed is not None) else None

    util = info.get("u")
    if util is None and supplied not in (None, 0) and borrowed is not None:
        util = borrowed / supplied
    # The API reports utilization as a percent (e.g. 74.84 = 74.84%). Normalize to a
    # 0..1 fraction and clamp — utilization can't exceed 100%.
    util = _as_decimal(util)
    if util is not None:
        util = max(0.0, min(1.0, util))

    supply_apy = _as_decimal(info.get("supplyApy"))
    borrow_apr = _as_decimal(info.get("borrowApr"))
    hist_apy = _as_decimal(info.get("historicalApy"))

    # Slim copy of the pool for debugging without bloating each row.
    raw_pool = {k: v for k, v in info.items() if k != "collateralAssets"}

    base = {
        "pool_id": pool_id,
        "loan_asset": pool_ticker,
        "loan_price": loan_price,
        "supplied": supplied,
        "borrowed": borrowed,
        "available": available,
        "utilization": util,
        "supply_apy": supply_apy,
        "borrow_apr": borrow_apr,
        "historical_apy": hist_apy,
        "total_volume": volume,
        "raw_pool": raw_pool,
    }

    collaterals = info.get("collateralAssets") or []
    rows = []
    if collaterals:
        for c in collaterals:
            casset = c.get("asset") or {}
            rows.append({
                **base,
                "collateral_asset": _ticker(casset),
                "collateral_price": c.get("price"),
                "max_ltv": _as_decimal(c.get("maxBorrowLTV")),
                "recommended_ltv": _as_decimal(c.get("recommendedBorrowLTV")),
                "liq_threshold": _as_decimal(c.get("liquidationThresholdLTV")),
            })
    else:
        # Fall back to pool-level LTVs if a pool lists no collaterals.
        rows.append({
            **base,
            "collateral_asset": None,
            "collateral_price": None,
            "max_ltv": _as_decimal(info.get("maxBorrowLTV")),
            "recommended_ltv": _as_decimal(info.get("recommendedBorrowLTV")),
            "liq_threshold": _as_decimal(info.get("liquidationThresholdLTV")),
        })
    return rows


# ---------------------------------------------------------------------------
def http_get_json(url: str):
    last_err = None
    delay = RETRY_BACKOFF
    for attempt in range(1, RETRIES + 1):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < RETRIES:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"GET {url} failed after {RETRIES} attempts: {last_err}")


def write_history_snapshot(rows, ada_usd):
    """Append/overwrite today's compact pool-metrics snapshot at
    data/history/YYYY-MM-DD.json. One pool may appear as several pair-rows; we
    collapse to one entry per pool using the pool-level aggregates (identical
    across that pool's pairs). Re-running the same day overwrites that day's file,
    so the latest run of the day wins."""
    from collections import OrderedDict

    by_pool = OrderedDict()
    for r in rows:
        pid = r.get("pool_id")
        if pid is None or pid in by_pool:
            continue
        by_pool[pid] = {
            "pool_id": pid,
            "loan_asset": r.get("loan_asset"),
            "supplied": r.get("supplied"),
            "borrowed": r.get("borrowed"),
            "available": r.get("available"),
            "utilization": r.get("utilization"),
            "supply_apy": r.get("supply_apy"),
            "borrow_apr": r.get("borrow_apr"),
            "loan_price": r.get("loan_price"),
        }

    now = datetime.now(timezone.utc)
    snapshot = {
        "date": now.date().isoformat(),
        "fetched_at": now.isoformat(),
        "ada_usd": ada_usd,
        "pools": list(by_pool.values()),
    }
    hist_dir = Path("data/history")
    hist_dir.mkdir(parents=True, exist_ok=True)
    (hist_dir / f"{now.date().isoformat()}.json").write_text(json.dumps(snapshot))

    # Maintain a lightweight index so the dashboard can discover available days
    # without listing the directory (GitHub Pages can't list dirs).
    index_path = hist_dir / "index.json"
    days = []
    if index_path.exists():
        try:
            days = json.loads(index_path.read_text()).get("days", [])
        except Exception:
            days = []
    d = now.date().isoformat()
    if d not in days:
        days.append(d)
        days.sort()
    index_path.write_text(json.dumps({"days": days, "updated_at": now.isoformat()}))


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch Surf lending pools -> JSON")
    ap.add_argument("--out", default="data/markets.json")
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--dump-raw", action="store_true",
                    help="also write the untouched API response to data/_raw.json")
    args = ap.parse_args()

    url = BASE_URL.rstrip("/") + POOLS_ENDPOINT
    print(f"Fetching {url} ...", file=sys.stderr)

    try:
        payload = http_get_json(url)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.dump_raw:
        Path("data/_raw.json").write_text(json.dumps(payload, indent=2))

    pool_infos = payload.get("poolInfos") if isinstance(payload, dict) else None
    if not isinstance(pool_infos, dict):
        print("ERROR: response had no 'poolInfos' object. See data/_raw.json.", file=sys.stderr)
        Path("data/_raw.json").write_text(json.dumps(payload, indent=2))
        return 1

    rows = []
    for pool_id, info in pool_infos.items():
        if isinstance(info, dict):
            rows.extend(normalize_pool_info(pool_id, info))

    ada_usd, rate_src = fetch_ada_usd()

    snapshot = {
        "source": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "refresh_hours": REFRESH_HOURS,
        "pool_count": len(pool_infos),
        "count": len(rows),
        "ada_usd": ada_usd,            # ADA price in USD; null if the rate fetch failed
        "ada_usd_source": rate_src,
        "markets": rows,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2 if args.pretty else None))

    # --- History accumulation ---
    # Append a compact daily snapshot of per-pool metrics so a future "trends" view
    # (rate stability, APY/TVL over time) becomes possible. One file per UTC day keeps
    # git diffs clean and files small. We store pool-level aggregates, not pairs/positions.
    try:
        write_history_snapshot(rows, ada_usd)
    except Exception as e:  # history is best-effort; never fail the main run over it
        print(f"(history snapshot skipped: {type(e).__name__})", file=sys.stderr)

    rate_msg = f"ADA/USD={ada_usd}" if ada_usd else f"ADA/USD unavailable ({rate_src})"
    print(f"Wrote {len(rows)} pair-rows from {len(pool_infos)} pools to {out}. {rate_msg}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
