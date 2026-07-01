"""
TCGplayer "Sales History Snapshot" — full SKU-level coverage.

The product page's Sales History Snapshot is served by
``infinite-api.tcgplayer.com/price/history/{productId}/detailed?range=...`` and
returns **one row per SKU** (condition × printing/variant × language), each with
aggregate quantity/transaction counts and a list of dated price buckets.

This module pulls *all* SKUs (never just Near Mint or the first Normal SKU),
keeps every bucket, computes 30/90/180-day aggregates per SKU, and logs SKUs
with no sales so callers can tell "no data" from "code skipped it".

Pure-ish: one network call via `requests`; everything else is data shaping, so
the parsing/aggregation is unit-testable by feeding `parse_detailed()` a payload.
"""

from __future__ import annotations

import re
from datetime import date

import requests

_RANGES = {"month": 30, "quarter": 90, "semiAnnual": 180, "annual": 365}

_CONDITION_MAP = {
    "Near Mint": "NM", "Lightly Played": "LP", "Moderately Played": "MP",
    "Heavily Played": "HP", "Damaged": "DMG",
}
_VARIANT_MAP = {
    "Normal": "normal", "Holofoil": "holofoil", "Reverse Holofoil": "reverseHolofoil",
    "1st Edition Holofoil": "1stEditionHolofoil", "1st Edition Normal": "1stEditionNormal",
    "Unlimited Holofoil": "unlimitedHolofoil", "Unlimited": "unlimited",
}

_H = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Origin": "https://www.tcgplayer.com",
    "Referer": "https://www.tcgplayer.com/",
}


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _age_days(d: str) -> float:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", d or "")
    if not m:
        return float("inf")
    try:
        return (date.today() - date(int(m[1]), int(m[2]), int(m[3]))).days
    except ValueError:
        return float("inf")


def _window_for(buckets: list[dict], days: int) -> dict:
    """Aggregate the buckets that fall within the last `days`."""
    rows = [b for b in buckets if _age_days(b.get("bucketStartDate", "")) <= days]
    qty = sum(_i(b.get("quantitySold")) for b in rows)
    txns = sum(_i(b.get("transactionCount")) for b in rows)
    lows = [_f(b.get("lowSalePriceWithShipping")) for b in rows
            if _f(b.get("lowSalePriceWithShipping")) > 0]
    highs = [_f(b.get("highSalePriceWithShipping")) for b in rows
             if _f(b.get("highSalePriceWithShipping")) > 0]
    sold = [b for b in rows if _i(b.get("quantitySold")) > 0]
    dates = sorted(b.get("bucketStartDate", "") for b in sold if b.get("bucketStartDate"))
    market = next((_f(b.get("marketPrice")) for b in
                   sorted(rows, key=lambda x: x.get("bucketStartDate", ""), reverse=True)
                   if _f(b.get("marketPrice")) > 0), None)
    return {
        "quantitySold": qty, "transactionCount": txns,
        "lowSalePriceWithShipping": round(min(lows), 2) if lows else None,
        "highSalePriceWithShipping": round(max(highs), 2) if highs else None,
        "marketPrice": round(market, 2) if market else None,
        "bucketStartDate": dates[0] if dates else None,
        "bucketsWithSales": len(sold), "bucketsChecked": len(rows),
    }


