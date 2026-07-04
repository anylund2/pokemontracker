"""
TCGplayer product-hit scoring (`tcg_resolve`).

Regression suite for the Call-of-Legends shiny bug: "Rayquaza SL10" was resolving
to the regular Rayquaza product (they tied, first won), so the shiny showed the
common card's price. These lock in variant-aware, sealed-rejecting selection.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from tcg_resolve import (  # noqa: E402
    is_shiny_number, score_tcg_hit, pick_best_hit, verify_product_match,
)

# The actual TCGplayer catalog hits for "Rayquaza Call of Legends".
RAYQUAZA_HITS = [
    {"productId": 88628, "productName": "Rayquaza",          "setName": "Call of Legends"},
    {"productId": 88629, "productName": "Rayquaza (Shiny)",  "setName": "Call of Legends"},
    {"productId": 98515, "productName": "Call of Legends Booster Pack", "setName": "Call of Legends"},
    {"productId": 90003, "productName": "Totodile",          "setName": "Call of Legends"},
]


# ── The headline bug ─────────────────────────────────────────────────────────
def test_shiny_number_resolves_shiny_product():
    assert pick_best_hit(RAYQUAZA_HITS, "Rayquaza", "Call of Legends", "SL10") == "88629"


def test_regular_number_resolves_regular_product():
    assert pick_best_hit(RAYQUAZA_HITS, "Rayquaza", "Call of Legends", "20") == "88628"


def test_regular_never_grabs_shiny_product():
    pid = pick_best_hit(RAYQUAZA_HITS, "Rayquaza", "Call of Legends", "20")
    assert pid != "88629"


# ── Shiny-number detection ───────────────────────────────────────────────────
def test_is_shiny_number():
    assert is_shiny_number("SL10")
    assert is_shiny_number("sl1")
    assert is_shiny_number(" SL3 ")
    assert not is_shiny_number("20")
    assert not is_shiny_number("")
    assert not is_shiny_number("SLOWKING")   # needs a digit right after SL


# ── Sealed products are rejected ─────────────────────────────────────────────
def test_sealed_products_rejected():
    tokens = ["rayquaza"]
    assert score_tcg_hit("Call of Legends Booster Pack", "Call of Legends",
                         tokens, "Call of Legends", "SL10", True) < 0
    assert score_tcg_hit("Call of Legends Booster Box", "Call of Legends",
                         tokens, "Call of Legends", "20", False) < 0


def test_only_sealed_available_returns_none():
    hits = [{"productId": 98515, "productName": "Call of Legends Booster Pack",
             "setName": "Call of Legends"}]
    assert pick_best_hit(hits, "Rayquaza", "Call of Legends", "SL10") is None


# ── Scoring direction ────────────────────────────────────────────────────────
def test_shiny_target_prefers_shiny_over_regular_score():
    tok = ["rayquaza"]
    reg = score_tcg_hit("Rayquaza", "Call of Legends", tok, "Call of Legends", "SL10", True)
    shy = score_tcg_hit("Rayquaza (Shiny)", "Call of Legends", tok, "Call of Legends", "SL10", True)
    assert shy > reg


def test_regular_target_prefers_regular_over_shiny_score():
    tok = ["rayquaza"]
    reg = score_tcg_hit("Rayquaza", "Call of Legends", tok, "Call of Legends", "20", False)
    shy = score_tcg_hit("Rayquaza (Shiny)", "Call of Legends", tok, "Call of Legends", "20", False)
    assert reg > shy


def test_set_and_name_overlap_boost():
    tok = ["rayquaza"]
    hit = score_tcg_hit("Rayquaza", "Call of Legends", tok, "Call of Legends", "20", False)
    wrong_set = score_tcg_hit("Rayquaza", "EX Deoxys", tok, "Call of Legends", "20", False)
    assert hit > wrong_set


# ── Robustness ───────────────────────────────────────────────────────────────
def test_empty_hits_returns_none():
    assert pick_best_hit([], "Rayquaza", "Call of Legends", "SL10") is None


def test_below_threshold_returns_none():
    # A hit sharing neither name nor set stays under the min score.
    hits = [{"productId": 1, "productName": "Pikachu", "setName": "Jungle"}]
    assert pick_best_hit(hits, "Rayquaza", "Call of Legends", "SL10") is None


def test_disambiguates_two_shinies_by_set():
    hits = [
        {"productId": 1, "productName": "Rayquaza (Shiny)", "setName": "Some Other Set"},
        {"productId": 2, "productName": "Rayquaza (Shiny)", "setName": "Call of Legends"},
    ]
    assert pick_best_hit(hits, "Rayquaza", "Call of Legends", "SL10") == "2"


# ── Recognition / verification of a resolved product ─────────────────────────
def test_verify_correct_shiny_product_is_verified():
    v = verify_product_match("Rayquaza", "Call of Legends", "SL10",
                             "Rayquaza (Shiny)", "Call of Legends", "SL10", "Shiny Holo Rare")
    assert v["status"] == "verified" and v["confidence"] >= 70


def test_verify_shiny_card_on_regular_product_is_mismatch():
    # The exact bug: shiny SL10 resolved to the regular "Rayquaza" #20/95.
    v = verify_product_match("Rayquaza", "Call of Legends", "SL10",
                             "Rayquaza", "Call of Legends", "20/95", "Holo Rare")
    assert v["status"] == "mismatch"
    assert any("VARIANT MISMATCH" in r for r in v["reasons"])


def test_verify_regular_product_is_verified():
    v = verify_product_match("Rayquaza", "Call of Legends", "20",
                             "Rayquaza", "Call of Legends", "20/95", "Holo Rare")
    assert v["status"] == "verified"


def test_verify_wrong_card_name_is_mismatch():
    v = verify_product_match("Charizard", "Base Set", "4",
                             "Blastoise", "Base Set", "2", "Holo Rare")
    assert v["status"] == "mismatch"


def test_verify_no_metadata_is_unverified():
    v = verify_product_match("Rayquaza", "Call of Legends", "SL10", "")
    assert v["status"] == "unverified" and v["confidence"] == 0


def test_verify_number_variants_normalize():
    # "004" vs "4/102" should still count as the same number.
    v = verify_product_match("Charizard", "Base Set", "004",
                             "Charizard", "Base Set", "4/102", "Holo Rare")
    assert "number" in v["reasons"] and v["status"] == "verified"
