import csv
import io
import json
import re
import time
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from datetime import timedelta
from flask import Flask, jsonify, render_template, request, redirect, url_for, session
import os

import collection as col_db
import auth
from set_matcher import SET_ALIASES, generate_set_aliases, match_set_in_query
from csv_import import parse_import_csv

load_dotenv()

app = Flask(__name__)
# Session signing key — MUST come from the environment in production.  A random
# dev fallback keeps localhost working but logs everyone out on restart.
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32).hex()
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure cookies when served over HTTPS (set APP_ENV=production behind TLS).
    SESSION_COOKIE_SECURE=os.getenv("APP_ENV", "development") == "production",
)
col_db.init_db()
auth.init_db()

EBAY_APP_ID = os.getenv("EBAY_APP_ID", "")
POKEMON_TCG_API_KEY = os.getenv("POKEMON_TCG_API_KEY", "")
PSA_EMAIL = os.getenv("PSA_EMAIL", "")
PSA_PASSWORD = os.getenv("PSA_PASSWORD", "")

# Simple in-memory cache {key: (timestamp, data)}
_cache: dict = {}
CACHE_TTL = 300  # 5 minutes
PSA_CACHE_TTL = 3600  # PSA scrapes are slow — cache for 1 hour

# Tracks in-progress PSA scrape jobs: {job_id: {"status": ..., "result": ...}}
_psa_jobs: dict = {}
_psa_jobs_lock = threading.Lock()

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# Playwright-backed market scraper (eBay + PSA).  Plain requests get 403 /
# Cloudflare-blocked, so a persistent headless Chrome does the fetching.
# ---------------------------------------------------------------------------
def _scraper():
    """Lazily build the shared MarketScraper singleton."""
    from market_scraper import get_scraper
    return get_scraper(PSA_EMAIL, PSA_PASSWORD)


def _detect_ebay_condition(title: str) -> str:
    """Extract condition/grade from an eBay listing title."""
    t = title.upper()
    # Graded — check grader + grade number
    m = re.search(r'\bPSA\s*-?\s*(10|9\.5|9|8\.5|8|7|6|5|4|3|2|1)\b', t)
    if m:
        return f"PSA-{m.group(1)}"
    m = re.search(r'\bBGS\s*-?\s*(10|9\.5|9|8\.5|8)\b', t)
    if m:
        return f"BGS-{m.group(1)}"
    m = re.search(r'\bCGC\s*-?\s*(10|9\.5|9|8\.5|8)\b', t)
    if m:
        return f"CGC-{m.group(1)}"
    # Raw conditions — long phrases first
    if re.search(r'\bNEAR[\s\-]?MINT\b|\bNM[-/]?MT\b', t):      return "NM"
    if re.search(r'\bLIGHTLY[\s\-]?PLAY', t):                     return "LP"
    if re.search(r'\bMODERATE\b|\bMODERATELY[\s\-]?PLAY', t):   return "MP"
    if re.search(r'\bHEAVILY[\s\-]?PLAY', t):                     return "HP"
    if re.search(r'\bDAMAGED\b|\bDMG\b|\bPOOR\b', t):             return "DMG"
    # Short codes — only match as isolated tokens
    if re.search(r'(?<![A-Z\d])\bNM\b(?![A-Z\d])', t):  return "NM"
    if re.search(r'(?<![A-Z\d])\bLP\b(?![A-Z\d])', t):  return "LP"
    if re.search(r'(?<![A-Z\d])\bMP\b(?![A-Z\d])', t):  return "MP"
    if re.search(r'(?<![A-Z\d])\bHP\b(?![A-Z\d])', t):  return "HP"
    if re.search(r'\bMINT\b|\bEXCELLENT\b', t):          return "NM"
    return ""


def _scrape_psa_sales(card_name: str, number: str = "", set_name: str = "") -> dict:
    """
    Fetch recent PSA auction results for a card.
    Tries:
      1. PSA spec search API → spec detail page for recent sales
      2. Returns {grades: {10: [...], 9: [...], ...}, smr: {10: price, ...}}
    """
    result = {"grades": {}, "smr": {}, "specId": None, "error": None}
    try:
        # Search PSA catalog for the card
        search_q = " ".join(filter(None, [card_name, number])).strip()
        sr = requests.get(
            "https://www.psacard.com/smrpriceguide/GetItemsBySetId",
            params={"q": search_q},
            headers=BROWSER_HEADERS, timeout=10,
        )
        # Also try the general search endpoint
        sr2 = requests.get(
            "https://www.psacard.com/pop/search",
            params={"q": search_q, "category": "13"},  # 13 = Pokemon
            headers=BROWSER_HEADERS, timeout=10,
        )
    except Exception:
        pass

    try:
        # Direct approach: scrape the spec page for recent auction prices
        # First find the spec ID via PSA's search JSON
        search_resp = requests.get(
            "https://www.psacard.com/smrpriceguide/GetItemsBySpecID",
            params={"specID": "", "name": card_name, "number": number},
            headers={**BROWSER_HEADERS, "X-Requested-With": "XMLHttpRequest",
                     "Referer": "https://www.psacard.com/smrpriceguide/"},
            timeout=10,
        )
        if search_resp.ok and search_resp.text.strip().startswith("{"):
            data = search_resp.json()
            # Extract SMR prices by grade if available
            for grade_key, grade_data in (data.get("grades") or {}).items():
                if grade_data:
                    result["smr"][grade_key] = grade_data
    except Exception:
        pass

    try:
        # Try PSA's auctionprices endpoint
        search_term = quote(f"Pokemon {card_name} {number}".strip())
        auction_r = requests.get(
            f"https://www.psacard.com/auctionprices/cardinformation/{search_term}/",
            headers=BROWSER_HEADERS, timeout=12,
        )
        if auction_r.ok:
            html = auction_r.text
            # Extract JSON data embedded in the page (Next.js __NEXT_DATA__ or window.__STATE__)
            nd_m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if nd_m:
                try:
                    nd = json.loads(nd_m.group(1))
                    page_props = (nd.get("props") or {}).get("pageProps") or {}
                    items = page_props.get("auctionResults") or page_props.get("items") or []
                    for item in items[:50]:
                        grade = str(item.get("grade") or item.get("Grade") or "")
                        price = item.get("salePrice") or item.get("SalePrice") or item.get("price") or 0
                        date  = item.get("dateSold") or item.get("SaleDate") or ""
                        auction = item.get("auctionHouse") or item.get("AuctionHouse") or ""
                        url = item.get("url") or item.get("auctionUrl") or ""
                        if grade and price:
                            if grade not in result["grades"]:
                                result["grades"][grade] = []
                            result["grades"][grade].append({
                                "price": float(price), "date": str(date)[:10],
                                "auction": str(auction), "url": str(url),
                            })
                except Exception:
                    pass
    except Exception:
        pass

    return result


_MONTH_ABBR = {
    'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
    'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
}

def _parse_ebay_date(block: str) -> str:
    """Try multiple patterns; return YYYY-MM-DD or empty string."""
    # ISO: 2024-06-14
    dm = re.search(r'(\d{4}-\d{2}-\d{2})', block)
    if dm:
        return dm.group(1)
    # "Jun 14, 2024" or "June 14, 2024"
    dm = re.search(
        r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*'
        r'\s+(\d{1,2}),?\s*(\d{4})\b', block, re.IGNORECASE)
    if dm:
        mo = _MONTH_ABBR.get(dm.group(1).lower()[:3], 0)
        if mo:
            return f"{dm.group(3)}-{mo:02d}-{int(dm.group(2)):02d}"
    # US: 6/14/24 or 06/14/2024
    dm = re.search(r'(?:Sold\s+)?(\d{1,2})/(\d{1,2})/(\d{2,4})\b', block)
    if dm:
        y = int(dm.group(3)); y = y + 2000 if y < 100 else y
        return f"{y:04d}-{int(dm.group(1)):02d}-{int(dm.group(2)):02d}"
    return ""


def _scrape_ebay_sold(keywords: str, max_items: int = 40) -> list[dict]:
    """
    Scrape eBay completed/sold listings via the persistent Playwright session.
    Plain requests are 403-blocked; the headless Chrome session gets through.
    Returns enriched dicts: {price, url, title, date, endTime, condition,
    ebayCondition, graded, foil, source}.
    """
    try:
        return _scraper().ebay_sold(keywords, max_items=max_items)
    except Exception as e:
        print(f"[ebay] scrape failed: {e}")
        return []


def _scrape_ebay_sold_detailed(keywords: str, max_items: int = 40,
                               max_details: int = 18) -> list[dict]:
    """
    Like _scrape_ebay_sold but ALSO opens each recent sold item's detail page to
    read eBay's official condition field + item specifics (Set, Card Number,
    Language, Finish, Graded …) — the authoritative source for raw cards.
    """
    try:
        return _scraper().ebay_sold_detailed(keywords, max_items=max_items,
                                             max_details=max_details)
    except Exception as e:
        print(f"[ebay] detailed scrape failed: {e}")
        return []


def _number_in_title(number: str, title: str) -> bool:
    """True if the card number appears in the eBay title (e.g. '215' or '215/203')."""
    if not number:
        return True
    num = number.lstrip("0") or number
    t = title or ""
    # Match '215', '215/203', '#215', '215/' — bounded so '4' won't hit '2024'.
    return bool(re.search(rf'(?<!\d){re.escape(num)}\s*/\s*\d+', t) or
                re.search(rf'#\s*0*{re.escape(num)}(?!\d)', t) or
                re.search(rf'(?<![\w/]){re.escape(num)}(?![\w])', t))


