"""
Precise eBay query construction + graded item-specifics enforcement.

Levers to approach PriceCharting-grade accuracy: (1) build a tight per-variant /
per-grade search so there's less to filter, and (3) never let a slab of the
wrong (or unconfirmed) grade into a per-grade median.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pricing_engine import (  # noqa: E402
    build_ebay_query, grades_match, score_candidate, CardTarget, SaleCandidate,
)


# ── build_ebay_query ─────────────────────────────────────────────────────────
def test_raw_query_excludes_graders_and_lots():
    q = build_ebay_query("Charizard", "4", "Base Set", finish="holofoil")
    assert q.startswith("Charizard 4")
    for neg in ("-psa", "-bgs", "-cgc", "-graded", "-lot", "-bundle"):
        assert neg in q
    assert "Base Set" not in q          # set name never over-narrows the query


def test_reverse_holo_adds_keyword():
    q = build_ebay_query("Umbreon", "13", finish="reverseHolofoil")
    assert "reverse holo" in q


def test_first_edition_adds_keyword():
    q = build_ebay_query("Charizard", "4", finish="1stEditionHolofoil")
    assert "1st edition" in q


def test_holofoil_and_normal_add_no_finish_keyword():
    assert "holo" not in build_ebay_query("Pikachu", "58", finish="holofoil").lower().replace("holofoil","")
    q = build_ebay_query("Pikachu", "58", finish="normal")
    assert "reverse" not in q and "1st edition" not in q


def test_graded_query_includes_grade_and_keeps_slabs():
    q = build_ebay_query("Umbreon VMAX", "215", finish="holofoil", grade="PSA-10")
    assert '"PSA 10"' in q            # quoted grade phrase
    assert "-psa" not in q            # must NOT exclude graded when searching a grade
    assert "-lot" in q and "-bundle" in q


def test_japanese_adds_keyword():
    assert "japanese" in build_ebay_query("Pikachu", "25", language="JP")


# ── grades_match ─────────────────────────────────────────────────────────────
def test_grades_match_true_false_none():
    assert grades_match("PSA-10", "PSA 10") is True
    assert grades_match("PSA-10", "PSA 9") is False
    assert grades_match("PSA-10", "") is None
    assert grades_match("", "PSA 10") is None


def test_grades_match_numeric_only_when_grader_absent():
    assert grades_match("10", "PSA 10") is True        # bare 10 matches PSA 10
    assert grades_match("PSA-10", "BGS 10") is False   # graders differ
    assert grades_match("PSA-9.5", "PSA 9.5") is True


# ── graded enforcement in score_candidate ────────────────────────────────────
def _gtarget(grade="PSA-10", finish="holofoil"):
    return CardTarget(name="Umbreon VMAX", set_name="Evolving Skies", number="215",
                      finish=finish, condition=grade, graded=True, grade=grade)


def test_graded_accepts_matching_grade_from_title():
    r = score_candidate(_gtarget(), SaleCandidate(
        price=3000, title="Umbreon VMAX 215/203 Evolving Skies PSA 10 GEM MINT",
        source="ebay"))
    assert r.accepted


def test_graded_rejects_wrong_grade():
    r = score_candidate(_gtarget(), SaleCandidate(
        price=1500, title="Umbreon VMAX 215/203 Evolving Skies PSA 9", source="ebay"))
    assert not r.accepted and "grade" in r.reject_reason


def test_graded_rejects_unconfirmed_grade():
    # Graded slab but no readable grade in title or specifics → must not be
    # counted toward the PSA-10 median.
    r = score_candidate(_gtarget(), SaleCandidate(
        price=2000, title="Umbreon VMAX 215/203 Evolving Skies graded slab",
        source="ebay", item_specifics={"Graded": "Yes"}))
    assert not r.accepted and "unconfirmed" in r.reject_reason


def test_graded_accepts_grade_from_item_specifics():
    r = score_candidate(_gtarget(), SaleCandidate(
        price=3100, title="Umbreon VMAX 215/203 Evolving Skies", source="ebay",
        item_specifics={"Graded": "Yes", "Professional Grader": "PSA", "Grade": "10"}))
    assert r.accepted


def test_graded_rejects_ungraded_listing():
    r = score_candidate(_gtarget(), SaleCandidate(
        price=200, title="Umbreon VMAX 215/203 Evolving Skies NM", source="ebay",
        official_condition="Ungraded - Near Mint or Better"))
    assert not r.accepted
