#!/usr/bin/env python3
"""
fetch_liqwid.py
Pulls Liqwid Finance market data from its public GraphQL API and writes a
normalized snapshot to data/liqwid_markets.json, mirroring the Surf fetcher so the
Liqwid dashboard/scorecard can read it the same way.

API: POST https://v2.api.liqwid.finance/graphql   (public, no auth)
  query { liqwid { data { markets(input:{page:N}) { pagesCount results {...} } } } }

Liqwid is a POOLED (Compound/Aave-style qToken) protocol, unlike Surf's per-pair
model. Each market is one borrowable asset. Suppliers mint qTokens (exchangeRate
grows as interest accrues). Each market carries a COLLATERAL MATRIX: a list of other
assets that can back loans in it, each with its own max-LTV / liquidation-threshold.

Important data-shape notes (verified against live responses):
  - id / displayName are human tickers (Ada, BTC, SNEK...). No hex decode needed.
  - utilization is already a 0..1 fraction. supply/borrow/liquidity already decimal-
    adjusted (human units). borrowAPR is a 0..1 fraction.
  - There is NO direct supplyAPY field. Supply yield is derived:
        supplyAPY = borrowAPR * utilization * incomeParameters.supplier
    where `supplier` is the fraction of borrow interest routed to suppliers.
  - LQ/POL etc. are supply-only markets (empty collateralParameters, borrowCap 0).

We emit ONE ROW PER market->collateral pair (like Surf's pool->collateral pairs),
each carrying that collateral's maxLoanToValue / liquidationThreshold / penalty
alongside the market-level supply/borrow/APY aggregates. Markets with no collateral
options (supply-only) emit a single row with null collateral.

Usage:
    python scripts/fetch_liqwid.py
    python scripts/fetch_liqwid.py --pretty --dump-raw
    python scripts/fetch_liqwid.py --out data/liqwid_markets.json
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ENDPOINT = "https://v2.api.liqwid.finance/graphql"
REFRESH_HOURS = 6

# Paginated markets query. All field names verified against the live schema.
MARKETS_QUERY = """query($input: MarketsInput){
  liqwid { data { markets(input:$input){
    page
    pagesCount
    results {
      id
      displayName
      asset { id currencySymbol name decimals }
      supply
      borrow
      liquidity
      utilization
      borrowAPR
      exchangeRate
      loanOriginationFeePercentage
      frozen
      delisting
      batching
      parameters {
        borrowCap
        supplyCap
        minHealthFactor
        incomeParameters { supplier reserve staker treasury }
        collateralParameters {
          collateral { symbol displayName }
          maxLoanToValue
          liquidationThreshold
          liquidationPenalty
          collateralWeight
          weightedMaxLoanToValue
          weightedLiquidationThreshold
        }
      }
    }
  }}}
}"""


def fetch_ada_usd():
    """ADA/USD from CoinGecko (best-effort), to power the USD unit toggle."""
    url = "https://api.coingecko.com/api/v3/simple/price?ids=cardano&vs_currencies=usd"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "surf-analytics"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
        rate = d.get("cardano", {}).get("usd")
        if isinstance(rate, (int, float)) and rate > 0:
            return float(rate), "coingecko"
    except Exception:
        pass
    return None, "unavailable"


def graphql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "surf-analytics"},
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        payload = json.loads(r.read())
    if "errors" in payload:
        raise RuntimeError("GraphQL errors: " + json.dumps(payload["errors"])[:500])
    return payload["data"]


def fetch_all_markets():
    """Page through all markets."""
    out, page = [], 0
    while True:
        data = graphql(MARKETS_QUERY, {"input": {"page": page}})
        mk = data["liqwid"]["data"]["markets"]
        out.extend(mk["results"])
        if page >= (mk.get("pagesCount", 1) - 1):
            break
        page += 1
    return out


def _f(v):
    """Coerce to float or None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_market(m):
    """Flatten one Liqwid market into one row per market->collateral pair.
    Returns a list of rows (>=1)."""
    p = m.get("parameters") or {}
    inc = p.get("incomeParameters") or {}
    supplier_share = _f(inc.get("supplier"))

    util = _f(m.get("utilization")) or 0.0
    util = max(0.0, min(1.0, util))
    borrow_apr = _f(m.get("borrowAPR")) or 0.0

    # Supply APY is derived: share of borrow interest that reaches suppliers.
    supply_apy = None
    if supplier_share is not None:
        supply_apy = borrow_apr * util * supplier_share

    base = {
        "market_id": m.get("id"),
        "loan_asset": m.get("displayName") or m.get("id"),
        "decimals": (m.get("asset") or {}).get("decimals"),
        "supplied": _f(m.get("supply")),
        "borrowed": _f(m.get("borrow")),
        "available": _f(m.get("liquidity")),
        "utilization": util,
        "supply_apy": supply_apy,
        "borrow_apr": borrow_apr,
        "exchange_rate": _f(m.get("exchangeRate")),
        "origination_fee": _f(m.get("loanOriginationFeePercentage")),
        "borrow_cap": _f(p.get("borrowCap")),
        "supply_cap": _f(p.get("supplyCap")),
        "min_health_factor": _f(p.get("minHealthFactor")),
        "supplier_share": supplier_share,
        "reserve_share": _f(inc.get("reserve")),
        "frozen": bool(m.get("frozen")),
        "delisting": bool(m.get("delisting")),
        "batching": bool(m.get("batching")),
    }

    cps = p.get("collateralParameters") or []
    if not cps:
        # supply-only / no collateral matrix -> single row, null collateral
        row = dict(base)
        row.update({"collateral_asset": None, "max_ltv": None,
                    "liq_threshold": None, "liq_penalty": None})
        return [row]

    rows = []
    for cp in cps:
        coll = cp.get("collateral") or {}
        # Liqwid sets a uniform base maxLTV/threshold, then applies a per-collateral
        # `collateralWeight`. The WEIGHTED fields are the effective risk numbers, so
        # prefer them; fall back to base if weighted is absent.
        w_ltv = _f(cp.get("weightedMaxLoanToValue"))
        w_thr = _f(cp.get("weightedLiquidationThreshold"))
        base_ltv = _f(cp.get("maxLoanToValue"))
        base_thr = _f(cp.get("liquidationThreshold"))
        rows.append({
            **base,
            "collateral_asset": coll.get("symbol") or coll.get("displayName"),
            "max_ltv": w_ltv if w_ltv is not None else base_ltv,
            "liq_threshold": w_thr if w_thr is not None else base_thr,
            "base_max_ltv": base_ltv,
            "base_liq_threshold": base_thr,
            "collateral_weight": _f(cp.get("collateralWeight")),
            "liq_penalty": _f(cp.get("liquidationPenalty")),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch Liqwid markets -> JSON")
    ap.add_argument("--out", default="data/liqwid_markets.json")
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--dump-raw", action="store_true")
    args = ap.parse_args()

    markets = fetch_all_markets()
    if args.dump_raw:
        Path("data").mkdir(exist_ok=True)
        Path("data/_liqwid_raw.json").write_text(json.dumps(markets, indent=2))

    rows = []
    for m in markets:
        rows.extend(normalize_market(m))

    ada_usd, rate_src = fetch_ada_usd()

    snapshot = {
        "source": ENDPOINT,
        "protocol": "Liqwid",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "refresh_hours": REFRESH_HOURS,
        "market_count": len(markets),
        "count": len(rows),
        "ada_usd": ada_usd,
        "ada_usd_source": rate_src,
        "markets": rows,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2 if args.pretty else None))
    print(f"Wrote {len(rows)} rows from {len(markets)} Liqwid markets to {out}.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
