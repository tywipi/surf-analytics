#!/usr/bin/env python3
"""
fetch_positions.py
Builds the position-level risk dataset for the Surf Lending risk monitor.

Joins two endpoints:
  GET /api/getAllPositions   -> every borrow position across all pools (grouped by pool)
  GET /api/getAllPoolInfos   -> per-pool / per-collateral prices, decimals, and
                                liquidationThresholdLTV (the positions endpoint omits these)

For each position it computes:
  debt_value         principal scaled to human units (pool asset)        [pool-asset units]
  collateral_qty     collateral scaled to human units                    [collateral units]
  collateral_value   collateral_qty * collateral_price                   [pool-asset units]
  ltv_computed       debt_value / collateral_value                       (sanity-checked vs API ltv)
  ltv                API-reported ltv (authoritative for display)
  liq_threshold      collateral's liquidationThresholdLTV
  health_factor      liq_threshold / ltv          (>1 safe, <=1 liquidatable)
  dist_to_liq        1 - (ltv / liq_threshold)    fraction of buffer remaining (0 = at liq)
  liq_price_drop     collateral price drop % that pushes ltv to liq_threshold
  liq_price          collateral price at which liquidation triggers

Writes data/positions.json consumed by positions.html.

Usage:
    python scripts/fetch_positions.py
    python scripts/fetch_positions.py --pretty --dump-raw
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

BASE_URL = "https://surflending.org"
POSITIONS_ENDPOINT = "/api/getAllPositions"
POOLS_ENDPOINT = "/api/getAllPoolInfos"
HEADERS = {"User-Agent": "surf-analytics/1.0", "Accept": "application/json"}
TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF = 2.0

ADA_POLICY = ""  # empty policyId denotes ADA (lovelace)

# Refresh cadence the GitHub Actions workflow runs at. MUST match the cron in
# .github/workflows/refresh.yml. The dashboards use this to show "next update ~...".
REFRESH_HOURS = 6

# ADA/USD rate source (free, no key).
ADAUSD_URL = "https://api.coingecko.com/api/v3/simple/price?ids=cardano&vs_currencies=usd"


def fetch_ada_usd():
    """Best-effort ADA/USD rate. Returns (rate, source) or (None, reason)."""
    try:
        req = Request(ADAUSD_URL, headers=HEADERS)
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rate = data.get("cardano", {}).get("usd")
        if isinstance(rate, (int, float)) and rate > 0:
            return float(rate), "coingecko"
        return None, "unexpected_response"
    except Exception as e:  # noqa: BLE001
        return None, f"error:{type(e).__name__}"


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


def asset_key(asset: dict) -> str:
    """Stable key for an asset: 'ADA' for lovelace, else policyId.assetName."""
    if not isinstance(asset, dict):
        return "UNKNOWN"
    pid = asset.get("policyId", "")
    name = asset.get("assetName", "")
    if pid == ADA_POLICY:
        return "ADA"
    return f"{pid}.{name}"


def hex_to_ascii(h: str) -> str:
    """Best-effort decode of a hex asset name to a ticker-ish string."""
    if not h:
        return ""
    try:
        s = bytes.fromhex(h).decode("utf-8")
        return s if s.isprintable() else h
    except (ValueError, UnicodeDecodeError):
        return h


def build_asset_reference(pool_infos: dict):
    """
    From getAllPoolInfos, build lookups:
      pool_meta[poolId] = {ticker, decimals, price}                    (pool/loan asset)
      collateral_ref[(poolId, asset_key)] = {ticker, decimals, price,
                                              liq_threshold, max_ltv, rec_ltv}
      asset_price[asset_key] = price   (global; used for cross-pool shocks)
      asset_decimals[asset_key] = decimals
      asset_ticker[asset_key] = readable ticker (for UI labels)
    """
    pool_meta = {}
    collateral_ref = {}
    asset_price = {}
    asset_decimals = {}
    asset_ticker = {}

    for pool_id, info in pool_infos.items():
        if not isinstance(info, dict):
            continue
        pa = info.get("asset") or {}
        pa_key = asset_key(pa)
        pa_dec = int(pa.get("decimals", 6)) if isinstance(pa.get("decimals"), (int, float)) else 6
        pa_ticker = pa.get("ticker") or hex_to_ascii(pa.get("assetName", "")) or pa_key
        pool_meta[pool_id] = {"ticker": pa_ticker, "decimals": pa_dec, "price": info.get("price")}
        if info.get("price") is not None:
            asset_price.setdefault(pa_key, info["price"])
        asset_decimals.setdefault(pa_key, pa_dec)
        asset_ticker.setdefault(pa_key, pa_ticker)

        for c in info.get("collateralAssets") or []:
            ca = c.get("asset") or {}
            ck = asset_key(ca)
            cdec = int(ca.get("decimals", 6)) if isinstance(ca.get("decimals"), (int, float)) else 6
            cticker = ca.get("ticker") or hex_to_ascii(ca.get("assetName", "")) or ck
            collateral_ref[(pool_id, ck)] = {
                "ticker": cticker,
                "decimals": cdec,
                "price": c.get("price"),
                "liq_threshold": c.get("liquidationThresholdLTV"),
                "max_ltv": c.get("maxBorrowLTV"),
                "rec_ltv": c.get("recommendedBorrowLTV"),
            }
            if c.get("price") is not None:
                asset_price.setdefault(ck, c["price"])
            asset_decimals.setdefault(ck, cdec)
            # prefer a real ticker over a hex fallback if we see one later
            if ck not in asset_ticker or asset_ticker[ck] == ck:
                asset_ticker[ck] = cticker

    return pool_meta, collateral_ref, asset_price, asset_decimals, asset_ticker


def scaled(value, decimals):
    if value is None:
        return None
    try:
        return float(value) / (10 ** int(decimals))
    except (TypeError, ValueError):
        return None


def flatten_positions(payload: dict):
    """getAllPositions returns {positions:[{poolId, positions:[...]}]}. Flatten inner."""
    out = []
    groups = payload.get("positions", []) if isinstance(payload, dict) else []
    for g in groups:
        if not isinstance(g, dict):
            continue
        for p in g.get("positions", []) or []:
            if isinstance(p, dict):
                out.append(p)
    return out


def compute_risk(pos: dict, pool_meta, collateral_ref, asset_decimals):
    pool_id = pos.get("poolId")
    pm = pool_meta.get(pool_id, {})
    pa_dec = pm.get("decimals", 6)
    pa_price = pm.get("price")

    ca_key = asset_key(pos.get("collateralAsset") or {})
    cref = collateral_ref.get((pool_id, ca_key), {})
    c_dec = cref.get("decimals", asset_decimals.get(ca_key, 6))
    c_price = cref.get("price", None)
    liq_threshold = cref.get("liq_threshold")

    debt_qty = scaled(pos.get("principal"), pa_dec)               # in loan-asset (pool) units
    collateral_qty = scaled(pos.get("collateral"), c_dec)        # in collateral units

    # IMPORTANT: the collateral `price` from getAllPoolInfos is already denominated in the
    # POOL (loan) asset, not USD. So LTV = debt(loan units) / (collateral_qty * collateral_price),
    # with debt left in loan-asset units. Applying the pool's own price would double-count and
    # breaks stablecoin pools (verified against live USDM/NIGHT + ADA/SNEK positions).
    debt_value = debt_qty                                         # loan-asset units
    collateral_value = (                                         # loan-asset units
        collateral_qty * c_price if (collateral_qty is not None and c_price is not None) else None
    )

    # The API's `ltv` scaling is INCONSISTENT across pools — some report ltv*1e6,
    # some report the bare fraction. So we do NOT trust it for display/math. Instead we
    # compute LTV from amounts and the collateral price (denominated in the loan asset),
    # which is reliable. The API value is kept only as a loose cross-check, normalized
    # by detecting whether it looks scaled.
    _ltv_raw = pos.get("ltv")
    if _ltv_raw is None:
        ltv_api = None
    elif _ltv_raw > 100:            # clearly scaled (e.g. 467606 -> 0.4676)
        ltv_api = _ltv_raw / 1_000_000
    else:                           # already a fraction
        ltv_api = _ltv_raw

    ltv_computed = None
    if collateral_value not in (None, 0) and debt_value is not None:
        ltv_computed = debt_value / collateral_value

    # Price-based computed LTV is authoritative; fall back to API only if we can't compute.
    ltv = ltv_computed if ltv_computed is not None else ltv_api

    health_factor = None
    dist_to_liq = None
    liq_price_drop = None
    liq_price = None
    if ltv not in (None, 0) and liq_threshold:
        health_factor = liq_threshold / ltv
        dist_to_liq = 1 - (ltv / liq_threshold)            # 0 = at liquidation
        # Collateral price can fall by this fraction before ltv hits threshold:
        #   ltv scales as 1/price, so liq when price * (ltv/threshold) ... solve:
        #   new_ltv = ltv / (1 - drop) = threshold  ->  drop = 1 - ltv/threshold
        liq_price_drop = dist_to_liq
        if c_price is not None and liq_price_drop is not None:
            liq_price = c_price * (1 - liq_price_drop)

    return {
        "pool_id": pool_id,
        "address": pos.get("address"),
        "loan_asset": pm.get("ticker"),
        "loan_price": pa_price,        # ADA per loan-asset token (e.g. USDM=5.78, ADA=1.0)
        "collateral_asset": cref.get("ticker") or hex_to_ascii(
            (pos.get("collateralAsset") or {}).get("assetName", "")
        ),
        "collateral_key": ca_key,
        "debt_qty": debt_qty,
        "debt_value": debt_value,
        "collateral_qty": collateral_qty,
        "collateral_price": c_price,
        "collateral_value": collateral_value,
        "interest_rate": pos.get("interestRate"),
        "start_time": pos.get("startTime"),
        "ltv": ltv,
        "ltv_api": ltv_api,
        "ltv_computed": ltv_computed,
        "liq_threshold": liq_threshold,
        "health_factor": health_factor,
        "dist_to_liq": dist_to_liq,
        "liq_price_drop": liq_price_drop,
        "liq_price": liq_price,
        "out_ref": pos.get("outRef"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Surf position risk dataset")
    ap.add_argument("--out", default="data/positions.json")
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--dump-raw", action="store_true")
    args = ap.parse_args()

    pos_url = BASE_URL + POSITIONS_ENDPOINT
    pool_url = BASE_URL + POOLS_ENDPOINT
    print(f"Fetching {pool_url} ...", file=sys.stderr)
    try:
        pool_payload = http_get_json(pool_url)
        print(f"Fetching {pos_url} ...", file=sys.stderr)
        pos_payload = http_get_json(pos_url)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.dump_raw:
        Path("data/_raw_pools.json").write_text(json.dumps(pool_payload, indent=2))
        Path("data/_raw_positions.json").write_text(json.dumps(pos_payload, indent=2))

    pool_infos = pool_payload.get("poolInfos") if isinstance(pool_payload, dict) else None
    if not isinstance(pool_infos, dict):
        print("ERROR: getAllPoolInfos had no 'poolInfos'.", file=sys.stderr)
        return 1

    pool_meta, collateral_ref, asset_price, asset_decimals, asset_ticker = build_asset_reference(pool_infos)
    raw_positions = flatten_positions(pos_payload)
    rows = [compute_risk(p, pool_meta, collateral_ref, asset_decimals) for p in raw_positions]

    # Data-quality summary. Computed LTV is authoritative; we report coverage and
    # any positions that couldn't be priced or look out of range, plus how many
    # agree with the (inconsistently-scaled) API value as a loose secondary signal.
    priced = [r for r in rows if r["ltv"] is not None]
    unpriced = [r for r in rows if r["ltv"] is None]
    out_of_range = [r for r in rows if r["ltv"] is not None and r["ltv"] > 2.0]
    agree = 0
    for r in rows:
        if r["ltv_computed"] is not None and r["ltv_api"] is not None:
            if abs(r["ltv_computed"] - r["ltv_api"]) < 0.01:
                agree += 1
    ltv_check = {
        "total": len(rows),
        "priced": len(priced),
        "unpriced": len(unpriced),
        "out_of_range_gt2": len(out_of_range),
        "agree_with_api": agree,
    }

    # Total debt (in ADA) backed by each collateral asset, so the UI can rank
    # collaterals and show only the most significant ones in the override panel.
    debt_by_collateral = {}
    for r in rows:
        ck = r.get("collateral_key")
        lp = r.get("loan_price") or 1
        dq = r.get("debt_qty")
        if ck is None or dq is None:
            continue
        debt_by_collateral[ck] = debt_by_collateral.get(ck, 0.0) + dq * lp

    # Per-pool aggregates the scorecard needs: total debt and the share of it
    # sitting within 15% buffer of liquidation (a supplier-side risk signal).
    pool_debt = {}
    pool_debt_near = {}
    for r in rows:
        pid = r.get("pool_id")
        lp = r.get("loan_price") or 1
        dq = r.get("debt_qty")
        if pid is None or dq is None:
            continue
        d_ada = dq * lp
        pool_debt[pid] = pool_debt.get(pid, 0.0) + d_ada
        dist = r.get("dist_to_liq")
        if dist is not None and dist <= 0.15:
            pool_debt_near[pid] = pool_debt_near.get(pid, 0.0) + d_ada
    pool_risk = {
        pid: {
            "debt_ada": round(pool_debt.get(pid, 0.0), 2),
            "debt_near_liq_frac": round(pool_debt_near.get(pid, 0.0) / pool_debt[pid], 4)
            if pool_debt.get(pid) else 0.0,
        }
        for pid in pool_debt
    }

    # Asset reference for the front-end stress test: readable ticker, current price
    # (in ADA), decimals, and total debt-at-risk so the UI can sort/limit sliders.
    asset_table = {
        k: {
            "ticker": asset_ticker.get(k, k),
            "price": v,
            "decimals": asset_decimals.get(k),
            "debt_ada": round(debt_by_collateral.get(k, 0.0), 2),
        }
        for k, v in asset_price.items()
    }

    ada_usd, rate_src = fetch_ada_usd()

    snapshot = {
        "source": {"positions": pos_url, "pools": pool_url},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "refresh_hours": REFRESH_HOURS,
        "position_count": len(rows),
        "ltv_check": ltv_check,
        "ada_usd": ada_usd,            # ADA price in USD; null if unavailable
        "ada_usd_source": rate_src,
        "pool_risk": pool_risk,        # per-pool debt + share near liquidation (for scorecard)
        "assets": asset_table,
        "positions": rows,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2 if args.pretty else None))
    rate_msg = f"ADA/USD={ada_usd}" if ada_usd else f"ADA/USD unavailable ({rate_src})"
    print(
        f"Wrote {len(rows)} positions to {out}. "
        f"priced={ltv_check['priced']}, unpriced={ltv_check['unpriced']}, "
        f"out-of-range(>2.0)={ltv_check['out_of_range_gt2']}. {rate_msg}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
