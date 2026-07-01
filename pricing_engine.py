"""
Card sales matching, validation, scoring and pricing engine.

A *candidate* sale (from TCGplayer or eBay) is accepted into a price calculation
only if it passes **structured validation** against a :class:`CardTarget`.  Fuzzy
text similarity may surface candidates upstream, but acceptance here is rule
based — never fuzzy alone.

Pipeline per candidate:
  1. parse structured features out of the listing (name tokens, set, number,
     finish, condition, language, graded/sealed flags, reject keywords);
  2. hard-reject on disqualifying signals (lots, proxies, wrong graded/sealed
     state, wrong language, wrong card);
  3. score 0–100 against the target using the weighted rubric;
  4. accept only at confidence ≥ ``MIN_CONFIDENCE`` (85);
  5. route accepted sales into (condition, finish, graded) buckets.

Pricing per bucket uses the **median** of IQR-trimmed sold prices, and reports
sample size, date range, a confidence figure, and a TCGplayer/eBay source split.
Raw and graded are never mixed; normal/foil/reverse/etched are never mixed.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field, asdict

MIN_CONFIDENCE = 85

# Listings containing any of these are never valid single-card sales.
REJECT_KEYWORDS = [
    "lot", "lots", "bundle", "playset", "play set", "proxy", "custom",
    "digital", "online code", "ptcgo", "ptcgl", "repack", "re-pack",
    "mystery", "orica", "fan art", "fan-made", "sticker", "jumbo", "oversized",
]

SEALED_KEYWORDS = [
    "booster box", "booster pack", "elite trainer", "etb", "sealed",
    "blister", "collection box", "tin", "bundle box", "case",
]

_GRADER_RE = re.compile(
    r"\b(PSA|BGS|BECKETT|CGC|SGC|ACE|TAG|GMA|HGA|MNT)\b\s*-?\s*(10|9\.5|9|8\.5|8|7|6|5|4|3|2|1)\b",
    re.I,
)
_GEM_RE = re.compile(r"\bgem\s*-?\s*mint\b|\bgem\s*mt\b", re.I)
# Any of these mean the listing is graded/slabbed/authenticated → not a raw card.
_GRADED_TERMS_RE = re.compile(
    r"\b(psa|bgs|beckett|cgc|sgc|tag|gma|hga|ace\s*grading|slab(?:bed)?|graded|"
    r"gem\s*mint|gem\s*mt|cert(?:ified|ification)?|authenticated|encapsulated)\b",
    re.I,
)

_LANG_MARKERS = {
    "japanese": ["japanese", "japan", "jpn", " jp ", "pokemon card game"],
    "german":   ["german", "deutsch"],
    "spanish":  ["spanish", "espanol", "español"],
    "french":   ["french", "francais", "français"],
    "italian":  ["italian", "italiano"],
    "korean":   ["korean"],
    "chinese":  ["chinese"],
    "portuguese": ["portuguese", "portugues"],
}

# "Unknown Raw" — a real raw sale whose condition couldn't be determined.  Kept
# OUT of NM/LP/MP/HP/DMG pricing unless the caller opts in.
UNKNOWN = "UNK"

_CONDITION_CANON = {
    "near mint": "NM", "lightly played": "LP", "moderately played": "MP",
    "heavily played": "HP", "damaged": "DMG", "nm": "NM", "lp": "LP",
    "mp": "MP", "hp": "HP", "dmg": "DMG",
}

# Worse condition wins when a listing carries conflicting signals.
_SEVERITY = {"NM": 0, "LP": 1, "MP": 2, "HP": 3, "DMG": 4}

# Physical-damage language → forces (at least) DMG.
_DAMAGE_TERMS_RE = re.compile(
    r"crease|creas(?:ed|ing)|\bbent\b|\bbend\b|water\s*damage|water\s*dmg|"
    r"\bink\b|written|\bmarked?\b|\btorn\b|\btear\b|peel(?:ed|ing)?|"
    r"heavy\s*whitening|\bscratch(?:ed|es)?\b|\bdinged?\b|\bdamaged?\b|\bdmg\b|\bpoor\b",
    re.I,
)


def _hp_is_condition(text: str) -> bool:
    """
    True only when 'HP' clearly means *Heavily Played*, never the card's Hit
    Points stat.  '120 HP', 'HP 120', '150HP', 'Hit Points', 'high HP' → False.
    """
    low = text.lower()
    if re.search(r"heavily\s*played", low):
        return True
    # 'condition: HP', 'HP condition', 'played condition'
    if re.search(r"condition\s*[:\-]?\s*hp\b|\bhp\s*condition|played\s*condition", low):
        return True
    # HP joined to another condition code by / or - : LP/MP/HP, MP-HP, HP/DMG
    if re.search(r"(?:nm|lp|mp|dmg)\s*[/\-]\s*hp\b|\bhp\s*[/\-]\s*(?:nm|lp|mp|dmg)", low):
        return True
    return False


def classify_condition(*texts: str) -> str:
    """
    Map a listing's text (title + subtitle + specifics + description) to a single
    condition code, applying *worse-condition-wins*.  Returns '' (→ Unknown Raw)
    when nothing condition-related is found — raw cards are NOT assumed NM.
    """
    blob = " ".join(t for t in texts if t)
    if not blob.strip():
        return ""
    low = blob.lower()
    found: set[str] = set()

    # explicit multi-word grades
    if re.search(r"near\s*mint|\bnm[-/ ]?mt\b|pack\s*fresh", low):
        found.add("NM")
    if re.search(r"\bmint\b", low) and not _GEM_RE.search(blob):
        found.add("NM")
    if re.search(r"lightly\s*played|\bexcellent\b", low):
        found.add("LP")
    if re.search(r"moderately\s*played", low):
        found.add("MP")
    # bare 'played' (not lightly/moderately/heavily) → MP
    if re.search(r"(?<!lightly\s)(?<!moderately\s)(?<!heavily\s)\bplayed\b", low):
        found.add("MP")
    if re.search(r"\bdamaged?\b|(?<![a-z])dmg(?![a-z])|\bpoor\b", low):
        found.add("DMG")

    # short codes in condition context (avoid matching parts of other words /
    # set names is rare in card titles); 'HP' handled separately below.
    for code in ("NM", "LP", "MP", "DMG"):
        if re.search(rf"(?<![A-Za-z0-9]){code}(?![A-Za-z0-9])", blob):
            found.add(code)

    # Heavily Played only in true condition context (never the Hit Points stat).
    if _hp_is_condition(blob):
        found.add("HP")

    # Any physical-damage language forces DMG (worse-condition-wins handles the
    # 'NM with a crease' case → DMG).
    if _DAMAGE_TERMS_RE.search(blob):
        found.add("DMG")

    if not found:
        return ""
    return max(found, key=lambda c: _SEVERITY[c])


def map_official_condition(condition_display_name: str) -> str:
    """
    Map eBay's OFFICIAL trading-card condition field (conditionDisplayName) to an
    internal bucket.  This is the authoritative source for raw cards, e.g.
    "Ungraded - Near Mint or Better", "Ungraded - Excellent", "Ungraded - Poor".

    Returns "" (→ UNKNOWN_RAW) when the field is missing or non-committal.
    """
    if not condition_display_name:
        return ""
    c = condition_display_name.lower()
    # Explicit physical damage → DMG.  NOTE: "Poor" is eBay's lowest *grade*
    # (→ HP per spec), so it is deliberately NOT treated as damage here.
    if re.search(r"crease|creas|\bbent\b|\bbend\b|water\s*damage|\bink\b|written|"
                 r"\bmarked?\b|\btorn\b|\btear\b|peel|\bdamaged?\b|\bdmg\b", c):
        return "DMG"
    if "near mint or better" in c or "near mint" in c or re.search(r"\bnm\b", c):
        return "NM"
    if "excellent" in c or "lightly played" in c or re.search(r"\blp\b|\bex\b", c):
        return "LP"
    if "very good" in c or "moderately played" in c or re.search(r"\bmp\b", c) \
            or re.search(r"(?<!lightly )(?<!moderately )(?<!heavily )\bplayed\b", c):
        return "MP"
    if "poor" in c or "heavily played" in c:
        return "HP"
    if "mint" in c and "near" not in c:
        return "NM"          # plain "Mint" on a raw card
    return ""


# Finish/printing aliases → canonical code.
FINISH_CANON = {
    "holofoil": "holofoil", "holo": "holofoil", "holographic": "holofoil",
    "foil": "holofoil",
    "reverse holofoil": "reverseHolofoil", "reverse holo": "reverseHolofoil",
    "reverse foil": "reverseHolofoil", "rev holo": "reverseHolofoil",
    "normal": "normal", "non-holo": "normal", "non holo": "normal",
    "1st edition holofoil": "1stEditionHolofoil",
    "1st edition": "1stEditionNormal",
    "unlimited holofoil": "unlimitedHolofoil",
    "etched": "etchedFoil", "etch": "etchedFoil",
}


def canon_condition(text: str) -> str:
    if not text:
        return ""
    return _CONDITION_CANON.get(text.strip().lower(), "")


def canon_finish(text: str) -> str:
    if not text:
        return ""
    return FINISH_CANON.get(text.strip().lower(), text)


# Condition severity, best → worst. Used to fall back within a printing.
_COND_SEVERITY = ["NM", "LP", "MP", "HP", "DMG"]


def _variant_key(text: str) -> str:
    """Collapse a printing label to a comparable key.

    TCGplayer's snapshot spells the printing with spaces ("Reverse Holofoil")
    while our foil codes are camel-case ("reverseHolofoil"); stripping every
    non-alphanumeric and lower-casing makes both sides equal so a reverse-holo
    card can never silently match the plain-holo SKU (the bug this guards).
    """
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def select_condition_market(skus: list, condition: str, foil: str = "holofoil"):
    """TCGplayer snapshot market price for the card's EXACT printing × condition.

    Printing is **strict**: a value is only ever taken from a SKU of the same
    printing (raw NM holo must never stand in for a reverse-holo price — that is
    "mixing printings", which the pricing rules forbid). When the exact
    condition has no market price for that printing, fall back to the nearest
    condition *within the same printing* (ties resolve to the worse condition,
    so we never over-value). Returns ``None`` when the printing has no priced
    SKU at all, letting the caller fall through to other sources.

    ``skus`` — snapshot rows with ``condition`` (NM/LP/…), ``variantRaw``
    (printing label) and ``marketPrice``. ``foil`` — a foil code like
    ``reverseHolofoil`` / ``holofoil`` / ``normal``.
    """
    want_cond = (condition or "").upper()
    want_var = _variant_key(foil)
    # Same-printing priced SKUs only — this is what stops printing cross-over.
    priced: dict[str, float] = {}
    for s in skus or []:
        if _variant_key(s.get("variantRaw", "")) != want_var:
            continue
        mp = s.get("marketPrice")
        if mp:
            priced[(s.get("condition") or "").upper()] = float(mp)
    if not priced:
        return None
    if want_cond in priced:
        return priced[want_cond]

    def _sev(c: str) -> int:
        return _COND_SEVERITY.index(c) if c in _COND_SEVERITY else len(_COND_SEVERITY)

    if want_cond not in _COND_SEVERITY:
        # Unknown condition → best (least-played) same-printing price available.
        return priced[min(priced, key=_sev)]
    ti = _sev(want_cond)
    # Nearest condition; equal distance → the worse (larger severity) one.
    best = min(priced, key=lambda c: (abs(_sev(c) - ti), -_sev(c)))
    return priced[best]


def normalize_number_variants(number: str) -> set[str]:
    """All reasonable string forms of a collector number ('4' → {'4','04','004'})."""
    n = (number or "").strip()
    if not n:
        return set()
    out = {n, n.lstrip("0") or n}
    if n.isdigit():
        out.update({n.zfill(2), n.zfill(3)})
    return {x for x in out if x}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class CardTarget:
    name: str
    line: str = "pokemon"
    set_name: str = ""
    set_code: str = ""
    number: str = ""
    rarity: str = ""
    finish: str = "holofoil"       # canonical finish code
    language: str = "english"
    condition: str = "NM"
    graded: bool = False
    grade: str = ""                # e.g. "PSA-10" when graded
    sealed: bool = False

    @property
    def name_tokens(self) -> list[str]:
        return [w.lower() for w in re.findall(r"[a-zA-Z]+", self.name or "")
                if len(w) > 1]


@dataclass
class SaleCandidate:
    price: float
    title: str = ""
    url: str = ""
    date: str = ""
    source: str = "ebay"            # "ebay" | "tcgplayer"
    subtitle: str = ""              # eBay subtitle, if any
    tcg_product_id: str = ""
    tcg_condition: str = ""         # structured TCG condition, e.g. "Near Mint"
    tcg_printing: str = ""          # structured TCG printing, e.g. "Holofoil"
    qty: int = 1
    # eBay item-detail fields (authoritative for raw cards)
    official_condition: str = ""    # eBay conditionDisplayName
    item_specifics: dict = field(default_factory=dict)
    shipping: float = 0.0
    item_id: str = ""


@dataclass
class CandidateResult:
    candidate: SaleCandidate
    score: int
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    reject_reason: str = ""
    condition: str = ""             # routed bucket condition
    finish: str = ""                # routed bucket finish
    graded: bool = False
    grade: str = ""


# ---------------------------------------------------------------------------
# Candidate parsing
# ---------------------------------------------------------------------------
def _detect_finish(title: str) -> str:
    t = (title or "").lower()
    if re.search(r"reverse\s*holo|rev\.?\s*holo|reverse\s*foil", t):
        return "reverseHolofoil"
    if re.search(r"etched", t):
        return "etchedFoil"
    if re.search(r"1st\s*ed", t):
        return "1stEditionHolofoil" if re.search(r"holo|foil", t) else "1stEditionNormal"
    if re.search(r"\bholo(?:foil|graphic)?\b|\bfoil\b", t):
        return "holofoil"
    if re.search(r"non[\s-]?holo|normal", t):
        return "normal"
    return ""


def _detect_grade(title: str) -> str:
    m = _GRADER_RE.search(title or "")
    if m:
        return f"{m.group(1).upper()}-{m.group(2)}"
    return ""


def _detect_language(title: str) -> str:
    t = f" {(title or '').lower()} "
    for lang, markers in _LANG_MARKERS.items():
        if any(mk in t for mk in markers):
            return lang
    return "english"


def _spec(c: SaleCandidate, *keys):
    """Case-insensitive lookup in eBay item specifics."""
    sp = c.item_specifics or {}
    low = {k.lower(): v for k, v in sp.items()}
    for key in keys:
        v = low.get(key.lower())
        if v:
            return v
    return ""


def parse_candidate(c: SaleCandidate) -> dict:
    """
    Extract structured features.  For eBay raw cards the OFFICIAL condition field
    and item specifics (when present) take priority over the listing title.
    """
    title = c.title or ""
    blob = f"{title} {c.subtitle or ''}"
    low = blob.lower()

    # Graded? eBay item specifics are authoritative.  "Ungraded - …" → raw.
    spec_graded = _spec(c, "Graded")
    spec_grader = _spec(c, "Professional Grader")
    official = (c.official_condition or "").lower()
    if official.startswith("ungraded") or spec_graded.lower() in ("no", "none"):
        graded = False
        grade = ""
    elif spec_graded.lower() == "yes" or spec_grader:
        graded = True
        grade = (f"{spec_grader} {_spec(c, 'Grade')}".strip()
                 or _detect_grade(blob))
    else:
        graded = bool(_GRADED_TERMS_RE.search(blob)) or bool(_detect_grade(blob)) \
                 or bool(_GEM_RE.search(blob))
        grade = _detect_grade(blob)

    # Condition priority: TCG structured → eBay official → text classifier.
    cond = (canon_condition(c.tcg_condition)
            or map_official_condition(c.official_condition)
            or classify_condition(title, c.subtitle or ""))

    # Finish: item specifics ('Features'/'Finish') → TCG printing → title.
    spec_finish = _spec(c, "Features", "Finish")
    finish = (canon_finish(c.tcg_printing) or canon_finish(spec_finish)
              or _detect_finish(f"{spec_finish} {title}"))

    spec_lang = _spec(c, "Language")
    language = (spec_lang.split("/")[0].strip().lower() if spec_lang
                else _detect_language(title))

    return {
        "graded": graded,
        "grade": grade,
        "finish": finish,
        "condition": cond,
        "language": language or "english",
        "reject_kw": next((k for k in REJECT_KEYWORDS
                           if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low)), ""),
        "sealed": any(k in low for k in SEALED_KEYWORDS),
        # item-specifics identity (authoritative when present)
        "spec_set": _spec(c, "Set"),
        "spec_number": _spec(c, "Card Number", "Collector Number"),
        "spec_name": _spec(c, "Card Name", "Character"),
    }


# ---------------------------------------------------------------------------
# Scoring + validation
# ---------------------------------------------------------------------------
def _number_in_title(target: CardTarget, title: str) -> bool:
    variants = normalize_number_variants(target.number)
    if not variants:
        return True
    t = title or ""
    for v in variants:
        if re.search(rf"(?<!\d){re.escape(v)}\s*/\s*\d+", t):
            return True
        if re.search(rf"#\s*0*{re.escape(v.lstrip('0') or v)}(?!\d)", t):
            return True
    # bare number with word boundaries (avoid matching years etc.)
    bare = target.number.lstrip("0") or target.number
    return bool(re.search(rf"(?<![\w/]){re.escape(bare)}(?![\w])", t))


def _set_in_title(target: CardTarget, title: str) -> bool:
    low = (title or "").lower()
    if target.set_name and target.set_name.lower() in low:
        return True
    if target.set_code and re.search(rf"\b{re.escape(target.set_code.lower())}\b", low):
        return True
    return False


def _score_tcg_product(target: CardTarget, c: SaleCandidate) -> CandidateResult:
    """
    Authoritative path: a TCGplayer sale tied to a resolved productId already
    identifies the exact product + printing, so we trust it and only validate
    the structured condition/finish for bucketing.  Graded sales never come
    from this feed.
    """
    finish = canon_finish(c.tcg_printing) or target.finish
    if target.finish and finish != target.finish:
        return CandidateResult(c, 0, False,
                               reject_reason=f"finish {finish} != {target.finish}")
    cond = canon_condition(c.tcg_condition) or "NM"
    reasons = ["productId+40", "name+20", "set+15", "number+10"]
    score = 85
    if finish == target.finish:
        score += 10; reasons.append("finish+10")
    if cond == target.condition:
        score += 5; reasons.append("condition+5")
    score = min(score, 100)
    return CandidateResult(c, score, True, reasons=reasons,
                           condition=cond, finish=finish)


def score_candidate(target: CardTarget, c: SaleCandidate) -> CandidateResult:
    if c.source == "tcgplayer" and c.tcg_product_id and not target.graded:
        return _score_tcg_product(target, c)

    feat = parse_candidate(c)
    reasons: list[str] = []
    title = c.title or ""
    low = title.lower()

    # ── hard rejects ────────────────────────────────────────────────────────
    if feat["reject_kw"]:
        return CandidateResult(c, 0, False, reject_reason=f"keyword:{feat['reject_kw']}")
    if feat["sealed"] and not target.sealed:
        return CandidateResult(c, 0, False, reject_reason="sealed product")
    if target.sealed and not feat["sealed"]:
        return CandidateResult(c, 0, False, reject_reason="not sealed")
    if feat["graded"] and not target.graded:
        return CandidateResult(c, 0, False, reject_reason="graded card")
    if target.graded and not feat["graded"]:
        return CandidateResult(c, 0, False, reject_reason="ungraded card")
    if target.graded and feat["graded"] and target.grade and feat["grade"] \
            and feat["grade"].replace(" ", "") != target.grade.replace(" ", ""):
        return CandidateResult(c, 0, False,
                               reject_reason=f"grade {feat['grade']} != {target.grade}")
    # language (default english unless target says otherwise)
    if feat["language"] != target.language.lower():
        return CandidateResult(c, 0, False,
                               reject_reason=f"language {feat['language']}")
    # name must appear (all significant tokens) — kills unrelated characters
    missing = [tok for tok in target.name_tokens if tok not in low]
    if missing:
        return CandidateResult(c, 0, False,
                               reject_reason=f"name mismatch (missing {','.join(missing)})")
    # finish mismatch (only when the candidate explicitly states a different finish)
    cand_finish = feat["finish"]
    if cand_finish and target.finish and cand_finish != target.finish:
        return CandidateResult(c, 0, False,
                               reject_reason=f"finish {cand_finish} != {target.finish}")

    # ── item-specifics identity (authoritative when eBay provides them) ───────
    spec_set, spec_number = feat.get("spec_set", ""), feat.get("spec_number", "")
    if spec_set and target.set_name and \
            target.set_name.lower() not in spec_set.lower() and \
            spec_set.lower() not in target.set_name.lower():
        return CandidateResult(c, 0, False,
                               reject_reason=f"wrong set ({spec_set} != {target.set_name})")
    if spec_number and target.number:
        nums = normalize_number_variants(target.number)
        if not any(re.search(rf"(?<!\d){re.escape(n)}(?!\d)", spec_number) for n in nums):
            return CandidateResult(c, 0, False,
                                   reject_reason=f"wrong number ({spec_number} != {target.number})")

    # ── scoring ─────────────────────────────────────────────────────────────
    score = 0
    name_ok = True
    score += 20; reasons.append("name+20")

    set_ok = bool(spec_set and (target.set_name.lower() in spec_set.lower()
                                or spec_set.lower() in target.set_name.lower())) \
             or _set_in_title(target, title) or (c.source == "tcgplayer" and c.tcg_product_id)
    if set_ok:
        score += 15; reasons.append("set+15")
    else:
        reasons.append("missing-set")

    number_ok = bool(spec_number and target.number and any(
            re.search(rf"(?<!\d){re.escape(n)}(?!\d)", spec_number)
            for n in normalize_number_variants(target.number))) \
        or _number_in_title(target, title) or (c.source == "tcgplayer" and c.tcg_product_id)
    if number_ok:
        score += 10; reasons.append("number+10")
    else:
        reasons.append("missing-number")

    # Finish: award when it matches OR is unstated — the eBay query is already
    # foil-filtered, so a title that simply omits "holo" is assumed to match.
    # A title that explicitly states a *different* finish was hard-rejected above.
    if cand_finish == target.finish or not cand_finish:
        score += 10
        reasons.append("finish+10" if cand_finish else "finish-assumed+10")

    cand_cond = feat["condition"]
    if cand_cond and cand_cond == target.condition:
        score += 10; reasons.append("condition+10")
    elif not cand_cond:
        reasons.append("condition-unstated")

    # Identity bonus: a TCG productId is authoritative.  For eBay, the card name
    # plus EITHER the set name OR the (set-specific) collector number confirms
    # identity — real titles routinely omit the full set name, so requiring both
    # was throwing away most valid sales.
    if c.source == "tcgplayer" and c.tcg_product_id:
        score += 40; reasons.append("productId+40")
    elif name_ok and (set_ok or number_ok):
        score += 45; reasons.append("verified-id+45")
    else:
        reasons.append("unidentified-penalty")

    score = min(score, 100)
    accepted = score >= MIN_CONFIDENCE

    # Route into the correct bucket.  Raw cards with no stated condition go to
    # the explicit "Unknown Raw" bucket — never silently assumed Near Mint.
    if target.graded:
        bucket_cond = cand_cond or target.grade
    else:
        bucket_cond = cand_cond or UNKNOWN
    bucket_finish = cand_finish or target.finish
    res = CandidateResult(
        c, score, accepted, reasons=reasons,
        reject_reason="" if accepted else f"below confidence ({score}<{MIN_CONFIDENCE})",
        condition=bucket_cond, finish=bucket_finish,
        graded=feat["graded"], grade=feat["grade"],
    )
    return res


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
def _iqr_bounds(prices: list[float], ratio: float = 4.0) -> tuple[float, float]:
    """
    Acceptance band = Tukey IQR fences intersected with a median-relative
    multiplicative band (median/ratio … median·ratio).  IQR alone is too
    permissive for the bimodal price spreads cards have (a real $1800 alt-art
    next to a mispriced $1.25 listing), so the multiplicative guard keeps
    extremes out even when the IQR is wide.
    """
    if not prices:
        return (float("-inf"), float("inf"))
    s = sorted(prices)
    med = statistics.median(s)
    n = len(s)
    if n < 4:
        # Too few points for IQR — keep a gentle multiplicative guard only.
        return (med / (ratio * 2), med * (ratio * 2)) if med > 0 else (
            float("-inf"), float("inf"))
    q1 = statistics.median(s[: n // 2])
    q3 = statistics.median(s[(n + 1) // 2:])
    iqr = q3 - q1
    iqr_lo, iqr_hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    if med > 0:
        return (max(iqr_lo, med / ratio), min(iqr_hi, med * ratio))
    return (iqr_lo, iqr_hi)


def _iqr_filter(prices: list[float]) -> list[float]:
    lo, hi = _iqr_bounds(prices)
    kept = [p for p in prices if lo <= p <= hi]
    return kept or prices


def _date_range(dates: list[str]) -> dict:
    ds = sorted(d for d in dates if d)
    return {"from": ds[0] if ds else None, "to": ds[-1] if ds else None}


def _age_days(date_str: str) -> float:
    """Days since a 'YYYY-MM-DD' sale date; +inf if unparseable."""
    from datetime import date
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_str or "")
    if not m:
        return float("inf")
    try:
        d = date(int(m[1]), int(m[2]), int(m[3]))
        return (date.today() - d).days
    except ValueError:
        return float("inf")


def price_group(results: list[CandidateResult]) -> dict:
    """
    Median pricing for one (condition, finish, graded) bucket.

    Prefers sold comps from the last 90 days; if that leaves fewer than 3, the
    window expands to 180 days (then to all available) so we report *something*
    while still flagging low confidence.
    """
    window = "90d"
    pool = [r for r in results if _age_days(r.candidate.date) <= 90]
    if len(pool) < 3:
        wider = [r for r in results if _age_days(r.candidate.date) <= 180]
        if len(wider) > len(pool):
            pool, window = wider, "180d"
    if len(pool) < 3 and len(results) > len(pool):
        pool, window = results, "all"

    raw_prices = [r.candidate.price for r in pool]
    kept = _iqr_filter(raw_prices)
    kept_results = [r for r in pool if r.candidate.price in kept] or pool
    prices = [r.candidate.price for r in kept_results]
    sample = len(prices)
    src = {"tcgplayer": 0, "ebay": 0}
    for r in kept_results:
        src[r.candidate.source] = src.get(r.candidate.source, 0) + 1
    avg_score = round(sum(r.score for r in kept_results) / sample) if sample else 0
    low_conf = sample < 3
    return {
        "median": round(statistics.median(prices), 2) if prices else None,
        "mean": round(statistics.mean(prices), 2) if prices else None,
        "sample_size": sample,
        "outliers_removed": len(raw_prices) - sample,
        "window": window,
        "date_range": _date_range([r.candidate.date for r in kept_results]),
        "confidence": 0 if low_conf else avg_score,
        "low_confidence": low_conf,
        "sources": src,
        "prices": sorted(prices),
    }


def _bucket_key(r: "CandidateResult") -> str:
    return f"{'graded:' + r.grade if r.graded else r.condition}|{r.finish}"


def evaluate(target: CardTarget, candidates: list[SaleCandidate],
             include_unknown: bool = False) -> dict:
    """
    Validate, score, group and price a set of candidate sales.

    Returns structured output: matched sales, rejected sales (with reason),
    median price per condition/finish (raw and graded kept separate, "Unknown
    Raw" kept separate), sample sizes, confidences, source breakdown.

    ``include_unknown`` — when True and the target is a raw card, Unknown-Raw
    sales are folded into the headline (the user opted into estimates from
    listings with no stated condition).
    """
    results = [score_candidate(target, c) for c in candidates]
    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]

    bucket: dict[str, list[CandidateResult]] = {}
    for r in accepted:
        bucket.setdefault(_bucket_key(r), []).append(r)

    groups: dict[str, dict] = {}
    bucket_bounds: dict[str, tuple] = {}
    for key, rs in bucket.items():
        groups[key] = price_group(rs)
        bucket_bounds[key] = _iqr_bounds([r.candidate.price for r in rs])

    def _is_outlier(r: CandidateResult) -> bool:
        lo, hi = bucket_bounds.get(_bucket_key(r), (float("-inf"), float("inf")))
        return not (lo <= r.candidate.price <= hi)

    # Headline = the bucket matching the target's own condition/finish.
    target_key = (f"graded:{target.grade}|{target.finish}" if target.graded
                  else f"{target.condition}|{target.finish}")
    unknown_key = f"{UNKNOWN}|{target.finish}"

    if include_unknown and not target.graded and unknown_key in bucket:
        merged = bucket.get(target_key, []) + bucket[unknown_key]
        headline = price_group(merged)
        headline["includes_unknown"] = True
    else:
        headline = groups.get(target_key)

    src_total = {"tcgplayer": 0, "ebay": 0}
    for r in accepted:
        src_total[r.candidate.source] = src_total.get(r.candidate.source, 0) + 1

    return {
        "target": asdict(target),
        "headline": headline,                 # price for the requested cond/finish
        "headline_key": target_key,
        "unknown_key": unknown_key,
        "groups": groups,                     # every condition/finish bucket
        "matched": [
            {"price": r.candidate.price, "date": r.candidate.date,
             "source": r.candidate.source, "url": r.candidate.url,
             "score": r.score, "condition": r.condition, "finish": r.finish,
             "title": r.candidate.title, "outlier": _is_outlier(r)}
            for r in accepted
        ],
        "rejected": [
            {"price": r.candidate.price, "title": r.candidate.title,
             "source": r.candidate.source, "reason": r.reject_reason}
            for r in rejected
        ],
        "matched_count": len(accepted),
        "rejected_count": len(rejected),
        "source_breakdown": src_total,
    }
