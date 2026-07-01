"""
Set-name detection for free-text card queries — pure, offline-testable.

Players type a Pokemon plus a set as one string ("Tyranitar Expedition",
"Umbreon Undaunted 2010"). We must peel the set name off and turn it into a
`set.id:` filter; leaving it in the name wildcard returns nothing, and the
old word-by-word fallback then dragged in every printing from every set.

The tricky part is that the name a player types rarely equals the official set
name: "Expedition" vs "Expedition Base Set", "Undaunted" vs "HS—Undaunted". So
we build an alias index (full name + the 'Base Set'/'Set'-stripped remainder +
a curated map of oddballs) and match the LONGEST whole-word phrase, so
'expedition' resolves the set while 'base' never fires inside 'Baltoy'.
"""
from __future__ import annotations

import re

# Curated short/common names → pokemontcg.io set id, for cases a player's name
# isn't a substring of the official one (em dashes, dropped "Base Set", etc.).
SET_ALIASES = {
    "expedition": "ecard1", "aquapolis": "ecard2", "skyridge": "ecard3",
    "undaunted": "hgss3", "unleashed": "hgss2", "triumphant": "hgss4",
    "heartgold soulsilver": "hgss1", "heartgold": "hgss1", "soulsilver": "hgss1",
    "call of legends": "col1",
    "base set": "base1", "base": "base1", "base set 2": "base4",
    "jungle": "base2", "fossil": "base3", "team rocket": "base5",
    "gym heroes": "gym1", "gym challenge": "gym2",
    "neo genesis": "neo1", "neo discovery": "neo2",
    "neo revelation": "neo3", "neo destiny": "neo4",
    "legendary collection": "base6",
    "evolutions": "xy12", "generations": "g1",
    "surging sparks": "sv8", "scarlet violet": "sv1", "paradox rift": "sv4",
    "obsidian flames": "sv3", "paldea evolved": "sv2", "temporal forces": "sv5",
    "twilight masquerade": "sv6", "stellar crown": "sv7",
    "prismatic evolutions": "sv8pt5",
}

# Fragments to drop from the leftover after a set name is stripped out.
SET_STOPWORDS = {"set", "the", "of", "and"}

# Minimum alias length — shorter phrases match too eagerly.
_MIN_ALIAS = 4


def norm_phrase(text: str) -> str:
    """Lowercase and collapse punctuation to single spaces so 'HS—Undaunted'
    and 'HeartGold & SoulSilver' compare cleanly against typed queries."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def generate_set_aliases(sets: list, curated: dict | None = None) -> dict:
    """{alias_phrase: set_id} from each set's name plus a curated map.

    Pure (no network). For every set we register the full normalized name and,
    when it ends in 'Base Set'/'Set', the leading remainder
    ('Expedition Base Set' → 'expedition') — the part players actually type.
    Curated aliases win ties; otherwise a longer alias beats a shorter one so
    the match stays specific ('base set 2' over 'base set').
    """
    if curated is None:
        curated = SET_ALIASES
    weighted: dict[str, tuple] = {}  # alias -> (set_id, weight)

    def add(alias: str, sid: str, weight: int):
        alias = alias.strip()
        if len(alias) < _MIN_ALIAS or not sid:
            return
        cur = weighted.get(alias)
        if cur is None or weight > cur[1]:
            weighted[alias] = (sid, weight)

    for s in sets or []:
        sid = s.get("id")
        norm = norm_phrase(s.get("name", ""))
        if not norm:
            continue
        add(norm, sid, len(norm))
        stripped = re.sub(r"\s+", " ", re.sub(r"\b(?:base )?set\b", "", norm)).strip()
        if stripped and stripped != norm:
            add(stripped, sid, len(stripped))
    for alias, sid in curated.items():
        add(norm_phrase(alias), sid, 10_000)  # curated always wins
    return {a: v[0] for a, v in weighted.items()}


def match_set_in_query(raw_q: str, idx: dict):
    """
    Find a set mentioned in the query and split it off. Matches whole-word
    phrases only (so 'base' can't fire inside 'Baltoy'); longest phrase wins
    ('Call of Legends' beats 'Legends'). Returns (set_id, leftover_query), or
    (None, raw_q) when nothing matches. ``idx`` is a {alias: set_id} map.
    """
    low = norm_phrase(raw_q)
    best = None  # (alias, set_id)
    for alias, sid in (idx or {}).items():
        if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", low):
            if best is None or len(alias) > len(best[0]):
                best = (alias, sid)
    if not best:
        return None, raw_q
    leftover = re.sub(r"(?<!\w)" + re.escape(best[0]) + r"(?!\w)", " ", low)
    leftover = [w for w in leftover.split() if w not in SET_STOPWORDS]
    return best[1], " ".join(leftover).strip()