def _drop_price_outliers(sales: list[dict], ratio: float = 4.0) -> list[dict]:
    """
    Remove price outliers with a multiplicative band around the median
    (robust to the wide-but-legitimate spreads card prices have).  A sale is
    kept when median/ratio ≤ price ≤ median·ratio, which kills obvious wrong
    cards / lots / accessories ($1, $5000) without trimming real variation.
    Keeps everything when there are too few points to judge.
    """
    prices = sorted(s["price"] for s in sales)
    n = len(prices)
    if n < 6:
        return sales
    median = prices[n // 2]
    if median <= 0:
        return sales
    lo, hi = median / ratio, median * ratio
    return [s for s in sales if lo <= s["price"] <= hi]


def _foil_ok(sale_foil: str, want_foil: str) -> bool:
    if not want_foil:
        return True
    if want_foil in ("holofoil", "1stEditionHolofoil", "unlimitedHolofoil"):
        return sale_foil != "reverseHolofoil"      # holo: anything but reverse
    if want_foil == "reverseHolofoil":
        return sale_foil == "reverseHolofoil"       # reverse: must say reverse
    if want_foil in ("normal", "1stEditionNormal"):
        return sale_foil not in ("holofoil", "reverseHolofoil")
    return True


def _filter_ebay_sales(sales: list[dict], number: str = "", foil: str = "",
                       want_graded: bool = False, min_results: int = 4) -> list[dict]:
    """
    Clean a raw eBay sales list for display:
      • keep only ungraded (or only graded) listings,
      • require the card number in the title (kills wrong-card outliers),
      • match the requested foil/printing when one is given,
      • strip price outliers.

    To avoid empty charts, the number/foil constraints are relaxed step by step
    if strict filtering leaves fewer than ``min_results`` sales — the eBay query
    itself already constrains relevance, so this recovers real data for cards
    whose titles omit the number or printing.
    """
    graded_ok = [s for s in sales if bool(s.get("graded")) == want_graded]

    strict = [s for s in graded_ok
              if _number_in_title(number, s.get("title", ""))
              and _foil_ok(s.get("foil") or "", foil)]
    if len(strict) >= min_results or (not number and not foil):
        return _drop_price_outliers(strict)

    # Relax foil first (titles often omit "holo"), keep number match.
    relaxed_foil = [s for s in graded_ok
                    if _number_in_title(number, s.get("title", ""))]
    if len(relaxed_foil) >= min_results:
        return _drop_price_outliers(relaxed_foil)

    # Relax the number requirement too — rely on the eBay query relevance +
    # outlier filter to keep things sane.
    if len(graded_ok) > len(relaxed_foil):
        return _drop_price_outliers(graded_ok)
    return _drop_price_outliers(relaxed_foil or strict)


def _tcgplayer_image(product_id) -> str:
    return f"https://tcgplayer-cdn.tcgplayer.com/product/{product_id}_in_1000x1000.jpg"


def search_tcgplayer_jp(query: str = "", limit: int = 24,
                        set_name: str = "") -> list[dict]:
    """
    Search/browse TCGplayer's Japanese product line.  Japanese sets are their
    OWN sets (e.g. "Terastal Festival ex"), distinct from English — so JP cards
    are a separate, browsable database.  Returns market-card-shaped dicts
    (id 'tcgjp-{pid}', productId carried so pricing works directly).
    Pass `set_name` to browse a whole JP set; `query` for text search.
    """
    cache_key = f"jp_{query.lower()}_{set_name.lower()}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached[:limit]
    out = []
    try:
        term = {"productLineName": ["pokemon-japan"],
                "productTypeName": ["Cards"]}   # singles only (sealed handled elsewhere)
        if set_name:
            term["setName"] = [set_name]
        body = {"algorithm": "", "from": 0, "size": min(max(limit, 12), 50),
                "filters": {"term": term, "range": {}, "match": {}},
                "context": {"cart": {}, "shippingCountry": "US"}, "sort": {}}
        r = requests.post(
            "https://mp-search-api.tcgplayer.com/v1/search/request?q="
            + quote(query) + "&isList=false",
            json=body, headers=dict(_TCG_H, **{"Content-Type": "application/json"}),
            timeout=12)
        if r.ok:
            for x in (r.json().get("results") or [{}])[0].get("results", []):
                pid = x.get("productId")
                if not pid:
                    continue
                name = x.get("productName", "")
                m = re.search(r"-\s*([\dA-Za-z]+/[\dA-Za-z]+)\s*$", name)
                number = m.group(1) if m else ""
                clean_name = re.sub(r"\s*-\s*[\dA-Za-z]+/[\dA-Za-z]+\s*$", "", name)
                out.append({
                    "id": f"tcgjp-{int(pid)}",
                    "tcgProductId": str(int(pid)),
                    "name": clean_name,
                    "number": number,
                    "rarity": x.get("rarityName", ""),
                    "setName": x.get("setName", ""),
                    "setId": "",
                    "image": _tcgplayer_image(int(pid)),
                    "largeImage": _tcgplayer_image(int(pid)),
                    "releaseDate": "",
                    "language": "JP",
                    "tcgHolo": (x.get("marketPrice") if x.get("marketPrice") else None),
                })
    except Exception:
        pass
    cache_set(cache_key, out)
    return out[:limit]


def get_jp_sets() -> list[dict]:
    """Japanese set list (TCGplayer pokemon-japan), separate from English sets."""
    cached = cache_get("jp_sets")
    if cached:
        return cached
    out = []
    try:
        body = {"algorithm": "", "from": 0, "size": 1,
                "filters": {"term": {"productLineName": ["pokemon-japan"]},
                            "range": {}, "match": {}},
                "context": {"cart": {}, "shippingCountry": "US"}, "sort": {},
                "aggregations": ["setName"]}
        r = requests.post(
            "https://mp-search-api.tcgplayer.com/v1/search/request?q=&isList=false",
            json=body, headers=dict(_TCG_H, **{"Content-Type": "application/json"}),
            timeout=12)
        agg = (r.json().get("results") or [{}])[0].get("aggregations", {})
        for s in agg.get("setName", []):
            name = s.get("value")
            if name and name != "Miscellaneous Cards & Products":
                out.append({"name": name, "count": int(s.get("count") or 0)})
    except Exception:
        pass
    # JP release dates aren't reliably exposed; surface biggest/most-recent sets
    # first by product count so the dropdown is usable.
    out.sort(key=lambda s: -s["count"])
    cache_set("jp_sets", out)
    return out


# ---------------------------------------------------------------------------
# Sealed products (TCGplayer "Sealed Products" catalog) — browse by set like
# singles, priced from TCGplayer market + eBay sold comps.
# ---------------------------------------------------------------------------
def _sealed_type(name: str) -> str:
    n = (name or "").lower()
    if "booster box" in n and "case" in n: return "Booster Box Case"
    if "booster box" in n: return "Booster Box"
    if "elite trainer" in n or "etb" in n: return "Elite Trainer Box"
    if "build & battle" in n or "build and battle" in n: return "Build & Battle"
    if "sleeved" in n and "pack" in n: return "Sleeved Pack"
    if "booster pack" in n or n.endswith(" pack"): return "Booster Pack"
    if "blister" in n: return "Blister"
    if "tin" in n: return "Tin"
    if "collection" in n or "box" in n: return "Collection/Box"
    if "bundle" in n: return "Bundle"
    if "case" in n: return "Case"
    return "Other"


_SEALED_PREFIX_RE = re.compile(r"^[A-Za-z]+\d*\s*[:\-]\s*")


def _sealed_year_map() -> dict:
    """{normalized-set-name: release-year} from the pokemontcg.io set list."""
    cached = cache_get("sealed_year_map")
    if cached:
        return cached
    m = {}
    for s in _all_sets():
        rd = (s.get("releaseDate") or "")[:4]
        nm = (s.get("name") or "").strip().lower()
        if nm:
            m[nm] = int(rd) if rd.isdigit() else 0
    cache_set("sealed_year_map", m)
    return m


def _sealed_set_year(set_name: str) -> int:
    """Release year for a TCGplayer set name (strips 'SWSH07:' / 'SM -' prefixes)."""
    base = _SEALED_PREFIX_RE.sub("", set_name or "").strip().lower()
    return _sealed_year_map().get(base, 0)


def _shape_sealed(x: dict) -> dict:
    pid = x.get("productId")
    name = x.get("productName", "")
    # Older/low-volume products have no recent market price → fall back to the
    # current TCGplayer listed value (median, else lowest listing).
    mkt    = x.get("marketPrice")
    listed = x.get("medianPrice") or x.get("lowestPrice")
    price  = mkt if mkt else listed
    set_name = x.get("setName", "")
    return {
        "id": f"sealed-{int(pid)}" if pid else "",
        "productId": str(int(pid)) if pid else "",
        "name": name,
        "type": _sealed_type(name),
        "setName": set_name,
        "setId": str(x.get("setId") or ""),
        "year": _sealed_set_year(set_name),
        "marketPrice": price,
        "priceType": "market" if mkt else ("listed" if listed else None),
        "listings": int(x.get("totalListings") or 0),
        "image": _tcgplayer_image(int(pid)) if pid else "",
    }


def _tcg_sealed_request(filters: dict, q: str = "", size: int = 50,
                        aggregations: list = None) -> dict:
    body = {"algorithm": "", "from": 0, "size": size,
            "filters": {"term": filters, "range": {}, "match": {}},
            "context": {"cart": {}, "shippingCountry": "US"}, "sort": {}}
    if aggregations:
        body["aggregations"] = aggregations
    r = requests.post(
        "https://mp-search-api.tcgplayer.com/v1/search/request?q="
        + quote(q) + "&isList=false",
        json=body, headers=dict(_TCG_H, **{"Content-Type": "application/json"}),
        timeout=15)
    return r.json() if r.ok else {}


def get_sealed_sets() -> list[dict]:
    """All TCGplayer Pokemon sets that have sealed products (cached)."""
    cached = cache_get("sealed_sets")
    if cached:
        return cached
    out = []
    try:
        j = _tcg_sealed_request(
            {"productLineName": ["pokemon"], "productTypeName": ["Sealed Products"]},
            size=1, aggregations=["setName"])
        agg = (j.get("results") or [{}])[0].get("aggregations", {})
        for s in agg.get("setName", []):
            name = s.get("value")
            if name and name != "Miscellaneous Cards & Products":
                out.append({"name": name, "count": int(s.get("count") or 0),
                            "year": _sealed_set_year(name)})
    except Exception:
        pass
    # Newest sets first (like the card set browser's -releaseDate ordering).
    out.sort(key=lambda s: (-s["year"], s["name"]))
    cache_set("sealed_sets", out)
    return out


def _sealed_current_price(product_id: str):
    """(price, type) for a sealed productId — market price, else listed value.

    Works for both EN (pokemon) and JP (pokemon-japan) product lines.
    """
    if not product_id:
        return None, None
    cache_key = f"sealed_cur_{product_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    price, ptype = None, None
    try:
        j = _tcg_sealed_request(
            {"productLineName": ["pokemon", "pokemon-japan"],
             "productId": [str(product_id)]}, size=1)
        res = (j.get("results") or [{}])[0].get("results", [])
        if res:
            x = res[0]
            mkt    = x.get("marketPrice")
            listed = x.get("medianPrice") or x.get("lowestPrice")
            price  = mkt if mkt else listed
            ptype  = "market" if mkt else ("listed" if listed else None)
    except Exception:
        pass
    cache_set(cache_key, (price, ptype))
    return price, ptype


# ---------------------------------------------------------------------------
# TCGplayer product-id resolution (cached, with a search fallback).
# Better resolution = better recent-sales coverage.
# ---------------------------------------------------------------------------
_TCG_PID_CACHE: dict = {}
_TCG_H = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json",
    "Origin": "https://www.tcgplayer.com",
    "Referer": "https://www.tcgplayer.com/",
}


def _resolve_tcg_product_id(card_id: str = "", card_name: str = "",
                            set_name: str = "", number: str = "") -> str:
    """
    Resolve a TCGplayer numeric product id for a card.

      1. the exact prices.pokemontcg.io redirect (keyed by card_id → the right
         variant), then
      2. a TCGplayer catalog search (name + set + number) scored by name/set
         overlap as a fallback when the redirect has no mapping.

    Results are cached per card_id so a one-off failure doesn't permanently
    starve a card of TCGplayer sales.
    """
    if card_id and card_id in _TCG_PID_CACHE:
        return _TCG_PID_CACHE[card_id]

    # Japanese cards carry their TCGplayer productId directly (id 'tcgjp-{pid}').
    if card_id.startswith("tcgjp-"):
        pid = card_id.split("-", 1)[1]
        _TCG_PID_CACHE[card_id] = pid
        return pid

    pid = ""
    if card_id:
        try:
            redir = requests.get(
                f"https://prices.pokemontcg.io/tcgplayer/{card_id}",
                headers=_TCG_H, allow_redirects=True, timeout=8)
            m = re.search(r"/product/(\d+)", redir.url)
            if m:
                pid = m.group(1)
        except Exception:
            pass

    if not pid and card_name:
        try:
            q = " ".join(filter(None, [card_name, set_name, number]))
            body = {
                "algorithm": "", "from": 0, "size": 10,
                "filters": {"term": {"productLineName": ["pokemon"]},
                            "range": {}, "match": {}},
                "context": {"cart": {}, "shippingCountry": "US"}, "sort": {},
            }
            r = requests.post(
                "https://mp-search-api.tcgplayer.com/v1/search/request?q="
                + quote(q) + "&isList=false",
                json=body, headers=dict(_TCG_H, **{"Content-Type": "application/json"}),
                timeout=12)
            if r.ok:
                hits = (r.json().get("results") or [{}])[0].get("results", [])
                name_tokens = [t for t in card_name.lower().split() if len(t) > 1]
                best, best_score = None, 0
                for x in hits:
                    pn = (x.get("productName") or "").lower()
                    sn = (x.get("setName") or "").lower()
                    score = sum(1 for t in name_tokens if t in pn)
                    if set_name and set_name.lower() in sn:
                        score += 3
                    if number and re.search(rf"\b0*{re.escape(number)}\b", pn + " " + sn):
                        score += 2
                    if score > best_score:
                        best, best_score = x, score
                if best and best_score >= 2:
                    pid = str(int(best["productId"]))
        except Exception:
            pass

    if card_id and pid:
        _TCG_PID_CACHE[card_id] = pid
    return pid


def cache_get(key):
    entry = _cache.get(key)
    if entry:
        ttl = PSA_CACHE_TTL if key.startswith(("psa_", "psa_sold_")) else CACHE_TTL
        if time.time() - entry[0] < ttl:
            return entry[1]
    return None


def cache_set(key, data):
    _cache[key] = (time.time(), data)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Authentication — accounts, sessions, profile
# ---------------------------------------------------------------------------
@app.route("/login")
def login_page():
    if auth.current_user_id():
        return redirect(url_for("portfolio_page"))
    return render_template("login.html")


