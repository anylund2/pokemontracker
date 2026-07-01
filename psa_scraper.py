"""
PSA Pop Report scraper.

Pipeline:
  1. Login to PSA/Collectors.com via Playwright (two-step Vaadin form)
  2. Run 30 AJAX search queries in the browser to collect ALL spec IDs for
     the set.  Each query uses a 2-digit prefix so decades 00-29 cover all
     3-digit card numbers.  Search is NOT rate-limited.
  3. Fetch getpopulationjson for each spec ID sequentially via Python
     requests with ≥1 s between calls.  Retries with a 15 s back-off when
     PSA returns 429.

CLI:
    python3 psa_scraper.py "Surging Sparks"
"""

import asyncio
import json
import re
import time
import requests

from playwright.async_api import async_playwright
from playwright_stealth import Stealth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gem_rate(psa10: int, total: int) -> float:
    return round(psa10 / total * 100, 1) if total > 0 else 0.0


def _safe_int(value) -> int:
    try:
        return int(str(value).strip().replace(",", "") or "0")
    except (ValueError, TypeError):
        return 0


def _parse_psa_data(psa: dict) -> dict:
    counts = psa.get("Counts") or {}

    def grade(key):
        return _safe_int(counts.get(key) or psa.get(key, 0))

    psa10 = grade("Grade10")
    psa9  = grade("Grade9")
    psa8  = grade("Grade8")
    psa7  = grade("Grade7")
    psa6  = grade("Grade6")
    psa5  = grade("Grade5")
    psa4  = grade("Grade4")
    psa3  = grade("Grade3")
    psa2  = grade("Grade2")
    psa1  = grade("Grade1")
    total = _safe_int(
        counts.get("GradeTotal") or counts.get("SumTotal") or psa.get("Total", 0)
    )
    if not total:
        total = psa10+psa9+psa8+psa7+psa6+psa5+psa4+psa3+psa2+psa1

    return {
        "name":      (psa.get("SubjectName") or "").strip(),
        "number":    str(psa.get("CardNumber") or "").strip(),
        "specId":    str(psa.get("SpecID") or ""),
        "headingId": str(psa.get("HeadingID") or ""),
        "psa10":     psa10,
        "psa9":      psa9,
        "total":     total,
        "gemRate":   _gem_rate(psa10, total),
        "grades": {
            "10": psa10, "9": psa9, "8": psa8, "7": psa7,
            "6":  psa6,  "5": psa5, "4": psa4, "3": psa3, "2": psa2, "1": psa1,
        },
    }


# ---------------------------------------------------------------------------
# Phase 1+2 — Login + collect ALL spec IDs via browser AJAX searches
# ---------------------------------------------------------------------------

