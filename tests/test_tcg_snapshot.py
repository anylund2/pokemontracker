"""Offline tests for TCGplayer Sales History Snapshot parsing/aggregation."""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from tcg_snapshot import parse_detailed  # noqa: E402


def _d(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _bucket(days_ago, market, qty, txn, low=None, high=None):
    return {"marketPrice": str(market), "quantitySold": str(qty),
            "transactionCount": str(txn),
            "lowSalePriceWithShipping": str(low if low is not None else market),
            "highSalePriceWithShipping": str(high if high is not None else market),
            "bucketStartDate": _d(days_ago)}


def _payload():
    return {"count": 3, "result": [
        # NM holo: plenty of recent sales → 30d window
        {"skuId": "111", "variant": "Holofoil", "language": "English",
         "condition": "Near Mint", "totalQuantitySold": "20", "totalTransactionCount": "18",
         "buckets": [_bucket(2, 100, 5, 5), _bucket(10, 102, 6, 6),
                     _bucket(20, 98, 4, 4), _bucket(120, 90, 5, 3)]},
        # LP holo: only old sales → must widen to 180d
        {"skuId": "222", "variant": "Holofoil", "language": "English",
         "condition": "Lightly Played", "totalQuantitySold": "5", "totalTransactionCount": "5",
         "buckets": [_bucket(150, 70, 5, 5)]},
        # MP holo: no sales at all → skipped/empty but still reported
        {"skuId": "333", "variant": "Holofoil", "language": "English",
         "condition": "Moderately Played", "totalQuantitySold": "0", "totalTransactionCount": "0",
         "buckets": [_bucket(5, 60, 0, 0)]},
    ]}


def test_all_skus_reported():
    out = parse_detailed(_payload())
    assert out["skuCount"] == 3
    assert set(out["skuIdsChecked"]) == {"111", "222", "333"}


def test_nm_uses_30d_window():
    out = parse_detailed(_payload())
    nm = next(s for s in out["skus"] if s["skuId"] == "111")
    assert nm["condition"] == "NM" and nm["variant"] == "holofoil"
    assert nm["window"] == "30d"
    assert nm["transactionCount"] == 15        # 5+6+4 within 30d (not the 120d bucket)
    assert nm["quantitySold"] == 15


def test_lp_widens_to_180d():
    out = parse_detailed(_payload())
    lp = next(s for s in out["skus"] if s["skuId"] == "222")
    assert lp["window"] == "180d"              # only sale is 150 days old → widened
    assert lp["transactionCount"] == 5
    assert lp["lowConfidence"] is False        # 5 txns ≥ 3


def test_empty_sku_skipped_but_listed():
    out = parse_detailed(_payload())
    mp = next(s for s in out["skus"] if s["skuId"] == "333")
    assert mp["quantitySold"] == 0 and mp["lowConfidence"] is True
    assert any(s["skuId"] == "333" for s in out["skipped"])
    assert out["skipped"][0]["reason"] in ("no sales in range", "no buckets")


def test_quantity_and_transaction_counts_present():
    out = parse_detailed(_payload())
    for s in out["skus"]:
        assert "quantitySold" in s and "transactionCount" in s
        assert "marketPrice" in s
        assert "lowSalePriceWithShipping" in s and "highSalePriceWithShipping" in s


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
