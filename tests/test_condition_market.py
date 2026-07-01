"""
Printing-strict per-condition valuation (`select_condition_market`).

Regression suite for the portfolio/market mismatch: a reverse-holo card was
being valued at the *plain-holo* SKU because TCGplayer spells the printing
"Reverse Holofoil" (space) while our foil code is "reverseHolofoil" (camel).
The exact-printing branch never matched, and the old fallback silently returned
whatever SKU shared the condition — the holo price. These tests lock in that a
value is only ever drawn from the SAME printing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pricing_engine import select_condition_market  # noqa: E402


# Real Flygon δ (Holon Phantoms #7) snapshot — the card from the bug report.
# Note the "Reverse Holofoil" label (with a space) and the null NM reverse price.
FLYGON = [
    {"variantRaw": "Holofoil",         "condition": "MP",  "marketPrice": 27.15},
    {"variantRaw": "Holofoil",         "condition": "NM",  "marketPrice": 54.92},
    {"variantRaw": "Holofoil",         "condition": "LP",  "marketPrice": 39.90},
    {"variantRaw": "Holofoil",         "condition": "DMG", "marketPrice": 8.06},
    {"variantRaw": "Reverse Holofoil", "condition": "DMG", "marketPrice": 15.58},
    {"variantRaw": "Holofoil",         "condition": "HP",  "marketPrice": 13.36},
    {"variantRaw": "Reverse Holofoil", "condition": "HP",  "marketPrice": 32.30},
    {"variantRaw": "Reverse Holofoil", "condition": "LP",  "marketPrice": 69.53},
    {"variantRaw": "Reverse Holofoil", "condition": "NM",  "marketPrice": None},
    {"variantRaw": "Reverse Holofoil", "condition": "MP",  "marketPrice": 38.10},
]


# ── The headline bug ─────────────────────────────────────────────────────────
def test_reverse_holo_lp_is_reverse_price_not_holo():
    # Was returning 39.90 (Holofoil LP); must be 69.53 (Reverse Holofoil LP).
    assert select_condition_market(FLYGON, "LP", "reverseHolofoil") == 69.53


def test_holo_lp_stays_holo_price():
    assert select_condition_market(FLYGON, "LP", "holofoil") == 39.90


def test_reverse_and_holo_never_collide():
    for cond in ("MP", "HP", "DMG"):
        holo = select_condition_market(FLYGON, cond, "holofoil")
        rev = select_condition_market(FLYGON, cond, "reverseHolofoil")
        assert holo is not None and rev is not None
        assert holo != rev, f"{cond}: holo and reverse resolved to the same price"


# ── Variant-label normalization (the actual root cause) ──────────────────────
def test_spaced_variant_label_matches_camel_foil():
    skus = [{"variantRaw": "Reverse Holofoil", "condition": "NM", "marketPrice": 100.0}]
    assert select_condition_market(skus, "NM", "reverseHolofoil") == 100.0


def test_variant_match_is_case_and_space_insensitive():
    skus = [{"variantRaw": "  reverse  HOLOFOIL ", "condition": "NM", "marketPrice": 5.0}]
    assert select_condition_market(skus, "NM", "reverseHolofoil") == 5.0


def test_first_edition_variant_matches():
    skus = [
        {"variantRaw": "1st Edition Holofoil", "condition": "NM", "marketPrice": 900.0},
        {"variantRaw": "Unlimited Holofoil",   "condition": "NM", "marketPrice": 120.0},
    ]
    assert select_condition_market(skus, "NM", "1stEditionHolofoil") == 900.0
    assert select_condition_market(skus, "NM", "unlimitedHolofoil") == 120.0


# ── Strictness: never cross printings ────────────────────────────────────────
def test_absent_printing_returns_none_not_other_printing():
    # Flygon has no Normal printing → must not fall through to Holofoil/Reverse.
    assert select_condition_market(FLYGON, "LP", "normal") is None


def test_missing_condition_falls_back_within_same_printing():
    # Reverse-holo NM has no price → nearest reverse condition (LP 69.53),
    # never the holo NM price (54.92).
    val = select_condition_market(FLYGON, "NM", "reverseHolofoil")
    assert val == 69.53


def test_normal_selected_over_holo():
    skus = [
        {"variantRaw": "Normal",   "condition": "NM", "marketPrice": 2.0},
        {"variantRaw": "Holofoil", "condition": "NM", "marketPrice": 40.0},
    ]
    assert select_condition_market(skus, "NM", "normal") == 2.0
    assert select_condition_market(skus, "NM", "holofoil") == 40.0


# ── Condition fallback rules ─────────────────────────────────────────────────
def test_exact_condition_always_wins_over_nearer_neighbor():
    skus = [
        {"variantRaw": "Holofoil", "condition": "NM", "marketPrice": 50.0},
        {"variantRaw": "Holofoil", "condition": "LP", "marketPrice": 40.0},
        {"variantRaw": "Holofoil", "condition": "MP", "marketPrice": 30.0},
    ]
    assert select_condition_market(skus, "LP", "holofoil") == 40.0


def test_condition_tie_prefers_worse_condition():
    # LP missing; NM (dist 1, better) vs MP (dist 1, worse) → pick MP, never over-value.
    skus = [
        {"variantRaw": "Holofoil", "condition": "NM", "marketPrice": 50.0},
        {"variantRaw": "Holofoil", "condition": "MP", "marketPrice": 30.0},
    ]
    assert select_condition_market(skus, "LP", "holofoil") == 30.0


def test_nearest_condition_picks_closest_available():
    # Only NM present; request DMG → falls back to the one same-printing price.
    skus = [{"variantRaw": "Holofoil", "condition": "NM", "marketPrice": 50.0}]
    assert select_condition_market(skus, "DMG", "holofoil") == 50.0


# ── Null / empty handling ────────────────────────────────────────────────────
def test_null_market_price_is_skipped():
    skus = [
        {"variantRaw": "Reverse Holofoil", "condition": "NM", "marketPrice": None},
        {"variantRaw": "Reverse Holofoil", "condition": "LP", "marketPrice": 12.0},
    ]
    assert select_condition_market(skus, "NM", "reverseHolofoil") == 12.0


def test_zero_market_price_treated_as_missing():
    skus = [
        {"variantRaw": "Holofoil", "condition": "NM", "marketPrice": 0},
        {"variantRaw": "Holofoil", "condition": "LP", "marketPrice": 8.0},
    ]
    assert select_condition_market(skus, "NM", "holofoil") == 8.0


def test_empty_or_all_null_returns_none():
    assert select_condition_market([], "NM", "holofoil") is None
    skus = [{"variantRaw": "Holofoil", "condition": "NM", "marketPrice": None}]
    assert select_condition_market(skus, "NM", "holofoil") is None


def test_default_foil_is_holofoil():
    skus = [
        {"variantRaw": "Holofoil", "condition": "NM", "marketPrice": 40.0},
        {"variantRaw": "Normal",   "condition": "NM", "marketPrice": 2.0},
    ]
    assert select_condition_market(skus, "NM") == 40.0


def test_unknown_condition_returns_best_same_printing():
    # Blank/odd condition → least-played available price for that printing.
    val = select_condition_market(FLYGON, "", "reverseHolofoil")
    assert val == 69.53  # NM reverse is null, so best available is LP