@app.route("/api/auth/signup", methods=["POST"])
def api_signup():
    data = request.get_json(silent=True) or {}
    try:
        first_user = auth.user_count() == 0
        uid = auth.create_user(data.get("email", ""), data.get("password", ""),
                               data.get("display_name", ""))
        # The very first account adopts any pre-auth collection data.
        if first_user:
            col_db.claim_orphans(uid)
        auth.login_session({"id": uid})
        return jsonify({"ok": True, "user": auth.get_user(uid)}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    user = auth.verify_user(data.get("email", ""), data.get("password", ""))
    if not user:
        return jsonify({"error": "Incorrect email or password."}), 401
    auth.login_session(user)
    return jsonify({"ok": True, "user": auth.get_user(user["id"])})


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    auth.logout_session()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def api_me():
    user = auth.get_user(auth.current_user_id())
    return jsonify({"user": user})


@app.route("/")
@auth.login_required
def index():
    return render_template("index.html")


def _all_sets() -> list[dict]:
    """Cached full set list (id, name, series, releaseDate, ...)."""
    cached = cache_get("sets")
    if cached:
        return cached
    headers = {"X-Api-Key": POKEMON_TCG_API_KEY} if POKEMON_TCG_API_KEY else {}
    try:
        resp = requests.get(
            "https://api.pokemontcg.io/v2/sets?orderBy=-releaseDate&pageSize=250",
            headers=headers, timeout=10,
        )
        resp.raise_for_status()
        sets = [
            {
                "id": s["id"],
                "name": s["name"],
                "series": s["series"],
                "releaseDate": s.get("releaseDate", ""),
                "total": s.get("total", 0),
                "logo": s.get("images", {}).get("logo", ""),
            }
            for s in resp.json().get("data", [])
        ]
        cache_set("sets", sets)
        return sets
    except Exception:
        return []


def _set_alias_index() -> dict:
    """Cached {alias: set_id} index built from the live set list + curated map."""
    cached = cache_get("set_alias_index")
    if cached:
        return cached
    idx = generate_set_aliases(_all_sets(), SET_ALIASES)
    cache_set("set_alias_index", idx)
    return idx


def _match_set_in_query(raw_q: str):
    """Find a set name embedded in the query → (set_id, leftover) or (None, q).

    Thin wrapper over the pure ``set_matcher`` logic with the live alias index.
    """
    return match_set_in_query(raw_q, _set_alias_index())


@app.route("/api/sets")
def get_sets():
    sets = _all_sets()
    if sets:
        return jsonify(sets)
    return jsonify({"error": "could not load sets"}), 500


@app.route("/api/cards/<set_id>")
def get_cards(set_id):
    cache_key = f"cards_{set_id}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    headers = {"X-Api-Key": POKEMON_TCG_API_KEY} if POKEMON_TCG_API_KEY else {}
    try:
        all_cards = []
        page = 1
        while True:
            resp = requests.get(
                "https://api.pokemontcg.io/v2/cards",
                params={"q": f"set.id:{set_id}", "pageSize": 250,
                        "orderBy": "number", "page": page},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            batch = body.get("data", [])
            all_cards.extend(batch)
            if len(all_cards) >= body.get("totalCount", 0) or not batch:
                break
            page += 1
        cards = [
            {
                "id": c["id"],
                "name": c["name"],
                "number": c["number"],
                "rarity": c.get("rarity", "Unknown"),
                "smallImage": c.get("images", {}).get("small", ""),
                "setName": c["set"]["name"],
                "setId": c["set"]["id"],
                "tcgPrices": list(c.get("tcgplayer", {}).get("prices", {}).keys()),
            }
            for c in all_cards
        ]
        cache_set(cache_key, cards)
        return jsonify(cards)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/card/<path:card_id>")
def get_card(card_id):
    """Fetch a single card (shaped like market search results) by pokemontcg.io id."""
    cache_key = f"card_{card_id}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)
    headers = {"X-Api-Key": POKEMON_TCG_API_KEY} if POKEMON_TCG_API_KEY else {}
    try:
        r = requests.get(f"https://api.pokemontcg.io/v2/cards/{card_id}",
                         headers=headers, timeout=10)
        if not r.ok:
            return jsonify({"error": "not found"}), 404
        c = r.json().get("data")
        if not c:
            return jsonify({"error": "not found"}), 404
        shaped = _shape_market_card(c)
        cache_set(cache_key, shaped)
        return jsonify(shaped)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pop/psa/status")
def psa_pop_status():
    """Check whether PSA credentials are configured."""
    return jsonify({
        "configured": bool(PSA_EMAIL and PSA_PASSWORD),
        "email": PSA_EMAIL if PSA_EMAIL else None,
    })


@app.route("/api/pop/psa/scrape", methods=["POST"])
def start_psa_scrape():
    """
    Start a background PSA pop scrape job.
    Body: {"set_name": "Surging Sparks"}
    Returns: {"job_id": "..."} — poll /api/pop/psa/job/<job_id> for results.
    """
    if not PSA_EMAIL or not PSA_PASSWORD:
        return jsonify({"error": "PSA_EMAIL and PSA_PASSWORD not set in .env"}), 503

    data = request.get_json(silent=True) or {}
    set_name = data.get("set_name", "").strip()
    if not set_name:
        return jsonify({"error": "set_name required"}), 400

    cache_key = f"psa_pop_{set_name.lower()}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify({"status": "done", "cached": True, "result": cached})

    job_id = f"{set_name.lower().replace(' ', '_')}_{int(time.time())}"

    with _psa_jobs_lock:
        _psa_jobs[job_id] = {
            "status": "running", "result": None, "error": None,
            "phase": "login", "progress": 0, "message": "Starting…",
        }

    def run_scrape():
        def progress_cb(info):
            with _psa_jobs_lock:
                _psa_jobs[job_id].update(info)

        try:
            from psa_scraper import scrape_psa_pop
            result = scrape_psa_pop(PSA_EMAIL, PSA_PASSWORD, set_name,
                                    progress_cb=progress_cb)
            cache_set(cache_key, result)
            with _psa_jobs_lock:
                _psa_jobs[job_id]["status"] = "done"
                _psa_jobs[job_id]["progress"] = 100
                _psa_jobs[job_id]["result"] = result
        except Exception as e:
            with _psa_jobs_lock:
                _psa_jobs[job_id]["status"] = "error"
                _psa_jobs[job_id]["error"] = str(e)

    thread = threading.Thread(target=run_scrape, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "running"})


@app.route("/api/pop/psa/job/<job_id>")
def psa_scrape_job(job_id):
    """Poll a PSA scrape job for completion."""
    with _psa_jobs_lock:
        job = _psa_jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


@app.route("/api/pop/gemrate")
def get_gemrate_pop():
    """
    Attempt to scrape PSA population data from GemRate.
    GemRate is a Next.js SPA; if they SSR the page we can extract __NEXT_DATA__.
    If they CSR, we get an empty shell and return a clear error so the UI can
    prompt the user to upload a CSV instead.
    """
    set_name = request.args.get("set_name", "").strip()
    year = request.args.get("year", "").strip()

    if not set_name:
        return jsonify({"error": "set_name required"}), 400

    cache_key = f"gemrate_{set_name}_{year}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    url = f"https://www.gemrate.com/item-details?grader=psa&category=tcg-cards&set_name={quote(set_name)}"
    if year:
        url += f"&year={year}"

    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)

        if resp.status_code == 403:
            return jsonify({
                "error": "blocked",
                "message": "GemRate returned 403 — the site requires a real browser session. Use CSV upload instead.",
            }), 403

        if resp.status_code != 200:
            return jsonify({"error": f"http_{resp.status_code}"}), 502

        # Try to find Next.js embedded server-side data
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            resp.text,
            re.DOTALL,
        )
        if match:
            next_data = json.loads(match.group(1))
            # Dig into pageProps for the actual card rows
            page_props = next_data.get("props", {}).get("pageProps", {})
            items = (
                page_props.get("items")
                or page_props.get("data")
                or page_props.get("cards")
                or []
            )
            if items:
                result = {"source": "gemrate", "items": items}
                cache_set(cache_key, result)
                return jsonify(result)
            # Data is present but structure unexpected — return raw for debugging
            return jsonify({"source": "gemrate_raw", "pageProps": page_props})

        return jsonify({
            "error": "csr_only",
            "message": (
                "GemRate rendered an empty HTML shell — data is loaded by client-side JS "
                "which we can't execute. Upload a PSA pop CSV instead."
            ),
        }), 422

    except requests.Timeout:
        return jsonify({"error": "timeout", "message": "GemRate request timed out."}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ebay/sold")
def get_ebay_sold():
    """Query eBay Finding API for sold PSA 10 listings in the last 30 days."""
    if not EBAY_APP_ID:
        return jsonify({"error": "EBAY_APP_ID not set in .env"}), 503

    card_name = request.args.get("card_name", "").strip()
    card_number = request.args.get("card_number", "").strip()
    set_name = request.args.get("set_name", "").strip()
    grade = request.args.get("grade", "PSA 10").strip()

    if not card_name:
        return jsonify({"error": "card_name required"}), 400

    # Build a tight but not over-specified query
    # Format: "Slakoth 212/191 PSA 10"  or  "Lillie Full Art PSA 10 Ultra Prism"
    parts = [card_name]
    if card_number:
        parts.append(card_number)
    parts.append(grade)
    keywords = " ".join(parts)

    cache_key = f"ebay_{keywords}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    thirty_days_ago = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    params = [
        ("OPERATION-NAME", "findCompletedItems"),
        ("SERVICE-VERSION", "1.0.0"),
        ("SECURITY-APPNAME", EBAY_APP_ID),
        ("RESPONSE-DATA-FORMAT", "JSON"),
        ("keywords", keywords),
        ("categoryId", "2536"),
        ("itemFilter(0).name", "SoldItemsOnly"),
        ("itemFilter(0).value", "true"),
        ("itemFilter(1).name", "EndTimeFrom"),
        ("itemFilter(1).value", thirty_days_ago),
        ("sortOrder", "EndTimeSoonest"),
        ("paginationInput.entriesPerPage", "50"),
        ("outputSelector(0)", "SellingStatus"),
    ]

    try:
        resp = requests.get(
            "https://svcs.ebay.com/services/search/FindingService/v1",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        root = data.get("findCompletedItemsResponse", [{}])[0]
        ack = root.get("ack", ["Failure"])[0]

        if ack not in ("Success", "Warning"):
            err = (
                root.get("errorMessage", [{}])[0]
                .get("error", [{}])[0]
                .get("message", ["eBay API error"])[0]
            )
            return jsonify({"error": "ebay_error", "message": err}), 502

        raw_items = root.get("searchResult", [{}])[0].get("item", [])
        sold = []
        for item in raw_items:
            try:
                status = item.get("sellingStatus", [{}])[0]
                state = status.get("sellingState", [""])[0]
                if "EndedWithSales" not in state:
                    continue
                price_node = status.get("currentPrice", [{}])[0]
                price = float(price_node.get("__value__", "0"))
                sold.append({
                    "title": item.get("title", [""])[0],
                    "price": price,
                    "currency": price_node.get("@currencyId", "USD"),
                    "endTime": item.get("listingInfo", [{}])[0].get("endTime", [""])[0],
                    "url": item.get("viewItemURL", [""])[0],
                })
            except (KeyError, IndexError, ValueError, TypeError):
                continue

        prices = [s["price"] for s in sold if s["price"] > 0]
        result = {
            "keyword": keywords,
            "count": len(sold),
            "avgPrice": round(sum(prices) / len(prices), 2) if prices else 0,
            "minPrice": min(prices) if prices else 0,
            "maxPrice": max(prices) if prices else 0,
            "items": sold[:20],
        }
        cache_set(cache_key, result)
        return jsonify(result)

    except requests.Timeout:
        return jsonify({"error": "timeout", "message": "eBay API timed out."}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pop/upload", methods=["POST"])
def upload_pop_csv():
    """
    Parse a PSA pop report CSV export and return structured data.

    PSA exports typically look like:
      Subject, 1, 1.5, 2, 3, 4, 4.5, 5, 6, 7, 8, 9, 10, Total
      Slakoth #212, 0, 0, 1, 2, 0, 4, 6, 12, 25, 40, 11, 101

    GemRate CSV exports vary; we try common column name patterns.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    content = request.files["file"].read().decode("utf-8-sig")  # strip BOM
    reader = csv.DictReader(io.StringIO(content))
    cards = []

    for row in reader:
        row = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items() if k}

        # Name — PSA uses "subject", some exports use "card" or "name"
        name = (
            row.get("subject")
            or row.get("card name")
            or row.get("card")
            or row.get("name")
            or ""
        )
        if not name:
            continue

        def parse_int(val):
            try:
                return int(str(val).replace(",", "").strip())
            except (ValueError, AttributeError):
                return 0

        grade_10 = parse_int(
            row.get("10") or row.get("psa 10") or row.get("gem mint 10") or row.get("gm 10") or "0"
        )

        # Total: try explicit column first, then sum all numeric grade columns
        total = parse_int(row.get("total") or row.get("total graded") or "0")
        if not total:
            grade_cols = ["1", "1.5", "2", "2.5", "3", "3.5", "4", "4.5",
                          "5", "5.5", "6", "7", "8", "9", "10"]
            total = sum(parse_int(row.get(g, "0")) for g in grade_cols)

        gem_rate = round(grade_10 / total * 100, 1) if total > 0 else 0

        cards.append({
            "name": name,
            "psa10": grade_10,
            "total": total,
            "gemRate": gem_rate,
        })

    return jsonify({"count": len(cards), "cards": cards})


# ---------------------------------------------------------------------------
# Collection page
# ---------------------------------------------------------------------------

@app.route("/collection")
@auth.login_required
def collection_page():
    return render_template("collection.html")


@app.route("/portfolio")
@auth.login_required
def portfolio_page():
    return render_template("portfolio.html")


@app.route("/movers")
@auth.login_required
def movers_page():
    return render_template("movers.html")


@app.route("/sealed")
@auth.login_required
def sealed_page():
    return render_template("sealed.html")


# ---------------------------------------------------------------------------
# Sealed product catalog + pricing
# ---------------------------------------------------------------------------
@app.route("/api/sealed/sets")
def api_sealed_sets():
    return jsonify(get_sealed_sets())


@app.route("/api/sealed/by-set")
def api_sealed_by_set():
    set_name = request.args.get("set_name", "").strip()
    if not set_name:
        return jsonify({"error": "set_name required"}), 400
    cache_key = f"sealed_set_{set_name.lower()}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)
    products = []
    try:
        j = _tcg_sealed_request(
            {"productLineName": ["pokemon"], "productTypeName": ["Sealed Products"],
             "setName": [set_name]}, size=50)   # TCGplayer caps page size at 50
        for x in (j.get("results") or [{}])[0].get("results", []):
            shaped = _shape_sealed(x)
            if shaped["id"]:
                products.append(shaped)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    # Order by type then price desc (boxes/cases first).
    order = ["Booster Box Case", "Case", "Booster Box", "Elite Trainer Box",
             "Collection/Box", "Bundle", "Build & Battle", "Tin", "Blister",
             "Sleeved Pack", "Booster Pack", "Other"]
    products.sort(key=lambda p: (order.index(p["type"]) if p["type"] in order else 99,
                                 -(p["marketPrice"] or 0)))
    result = {"setName": set_name, "products": products, "count": len(products)}
    cache_set(cache_key, result)
    return jsonify(result)


@app.route("/api/sealed/current")
def api_sealed_current():
    """Fast current price for a sealed productId (for collection valuation)."""
    pid = request.args.get("product_id", "").strip()
    price, ptype = _sealed_current_price(pid)
    return jsonify({"productId": pid, "price": price, "priceType": ptype})


@app.route("/api/sealed/search")
def api_sealed_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"products": []})
    cache_key = f"sealed_q_{q.lower()}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)
    products = []
    try:
        j = _tcg_sealed_request(
            {"productLineName": ["pokemon"], "productTypeName": ["Sealed Products"]},
            q=q, size=40)
        for x in (j.get("results") or [{}])[0].get("results", []):
            shaped = _shape_sealed(x)
            if shaped["id"]:
                products.append(shaped)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    # Order by set, newest year first (matching the card search ordering).
    products.sort(key=lambda p: (-p["year"], p["setName"]))
    result = {"products": products, "count": len(products)}
    cache_set(cache_key, result)
    return jsonify(result)


@app.route("/api/sealed/price")
def api_sealed_price():
    """
    Sealed-product pricing: TCGplayer market + history snapshot + eBay sold comps
    (median of recent verified sealed sales).  Old/rare products with no recent
    comps return "no recent comps" rather than a stale guess.
    """
    from tcg_snapshot import fetch_sales_history
    from pricing_engine import CardTarget, SaleCandidate, evaluate
    import statistics as _st

    product_id = request.args.get("product_id", "").strip()
    name       = request.args.get("name", "").strip()
    if not product_id:
        return jsonify({"error": "product_id required"}), 400

    cache_key = f"sealed_price_{product_id}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    result = {"productId": product_id, "name": name,
              "tcgMarket": None, "history": [], "ebay": None}

    # TCGplayer Sales History snapshot (the "Unopened" SKU).
    snap = fetch_sales_history(product_id)
    for sku in snap.get("skus", []):
        if sku.get("marketPrice") and result["tcgMarket"] is None:
            result["tcgMarket"] = sku["marketPrice"]
        for pt in sku.get("points", []):
            if pt.get("marketPrice", 0) > 0:
                result["history"].append({"date": pt["date"], "price": pt["marketPrice"]})
    result["history"].sort(key=lambda h: h["date"])

    # eBay sold comps — sealed only, recent (90d preferred).
    if name:
        kw = name + " sealed -lot -bundle -proxy -empty -opened -resealed"
        raw = _scrape_ebay_sold_detailed(kw, max_items=80, max_details=25)
        cands = []
        for s in raw:
            if not s.get("price"):
                continue
            ship = float(s.get("shipping") or 0)
            cands.append(SaleCandidate(
                price=round(float(s["price"]) + ship, 2),
                title=s.get("title", ""), url=s.get("url", ""),
                date=s.get("date") or s.get("endTime", ""), source="ebay",
                official_condition=s.get("conditionDisplayName", ""),
                item_specifics=s.get("itemSpecifics", {}) or {}, shipping=ship))
        # target is a sealed product (accept sealed, reject singles/graded/lots)
        target = CardTarget(name=name, sealed=True, finish="", language="english")
        analysis = evaluate(target, cands)
        matched = [m for m in analysis["matched"] if not m.get("outlier")]
        prices = [m["price"] for m in matched]
        if len(prices) >= 2:
            result["ebay"] = {
                "median": round(_st.median(prices), 2),
                "sampleSize": len(prices),
                "lowConfidence": len(prices) < 3,
                "sales": [{"price": m["price"], "date": m["date"], "url": m["url"],
                           "title": m["title"]} for m in matched][:30],
                "rejected": analysis["rejected_count"],
            }
        else:
            result["ebay"] = {"median": None, "sampleSize": len(prices),
                              "noRecentComps": True, "sales": [], "rejected": analysis["rejected_count"]}

    cache_set(cache_key, result)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Portfolio — total value, cost basis, P&L, value-over-time
# ---------------------------------------------------------------------------
def _snapshot_condition_value(card: dict):
    """
    TCGplayer market price for the card's EXACT condition × printing, from the
    Sales History snapshot (e.g. an LP card → the LP market, not the NM price).
    Returns None for graded cards or when no matching SKU exists.
    """
    cond = (card.get("condition") or "").upper()
    if cond.startswith("PSA") or not card.get("card_id"):
        return None
    try:
        from tcg_snapshot import fetch_sales_history
        pid = _resolve_tcg_product_id(card.get("card_id", ""), card.get("card_name", ""),
                                      card.get("set_name", ""), card.get("number", ""))
        if not pid:
            return None
        snap = fetch_sales_history(pid)
        from pricing_engine import select_condition_market
        return select_condition_market(
            snap.get("skus", []), cond, card.get("foil_type") or "holofoil")
    except Exception:
        return None


def _card_value(card: dict):
    """Per-unit market value for a collection card, valued at its OWN condition.

    Priority: user custom value → snapshot per-condition market price → latest
    saved daily snapshot (condition avg) → NM market.
    """
    if card.get("custom_market_value"):
        return float(card["custom_market_value"])
    cv = _snapshot_condition_value(card)
    if cv:
        return float(cv)
    hist = col_db.get_price_history(card["id"], days=400)
    for row in reversed(hist):
        v = row.get("tcg_cond_avg") or row.get("tcg_market")
        if v:
            return float(v)
    return None


@app.route("/api/portfolio")
@auth.login_required
def get_portfolio():
    """
    Stock-portfolio-style summary of the collection:
      • current market value, cost basis, unrealized P&L,
      • a value-over-time series (daily, carry-forward per card),
      • per-holding rows.
    """
    uid = auth.current_user_id()
    cards = col_db.get_all_cards(uid)
    snaps = col_db.get_all_snapshots(uid, days=400)

    # snapshots grouped by card and date → latest value per card per date
    by_card: dict[int, dict[str, float]] = {}
    all_dates: set[str] = set()
    for s in snaps:
        v = s.get("tcg_cond_avg") or s.get("tcg_market")
        if v:
            by_card.setdefault(s["card_db_id"], {})[s["date"]] = float(v)
            all_dates.add(s["date"])

    qty = {c["id"]: (c.get("quantity") or 1) for c in cards}

    # value-over-time: for each date, sum carry-forward latest value × qty
    series = []
    for d in sorted(all_dates):
        total = 0.0
        for cid, datemap in by_card.items():
            past = [dt for dt in datemap if dt <= d]
            if past:
                total += datemap[max(past)] * qty.get(cid, 1)
        series.append({"date": d, "value": round(total, 2)})

    # Value each holding concurrently — the per-condition snapshot lookups are
    # I/O-bound, so a large collection doesn't load one card at a time.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as _ex:
        _units = dict(zip((c["id"] for c in cards), _ex.map(_card_value, cards)))

    holdings = []
    cur_total = cost_total = 0.0
    for c in cards:
        unit = _units.get(c["id"])
        q = c.get("quantity") or 1
        paid = c.get("purchase_price")
        mv = (unit or 0) * q
        cost = (paid or 0) * q
        cur_total += mv
        cost_total += cost if paid else 0

        # Per-card daily change: latest vs previous distinct daily snapshot.
        day_chg = day_chg_pct = None
        dm = by_card.get(c["id"], {})
        if not c.get("custom_market_value") and len(dm) >= 2:
            ds = sorted(dm)
            pv, lv = dm[ds[-2]], dm[ds[-1]]
            if pv > 0:
                day_chg = round((lv - pv) * q, 2)
                day_chg_pct = round((lv / pv - 1) * 100, 2)

        # Market value WHEN ADDED = the earliest recorded snapshot for the card.
        # Gain-since-added tracks the market move since you got it (not vs paid).
        added_unit = added_date = gain_add = gain_add_pct = None
        if dm:
            first_date = min(dm)
            added_unit = dm[first_date]
            added_date = first_date
            if unit and added_unit:
                gain_add = round((unit - added_unit) * q, 2)
                gain_add_pct = round((unit / added_unit - 1) * 100, 1) if added_unit else None

        holdings.append({
            "id": c["id"], "card_id": c.get("card_id"), "name": c["card_name"],
            "set_name": c.get("set_name"), "number": c.get("number"),
            "condition": c.get("condition"), "foil_type": c.get("foil_type"),
            "quantity": q, "image_url": c.get("image_url"),
            "unit_value": round(unit, 2) if unit else None,
            "market_value": round(mv, 2) if unit else None,
            "purchase_price": paid,
            "cost_basis": round(cost, 2) if paid else None,
            "pnl": round(mv - cost, 2) if (unit and paid) else None,
            "pnl_pct": round((mv / cost - 1) * 100, 1) if (unit and paid and cost) else None,
            "day_change": day_chg, "day_change_pct": day_chg_pct,
            "added_at": c.get("added_at"),
            "value_at_add": round(added_unit, 2) if added_unit else None,
            "tracked_since": added_date,
            "gain_since_add": gain_add, "gain_since_add_pct": gain_add_pct,
        })
    holdings.sort(key=lambda h: h["market_value"] or 0, reverse=True)

    # Anchor today's series point to the LIVE valuation so the chart endpoint
    # matches the headline Total Value (older points keep their best snapshot).
    from datetime import date as _date
    _today = _date.today().isoformat()
    if series and series[-1]["date"] == _today:
        series[-1]["value"] = round(cur_total, 2)
    elif cur_total > 0:
        series.append({"date": _today, "value": round(cur_total, 2)})

    # Portfolio day change = sum of per-card day changes (apples-to-apples — only
    # cards that have a prior snapshot contribute), which is more reliable than a
    # sparse series delta while daily history is still filling in.
    per_card = [h["day_change"] for h in holdings if h.get("day_change") is not None]
    day_change = day_change_pct = None
    if per_card:
        day_change = round(sum(per_card), 2)
        prev_val = cur_total - day_change
        day_change_pct = round((day_change / prev_val) * 100, 2) if prev_val else None

    return jsonify({
        "currentValue": round(cur_total, 2),
        "costBasis": round(cost_total, 2),
        "unrealizedPnl": round(cur_total - cost_total, 2),
        "unrealizedPnlPct": round((cur_total / cost_total - 1) * 100, 2) if cost_total else None,
        "dayChange": day_change, "dayChangePct": day_change_pct,
        "series": series,
        "holdings": holdings,
        "count": sum(qty.values()),
    })


@app.route("/api/movers")
@auth.login_required
def get_movers():
    """
    24h (latest vs previous snapshot) gainers & losers across tracked cards.

    Scope: cards with daily price-history snapshots (the collection, kept fresh
    by the hourly refresher) — true market-wide movers would need a snapshotted
    card universe.  Raw market value only (tcg_cond_avg / tcg_market).
    """
    uid = auth.current_user_id()
    cards = {c["id"]: c for c in col_db.get_all_cards(uid)}
    snaps = col_db.get_all_snapshots(uid, days=14)
    by_card: dict[int, list] = {}
    for s in snaps:
        v = s.get("tcg_cond_avg") or s.get("tcg_market")
        if v:
            by_card.setdefault(s["card_db_id"], []).append((s["date"], float(v)))

    movers = []
    for cid, rows in by_card.items():
        if cid not in cards or len(rows) < 2:
            continue
        rows.sort()
        (pd, pv), (ld, lv) = rows[-2], rows[-1]
        if pv <= 0:
            continue
        c = cards[cid]
        movers.append({
            "id": cid, "card_id": c.get("card_id"), "name": c["card_name"],
            "set_name": c.get("set_name"), "number": c.get("number"),
            "condition": c.get("condition"), "image_url": c.get("image_url"),
            "prev": round(pv, 2), "current": round(lv, 2),
            "change": round(lv - pv, 2), "changePct": round((lv / pv - 1) * 100, 2),
            "from": pd, "to": ld,
        })
    gainers = sorted([m for m in movers if m["change"] > 0],
                     key=lambda m: m["changePct"], reverse=True)
    losers = sorted([m for m in movers if m["change"] < 0],
                    key=lambda m: m["changePct"])
    return jsonify({"gainers": gainers, "losers": losers,
                    "trackedCount": len(by_card)})


# ---------------------------------------------------------------------------
# Card search (fuzzy multi-strategy)
# ---------------------------------------------------------------------------

TCG_CONDITION_MAP = {
    "NM":  "NearMint",
    "LP":  "LightlyPlayed",
    "MP":  "ModeratelyPlayed",
    "HP":  "HeavilyPlayed",
    "DMG": "Damaged",
}
TCG_FOIL_MAP = {
    "holofoil":        "Holofoil",
    "normal":          "Normal",
    "reverseHolofoil": "ReverseHolofoil",
}
CONDITION_EBAY_TERMS = {
    "NM":  ("NM", "near mint"),
    "LP":  ("LP", "lightly played"),
    "MP":  ("MP", "moderately played"),
    "HP":  ("HP", "heavily played"),
    "DMG": ("DMG", "damaged"),
}


@app.route("/api/jp/sets")
def api_jp_sets():
    """Japanese set list (separate from English) for browsing JP cards by set."""
    return jsonify(get_jp_sets())


@app.route("/api/search/cards")
def search_cards():
    """
    Fuzzy card search using the Pokemon TCG API.
    Params: q (text query), set_id, language, page
    Strategy:
      1. Exact-phrase wildcard in name  → name:*<q>*
      2. If < 5 results, also try each word independently and union
      3. Sort: exact-name matches first, then newest sets
    """
    raw_q   = request.args.get("q", "").strip()
    set_id  = request.args.get("set_id", "").strip()
    lang    = request.args.get("language", "EN").strip().upper()
    page    = max(1, int(request.args.get("page", 1)))

    if not raw_q and not set_id:
        return jsonify({"cards": [], "total": 0})

    # Japanese cards: their own database (TCGplayer pokemon-japan), browsable by
    # JP set.  set_id carries the JP set NAME when browsing a Japanese set.
    if lang == "JP":
        jp_set = request.args.get("set_name", "") or set_id
        jp_cards = search_tcgplayer_jp(raw_q, limit=60, set_name=jp_set)
        return jsonify({"cards": jp_cards, "total": len(jp_cards),
                        "hint": "" if jp_cards else
                        "No Japanese matches. Try a different term, or use 'Add manually'."})

    headers = {"X-Api-Key": POKEMON_TCG_API_KEY} if POKEMON_TCG_API_KEY else {}

    def api_search(q_str: str, page_num: int = 1) -> list[dict]:
        try:
            r = requests.get(
                "https://api.pokemontcg.io/v2/cards",
                params={"q": q_str, "pageSize": 24, "page": page_num,
                        "orderBy": "-set.releaseDate"},
                headers=headers, timeout=10,
            )
            r.raise_for_status()
            return r.json().get("data", [])
        except Exception:
            return []

    # Detect a set name embedded in the query ("Tyranitar Expedition") and turn
    # it into a set filter — otherwise the set words pollute the name wildcard.
    detected_set_id = None
    if not set_id:
        detected_set_id, raw_q = _match_set_in_query(raw_q)

    # Parse number and year tokens out of the raw query so they become filters
    # instead of part of the name search.
    # e.g. "Umbreon 2010" → name:*Umbreon* set.releaseDate:2010*
    #      "Umbreon 86"   → name:*Umbreon* number:86
    #      "Umbreon 86/90 2010" → name:*Umbreon* number:86 set.releaseDate:2010*
    year_m = re.search(r'\b((?:19|20)\d{2})\b', raw_q)
    year   = year_m.group(1) if year_m else None
    q_sans_year = re.sub(r'\b(?:19|20)\d{2}\b', '', raw_q)
    num_m  = re.search(r'\b(\d{1,3})(?:/\d+)?\b', q_sans_year)
    number = num_m.group(1) if num_m else None

    # Name tokens = raw query minus the year/number tokens
    name_tokens = []
    for tok in raw_q.split():
        if year and tok == year:
            continue
        if number and re.match(r'^\d{1,3}(?:/\d+)?$', tok) and tok.split('/')[0] == number:
            continue
        name_tokens.append(tok)
    name_q = ' '.join(name_tokens).strip()

    # Build primary query
    parts = []
    if name_q:
        safe = name_q.replace('"', '').replace(':', '').replace('\\', '')
        parts.append(f'name:*{safe}*')
    if number:
        parts.append(f'number:{number}')
    if set_id:
        parts.append(f'set.id:{set_id}')
    elif detected_set_id:
        parts.append(f'set.id:{detected_set_id}')
    elif year:
        parts.append(f'set.releaseDate:{year}*')

    primary_q = " ".join(parts)
    results = api_search(primary_q, page)

    # Fallback: if very few results, relax number+year and try name-only
    if len(results) < 3 and (number or year) and name_q and not set_id:
        safe_name = name_q.replace('"', '').replace(':', '').replace('\\', '')
        extra = api_search(f'name:*{safe_name}*', 1)
        seen_ids = {c["id"] for c in results}
        for card in extra:
            if card["id"] not in seen_ids:
                results.append(card)
                seen_ids.add(card["id"])

    # Fallback: if still very few results and multi-word name, try word-by-word
    # union — but not when a set is pinned, or it drags in every other set.
    if len(results) < 4 and name_q and " " in name_q and not set_id and not detected_set_id:
        seen_ids: set[str] = {c["id"] for c in results}
        words = [w for w in name_q.split() if len(w) >= 3]
        for word in words[:3]:
            extra = api_search(f"name:*{word}*", 1)
            for card in extra:
                if card["id"] not in seen_ids:
                    results.append(card)
                    seen_ids.add(card["id"])

    # Fuzzy fallback: if still no results, try prefix of each name word
    # This handles typos like "Dragonait" → "Dragon" prefix still matches
    if not results and name_q and not detected_set_id:
        seen_ids = set()
        words = [w for w in name_q.split() if len(w) >= 4]
        for word in words[:4]:
            # Try progressively shorter prefixes of each word
            for prefix_len in range(len(word), max(3, len(word) - 3) - 1, -1):
                prefix = word[:prefix_len]
                extra = api_search(f"name:*{prefix}*", 1)
                if extra:
                    for card in extra:
                        if card["id"] not in seen_ids:
                            results.append(card)
                            seen_ids.add(card["id"])
                    break  # found something, move to next word

    # (Set-name detection now happens up-front via _match_set_in_query, so the
    # old inline alias-fallback here is no longer needed.)

    # Re-rank: exact name match first, then by set release date (newest first)
    name_lower = name_q.lower() if name_q else raw_q.lower()
    def relevance(card):
        cname = card.get("name", "").lower()
        cnum  = card.get("number", "")
        date  = card.get("set", {}).get("releaseDate", "0000-00-00")
        exact   = cname == name_lower
        starts  = cname.startswith(name_lower)
        num_hit = bool(number) and (cnum == number or cnum.lstrip('0') == number)
        yr_hit  = bool(year) and date.startswith(year)
        score = (0 if exact else 1 if starts else 2)
        bonus = -(1 if num_hit else 0) - (1 if yr_hit else 0)
        return (score + bonus, date)

    results.sort(key=lambda c: (relevance(c)[0], relevance(c)[1][::-1]))

    # Shape the response
    def shape(card):
        tcp = card.get("tcgplayer", {}).get("prices", {})

        def mkt(bucket):
            p = tcp.get(bucket) or {}
            v = p.get("market") or p.get("mid")
            return round(v, 2) if v else None

        holo    = mkt("holofoil")
        norm    = mkt("normal")
        rev     = mkt("reverseHolofoil")
        ed1h    = mkt("1stEditionHolofoil")
        unlimh  = mkt("unlimitedHolofoil")
        ed1n    = mkt("1stEditionNormal")
        # Best available NM price, prefer 1st edition holo for vintage
        best = ed1h or holo or unlimh or ed1n or norm or rev

        return {
            "id":        card["id"],
            "name":      card["name"],
            "number":    card.get("number", ""),
            "rarity":    card.get("rarity", ""),
            "setId":     card.get("set", {}).get("id", ""),
            "setName":   card.get("set", {}).get("name", ""),
            "releaseDate": card.get("set", {}).get("releaseDate", ""),
            "image":     card.get("images", {}).get("small", ""),
            "tcgProductId":    None,
            "tcgNormal":       norm,
            "tcgHolo":         holo,
            "tcgReverseHolo":  rev,
            "tcg1stEdHolo":    ed1h,
            "tcgUnlimitedHolo": unlimh,
            "tcg1stEdNormal":  ed1n,
            "tcgBestNM":       best,
            "tcgPrices":       list(tcp.keys()),
        }

    return jsonify({"cards": [shape(c) for c in results], "total": len(results)})


# ---------------------------------------------------------------------------
# Pricing endpoint
# ---------------------------------------------------------------------------

@app.route("/api/price")
def get_card_price():
    """
    Returns TCGPlayer NM market price + TCGPlayer recent sales for the given
    condition (via mpapi.tcgplayer.com — the same data TCGPlayer's website shows
    in their 3-month Sales History Snapshot).

    Falls back to eBay for PSA-graded cards or when no TCGPlayer product ID.

    Params:
      card_id        – pokemontcg.io card ID
      tcg_product_id – TCGPlayer numeric product ID (from search results)
      card_name      – card name (for eBay fallback)
      number         – card number (for eBay fallback)
      condition      – NM | LP | MP | HP | DMG | PSA-10 … PSA-1
      foil_type      – normal | holofoil | reverseHolofoil
    """
    card_id        = request.args.get("card_id", "").strip()
    tcg_product_id = request.args.get("tcg_product_id", "").strip()
    card_name      = request.args.get("card_name", "").strip()
    number         = request.args.get("number", "").strip()
    set_name       = request.args.get("set_name", "").strip()
    condition      = request.args.get("condition", "NM").strip().upper()
    foil_type      = request.args.get("foil_type", "holofoil").strip()
    db_id_raw      = request.args.get("db_id", "").strip()
    db_id          = int(db_id_raw) if db_id_raw.isdigit() else None
    force_refresh  = request.args.get("refresh", "").strip() in ("1", "true")

    cache_key = f"price_{card_id or card_name}_{condition}_{foil_type}"
    if not force_refresh:
        cached = cache_get(cache_key)
        if cached:
            return jsonify(cached)

    is_graded = condition.startswith("PSA")
    result: dict = {
        "condition": condition, "foilType": foil_type,
        "tcgMarket": None, "tcgProductId": None,
        "tcgSales": [], "tcgSalesAvg": None,
        "ebayAvg": None, "ebayLast5": [],
    }

    _tcg_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.tcgplayer.com/",
        "Accept":  "application/json",
    }

    # -- TCGPlayer NM market price (pokemontcg.io) --
    if card_id and not is_graded:
        ptcg_headers = {"X-Api-Key": POKEMON_TCG_API_KEY} if POKEMON_TCG_API_KEY else {}
        try:
            r = requests.get(
                f"https://api.pokemontcg.io/v2/cards/{card_id}",
                headers=ptcg_headers, timeout=8,
            )
            if r.ok:
                data = r.json().get("data", {})
                tcp = data.get("tcgplayer", {}).get("prices", {})
                bucket = tcp.get(foil_type) or tcp.get("holofoil") or tcp.get("normal") or {}
                market = bucket.get("market") or bucket.get("mid")
                if market:
                    result["tcgMarket"] = round(market, 2)
        except Exception:
            pass

    # -- TCGPlayer product ID (redirect + cached search fallback) --
    if not tcg_product_id and not is_graded:
        tcg_product_id = _resolve_tcg_product_id(card_id, card_name, "", number)

    # -- TCGPlayer recent sales (3-month window) --
    # The endpoint returns all recent sales for the product+printing regardless of
    # condition param; we filter by condition client-side from the `condition` field.
    TCG_COND_LABELS = {
        "NM":  "Near Mint",
        "LP":  "Lightly Played",
        "MP":  "Moderately Played",
        "HP":  "Heavily Played",
        "DMG": "Damaged",
    }
    if tcg_product_id and not is_graded:
        tcg_print = TCG_FOIL_MAP.get(foil_type, "Holofoil")
        want_cond = TCG_COND_LABELS.get(condition)
        three_months_ago = datetime.now(timezone.utc) - timedelta(days=365)
        try:
            resp = requests.post(
                f"https://mpapi.tcgplayer.com/v2/product/{tcg_product_id}/latestsales",
                json={"printing": tcg_print, "language": "English", "limit": 100},
                headers=dict(_tcg_headers, **{"Content-Type": "application/json"}),
                timeout=10,
            )
            if resp.ok:
                sales = []
                for item in resp.json().get("data", []):
                    try:
                        date_str = item.get("orderDate", "")
                        order_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        if order_dt < three_months_ago:
                            continue
                        item_cond = item.get("condition", "")
                        if want_cond and item_cond != want_cond:
                            continue  # filter to requested condition
                        price = float(item.get("purchasePrice", 0))
                        qty   = int(item.get("quantity", 1))
                        if price > 0:
                            sales.append({
                                "price":     price,
                                "qty":       qty,
                                "date":      date_str[:10],
                                "condition": item_cond,
                            })
                    except (ValueError, KeyError, TypeError):
                        pass
                if sales:
                    result["tcgSales"] = sales
                    # Market value = average of the 5 MOST RECENT TCGPlayer
                    # sold prices for this condition (what the user asked for).
                    recent = sorted(sales, key=lambda s: s["date"], reverse=True)[:5]
                    result["tcgSalesAvg"] = round(
                        sum(s["price"] for s in recent) / len(recent), 2)
                    result["tcgLast5Count"] = len(recent)
        except Exception:
            pass

    # ── Condition-specific market value from the Sales History snapshot ──────
    # An LP card is valued at the LP market, an MP card at the MP market, etc.
    # (the per-condition SKU marketPrice), NOT the NM price.  This is the
    # authoritative per-condition value, so it overrides the sparse last-5 calc.
    if tcg_product_id and not is_graded:
        try:
            from tcg_snapshot import fetch_sales_history
            from pricing_engine import select_condition_market
            snap = fetch_sales_history(tcg_product_id)
            # Printing-strict per-condition market price (a reverse-holo card is
            # valued at the reverse-holo SKU, never the plain-holo one).
            cond_price = select_condition_market(
                snap.get("skus", []), condition, foil_type)
            if cond_price:
                result["tcgSalesAvg"] = cond_price
                result["tcgCondValue"] = cond_price
                result["tcgCondSource"] = "snapshot"
        except Exception:
            pass

    # Fallback market value: TCGPlayer market price for the foil (from
    # pokemontcg.io) when no recent sold data is available.
    if not result.get("tcgSalesAvg") and result.get("tcgMarket"):
        result["tcgSalesAvg"] = result["tcgMarket"]

    # -- eBay pricing --
    # PSA graded cards: use eBay sold comps (PSA auction-spec matching is too
    # error-prone to trust as a silent collection value — it can grab the wrong
    # variant).  Both the collection and portfolio read the resulting snapshot
    # value via /api/portfolio, so they stay consistent regardless of source.
    # Non-graded cards without TCGPlayer data: use eBay API if key is set.
    if is_graded and card_name:
        grade_str = condition.replace("PSA-", "PSA ")
        kw = " ".join(filter(None, [card_name, number, grade_str])).strip()
        sold: list[dict] = []

        if EBAY_APP_ID:
            # Official eBay Finding API
            thirty_days_ago = (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            try:
                resp = requests.get(
                    "https://svcs.ebay.com/services/search/FindingService/v1",
                    params=[
                        ("OPERATION-NAME", "findCompletedItems"),
                        ("SERVICE-VERSION", "1.0.0"),
                        ("SECURITY-APPNAME", EBAY_APP_ID),
                        ("RESPONSE-DATA-FORMAT", "JSON"),
                        ("keywords", kw),
                        ("categoryId", "2536"),
                        ("itemFilter(0).name", "SoldItemsOnly"),
                        ("itemFilter(0).value", "true"),
                        ("itemFilter(1).name", "EndTimeFrom"),
                        ("itemFilter(1).value", thirty_days_ago),
                        ("sortOrder", "EndTimeSoonest"),
                        ("paginationInput.entriesPerPage", "10"),
                        ("outputSelector(0)", "SellingStatus"),
                    ],
                    timeout=10,
                )
                if resp.ok:
                    root = resp.json().get("findCompletedItemsResponse", [{}])[0]
                    for item in root.get("searchResult", [{}])[0].get("item", []):
                        try:
                            status = item.get("sellingStatus", [{}])[0]
                            if "EndedWithSales" not in status.get("sellingState", [""])[0]:
                                continue
                            price = float(status.get("currentPrice", [{}])[0].get("__value__", "0"))
                            if price > 0:
                                sold.append({
                                    "price":   price,
                                    "title":   item.get("title", [""])[0],
                                    "url":     item.get("viewItemURL", [""])[0],
                                    "endTime": item.get("listingInfo", [{}])[0].get("endTime", [""])[0],
                                })
                        except (KeyError, IndexError, ValueError):
                            pass
            except Exception:
                pass

        if not sold:
            # No API key or API returned nothing — scrape eBay completed listings
            sold = _scrape_ebay_sold(kw)

        if sold:
            last5 = sold[:5]
            prices = [s["price"] for s in last5]
            result["ebayLast5"] = last5
            result["ebayAvg"]   = round(sum(prices) / len(prices), 2)
            # For graded cards, also save daily snapshot using eBay avg
            if db_id:
                try:
                    col_db.save_price_snapshot(db_id, None, result["ebayAvg"])
                except Exception:
                    pass

    elif not is_graded and card_name:
        # Non-graded card: always fetch eBay completed sales for market data
        foil_kw = "reverse holo" if foil_type == "reverseHolofoil" else (
                  "holo" if foil_type == "holofoil" else "")
        # Build keyword — no condition term (eBay ungraded listings rarely have it)
        kw_parts = [card_name]
        if number:
            kw_parts.append(number)
        if foil_kw:
            kw_parts.append(foil_kw)
        # Exclude graded to keep results raw
        kw = " ".join(kw_parts) + " -PSA -BGS -CGC -graded -beckett -SGC"
        sold = _scrape_ebay_sold(kw, max_items=60)
        # Clean: ungraded only, number-matched, foil-matched, outliers removed.
        sold = _filter_ebay_sales(sold, number=number, foil=foil_type,
                                  want_graded=False)
        if sold:
            # Market value = average of the most recent ~10 cleaned sales.
            last10 = sold[:10]
            prices = [s["price"] for s in last10]
            result["ebayLast5"] = last10
            result["ebayAll"]   = sold
            result["ebayAvg"]   = round(sum(prices) / len(prices), 2)

    result["tcgProductId"] = tcg_product_id

    # Save daily price snapshot to DB when called from a collection card
    if db_id and not is_graded and (result.get("tcgMarket") or result.get("tcgSalesAvg")):
        try:
            col_db.save_price_snapshot(db_id, result.get("tcgMarket"), result.get("tcgSalesAvg"))
        except Exception:
            pass

    cache_set(cache_key, result)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Price history endpoint
# ---------------------------------------------------------------------------

@app.route("/api/collection/cards/<int:card_db_id>/history")
@auth.login_required
def get_card_price_history(card_db_id):
    # Only the card's owner may read its history.
    if col_db.card_owner(card_db_id) != auth.current_user_id():
        return jsonify({"error": "Not found"}), 404
    return jsonify(col_db.get_price_history(card_db_id))


# ---------------------------------------------------------------------------
# PSA auction prices endpoint
# ---------------------------------------------------------------------------

@app.route("/api/psa/prices")
def get_psa_prices():
    """
    Fetch PSA recent auction sale prices for a card by grade.
    Also scrapes eBay for PSA sales when PSA site has no data.

    Params: card_name, number, set_name, grade (optional, e.g. "10")
    Returns: {
      grades: { "10": [{price, date, auction, url}], "9": [...], ... },
      smr: { "10": price, "9": price, ... },
      ebaySales: { "10": [{price, url, title, endTime}], ... }
    }
    """
    card_name = request.args.get("card_name", "").strip()
    number    = request.args.get("number", "").strip()
    set_name  = request.args.get("set_name", "").strip()
    grade     = request.args.get("grade", "").strip()

    if not card_name:
        return jsonify({"error": "card_name required"}), 400

    cache_key = f"psa_{card_name}_{number}_{set_name}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    result = {
        "grades": {},   # PSA auction results keyed by grade string
        "smr": {},      # SMR (guide) prices by grade
        "ebaySales": {},# eBay completed sales by grade
    }

    # --- Try PSA auction prices page via undocumented API ---
    try:
        # PSA has a JSON endpoint for auction results
        search_name = f"{card_name} {number}".strip()
        psa_search = requests.get(
            "https://www.psacard.com/auctionprices/cardinformation/pokemon/",
            params={"name": search_name},
            headers={**BROWSER_HEADERS, "X-Requested-With": "XMLHttpRequest"},
            timeout=10,
        )
        if psa_search.ok:
            html = psa_search.text
            nd_m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if nd_m:
                nd = json.loads(nd_m.group(1))
                items = ((nd.get("props") or {}).get("pageProps") or {}).get("auctionResults") or []
                for item in items:
                    g = str(item.get("grade", "")).strip()
                    price = item.get("salePrice") or 0
                    if g and price:
                        if g not in result["grades"]:
                            result["grades"][g] = []
                        result["grades"][g].append({
                            "price":   float(price),
                            "date":    str(item.get("dateSold") or "")[:10],
                            "auction": str(item.get("auctionHouse") or ""),
                            "url":     str(item.get("url") or ""),
                        })
    except Exception:
        pass

    # --- Always scrape eBay for PSA graded sales by grade ---
    grades_to_check = [grade] if grade else ["10", "9", "8", "7"]
    for g in grades_to_check:
        kw = f"{card_name} {number} PSA {g}".strip()
        ebay_sold = _scrape_ebay_sold(kw, max_items=10)
        if ebay_sold:
            result["ebaySales"][g] = ebay_sold

    # Build a combined price estimate from eBay data (most recent 5 sales per grade)
    for g, sales in result["ebaySales"].items():
        if sales and g not in result["smr"]:
            prices = [s["price"] for s in sales[:5]]
            result["smr"][g] = round(sum(prices) / len(prices), 2)

    _cache[cache_key] = (time.time(), result)
    return jsonify(result)


@app.route("/api/psa/sold")
def get_psa_sold():
    """
    Fetch recent PSA auction sale prices for a card, keyed by grade.

    When spec_id is supplied (from the pop-scraper result), we hit PSA's
    auctionprices JSON API directly.  Otherwise we do a name-based SMR search
    to discover the specId.  Any grades without PSA data fall back to eBay
    completed-listings scraping.

    Params: card_name, number, set_name, grade (optional, limits to one grade)
    Returns: { sales: {grade: [{price, date, auction, url, source}]},
               smr:   {grade: avg_price},
               specId: str | None,
               source: "psa" | "ebay" | null }
    """
    card_name = request.args.get("card_name", "").strip()
    number    = request.args.get("number", "").strip()
    set_name  = request.args.get("set_name", "").strip()
    grade     = request.args.get("grade", "").strip()
    foil_type = request.args.get("foil_type", "").strip()

    if not card_name:
        return jsonify({"error": "card_name required"}), 400

    # Printing is part of the key — PSA grades reverse-foil/1st-ed as separate
    # specs, so each printing gets its own (different) auction data.
    cache_key = f"psa_sold_{card_name}_{number}_{set_name}_{grade}_{foil_type}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    result: dict = {"sales": {}, "smr": {}, "specId": None, "source": None,
                    "summary": {}, "gemRate": None, "totalVolume": 0}

    # PSA "Auction Prices Realized" — real cross-marketplace graded sales for
    # ALL grades 1–10 (already aggregates eBay + auction houses), via the
    # logged-in session.  One paginated combined fetch grouped by grade.
    if PSA_EMAIL and PSA_PASSWORD:
        try:
            psa = _scraper().psa_sales(card_name, number, set_name,
                                       since_days=400, finish=foil_type)
            result["specId"] = psa.get("spec_id")
            result.setdefault("debug", {})["specId"] = psa.get("spec_id")
            for g, sales in (psa.get("by_grade") or {}).items():
                if sales:
                    result["sales"][g] = sales
                    prices = [s["price"] for s in sales[:10]]
                    result["smr"][g] = round(sum(prices) / len(prices), 2)
                    result["source"] = "psa"
                    # Debug: the exact comps used for this grade's price, with the
                    # canonical eBay itemId + saved URL.  PSA pre-filters to the
                    # requested grade, so there are no cross-grade rejections.
                    result["debug"][f"PSA-{g}"] = {
                        "compsUsed": len(prices),
                        "comps": [{"itemId": s.get("itemId"), "url": s.get("url"),
                                   "cert": s.get("cert"), "price": s.get("price"),
                                   "date": s.get("date"), "house": s.get("auction"),
                                   "matchConfidence": s.get("matchConfidence")}
                                  for s in sales[:10]],
                    }
            summ = psa.get("summary") or {}
            result["summary"]     = summ.get("grades", {})
            result["gemRate"]     = summ.get("gemRate")
            result["totalVolume"] = summ.get("totalVolume", 0)
        except Exception as e:
            print(f"[psa] sold fetch failed: {e}")

    # eBay fallback ONLY when PSA lookup failed entirely (no spec resolved).
    if not result["specId"]:
        for g in (grade and [grade]) or ["10", "9", "8", "7"]:
            kw = f"{card_name} {number} PSA {g}".strip()
            ebay = _scrape_ebay_sold(kw, max_items=25)
            ebay = _filter_ebay_sales(ebay, number=number, want_graded=True)
            ebay = [s for s in ebay if s.get("condition") == f"PSA-{g}"]
            if ebay:
                result["sales"][g] = ebay
                prices = [s["price"] for s in ebay[:8]]
                result["smr"][g] = round(sum(prices) / len(prices), 2)
                if not result["source"]:
                    result["source"] = "ebay"

    cache_set(cache_key, result)
    return jsonify(result)


@app.route("/api/market/card-data")
def market_card_data():
    """
    All-in-one endpoint for the market explorer.
    Returns TCGPlayer sales grouped by ALL conditions, eBay ungraded sales,
    and PSA eBay sales by grade (10, 9, 8, 7).

    Params: card_id, card_name, number, foil_type
    """
    card_id   = request.args.get("card_id", "").strip()
    card_name = request.args.get("card_name", "").strip()
    number    = request.args.get("number", "").strip()
    set_name  = request.args.get("set_name", "").strip()
    foil_type = request.args.get("foil_type", "holofoil").strip()
    passed_pid = request.args.get("tcg_product_id", "").strip()

    if not card_name:
        return jsonify({"error": "card_name required"}), 400

    cache_key = f"mkt_{card_id or passed_pid}_{card_name}"   # printing-agnostic now
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    result = {
        "tcgSales":      {},   # keyed by condition abbreviation
        "tcgMarket":     {},   # keyed by foil type
        "tcgProductId":  None,
        "ebaySales":     [],   # all ungraded eBay sold
        "psaSales":      {},   # keyed by grade "10","9","8","7"
        "psaSmr":        {},   # estimated avg price by grade from eBay data
    }

    _tcg_h = {
        "User-Agent": BROWSER_HEADERS["User-Agent"],
        "Referer": "https://www.tcgplayer.com/",
        "Accept": "application/json",
    }

    # 1. TCGPlayer NM market price
    if card_id:
        ptcg_h = {"X-Api-Key": POKEMON_TCG_API_KEY} if POKEMON_TCG_API_KEY else {}
        try:
            r = requests.get(f"https://api.pokemontcg.io/v2/cards/{card_id}",
                             headers=ptcg_h, timeout=8)
            if r.ok:
                tcp = r.json().get("data", {}).get("tcgplayer", {}).get("prices", {})
                for fk, fv in tcp.items():
                    m = fv.get("market") or fv.get("mid")
                    if m:
                        result["tcgMarket"][fk] = round(m, 2)
        except Exception:
            pass

    # 2. TCGPlayer product ID (redirect + search fallback, cached)
    tcg_product_id = passed_pid or _resolve_tcg_product_id(card_id, card_name, set_name, number)
    if tcg_product_id:
        result["tcgProductId"] = tcg_product_id

    # ── Collect raw candidate sales from both sources, then run them through
    #    the strict matching/scoring engine so only validated sales are used.
    from pricing_engine import CardTarget, SaleCandidate, evaluate
    from tcg_snapshot import fetch_sales_history

    # TCGplayer: pull the FULL Sales History Snapshot — every SKU (condition ×
    # printing × language), not just Near Mint or the first Normal SKU.  Each
    # SKU's dated buckets become per-condition data points.
    # Build TCG candidates for EVERY printing (holofoil / normal / reverseHolofoil
    # / 1st edition / unlimited …) so the user can toggle between them client-side.
    tcg_candidates: list = []
    snapshot = fetch_sales_history(tcg_product_id) if tcg_product_id else {}
    result["tcgSnapshot"] = snapshot
    printings_seen = {}
    for sku in snapshot.get("skus", []):
        variant = sku.get("variant") or "normal"
        pts = sku.get("points") or []
        if pts:
            printings_seen[variant] = printings_seen.get(variant, 0) + len(pts)
        for pt in pts:
            if pt.get("marketPrice", 0) <= 0:
                continue
            tcg_candidates.append(SaleCandidate(
                price=pt["marketPrice"], date=pt.get("date") or "",
                source="tcgplayer", tcg_product_id=str(tcg_product_id),
                tcg_condition=sku.get("conditionRaw", ""),
                tcg_printing=sku.get("variantRaw", ""),
                qty=pt.get("quantitySold", 1),
                title=f"{card_name} {number} {set_name} {sku.get('variantRaw','')}".strip(),
            ))

    # Ordered list of printings that actually have sales (holo → reverse → 1st →
    # unlimited → normal), so the UI shows toggle buttons only where applicable.
    _PRINT_ORDER = ["holofoil", "reverseHolofoil", "1stEditionHolofoil",
                    "unlimitedHolofoil", "1stEditionNormal", "normal", "etchedFoil"]
    result["availablePrintings"] = sorted(
        printings_seen, key=lambda p: _PRINT_ORDER.index(p) if p in _PRINT_ORDER else 99)
    result["setName"] = set_name   # used client-side for the "stamped" label

    # Canonical CURRENT market price per printing × condition — the exact same
    # printing-strict snapshot value the portfolio uses (`select_condition_market`),
    # so the number shown here matches a holding's valuation to the cent.
    from pricing_engine import select_condition_market
    cond_market: dict = {}
    for p in result["availablePrintings"]:
        cm = {}
        for cond in ("NM", "LP", "MP", "HP", "DMG"):
            v = select_condition_market(snapshot.get("skus", []), cond, p)
            if v:
                cm[cond] = round(v, 2)
        if cm:
            cond_market[p] = cm
    result["condMarket"] = cond_market

    # ── Run the engine on TCGplayer sales (no finish constraint → groups for
    #    every printing × condition).  eBay raw comps load via /api/market/ebay-sold.
    target = CardTarget(
        name=card_name, set_name=set_name, number=number,
        finish="", condition="NM", language="english",
    )
    analysis = evaluate(target, tcg_candidates)
    result["priceAnalysis"] = analysis
    # tcgSales nested by printing → condition.
    for m in analysis["matched"]:
        if m.get("outlier"):
            continue
        fin = m["finish"] or "normal"
        result["tcgSales"].setdefault(fin, {}).setdefault(m["condition"], []).append(
            {"price": m["price"], "qty": 1, "date": m["date"],
             "condition": m["condition"]})

    _cache[cache_key] = (time.time(), result)
    return jsonify(result)


@app.route("/api/market/ebay-sold")
def market_ebay_sold():
    """
    Raw/ungraded eBay sold comps for a card, bucketed by OFFICIAL condition.

    Human-like pipeline: search completed listings → open each recent item's
    detail page → read the official condition field + item specifics → verify
    raw + exact card → normalise into NM/LP/MP/HP/DMG/UNKNOWN_RAW → median per
    bucket using totalPriceWithShipping.  Slow (~30s) → loaded async + cached.
    """
    from pricing_engine import CardTarget, SaleCandidate, evaluate

    card_name = request.args.get("card_name", "").strip()
    number    = request.args.get("number", "").strip()
    set_name  = request.args.get("set_name", "").strip()
    foil_type = request.args.get("foil_type", "holofoil").strip()
    language  = request.args.get("language", "EN").strip().upper()
    if not card_name:
        return jsonify({"error": "card_name required"}), 400

    is_jp = language == "JP"
    target_lang = "japanese" if is_jp else "english"
    cache_key = f"ebayraw_{card_name}_{number}_{set_name}_{foil_type}_{language}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    # Search BROADLY (name + number, + "reverse holo" only when needed): the set
    # name rarely appears in titles and over-narrows results.  Exact set / finish
    # are verified afterward from each item's official item specifics.
    foil_kw = "reverse holo" if foil_type == "reverseHolofoil" else ""
    lang_kw = "japanese" if is_jp else ""
    ebay_kw = " ".join(filter(None, [card_name, number, foil_kw, lang_kw])) \
              + " -PSA -BGS -CGC -graded -beckett -SGC -lot -bundle"
    raw_ebay = _scrape_ebay_sold_detailed(ebay_kw, max_items=120, max_details=60)

    candidates = []
    for s in raw_ebay:
        if not s.get("price"):
            continue
        ship = float(s.get("shipping") or 0)
        candidates.append(SaleCandidate(
            price=round(float(s["price"]) + ship, 2),    # totalPriceWithShipping
            title=s.get("title", ""), url=s.get("url", ""),
            date=s.get("date") or s.get("endTime", ""), source="ebay",
            subtitle=s.get("ebayCondition", ""),
            official_condition=s.get("conditionDisplayName", ""),
            item_specifics=s.get("itemSpecifics", {}) or {},
            shipping=ship,
            item_id=(s.get("url", "").rsplit("/", 1)[-1] if s.get("url") else ""),
        ))

    target = CardTarget(name=card_name, set_name=set_name, number=number,
                        finish=foil_type, condition="NM", language=target_lang)
    analysis = evaluate(target, candidates)

    # Which scraped listing URLs were verified to still show their sold price
    # (so the frontend links straight there vs falling back to a sold search).
    verified_by_url = {s.get("url"): bool(s.get("urlVerified"))
                       for s in raw_ebay if s.get("url")}

    sales, buckets = [], {}
    for m in analysis["matched"]:
        if m.get("outlier"):
            continue
        sales.append({"price": m["price"], "url": m["url"], "title": m["title"],
                      "endTime": m["date"], "date": m["date"],
                      "condition": m["condition"], "source": "ebay",
                      "urlVerified": verified_by_url.get(m["url"], False)})
        buckets[m["condition"]] = buckets.get(m["condition"], 0) + 1

    rej = analysis["rejected"]
    def _c(pred):
        return sum(1 for r in rej if pred(r.get("reason", "")))
    result = {
        "ebaySales": sales,
        "groups": {k: v for k, v in analysis["groups"].items()},
        "debug": {
            "soldResultsFound":    len(raw_ebay),
            "detailsFetched":      sum(1 for s in raw_ebay if s.get("detailFetched")),
            "rejectedGraded":      _c(lambda x: "graded" in x),
            "rejectedWrongCard":   _c(lambda x: "name mismatch" in x or "wrong" in x),
            "rejectedLotSealed":   _c(lambda x: "keyword:" in x or "sealed" in x),
            "rejectedUnknownCond": _c(lambda x: "condition" in x and "wrong" not in x),
            "savedPerBucket":      buckets,
        },
    }
    cache_set(cache_key, result)
    return jsonify(result)


@app.route("/api/market/tcg-snapshot")
def tcg_snapshot_debug():
    """
    Full TCGplayer Sales History Snapshot for a card, every SKU.

    Resolves the productId (params: card_id / card_name / set_name / number, or
    an explicit product_id), then returns per-SKU coverage: skuIds checked,
    condition, variant, language, marketPrice, low/high-with-shipping,
    quantitySold, transactionCount, dated points, plus skipped/empty SKUs with a
    reason and the total bucket count.
    """
    from tcg_snapshot import fetch_sales_history
    product_id = request.args.get("product_id", "").strip()
    if not product_id:
        product_id = _resolve_tcg_product_id(
            request.args.get("card_id", "").strip(),
            request.args.get("card_name", "").strip(),
            request.args.get("set_name", "").strip(),
            request.args.get("number", "").strip())
    if not product_id:
        return jsonify({"error": "could not resolve TCGplayer productId"}), 404
    return jsonify(fetch_sales_history(product_id))


# ---------------------------------------------------------------------------
# Market / Explore page
# ---------------------------------------------------------------------------

@app.route("/market")
@auth.login_required
def market():
    return render_template("market.html")


def _shape_market_card(c: dict) -> dict:
    """Shape a pokemontcg.io card dict into the market format."""
    tcp = c.get("tcgplayer", {}).get("prices", {})
    def mkt(b):
        p = tcp.get(b) or {}
        v = p.get("market") or p.get("mid")
        return round(v, 2) if v else None
    return {
        "id":          c["id"],
        "name":        c["name"],
        "number":      c.get("number", ""),
        "rarity":      c.get("rarity", ""),
        "setName":     c["set"]["name"],
        "setId":       c["set"]["id"],
        "image":       c.get("images", {}).get("small", ""),
        "largeImage":  c.get("images", {}).get("large", ""),
        "releaseDate": c.get("set", {}).get("releaseDate", ""),
        "types":       c.get("types", []),      # energy type(s) → UI accent color
        "supertype":   c.get("supertype", ""),
        "tcgHolo":         mkt("holofoil"),
        "tcgNormal":       mkt("normal"),
        "tcgReverseHolo":  mkt("reverseHolofoil"),
        "tcg1stEdHolo":    mkt("1stEditionHolofoil"),
        "tcgUnlimitedHolo": mkt("unlimitedHolofoil"),
        "tcg1stEdNormal":  mkt("1stEditionNormal"),
        "tcgPrices":   list(tcp.keys()),
    }


@app.route("/api/market/search")
def market_search():
    """
    Fuzzy card search for the market explorer.
    Reuses the same multi-strategy search as /api/search/cards.
    Params: q (search term), set_id, page
    """
    raw_q    = request.args.get("q", "").strip()
    set_id   = request.args.get("set_id", "").strip()
    page_num = int(request.args.get("page", 1))

    if not raw_q and not set_id:
        return jsonify({"cards": [], "total": 0})

    headers = {"X-Api-Key": POKEMON_TCG_API_KEY} if POKEMON_TCG_API_KEY else {}

    def api_search(q_str: str, pg: int = 1) -> list[dict]:
        try:
            r = requests.get("https://api.pokemontcg.io/v2/cards",
                params={"q": q_str, "pageSize": 12, "page": pg, "orderBy": "-set.releaseDate"},
                headers=headers, timeout=10)
            r.raise_for_status()
            return r.json().get("data", [])
        except Exception:
            return []

    # Detect a set name embedded in the query (e.g. "Typhlosion Call of Legends").
    detected_set_id = None
    if not set_id:
        detected_set_id, raw_q2 = _match_set_in_query(raw_q)
        if detected_set_id:
            raw_q = raw_q2

    # Parse tokens
    year_m = re.search(r'\b((?:19|20)\d{2})\b', raw_q)
    year   = year_m.group(1) if year_m else None
    q_sans = re.sub(r'\b(?:19|20)\d{2}\b', '', raw_q).strip()
    num_m  = re.search(r'\b(\d{1,3})(?:/\d+)?\b', q_sans)
    number = num_m.group(1) if num_m else None
    name_tokens = [t for t in raw_q.split()
        if not (year and t == year)
        and not (number and re.match(r'^\d{1,3}(?:/\d+)?$', t) and t.split('/')[0] == number)]
    name_q = ' '.join(name_tokens).strip()

    # Primary query
    parts = []
    if name_q:
        parts.append(f'name:*{name_q.replace(" ","*")}*')
    if number:
        parts.append(f'number:{number}')
    if set_id:
        parts.append(f'set.id:{set_id}')
    elif detected_set_id:
        parts.append(f'set.id:{detected_set_id}')
    elif year:
        parts.append(f'set.releaseDate:{year}*')
    results = api_search(' '.join(parts) if parts else f'name:*{raw_q}*', page_num)

    # If a set was detected but the combined query found nothing, retry with
    # just name+set (number may have been a false positive).
    if not results and detected_set_id and name_q:
        results = api_search(f'name:*{name_q.replace(" ","*")}* set.id:{detected_set_id}', page_num)

    # Word-by-word union fallback. Skipped once a set is pinned (via set_id or a
    # name detected in the query) — unioning bare name words there would drag in
    # every printing from every OTHER set and bury the ones the user asked for.
    if len(results) < 3 and name_q and not set_id and not detected_set_id:
        seen = {c["id"] for c in results}
        for word in [w for w in name_q.split() if len(w) >= 3][:3]:
            for extra in api_search(f'name:*{word}*', 1):
                if extra["id"] not in seen:
                    results.append(extra); seen.add(extra["id"])

    # Prefix-shrink fuzzy fallback (typo tolerance) — also skipped when a set is
    # pinned, so a wrong-set guess degrades to "nothing in that set", not noise.
    if not results and name_q and not detected_set_id:
        seen = set()
        for word in [w for w in name_q.split() if len(w) >= 4][:3]:
            for prefix_len in range(len(word), max(3, len(word)-3)-1, -1):
                extra = api_search(f'name:*{word[:prefix_len]}*', 1)
                if extra:
                    for c in extra:
                        if c["id"] not in seen:
                            results.append(c); seen.add(c["id"])
                    break

    cards = [_shape_market_card(c) for c in results]

    # Append Japanese cards (TCGplayer JP product line) so they're searchable in
    # the same database. Skipped when an English set is pinned (by id or detected
    # in the query) — the user is clearly after that specific English set.
    jp_query = raw_q or ""
    if jp_query and not set_id and not detected_set_id:
        try:
            jp_cards = search_tcgplayer_jp(jp_query, limit=6)
            seen = {c.get("name", "").lower() for c in cards}
            for jc in jp_cards:
                cards.append(jc)
        except Exception:
            pass

    return jsonify({"cards": cards, "total": len(cards)})


# ---------------------------------------------------------------------------
# Collection — cards
# ---------------------------------------------------------------------------

@app.route("/api/collection/cards", methods=["GET"])
@auth.login_required
def collection_get_cards():
    return jsonify(col_db.get_all_cards(auth.current_user_id()))


@app.route("/api/collection/cards", methods=["POST"])
@auth.login_required
def collection_add_card():
    data = request.get_json(silent=True) or {}
    if not data.get("card_name") or not data.get("condition"):
        return jsonify({"error": "card_name and condition required"}), 400
    new_id = col_db.add_card(data, auth.current_user_id())
    return jsonify({"id": new_id}), 201


@app.route("/api/collection/cards/<int:card_id>", methods=["PUT"])
@auth.login_required
def collection_update_card(card_id):
    data = request.get_json(silent=True) or {}
    col_db.update_card(card_id, data, auth.current_user_id())
    return jsonify({"ok": True})


@app.route("/api/collection/cards/<int:card_id>", methods=["DELETE"])
@auth.login_required
def collection_delete_card(card_id):
    col_db.delete_card(card_id, auth.current_user_id())
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Collection import / export (CSV — paste or file). CSV parsing/normalization
# lives in the pure, offline-tested `csv_import` module.
# ---------------------------------------------------------------------------
def _resolve_import_card(name: str, set_name: str, number: str, language: str) -> dict:
    """Best-effort lookup of a card's metadata (id, image, set) so imported rows
    price like searched ones.  Falls back to {} (the row still imports raw)."""
    name = (name or "").strip()
    number = (number or "").strip()
    if not name:
        return {}
    if (language or "EN").upper() == "JP":
        try:
            jp = search_tcgplayer_jp(f"{name} {number}".strip(), limit=1)
            if jp:
                c = jp[0]
                return {"card_id": c["id"], "image_url": c.get("image", ""),
                        "rarity": c.get("rarity", ""),
                        "set_name": c.get("setName") or set_name}
        except Exception:
            pass
        return {}
    headers = {"X-Api-Key": POKEMON_TCG_API_KEY} if POKEMON_TCG_API_KEY else {}
    num = number.split("/")[0].lstrip("0")
    base = f'name:"{name}"' + (f" number:{num}" if num else "")

    # Resolve the typed set name to a real set id FIRST ("Base Set" → base1,
    # "Undaunted" → hgss3, "Expedition" → ecard1). Querying by set.id is exact,
    # so the card links to the RIGHT set instead of pokemontcg.io's fuzzy
    # newest-first set.name match (which sent "Base Set" to "Base Set 2").
    set_id = None
    if set_name:
        set_id, _ = _match_set_in_query(set_name)

    queries = []
    if set_id:
        queries.append(base + f' set.id:{set_id}')          # name (+number) in the set
        queries.append(f'name:"{name}" set.id:{set_id}')    # set is right even if number is off
    if set_name:
        queries.append(base + f' set.name:"{set_name}"')    # fall back to fuzzy set name
    queries.append(base)
    queries.append(f'name:"{name}"')
    for q in queries:
        try:
            r = requests.get("https://api.pokemontcg.io/v2/cards",
                             params={"q": q, "pageSize": 1, "orderBy": "-set.releaseDate"},
                             headers=headers, timeout=10)
            data = (r.json() or {}).get("data") or []
            if data:
                c = data[0]
                return {"card_id": c["id"], "set_id": c["set"]["id"],
                        "set_name": c["set"]["name"], "number": c.get("number", ""),
                        "image_url": c.get("images", {}).get("small", ""),
                        "rarity": c.get("rarity", "")}
        except Exception:
            continue
    return {}


@app.route("/api/collection/import", methods=["POST"])
@auth.login_required
def collection_import():
    """Import cards from CSV (uploaded file or pasted text)."""
    uid = auth.current_user_id()

    if request.files.get("file"):
        # utf-8-sig strips the BOM that Excel / Google Sheets prepend to CSV
        # exports — otherwise the first header becomes "﻿name", the name
        # column is never found, and every row is silently skipped.
        csv_text = request.files["file"].read().decode("utf-8-sig", "ignore")
    else:
        body = request.get_json(silent=True) or {}
        csv_text = body.get("csv") or request.form.get("csv", "")

    parsed = parse_import_csv(csv_text)
    if not parsed["total"] and not (csv_text or "").strip():
        return jsonify({"error": "No CSV data provided."}), 400

    added, errors = 0, []
    for i, row in enumerate(parsed["rows"], 1):
        # Enrich with card metadata (id, image, set) so imported rows price like
        # searched ones; falls back to the raw row on lookup failure.
        meta = _resolve_import_card(row["card_name"], row["set_name"] or "",
                                    row["number"] or "", row["language"])
        card = dict(row,
            card_id=meta.get("card_id") or None,
            set_name=meta.get("set_name") or row["set_name"] or None,
            set_id=meta.get("set_id") or None,
            number=meta.get("number") or row["number"] or None,
            image_url=meta.get("image_url") or None,
            rarity=meta.get("rarity") or None,
        )
        try:
            col_db.add_card(card, uid)
            added += 1
        except Exception as e:
            errors.append(f"Row {i} ({row['card_name']}): {e}")

    return jsonify({"added": added, "skipped": parsed["skipped"],
                    "matched": added, "errors": errors[:12],
                    "total": parsed["total"]})


@app.route("/api/collection/export")
@auth.login_required
def collection_export():
    """Download the user's collection as CSV (re-importable)."""
    import csv as _csv
    import io as _io
    from flask import Response
    cards = col_db.get_all_cards(auth.current_user_id())
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["name", "set", "number", "condition", "foil", "quantity",
                "price_paid", "language"])
    for c in cards:
        w.writerow([c.get("card_name", ""), c.get("set_name", ""), c.get("number", ""),
                    c.get("condition", ""), c.get("foil_type", ""), c.get("quantity", 1),
                    c.get("purchase_price") if c.get("purchase_price") is not None else "",
                    c.get("language", "EN")])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=pokepop-collection.csv"})


@app.route("/api/collection/template")
def collection_template():
    """A CSV template showing the import format."""
    from flask import Response
    text = ("name,set,number,condition,foil,quantity,price_paid,language\n"
            "Charizard,Base Set,4,NM,holofoil,1,250.00,EN\n"
            "Pikachu,Surging Sparks,238,LP,reverseHolofoil,2,,EN\n"
            "Umbreon ex,Prismatic Evolutions,161,PSA-10,holofoil,1,900,EN\n")
    return Response(text, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=pokepop-import-template.csv"})


# ---------------------------------------------------------------------------
# Collection — sealed
# ---------------------------------------------------------------------------

@app.route("/api/collection/sealed", methods=["GET"])
@auth.login_required
def collection_get_sealed():
    return jsonify(col_db.get_all_sealed(auth.current_user_id()))


@app.route("/api/collection/sealed", methods=["POST"])
@auth.login_required
def collection_add_sealed():
    data = request.get_json(silent=True) or {}
    if not data.get("product_name") or not data.get("product_type"):
        return jsonify({"error": "product_name and product_type required"}), 400
    # Snapshot the current TCGplayer market value at add time so realized gain
    # (current market − value when added) can be shown later, like cards.
    if data.get("product_id") and not data.get("value_at_add"):
        price, _ = _sealed_current_price(str(data["product_id"]))
        if price:
            data["value_at_add"] = price
    new_id = col_db.add_sealed(data, auth.current_user_id())
    return jsonify({"id": new_id}), 201


@app.route("/api/collection/sealed/<int:sealed_id>", methods=["PUT"])
@auth.login_required
def collection_update_sealed(sealed_id):
    data = request.get_json(silent=True) or {}
    col_db.update_sealed(sealed_id, data, auth.current_user_id())
    return jsonify({"ok": True})


@app.route("/api/collection/sealed/<int:sealed_id>", methods=["DELETE"])
@auth.login_required
def collection_delete_sealed(sealed_id):
    col_db.delete_sealed(sealed_id, auth.current_user_id())
    return jsonify({"ok": True})


def _hourly_refresh_loop(port: int):
    """
    Every hour, re-price each collection card with fresh TCGPlayer/eBay data and
    write a daily price snapshot, so the collection reflects new sales over time.
    Runs as a daemon thread; uses internal HTTP calls to reuse the price logic.
    """
    import time as _t
    base = f"http://127.0.0.1:{port}"
    _t.sleep(45)   # let the server finish booting
    while True:
        try:
            cards = col_db.get_all_cards_all_users()   # refresh every user's cards
        except Exception:
            cards = []
        for c in cards:
            try:
                params = {
                    "card_id":   c.get("card_id") or "",
                    "card_name": c.get("card_name") or "",
                    "number":    c.get("number") or "",
                    "set_name":  c.get("set_name") or "",
                    "condition": c.get("condition") or "NM",
                    "foil_type": c.get("foil_type") or "holofoil",
                    "db_id":     c.get("id"),
                    "refresh":   "1",
                }
                requests.get(f"{base}/api/price", params=params, timeout=90)
            except Exception:
                pass
            _t.sleep(8)   # space out the scraping load
        _t.sleep(3600)    # ...then wait an hour and do it again


def _start_background_jobs(port: int = 5001):
    # Start only in the process that actually serves requests: under the debug
    # reloader that's the child (WERKZEUG_RUN_MAIN=true), so we never spawn two.
    threading.Thread(target=_hourly_refresh_loop, args=(port,), daemon=True).start()
    # Pre-warm the PSA login (~40s cold start) in the background so the user's
    # first PSA lookup is fast — it only pays for the (parallelized) data fetch.
    if PSA_EMAIL and PSA_PASSWORD:
        threading.Thread(target=lambda: _scraper().prewarm(), daemon=True).start()


if __name__ == "__main__":
    # Production (APP_ENV=production) serves via waitress in wsgi.py with debug
    # off; this dev entry keeps the hot-reloader for local work.
    debug = os.getenv("APP_ENV", "development") != "production"
    port = int(os.getenv("PORT", "5001"))
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _start_background_jobs(port)
    app.run(debug=debug, port=port, threaded=True)