async def _login_and_collect(email: str, password: str, set_name: str,
                             progress_cb=None) -> dict:
    """
    Returns:
      cookies     – session cookies for Python requests
      heading_id  – PSA integer HeadingID for the set
      spec_ids    – set[int] of all discovered spec IDs for the set
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        stealth = Stealth(init_scripts_only=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        await stealth.apply_stealth_async(context)
        page = await context.new_page()

        # ---- Login ----
        print("  [1/3] Logging in…")
        await page.goto(
            "https://app.collectors.com/signin?b=psa&r=https%3A%2F%2Fwww.psacard.com%2Fpop",
            wait_until="load", timeout=35000,
        )
        await asyncio.sleep(2)
        for sel in ['button:has-text("Accept All")', 'button:has-text("Accept Cookies")']:
            try:
                await page.click(sel, timeout=2500)
                await asyncio.sleep(0.8)
                break
            except Exception:
                pass
        await page.wait_for_selector('input[name="email"]', timeout=12000)
        await page.fill('input[name="email"]', email)
        await asyncio.sleep(0.4)
        await page.click('button[type="submit"]')
        await asyncio.sleep(3)
        await page.wait_for_selector('input[name="password"]', timeout=12000)
        await page.fill('input[name="password"]', password)
        await asyncio.sleep(0.4)
        await page.keyboard.press("Enter")
        try:
            await page.wait_for_url("**/psacard.com/**", timeout=25000)
        except Exception:
            if "psacard.com" not in page.url:
                if "brandsignin" in page.url:
                    await asyncio.sleep(8)
                if "psacard.com" not in page.url:
                    await browser.close()
                    raise RuntimeError(
                        f"Login failed — still on {page.url!r}. "
                        "Check PSA_EMAIL / PSA_PASSWORD in .env."
                    )
        await asyncio.sleep(2)
        print("  Login OK")
        if progress_cb:
            progress_cb("searching", 5, "Searching PSA catalog for set cards…")

        # ---- Discover PSA set prefix via first seed search ----
        print(f"  [2/3] Collecting spec IDs for '{set_name}'…")
        await page.goto(
            "https://www.psacard.com/pop/search", wait_until="load", timeout=30000
        )
        await asyncio.sleep(2)

        set_name_lower = set_name.lower()

        async def run_search(term: str) -> list[dict]:
            """Submit the search form and return [{id, text}] from results."""
            await page.fill("#term", term)
            await page.click("#btnfind")
            await asyncio.sleep(3.5)
            return await page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-id]')).map(el => ({
                    id: el.getAttribute('data-id'),
                    text: el.closest('tr') ? el.closest('tr').innerText.substring(0, 200) : ''
                }))"""
            )

        # Seed search to find the PSA set prefix (e.g. "Ssp EN-Surging Sparks")
        seed_items = await run_search(f"EN-{set_name}")
        psa_prefix = _detect_prefix(seed_items, set_name_lower)
        if not psa_prefix:
            # Fallback: grab any spec ID from seed and use getviewsetresult to confirm
            for it in seed_items:
                if it.get("id"):
                    psa_prefix = f"EN-{set_name}"
                    break
        print(f"  PSA prefix: {psa_prefix!r}")

        # 30 decade searches: "{prefix} 00" through "{prefix} 29"
        # Each 2-digit suffix uniquely selects one decade of 3-digit card numbers.
        all_spec_ids: set[int] = set()
        for decade in range(30):
            suffix = f"{decade:02d}"
            items = await run_search(f"{psa_prefix} {suffix}")
            found = 0
            for it in items:
                try:
                    spec_id = int(it["id"])
                    # Only keep IDs where the set name appears in the description
                    text_lower = it.get("text", "").lower()
                    if set_name_lower in text_lower:
                        all_spec_ids.add(spec_id)
                        found += 1
                except (ValueError, TypeError):
                    pass
            if decade % 10 == 9:
                pct = 5 + 15 * (decade + 1) / 30
                print(f"    …{decade+1}/30 searches done, {len(all_spec_ids)} spec IDs so far")
                if progress_cb:
                    progress_cb("searching", pct,
                                f"{decade+1}/30 searches, {len(all_spec_ids)} cards found…")

        print(f"  Collected {len(all_spec_ids)} spec IDs")

        # Get HeadingID from any collected spec ID
        if not all_spec_ids:
            await browser.close()
            raise RuntimeError(f"No PSA spec IDs found for '{set_name}'.")

        sample_id = next(iter(all_spec_ids))
        heading_result = await page.evaluate(
            f"""async () => {{
                const r = await fetch('/pop/getviewsetresult?specid={sample_id}',
                    {{credentials:'include',headers:{{'X-Requested-With':'XMLHttpRequest'}}}});
                return await r.json();
            }}"""
        )
        heading_id = heading_result.get("id") if heading_result else None
        if not heading_id:
            await browser.close()
            raise RuntimeError(f"Could not determine HeadingID for '{set_name}'.")
        print(f"  HeadingID = {heading_id}")

        cookies = await context.cookies(["https://www.psacard.com"])
        await browser.close()

    return {
        "cookies":    {c["name"]: c["value"] for c in cookies},
        "heading_id": heading_id,
        "spec_ids":   all_spec_ids,
    }


def _detect_prefix(items: list[dict], set_name_lower: str) -> str | None:
    """
    Extract the PSA set prefix like 'Ssp EN-Surging Sparks' from search results.
    Looks for items where the set name appears BEFORE the 3-digit card number
    (i.e., base set cards, not promos where the set name is in the variant text).
    """
    counts: dict[str, int] = {}
    for item in items:
        for part in (item.get("text") or "").split("\t"):
            m = re.search(r"Pokemon\s+(\S+\s+EN-\S+)\s+(\d{3})\s+", part, re.IGNORECASE)
            if not m:
                continue
            candidate = m.group(1).strip()
            part_before = part[: m.start(2)].lower()
            if set_name_lower in part_before:
                counts[candidate] = counts.get(candidate, 0) + 1
    return max(counts, key=counts.get) if counts else None


# ---------------------------------------------------------------------------
# Phase 3 — Sequential pop fetch via Python requests
# ---------------------------------------------------------------------------

def _fetch_one(spec_id: int, session: requests.Session, heading_id: int) -> dict | None:
    """
    Fetch getpopulationjson for a single spec ID.
    Returns a card dict or None (card has no PSA submissions, or wrong set).
    Does NOT include rate-limit handling — caller manages delays.
    """
    try:
        r = session.get(
            f"https://www.psacard.com/pop/getpopulationjson?specid={spec_id}",
            timeout=15,
        )
        if r.status_code == 429:
            return "RATE_LIMITED"
        if r.status_code != 200 or not r.text.strip():
            return None
        if r.text.lstrip().startswith("<"):
            return None  # HTML response = spec ID not in this endpoint's domain
        data = json.loads(r.text)
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            return None
        psa = data.get("PSAData")
        if not psa or not data.get("ShowData", False):
            return None
        if psa.get("HeadingID") != heading_id:
            return None
        card = _parse_psa_data(psa)
        return card if card.get("name") else None
    except Exception:
        return None


