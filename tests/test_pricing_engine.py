"""Offline tests for the sales matching / scoring / pricing engine."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pricing_engine import (  # noqa: E402
    CardTarget, SaleCandidate, score_candidate, evaluate,
    normalize_number_variants, canon_condition, canon_finish, MIN_CONFIDENCE,
    classify_condition, UNKNOWN, map_official_condition,
)


# ── eBay official condition + item specifics ─────────────────────────────────
def test_official_condition_mapping():
    assert map_official_condition("Ungraded - Near Mint or Better") == "NM"
    assert map_official_condition("Ungraded - Excellent: Not in original packaging") == "LP"
    assert map_official_condition("Ungraded - Very Good") == "MP"
    assert map_official_condition("Ungraded - Poor") == "HP"
    assert map_official_condition("Ungraded - Near mint or better, has a crease") == "DMG"
    assert map_official_condition("") == ""


def test_official_condition_beats_title():
    # Title says LP-MP but the official eBay field says Near Mint or Better → NM.
    t = CardTarget(name="Charizard", set_name="Base Set", number="4", finish="holofoil")
    c = SaleCandidate(price=445, title="Charizard Base Set Holo LP-MP 4/102", source="ebay",
                      date="2026-06-20",
                      official_condition="Ungraded - Near Mint or Better",
                      item_specifics={"Set": "Base Set", "Card Number": "4/102",
                                      "Language": "English", "Game": "Pokémon TCG"})
    r = score_candidate(t, c)
    assert r.accepted and r.condition == "NM"


def test_item_specifics_reject_wrong_set():
    t = CardTarget(name="Charizard", set_name="Base Set", number="4", finish="holofoil")
    c = SaleCandidate(price=700, title="Charizard 4/102 Holo", source="ebay",
                      official_condition="Ungraded - Poor",
                      item_specifics={"Set": "Shadowless", "Card Number": "4/102"})
    r = score_candidate(t, c)
    assert not r.accepted and "wrong set" in r.reject_reason


def test_kyogre_official_condition_examples():
    # Real eBay listings whose title labels disagree with the official field.
    assert map_official_condition("Ungraded - Very good: Not in original packaging") == "MP"
    assert map_official_condition("Ungraded - Near mint or better: Not in original packaging") == "NM"


def test_kyogre_title_hp_does_not_override_official_nm():
    # Listing 318518519226: title says "HP" but the official eBay condition is
    # "Near mint or better" → must bucket NM, never Heavily Played.
    t = CardTarget(name="Team Aqua's Kyogre EX", set_name="Double Crisis",
                   number="6", finish="holofoil")
    c = SaleCandidate(
        price=50,
        title="Team Aqua's Kyogre EX 6/34 Double Crisis Full Art Holo Pokemon HP",
        source="ebay", official_condition="Ungraded - Near mint or better",
        item_specifics={"Set": "Double Crisis", "Card Number": "6/34",
                        "Language": "English", "Graded": "No"})
    r = score_candidate(t, c)
    assert r.accepted and r.condition == "NM", (r.accepted, r.condition, r.reject_reason)


def test_kyogre_very_good_buckets_mp():
    # Listing 236635565790: official "Very good" → MP (regardless of title "MP").
    t = CardTarget(name="Kyogre", set_name="XY Black Star Promos",
                   number="XY51", finish="holofoil")
    c = SaleCandidate(
        price=15, title="Kyogre XY51 Holo XY Black Star Promo Pokemon MP",
        source="ebay", official_condition="Ungraded - Very good",
        item_specifics={"Set": "XY Black Star Promos", "Card Number": "XY51",
                        "Language": "English", "Graded": "No"})
    r = score_candidate(t, c)
    assert r.accepted and r.condition == "MP", (r.accepted, r.condition, r.reject_reason)


def test_item_specifics_graded_yes_rejected_for_raw():
    t = CardTarget(name="Charizard", set_name="Base Set", number="4")
    c = SaleCandidate(price=3000, title="Charizard 4/102", source="ebay",
                      item_specifics={"Graded": "Yes", "Professional Grader": "PSA",
                                      "Grade": "10", "Set": "Base Set"})
    r = score_candidate(t, c)
    assert not r.accepted and r.reject_reason == "graded card"


def _target(**kw):
    base = dict(name="Umbreon VMAX", set_name="Evolving Skies", number="215",
                finish="holofoil", condition="NM")
    base.update(kw)
    return CardTarget(**base)


# ── normalization ────────────────────────────────────────────────────────────
def test_number_variants():
    assert normalize_number_variants("4") == {"4", "04", "004"}
    assert normalize_number_variants("215") >= {"215"}
    assert normalize_number_variants("") == set()


def test_condition_finish_aliases():
    assert canon_condition("Near Mint") == "NM"
    assert canon_condition("lightly played") == "LP"
    assert canon_finish("Reverse Holo") == "reverseHolofoil"
    assert canon_finish("holo") == "holofoil"


# ── acceptance: a fully-matching eBay listing ────────────────────────────────
def test_accept_full_match_ebay():
    t = _target()
    c = SaleCandidate(price=1500, title="Umbreon VMAX 215/203 Evolving Skies Holo NM",
                      source="ebay", date="2026-06-01")
    r = score_candidate(t, c)
    assert r.accepted, r.reasons
    assert r.score >= MIN_CONFIDENCE
    assert r.condition == "NM" and r.finish == "holofoil"


def test_accept_tcg_productid():
    t = _target()
    c = SaleCandidate(price=1400, title="Umbreon VMAX", source="tcgplayer",
                      tcg_product_id="247312", tcg_condition="Near Mint",
                      tcg_printing="Holofoil", date="2026-06-02")
    r = score_candidate(t, c)
    assert r.accepted and "productId+40" in r.reasons


# ── rejects ──────────────────────────────────────────────────────────────────
def test_reject_lot():
    t = _target()
    r = score_candidate(t, SaleCandidate(
        price=50, title="Pokemon Umbreon VMAX 215 lot of 4 Evolving Skies", source="ebay"))
    assert not r.accepted and r.reject_reason.startswith("keyword:lot")


def test_reject_graded_when_target_raw():
    t = _target()
    r = score_candidate(t, SaleCandidate(
        price=3000, title="Umbreon VMAX 215/203 Evolving Skies PSA 10", source="ebay"))
    assert not r.accepted and r.reject_reason == "graded card"


def test_accept_name_plus_number_without_set():
    # Real eBay titles often omit the set name — name + (set-specific) number
    # is enough identity to accept (coverage), still rejecting unidentifiable ones.
    t = _target()
    r = score_candidate(t, SaleCandidate(
        price=1500, title="Umbreon VMAX 215/203 Holo", source="ebay", date="2026-06-01"))
    assert r.accepted and r.score >= MIN_CONFIDENCE


def test_reject_unidentified_name_only():
    # name only, no number and no set → cannot confirm identity → rejected
    t = _target()
    r = score_candidate(t, SaleCandidate(
        price=900, title="Umbreon VMAX Holo Pokemon card", source="ebay"))
    assert not r.accepted


def test_reject_wrong_card_similar_name():
    t = CardTarget(name="Umbreon", set_name="Evolving Skies", number="215")
    r = score_candidate(t, SaleCandidate(
        price=10, title="Espeon VMAX 200/203 Evolving Skies Holo", source="ebay"))
    assert not r.accepted and "name mismatch" in r.reject_reason


def test_reject_japanese_when_english():
    t = _target()
    r = score_candidate(t, SaleCandidate(
        price=500, title="Umbreon VMAX 215 Evolving Skies Japanese Holo", source="ebay"))
    assert not r.accepted and "language" in r.reject_reason


def test_reject_reverse_when_target_holo():
    t = _target(finish="holofoil")
    r = score_candidate(t, SaleCandidate(
        price=80, title="Umbreon VMAX 215/203 Evolving Skies Reverse Holo", source="ebay"))
    assert not r.accepted and "finish" in r.reject_reason


def test_graded_target_accepts_matching_grade():
    t = _target(graded=True, grade="PSA-10", condition="PSA-10")
    r = score_candidate(t, SaleCandidate(
        price=3200, title="Umbreon VMAX 215/203 Evolving Skies PSA 10 GEM MINT", source="ebay"))
    assert r.accepted and r.graded


def test_graded_target_rejects_wrong_grade():
    t = _target(graded=True, grade="PSA-10")
    r = score_candidate(t, SaleCandidate(
        price=1500, title="Umbreon VMAX 215/203 Evolving Skies PSA 9", source="ebay"))
    assert not r.accepted and "grade" in r.reject_reason


# ── pricing: median + IQR + separation ───────────────────────────────────────
def test_evaluate_median_and_outliers():
    t = _target()
    titles = "Umbreon VMAX 215/203 Evolving Skies Holo NM"
    prices = [1400, 1450, 1500, 1550, 1600, 9999]   # 9999 is an outlier
    cands = [SaleCandidate(price=p, title=titles, source="ebay", date=f"2026-06-0{i+1}")
             for i, p in enumerate(prices)]
    out = evaluate(t, cands)
    h = out["headline"]
    assert h is not None
    assert h["sample_size"] == 5            # outlier removed
    assert h["outliers_removed"] == 1
    assert 1400 <= h["median"] <= 1600
    assert h["sources"]["ebay"] == 5


def test_evaluate_separates_raw_and_graded_and_finish():
    t = _target()
    cands = [
        SaleCandidate(price=1500, title="Umbreon VMAX 215/203 Evolving Skies Holo NM", source="ebay", date="2026-06-01"),
        SaleCandidate(price=1520, title="Umbreon VMAX 215/203 Evolving Skies Holo Near Mint", source="ebay", date="2026-06-02"),
        SaleCandidate(price=1490, title="Umbreon VMAX 215/203 Evolving Skies Holo NM", source="ebay", date="2026-06-03"),
        # reverse holo should NOT land in the holofoil bucket (rejected vs holo target)
        SaleCandidate(price=80, title="Umbreon VMAX 215/203 Evolving Skies Reverse Holo", source="ebay", date="2026-06-04"),
        # graded should never mix with raw
        SaleCandidate(price=3000, title="Umbreon VMAX 215/203 Evolving Skies PSA 10", source="ebay", date="2026-06-05"),
    ]
    out = evaluate(t, cands)
    assert out["headline"]["sample_size"] == 3
    assert out["rejected_count"] == 2       # reverse + graded rejected for a raw-holo target
    assert 1490 <= out["headline"]["median"] <= 1520


# ── Unknown Raw bucket ───────────────────────────────────────────────────────
def test_unknown_condition_not_assumed_nm():
    t = _target()
    cands = [SaleCandidate(price=1500, title="Umbreon VMAX 215/203 Evolving Skies Holo",
                           source="ebay", date="2026-06-0%d" % i) for i in range(1, 6)]
    out = evaluate(t, cands)
    # No stated condition → Unknown Raw, NOT folded into NM.
    assert out["headline"] is None or out["headline"]["sample_size"] == 0
    assert f"{UNKNOWN}|holofoil" in out["groups"]
    assert out["groups"][f"{UNKNOWN}|holofoil"]["sample_size"] == 5


def test_include_unknown_merges_into_headline():
    t = _target()
    cands = [SaleCandidate(price=1500 + i, title="Umbreon VMAX 215/203 Evolving Skies Holo",
                           source="ebay", date="2026-06-0%d" % i) for i in range(1, 6)]
    out = evaluate(t, cands, include_unknown=True)
    assert out["headline"]["sample_size"] == 5
    assert out["headline"].get("includes_unknown") is True


# ── HP false positives (Hit Points ≠ Heavily Played) ─────────────────────────
def test_hp_hitpoints_not_heavily_played():
    for title in ["Charizard 150HP Base Set", "Pikachu HP 120 Holo",
                  "Mewtwo 120 HP Holo", "high HP Pokemon Holo"]:
        assert classify_condition(title) != "HP", title


def test_hp_condition_context_is_heavily_played():
    assert classify_condition("Charizard Base Set heavily played") == "HP"
    assert classify_condition("Blastoise condition HP") == "HP"
    assert classify_condition("Venusaur LP/MP/HP") == "HP"


def test_worse_condition_wins():
    # 'Near Mint' but with a crease → DMG
    assert classify_condition("Charizard Near Mint with crease") == "DMG"
    # NM with heavy whitening → DMG
    assert classify_condition("Charizard NM heavy whitening") == "DMG"


def test_hp_stat_in_full_pipeline_routes_unknown():
    t = _target(name="Charizard", set_name="Base Set", number="4")
    c = SaleCandidate(price=300, title="Charizard 4/102 Base Set Holo 120HP",
                      source="ebay", date="2026-06-10")
    r = score_candidate(t, c)
    # '120HP' must not classify as Heavily Played → routes to Unknown Raw, not HP.
    assert r.accepted and r.condition == UNKNOWN


def test_low_confidence_small_sample():
    t = _target()
    cands = [SaleCandidate(price=1500, title="Umbreon VMAX 215/203 Evolving Skies Holo NM",
                           source="ebay", date="2026-06-01")]
    out = evaluate(t, cands)
    assert out["headline"]["low_confidence"] is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
