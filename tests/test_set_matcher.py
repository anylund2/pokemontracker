"""
Set-name detection in free-text queries (`set_matcher`).

Regression suite for the search bug: "Tyranitar Expedition" resolved no set
(official name is "Expedition Base Set"), so the fallback returned every
Tyranitar from every set. These lock in that common set names resolve, that
matching is whole-word (no 'base' inside 'Baltoy'), and that the longest phrase
wins.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from set_matcher import (  # noqa: E402
    generate_set_aliases, match_set_in_query, norm_phrase, SET_ALIASES,
)

# A slice of the real pokemontcg.io set list — note the oddball official names.
SETS = [
    {"id": "ecard1", "name": "Expedition Base Set"},
    {"id": "ecard2", "name": "Aquapolis"},
    {"id": "ecard3", "name": "Skyridge"},
    {"id": "hgss3",  "name": "HS—Undaunted"},
    {"id": "hgss1",  "name": "HeartGold & SoulSilver"},
    {"id": "col1",   "name": "Call of Legends"},
    {"id": "base1",  "name": "Base"},
    {"id": "base4",  "name": "Base Set 2"},
    {"id": "base2",  "name": "Jungle"},
    {"id": "sm115",  "name": "Shining Legends"},
]

IDX = generate_set_aliases(SETS, SET_ALIASES)


def _match(q):
    return match_set_in_query(q, IDX)


# ── The headline bug ─────────────────────────────────────────────────────────
def test_expedition_resolves_despite_longer_official_name():
    sid, leftover = _match("tyranitar expedition")
    assert sid == "ecard1"
    assert leftover == "tyranitar"


def test_undaunted_resolves_through_em_dash_name():
    sid, leftover = _match("umbreon undaunted 2010")
    assert sid == "hgss3"
    # year is left for the caller's number/year parser; set word is consumed
    assert leftover == "umbreon 2010"


# ── Whole-word matching (no false positives) ─────────────────────────────────
def test_base_does_not_match_inside_baltoy():
    assert _match("baltoy") == (None, "baltoy")


def test_plain_pokemon_name_matches_no_set():
    assert _match("charizard") == (None, "charizard")


def test_short_fragment_below_min_length_ignored():
    # "ex" etc. are too short to be aliases; nothing should match here.
    assert _match("mew") == (None, "mew")


# ── Longest phrase wins ──────────────────────────────────────────────────────
def test_base_set_2_beats_base_and_base_set():
    sid, leftover = _match("dragonite base set 2")
    assert sid == "base4"
    assert leftover == "dragonite"


def test_call_of_legends_beats_shorter_matches():
    sid, leftover = _match("typhlosion call of legends")
    assert sid == "col1"
    assert leftover == "typhlosion"


def test_base_set_maps_to_base1_not_leftover_set_word():
    sid, leftover = _match("charizard base set")
    assert sid == "base1"
    assert leftover == "charizard"          # the stray "set" is dropped


# ── Alias generation ─────────────────────────────────────────────────────────
def test_stripped_base_set_alias_is_generated():
    idx = generate_set_aliases([{"id": "ecard1", "name": "Expedition Base Set"}], {})
    assert idx.get("expedition") == "ecard1"
    assert idx.get("expedition base set") == "ecard1"


def test_curated_alias_wins_over_generated():
    # "base" (from official name "Base") and curated "base"->base1 agree; the
    # curated map must still be represented in the index.
    idx = generate_set_aliases(SETS, SET_ALIASES)
    assert idx["base"] == "base1"
    assert idx["undaunted"] == "hgss3"


def test_single_word_full_name_matches():
    assert _match("tyranitar aquapolis") == ("ecard2", "tyranitar")
    assert _match("charizard skyridge") == ("ecard3", "charizard")


def test_heartgold_soulsilver_alias_resolves():
    assert _match("lugia heartgold soulsilver")[0] == "hgss1"
    assert _match("ho-oh soulsilver")[0] == "hgss1"


# ── Leftover / ordering behaviour ────────────────────────────────────────────
def test_set_name_stripped_regardless_of_position():
    # set name at the front, pokemon after
    sid, leftover = _match("expedition tyranitar")
    assert sid == "ecard1" and leftover == "tyranitar"


def test_set_only_query_returns_empty_leftover():
    sid, leftover = _match("aquapolis")
    assert sid == "ecard2" and leftover == ""


def test_norm_phrase_collapses_punctuation():
    assert norm_phrase("HS—Undaunted") == "hs undaunted"
    assert norm_phrase("HeartGold & SoulSilver") == "heartgold soulsilver"


def test_empty_query_matches_nothing():
    assert match_set_in_query("", IDX) == (None, "")


def test_shining_legends_not_matched_by_bare_legends():
    # There is no bare "Legends" set here, so "mew legends" must not resolve one.
    assert _match("mew legends") == (None, "mew legends")
