"""
PSA spec-selection tests — `_pick_spec` must be printing-aware so a reverse-holo
card lands on PSA's Reverse-Foil spec and a regular card does NOT (they're
separate specs with different auction data).
"""
from market_scraper import _pick_spec

ROWS = [
    {"href": "/spec/psa/111", "text": "Rocket's Meowth #19 Team Rocket Returns Pokemon Game"},
    {"href": "/spec/psa/222", "text": "Rocket's Meowth #19 Team Rocket Returns Reverse Foil Pokemon Game"},
]


def test_regular_avoids_reverse_spec():
    assert _pick_spec(ROWS, "Rocket's Meowth", "19", "holofoil") == "111"
    assert _pick_spec(ROWS, "Rocket's Meowth", "19", "normal") == "111"


def test_reverse_picks_reverse_spec():
    assert _pick_spec(ROWS, "Rocket's Meowth", "19", "reverseHolofoil") == "222"


def test_first_edition_prefers_1st_ed_spec():
    rows = [
        {"href": "/spec/psa/1", "text": "Charizard #4 Base Set Unlimited Holo"},
        {"href": "/spec/psa/2", "text": "Charizard #4 Base Set 1st Edition Holo"},
    ]
    assert _pick_spec(rows, "Charizard", "4", "1stEditionHolofoil") == "2"
    assert _pick_spec(rows, "Charizard", "4", "holofoil") == "1"


def test_single_spec_still_resolves_for_any_printing():
    rows = [{"href": "/spec/psa/9", "text": "Pikachu #58 Base Set Pokemon Game"}]
    assert _pick_spec(rows, "Pikachu", "58", "reverseHolofoil") == "9"
    assert _pick_spec(rows, "Pikachu", "58", "holofoil") == "9"


def test_rejects_packs_and_boxes():
    rows = [{"href": "/spec/psa/5", "text": "Team Rocket Returns Booster Box"}]
    assert _pick_spec(rows, "Rocket's Meowth", "19", "holofoil") is None
