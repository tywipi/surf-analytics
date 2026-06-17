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
    util = _as_decimal(util)

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

    snapshot = {
        "source": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "pool_count": len(pool_infos),
        "count": len(rows),
        "markets": rows,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2 if args.pretty else None))
    print(f"Wrote {len(rows)} pair-rows from {len(pool_infos)} pools to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