def parse_detailed(payload: dict) -> dict:
    """
    Shape an infinite-api `detailed` payload into per-SKU coverage.

    Picks the tightest window with real volume per SKU: prefer 30 days; widen to
    90, then 180 when a SKU has fewer than 3 transactions.
    """
    skus_out: list[dict] = []
    skipped: list[dict] = []
    total_buckets = 0

    for sku in payload.get("result", []):
        buckets = sku.get("buckets", []) or []
        total_buckets += len(buckets)
        sku_id = str(sku.get("skuId", ""))
        cond_raw = sku.get("condition", "")
        var_raw = sku.get("variant", "")
        entry_base = {
            "skuId": sku_id,
            "condition": _CONDITION_MAP.get(cond_raw, cond_raw),
            "conditionRaw": cond_raw,
            "variant": _VARIANT_MAP.get(var_raw, var_raw),
            "variantRaw": var_raw,
            "language": sku.get("language", "English"),
            "totalQuantitySold": _i(sku.get("totalQuantitySold")),
            "totalTransactionCount": _i(sku.get("totalTransactionCount")),
            "bucketsFound": len(buckets),
        }

        if not buckets or entry_base["totalQuantitySold"] == 0:
            skipped.append({**entry_base,
                            "reason": "no buckets" if not buckets else "no sales in range"})
            # Still emit the SKU so callers see it was checked, but flagged empty.
            entry_base.update({"window": None, "marketPrice": None,
                               "quantitySold": 0, "transactionCount": 0,
                               "lowSalePriceWithShipping": None,
                               "highSalePriceWithShipping": None,
                               "bucketStartDate": None, "lowConfidence": True})
            skus_out.append(entry_base)
            continue

        chosen, window = None, None
        for days, label in [(30, "30d"), (90, "90d"), (180, "180d")]:
            agg = _window_for(buckets, days)
            chosen, window = agg, label
            if agg["transactionCount"] >= 3:
                break
        # Dated points (last 180d) with sales — a per-condition price time series.
        points = [
            {"date": b.get("bucketStartDate"),
             "marketPrice": _f(b.get("marketPrice")),
             "quantitySold": _i(b.get("quantitySold")),
             "transactionCount": _i(b.get("transactionCount")),
             "lowSalePriceWithShipping": _f(b.get("lowSalePriceWithShipping")),
             "highSalePriceWithShipping": _f(b.get("highSalePriceWithShipping"))}
            for b in buckets
            if _age_days(b.get("bucketStartDate", "")) <= 180
            and _i(b.get("quantitySold")) > 0
        ]
        points.sort(key=lambda p: p["date"] or "")
        entry_base.update({
            "window": window,
            "marketPrice": chosen["marketPrice"],
            "quantitySold": chosen["quantitySold"],
            "transactionCount": chosen["transactionCount"],
            "lowSalePriceWithShipping": chosen["lowSalePriceWithShipping"],
            "highSalePriceWithShipping": chosen["highSalePriceWithShipping"],
            "bucketStartDate": chosen["bucketStartDate"],
            "bucketsWithSales": chosen["bucketsWithSales"],
            "lowConfidence": chosen["transactionCount"] < 3,
            "points": points,
        })
        skus_out.append(entry_base)

    return {
        "skuCount": len(skus_out),
        "skuIdsChecked": [s["skuId"] for s in skus_out],
        "totalBuckets": total_buckets,
        "skus": skus_out,
        "skipped": skipped,
    }


def fetch_sales_history(product_id: str, base_range: str = "semiAnnual",
                        timeout: int = 12) -> dict:
    """
    Fetch the full SKU-level Sales History Snapshot for a product.

    `base_range` is the widest window pulled in one request (default 180 days);
    per-SKU 30/90/180 aggregates are computed from its dated buckets.
    """
    out = {"productId": str(product_id), "source": "tcgplayer-infinite",
           "range": base_range, "skuCount": 0, "skuIdsChecked": [],
           "totalBuckets": 0, "skus": [], "skipped": [], "error": None}
    if not product_id:
        out["error"] = "no productId"
        return out
    try:
        r = requests.get(
            f"https://infinite-api.tcgplayer.com/price/history/{product_id}/detailed",
            params={"range": base_range}, headers=_H, timeout=timeout)
        if not r.ok:
            out["error"] = f"HTTP {r.status_code}"
            return out
        parsed = parse_detailed(r.json())
        out.update(parsed)
    except Exception as e:  # network / json
        out["error"] = str(e)
    return out