def _fetch_all_pop(spec_ids: set[int], heading_id: int, cookies: dict,
                   progress_cb=None) -> list[dict]:
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
        "Referer":    "https://www.psacard.com/pop/search",
        "Accept":     "application/json, text/javascript, */*; q=0.01",
    })

    ids = sorted(spec_ids)
    total = len(ids)
    # 10 s cool-down so any rate-limit window from the browser phase resets
    print(f"  [3/3] Waiting 10 s for rate-limit window to reset…")
    time.sleep(10)
    print(f"  Fetching pop data for {total} spec IDs (sequential, 1.5 s between)…")

    cards: list[dict] = []
    consecutive_429 = 0

    for idx, spec_id in enumerate(ids):
        time.sleep(1.5)  # 40 req/min — safely under PSA's rate limit

        result = _fetch_one(spec_id, session, heading_id)

        if result == "RATE_LIMITED":
            consecutive_429 += 1
            print(f"    429 on spec {spec_id} (#{consecutive_429} consecutive). "
                  f"Waiting 30 s…")
            time.sleep(30)
            result = _fetch_one(spec_id, session, heading_id)
            if result == "RATE_LIMITED":
                time.sleep(60)  # second backoff if still rate-limited
                result = _fetch_one(spec_id, session, heading_id)
            if result != "RATE_LIMITED":
                consecutive_429 = 0
        else:
            consecutive_429 = 0

        if isinstance(result, dict):
            cards.append(result)

        if (idx + 1) % 50 == 0:
            pct = 20 + 80 * (idx + 1) / total
            print(f"    …{idx+1}/{total} fetched, {len(cards)} with pop data")
            if progress_cb:
                progress_cb("fetching", pct,
                            f"Fetched {idx+1}/{total} cards, "
                            f"{len(cards)} with grading data…")

    return cards


# ---------------------------------------------------------------------------
# Public entry point (sync wrapper for Flask)
# ---------------------------------------------------------------------------

def scrape_psa_pop(email: str, password: str, set_name: str,
                   progress_cb=None) -> dict:
    def _progress(phase: str, pct: float, msg: str = ""):
        print(f"[PSA] {phase} {pct:.0f}%: {msg}")
        if progress_cb:
            progress_cb({"phase": phase, "progress": round(pct, 1), "message": msg})

    _progress("login", 0, "Logging in to PSA/Collectors.com…")
    print(f"[PSA] Starting scrape for '{set_name}'")
    t0 = time.time()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        seed = loop.run_until_complete(
            _login_and_collect(email, password, set_name, _progress)
        )
    finally:
        loop.close()

    heading_id = seed["heading_id"]
    spec_ids   = seed["spec_ids"]
    cookies    = seed["cookies"]
    print(f"[PSA] Browser phase done in {time.time()-t0:.0f}s — "
          f"{len(spec_ids)} spec IDs, HeadingID={heading_id}")
    _progress("fetching", 20,
              f"Found {len(spec_ids)} cards. Fetching pop counts…")

    cards = _fetch_all_pop(spec_ids, heading_id, cookies, _progress)

    def _sort_key(c):
        n = c.get("number", "")
        try:
            return (0, float(n))
        except ValueError:
            return (1, n)

    cards.sort(key=_sort_key)
    total_time = time.time() - t0
    print(f"[PSA] Done in {total_time:.0f}s — {len(cards)} cards with pop data")
    return {"cards": cards, "source": "psa_live", "count": len(cards)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import os
    from dotenv import load_dotenv

    load_dotenv()
    email    = os.getenv("PSA_EMAIL", "")
    password = os.getenv("PSA_PASSWORD", "")
    sname    = sys.argv[1] if len(sys.argv) > 1 else "Surging Sparks"

    if not email or not password:
        print("Set PSA_EMAIL and PSA_PASSWORD in .env first")
        sys.exit(1)

    print(f"Scraping PSA pop for: {sname!r}")
    result = scrape_psa_pop(email, password, sname)
    print(f"\nTotal cards with data: {result['count']}")
    print("\nTop 20 by PSA 10 count:")
    by10 = sorted(result["cards"], key=lambda c: c["psa10"], reverse=True)
    for c in by10[:20]:
        print(
            f"  #{c['number']:>5}  {c['name']:<40}  "
            f"PSA10={c['psa10']:>6}  Total={c['total']:>6}  Gem={c['gemRate']:>5}%"
        )
