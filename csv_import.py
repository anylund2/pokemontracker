"""
Collection CSV/TSV import parsing — pure, offline-testable (no network).

Players paste rows or upload a file exported from Excel / Google Sheets / a
deckbox tool. Those exports are messy: a UTF-8 BOM on the first header, CRLF
line endings, quoted headers, tabs instead of commas, and wildly varied column
names and condition/finish spellings. This module turns all of that into clean,
normalized rows; the caller then resolves each card's metadata and stores it.

The BOM is the important one: without stripping it the first header reads
"﻿name", the name column is never found, and every row is skipped — the
"import does nothing" bug.
"""
from __future__ import annotations

import csv
import io
import re

# Condition spellings → our canonical codes.
IMPORT_COND_MAP = {
    "nm": "NM", "near mint": "NM", "mint": "NM", "m": "NM", "nearmint": "NM",
    "lp": "LP", "lightly played": "LP", "light play": "LP", "ex": "LP", "excellent": "LP",
    "mp": "MP", "moderately played": "MP", "moderate play": "MP", "good": "MP", "vg": "MP",
    "hp": "HP", "heavily played": "HP", "heavy play": "HP", "played": "HP", "poor": "HP",
    "dmg": "DMG", "damaged": "DMG", "dm": "DMG",
}
# Finish/printing spellings → TCG foil codes.
IMPORT_FOIL_MAP = {
    "holo": "holofoil", "holofoil": "holofoil", "foil": "holofoil",
    "reverse": "reverseHolofoil", "reverse holo": "reverseHolofoil",
    "reverseholofoil": "reverseHolofoil", "rev holo": "reverseHolofoil",
    "normal": "normal", "non-holo": "normal", "nonholo": "normal", "regular": "normal",
    "1st edition": "1stEditionHolofoil", "1st edition holo": "1stEditionHolofoil",
    "1st ed": "1stEditionHolofoil", "unlimited": "unlimitedHolofoil",
}


def norm_cond(s: str) -> str:
    """Normalize a free-text condition/grade → 'NM'/'LP'/… or 'PSA-10' etc."""
    s = (s or "").strip().lower()
    m = re.match(r"psa\s*-?\s*(10|[1-9](?:\.5)?)", s)
    if m:
        return f"PSA-{m.group(1)}"
    for grader in ("bgs", "cgc", "sgc", "tag", "beckett"):
        m = re.match(grader + r"\s*-?\s*(10|[1-9](?:\.5)?)", s)
        if m:
            return f"{grader.upper()}-{m.group(1)}"
    return IMPORT_COND_MAP.get(s, (s.upper() if s else "NM"))


def norm_foil(s: str) -> str:
    """Normalize a free-text finish → a TCG foil code, or '' if unrecognized."""
    return IMPORT_FOIL_MAP.get((s or "").strip().lower(), "")


def _clean_text(text: str) -> str:
    """Strip a leading BOM and normalize newlines so the parser sees clean data."""
    return (text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


def _pick(row: dict, *keys: str) -> str:
    for k in keys:
        if row.get(k):
            return row[k]
    return ""


def parse_import_csv(text: str) -> dict:
    """Parse pasted/uploaded CSV or TSV into normalized card rows.

    Pure — no network, no DB. Returns::

        {"rows": [ {card_name, set_name, number, language, condition,
                    foil_type, quantity, purchase_price} ... ],
         "skipped": <rows with no name>, "total": <data rows seen>}

    Tolerant of a UTF-8 BOM, CRLF endings, tab or comma delimiters, quoted or
    oddly-cased headers, and many column-name/condition/finish spellings.
    """
    text = _clean_text(text)
    if not text.strip():
        return {"rows": [], "skipped": 0, "total": 0}

    sample = text[:2000]
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    raw_rows = list(reader)

    out, skipped = [], 0
    for raw in raw_rows:
        # Normalize headers: drop BOM + surrounding quotes/space so "﻿name"
        # and '"Name"' both become "name". A ragged row (more fields than
        # headers, e.g. a stray comma) puts the overflow in a list under the
        # None restkey — skip it instead of crashing the whole import.
        r = {}
        for k, v in raw.items():
            if k is None:
                continue
            if isinstance(v, list):
                v = " ".join(x for x in v if x)
            r[(k or "").strip().strip('"﻿').strip().lower()] = (v or "").strip()
        name = _pick(r, "name", "card", "card name", "card_name", "cardname")
        if not name:
            skipped += 1
            continue
        language = (_pick(r, "language", "lang") or "EN").upper()
        number = _pick(r, "number", "no", "card number", "card_number", "#")
        set_name = _pick(r, "set", "set name", "set_name", "setname")
        cond = norm_cond(_pick(r, "condition", "cond", "grade") or "NM")
        foil = norm_foil(_pick(r, "foil", "foil_type", "foiltype", "printing", "finish"))
        qty_s = _pick(r, "quantity", "qty", "count")
        paid_s = _pick(r, "price paid", "price_paid", "paid",
                       "purchase price", "purchase_price", "cost")
        try:
            qty = max(1, int(float(qty_s))) if qty_s else 1
        except ValueError:
            qty = 1
        try:
            paid = float(re.sub(r"[^\d.]", "", paid_s)) if paid_s else None
        except ValueError:
            paid = None

        out.append({
            "card_name": name,
            "set_name": set_name or None,
            "number": number or None,
            "language": language,
            "condition": cond,
            "foil_type": foil or "holofoil",
            "quantity": qty,
            "purchase_price": paid,
        })
    return {"rows": out, "skipped": skipped, "total": len(raw_rows)}
