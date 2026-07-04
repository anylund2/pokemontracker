"""
TCGplayer product-hit scoring — pure, offline-testable.

When the authoritative prices.pokemontcg.io redirect can't map a card to its
TCGplayer product, we fall back to a catalog search and pick the best hit. The
scoring must distinguish *variants* of the same card — most importantly the
Call-of-Legends "SL##" shinies, which TCGplayer lists as a separate
"Rayquaza (Shiny)" product. Without variant awareness the regular and shiny hits
tie and the first (regular) wins, so the shiny gets priced as the common card.
"""
from __future__ import annotations

import re

_SEALED_WORDS = ("booster", " box", "pack", " tin", "collection", "bundle", "case")


def is_shiny_number(number: str) -> bool:
    """True for the 'SL##' collector numbers of the Call of Legends shiny subset."""
    return bool(re.match(r"(?i)\s*sl\d", number or ""))


def score_tcg_hit(hit_name: str, hit_set: str, name_tokens: list,
                  target_set: str, number: str, is_shiny: bool) -> int:
    """Relevance of one catalog hit to the target card. Higher is better;
    returns a strong negative to reject sealed products outright.

    ``name_tokens`` — lowercased significant words of the card name.
    ``is_shiny`` — whether the TARGET card is a shiny (SL##) variant.
    """
    pn = (hit_name or "").lower()
    sn = (hit_set or "").lower()
    if any(w in pn for w in _SEALED_WORDS):
        return -100
    score = sum(1 for t in name_tokens if t in pn)
    if target_set and target_set.lower() in sn:
        score += 3
    if number and re.search(rf"\b0*{re.escape(number)}\b", pn + " " + sn):
        score += 2
    prod_shiny = "shiny" in pn
    if is_shiny and prod_shiny:
        score += 4
    elif is_shiny and not prod_shiny:
        score -= 3
    elif not is_shiny and prod_shiny:
        score -= 4
    return score


def _norm_num(n: str) -> str:
    """Collector number to a comparable form: '20/95'→'20', 'SL10'→'sl10', '004'→'4'."""
    n = (n or "").strip().lower().split("/")[0].strip()
    m = re.match(r"^([a-z]*)0*(\d+)$", n)
    return (m.group(1) + m.group(2)) if m else n


def verify_product_match(card_name: str, set_name: str, number: str,
                         product_name: str, product_set: str = "",
                         product_number: str = "", product_rarity: str = "") -> dict:
    """Cross-check a resolved TCGplayer product against the expected card.

    Pure/offline. Returns ``{"status", "confidence" (0-100), "reasons",
    "productName", "productNumber"}`` where status is ``verified`` (confident
    same card), ``mismatch`` (resolved product looks like a different card /
    variant — e.g. the regular card standing in for the shiny), or
    ``unverified`` (no product metadata to check against).
    """
    if not product_name:
        return {"status": "unverified", "confidence": 0,
                "reasons": ["no product metadata"], "productName": "",
                "productNumber": ""}
    pn, ps = product_name.lower(), (product_set or "").lower()
    reasons, score = [], 0

    toks = [t for t in re.sub(r"[^a-z0-9 ]", " ", (card_name or "").lower()).split()
            if len(t) > 1]
    hit = sum(1 for t in toks if t in pn)
    if toks and hit == len(toks):
        score += 45; reasons.append("name")
    elif hit:
        score += 25; reasons.append(f"name~{hit}/{len(toks)}")
    else:
        reasons.append("name✗")

    if set_name and ps:
        if set_name.lower() in ps or ps in set_name.lower():
            score += 25; reasons.append("set")
        else:
            reasons.append("set✗")

    if number and product_number:
        if _norm_num(number) == _norm_num(product_number):
            score += 20; reasons.append("number")
        else:
            reasons.append(f"number✗:{product_number}")

    want_shiny = is_shiny_number(number)
    prod_shiny = ("shiny" in pn) or ("shiny" in (product_rarity or "").lower())
    if want_shiny == prod_shiny:
        score += 10
        if want_shiny:
            reasons.append("shiny")
    else:
        score -= 30
        reasons.append("VARIANT MISMATCH: expected shiny" if want_shiny
                       else "VARIANT MISMATCH: shiny product")

    score = max(0, min(100, score))
    return {"status": "verified" if score >= 70 else "mismatch",
            "confidence": score, "reasons": reasons,
            "productName": product_name, "productNumber": product_number}


def pick_best_hit(hits: list, card_name: str, target_set: str, number: str,
                  min_score: int = 2):
    """Choose the best-scoring catalog hit's productId (str) or None.

    ``hits`` — list of dicts with ``productName``/``setName``/``productId``.
    """
    name_tokens = [t for t in (card_name or "").lower().split() if len(t) > 1]
    is_shiny = is_shiny_number(number)
    best, best_score = None, 0
    for x in hits or []:
        s = score_tcg_hit(x.get("productName"), x.get("setName"),
                          name_tokens, target_set, number, is_shiny)
        if s > best_score:
            best, best_score = x, s
    if best and best_score >= min_score:
        try:
            return str(int(best["productId"]))
        except (KeyError, ValueError, TypeError):
            return None
    return None
