"""
Collection CSV import parsing (`csv_import.parse_import_csv`).

Regression suite for "import does nothing": a UTF-8 BOM on the first header
(Excel / Google Sheets exports) made the name column unreachable, so every row
was skipped. Also locks in delimiter/newline tolerance and field normalization.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from csv_import import parse_import_csv, norm_cond, norm_foil  # noqa: E402


# ── The headline bug: BOM ────────────────────────────────────────────────────
def test_utf8_bom_header_still_finds_name():
    text = "﻿name,set,number,condition\nCharizard,Base Set,4,NM"
    out = parse_import_csv(text)
    assert out["skipped"] == 0 and out["total"] == 1
    assert out["rows"][0]["card_name"] == "Charizard"


def test_bom_with_crlf_excel_export():
    text = "﻿name,set,condition,quantity\r\nBlastoise,Base Set,LP,2\r\n"
    out = parse_import_csv(text)
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["card_name"] == "Blastoise" and row["quantity"] == 2


# ── Delimiters / quoting ─────────────────────────────────────────────────────
def test_tab_delimited_is_detected():
    text = "name\tset\tcondition\nPikachu\tJungle\tMP"
    out = parse_import_csv(text)
    assert out["rows"][0]["card_name"] == "Pikachu"
    assert out["rows"][0]["set_name"] == "Jungle"


def test_quoted_headers_and_values():
    text = '"name","set","condition"\n"Mr. Mime","Jungle","NM"'
    out = parse_import_csv(text)
    assert out["rows"][0]["card_name"] == "Mr. Mime"


def test_alternate_column_names():
    text = "card,set name,card_number,cond,qty,cost,lang\nMew,Wizards Promo,8,near mint,2,$5.50,en"
    row = parse_import_csv(text)["rows"][0]
    assert row["card_name"] == "Mew"
    assert row["set_name"] == "Wizards Promo"
    assert row["number"] == "8"
    assert row["condition"] == "NM"
    assert row["quantity"] == 2
    assert row["purchase_price"] == 5.50
    assert row["language"] == "EN"


# ── Row skipping / counts ────────────────────────────────────────────────────
def test_rows_without_name_are_skipped_not_added():
    text = "name,set,condition\nCharizard,Base Set,NM\n,Jungle,LP\n  ,Fossil,MP"
    out = parse_import_csv(text)
    assert len(out["rows"]) == 1
    assert out["skipped"] == 2
    assert out["total"] == 3


def test_empty_input_returns_empty():
    assert parse_import_csv("") == {"rows": [], "skipped": 0, "total": 0}
    assert parse_import_csv("   \n  ")["rows"] == []


# ── Field normalization ──────────────────────────────────────────────────────
def test_condition_normalization():
    assert norm_cond("near mint") == "NM"
    assert norm_cond("Lightly Played") == "LP"
    assert norm_cond("psa 10") == "PSA-10"
    assert norm_cond("PSA-9") == "PSA-9"
    assert norm_cond("bgs 9.5") == "BGS-9.5"
    assert norm_cond("") == "NM"           # blank defaults to NM


def test_foil_normalization_and_default():
    assert norm_foil("reverse holo") == "reverseHolofoil"
    assert norm_foil("Holo") == "holofoil"
    assert norm_foil("1st edition") == "1stEditionHolofoil"
    assert norm_foil("") == ""             # unknown → empty ...
    # ... but a parsed row defaults empty foil to holofoil
    row = parse_import_csv("name,condition\nAbra,NM")["rows"][0]
    assert row["foil_type"] == "holofoil"


def test_reverse_holo_foil_survives_a_full_row():
    text = "name,set,number,condition,foil\nUmbreon,Neo Discovery,13,LP,reverse holo"
    row = parse_import_csv(text)["rows"][0]
    assert row["foil_type"] == "reverseHolofoil"
    assert row["condition"] == "LP"


# ── Numeric parsing ──────────────────────────────────────────────────────────
def test_quantity_parsing_edge_cases():
    def qty(v):
        return parse_import_csv(f"name,quantity\nX,{v}")["rows"][0]["quantity"]
    assert qty("") == 1
    assert qty("3") == 3
    assert qty("3.0") == 3
    assert qty("abc") == 1
    assert qty("0") == 1                    # clamped to at least 1


def test_price_parsing_strips_currency_and_commas():
    def paid(v):
        return parse_import_csv(f'name,price_paid\nX,{v}')["rows"][0]["purchase_price"]
    assert paid("$250") == 250.0
    assert paid('"1,250.50"') == 1250.50   # comma price must be quoted in CSV
    assert paid("") is None
    assert paid("n/a") is None


def test_ragged_row_with_stray_comma_does_not_crash():
    # An unquoted comma makes the row wider than the header — must not raise,
    # and the mapped columns still import.
    text = "name,set,condition\nCharizard,Base Set,NM,extra,junk"
    out = parse_import_csv(text)
    assert out["rows"][0]["card_name"] == "Charizard"
    assert out["rows"][0]["condition"] == "NM"


def test_language_uppercased():
    assert parse_import_csv("name,lang\nPikachu,jp")["rows"][0]["language"] == "JP"


def test_headers_are_case_insensitive():
    text = "Name,SET,Condition\nGyarados,Base Set,HP"
    row = parse_import_csv(text)["rows"][0]
    assert row["card_name"] == "Gyarados" and row["condition"] == "HP"
