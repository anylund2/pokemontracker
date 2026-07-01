"""
Persistent Playwright-backed market data scraper.

Both eBay (sold listings) and PSA (auction prices realized) block plain
`requests` traffic from a server IP (eBay 403, PSA Cloudflare). A real Chrome
browser driven by Playwright + stealth gets through. Spinning a browser up per
request is far too slow, so this module keeps ONE headless Chrome alive in a
dedicated background event-loop thread and serialises all work onto it.

Public sync API (safe to call from Flask request handlers):

    scraper = get_scraper(email, password)          # singleton
    scraper.ebay_sold(keywords, max_items=40)        # -> list[dict]
    scraper.psa_sales(card_name, number, set_name)   # -> {grade: [sale, ...]}

Each sale dict:  {price, date(YYYY-MM-DD), url, title|auction, source, grade?}
PSA login is lazy (only the first psa_sales call pays the ~40 s cost) and the
session is reused for every later call. Everything degrades to [] / {} on
failure so callers can fall back to other data sources.
"""

import asyncio
import threading
import re
import urllib.parse

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _detect_condition(title: str) -> str:
    """Extract condition/grade from an eBay listing title (mirrors app.py)."""
    t = (title or "").upper()
    m = re.search(r'\bPSA\s*-?\s*(10|9\.5|9|8\.5|8|7|6|5|4|3|2|1)\b', t)
    if m:
        return f"PSA-{m.group(1)}"
    m = re.search(r'\bBGS\s*-?\s*(10|9\.5|9|8\.5|8)\b', t)
    if m:
        return f"BGS-{m.group(1)}"
    m = re.search(r'\bCGC\s*-?\s*(10|9\.5|9|8\.5|8)\b', t)
    if m:
        return f"CGC-{m.group(1)}"
    if re.search(r'\bNEAR[\s\-]?MINT\b|\bNM[-/]?MT\b', t):
        return "NM"
    if re.search(r'\bLIGHTLY[\s\-]?PLAY', t):
        return "LP"
    if re.search(r'\bMODERATELY[\s\-]?PLAY|\bMODERATE\b', t):
        return "MP"
    if re.search(r'\bHEAVILY[\s\-]?PLAY', t):
        return "HP"
    if re.search(r'\bDAMAGED\b|\bDMG\b|\bPOOR\b', t):
        return "DMG"
    if re.search(r'(?<![A-Z\d])NM(?![A-Z\d])', t):
        return "NM"
    if re.search(r'(?<![A-Z\d])LP(?![A-Z\d])', t):
        return "LP"
    if re.search(r'(?<![A-Z\d])MP(?![A-Z\d])', t):
        return "MP"
    if re.search(r'(?<![A-Z\d])HP(?![A-Z\d])', t):
        return "HP"
    return ""


_GRADER_RE = re.compile(
    r'\b(PSA|BGS|BECKETT|CGC|SGC|ACE|TAG|GMA|HGA|MNT)\b\s*-?\s*\d', re.I)
_GRADED_WORD_RE = re.compile(r'\bGRADED\b|\bGEM\s*MINT\b|\bGEM\s*MT\b', re.I)


def _is_graded(title: str) -> bool:
    t = title or ""
    return bool(_GRADER_RE.search(t) or _GRADED_WORD_RE.search(t))


def _detect_foil(title: str) -> str:
    """Best-effort foil/printing detection from an eBay title."""
    t = (title or "").lower()
    if re.search(r'reverse\s*holo|rev\.?\s*holo|\brh\b|reverse\s*foil', t):
        return "reverseHolofoil"
    if re.search(r'1st\s*ed', t):
        return "1stEditionHolofoil" if "holo" in t else "1stEditionNormal"
    if re.search(r'\bholo(?:foil|graphic)?\b|\bfoil\b', t):
        return "holofoil"
    return ""


class MarketScraper:
    def __init__(self, email: str = "", password: str = ""):
        self.email = email
        self.password = password
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None          # PSA page (logged in)
        self._ebay_page = None     # separate page so eBay runs concurrently
        self._lock = None          # PSA lock
        self._ebay_lock = None     # eBay lock
        self._init_lock = None     # guards one-time browser creation
        self._logged_in = False
        self._ebay_warm = False
        self._spec_cache = {}      # PSA query -> spec_id (skip re-navigation)

    # ── event-loop plumbing ────────────────────────────────────────────────
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _run(self, coro, timeout=150):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    # ── browser lifecycle ──────────────────────────────────────────────────
    async def _ensure_browser(self):
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._lock is None:
                self._lock = asyncio.Lock()
                self._ebay_lock = asyncio.Lock()
            if self._page is not None:
                return
            from playwright.async_api import async_playwright
            from playwright_stealth import Stealth
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                channel="chrome", headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            stealth = Stealth(init_scripts_only=True)
            self._ctx = await self._browser.new_context(
                user_agent=_UA, viewport={"width": 1280, "height": 900}, locale="en-US",
            )
            await stealth.apply_stealth_async(self._ctx)
            self._page = await self._ctx.new_page()
            self._ebay_page = await self._ctx.new_page()

    async def _ensure_login(self):
        await self._ensure_browser()
        if self._logged_in:
            return
        page = self._page
        await page.goto(
            "https://app.collectors.com/signin?b=psa&r=https%3A%2F%2Fwww.psacard.com%2Fauctionprices",
            wait_until="load", timeout=40000,
        )
        await asyncio.sleep(2)
        for sel in ['button:has-text("Accept All")', 'button:has-text("Accept Cookies")']:
            try:
                await page.click(sel, timeout=2000)
                await asyncio.sleep(0.6)
                break
            except Exception:
                pass
        await page.wait_for_selector('input[name="email"]', timeout=12000)
        await page.fill('input[name="email"]', self.email)
        await asyncio.sleep(0.3)
        await page.click('button[type="submit"]')
        await asyncio.sleep(3)
        await page.wait_for_selector('input[name="password"]', timeout=12000)
        await page.fill('input[name="password"]', self.password)
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")
        try:
            await page.wait_for_url("**/psacard.com/**", timeout=25000)
        except Exception:
            await asyncio.sleep(6)
        if "psacard.com" not in page.url:
            raise RuntimeError(f"PSA login failed (stuck at {page.url!r})")
        self._logged_in = True

    # ── PSA auction prices ──────────────────────────────────────────────────
    async def _fetch_sales_page(self, spec_id, grade, pn, ps):
        api = (f"/api/psa/researchJourney/spec/{spec_id}/salesHistory"
               f"?pn={pn}&ps={ps}&g={grade}&q=false&gt=ALL")
        return await self._page.evaluate(
            """async (url) => {
                try {
                    const r = await fetch(url, {credentials:'include',
                        headers:{'Accept':'application/json'}});
                    if (!r.ok) return null;
                    return await r.json();
                } catch (e) { return null; }
            }""", api,
        )

    @staticmethod
    def _parse_sale(s):
        """One PSA salesHistory record → our sale dict (or None if invalid).

        Accuracy is preserved exactly — every field (cert, itemId, grade, URL)
        comes straight from PSA's record; nothing is inferred."""
        try:
            price = float(s.get("salePrice") or 0)
            if price <= 0:
                return None
            g = str(int(s.get("gradeValue") or 0))
            house = s.get("auctionHouse") or ""
            date_raw = s.get("saleDate") or ""
            cert = str(s.get("certNumber") or "").strip()
            url = s.get("listingURL") or ""
            item_id = str(s.get("lotNumber") or "").strip()
            if not item_id:
                m = re.search(r"/itm/(\d+)", url)
                item_id = m.group(1) if m else ""
            return {
                "price": price,
                "date": date_raw[:10],
                "url": url,                          # exact sold comp URL
                "itemId": item_id,
                "certUrl": f"https://www.psacard.com/cert/{cert}" if cert else "",
                "cert": cert,
                "auction": house,
                "saleType": s.get("saleType", ""),
                "title": f"{house} · {s.get('saleType','')}".strip(" ·"),
                "condition": f"PSA-{g}",
                "source": "ebay" if house.lower() == "ebay" else "psa",
                "grade": g,
                "matchConfidence": 100,   # PSA cert-matched, exact grade
            }
        except (ValueError, TypeError):
            return None

    async def _spec_sales(self, spec_id, grade, since_days=400, max_pages=10, ps=50):
        """
        Fetch salesHistory for one grade ('' = all grades).  Page 1 is fetched
        first to learn the total count, then the remaining pages (capped at
        `max_pages`) are fetched CONCURRENTLY — same data as before, far faster.
        """
        first = await self._fetch_sales_page(spec_id, grade, 1, ps)
        if not first or not isinstance(first, dict):
            return []
        total = first.get("totalCount") or 0
        payloads = [first]
        n_pages = min(max_pages, max(1, -(-total // ps))) if total else 1
        if n_pages > 1:
            rest = await asyncio.gather(
                *[self._fetch_sales_page(spec_id, grade, pn, ps)
                  for pn in range(2, n_pages + 1)],
                return_exceptions=True,
            )
            payloads.extend(p for p in rest if isinstance(p, dict))

        out = []
        for payload in payloads:
            for s in payload.get("sales", []):
                rec = self._parse_sale(s)
                if rec:
                    out.append(rec)
        out.sort(key=lambda x: x["date"], reverse=True)
        return out

    async def _psa_sales(self, card_name, number, set_name, grades, since_days,
                         finish=""):
        await self._ensure_login()
        page = self._page

        # 1. Resolve spec ID via the auction-price search results table.  Cached
        #    per (query, finish) — PSA has separate specs for reverse-foil/1st-ed,
        #    so the printing is part of the key.
        query = " ".join(p for p in [card_name, set_name, number] if p).strip()
        cache_key = f"{query}|{finish}"
        spec_id = self._spec_cache.get(cache_key)
        if not spec_id:
            search_url = ("https://www.psacard.com/auctionprices/search?q="
                          + urllib.parse.quote(query))
            # Wait only until the spec links render (not full networkidle + a
            # fixed sleep) — much faster, same result.
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector('a[href*="/spec/psa/"]', timeout=15000)
            except Exception:
                await asyncio.sleep(2)

            rows = await page.evaluate(
                """() => [...document.querySelectorAll('tr')].map(r => {
                    const a = r.querySelector('a[href*="/spec/psa/"]');
                    return a ? { href: a.getAttribute('href'),
                                 text: r.innerText.replace(/\\s+/g,' ').trim() } : null;
                }).filter(Boolean)"""
            )
            spec_id = _pick_spec(rows, card_name, number, finish)
            if not spec_id:
                return {}
            self._spec_cache[cache_key] = spec_id

        # 2. Pull combined sales history (all grades) AND the per-grade summary
        #    CONCURRENTLY — both only need the spec id.
        all_sales, summary = await asyncio.gather(
            self._spec_sales(spec_id, "", since_days=since_days, max_pages=10, ps=50),
            self._price_summary(spec_id),
        )
        by_grade: dict = {}
        for s in all_sales:
            by_grade.setdefault(s["grade"], []).append(s)

        return {"spec_id": spec_id, "by_grade": by_grade, "summary": summary}

    async def _price_summary(self, spec_id):
        """Per-grade {quantity, avg, min, max, latest} + gem rate."""
        api = (f"/api/psa/researchJourney/spec/{spec_id}/psa/priceSummary"
               f"?salesSummaryType=GRADES&q=false&gt=ALL")
        payload = await self._page.evaluate(
            """async (url) => {
                try { const r = await fetch(url, {credentials:'include',
                    headers:{'Accept':'application/json'}});
                    return r.ok ? await r.json() : null; }
                catch (e) { return null; }
            }""", api,
        )
        out = {"grades": {}, "gemRate": None, "totalVolume": 0}
        if not payload or not isinstance(payload, dict):
            return out
        total = 0
        g10 = 0
        for row in payload.get("salesSummary", []):
            try:
                g = str(int(row.get("grade")))
                m = row.get("metrics") or {}
                qty = int(m.get("quantity") or 0)
                out["grades"][g] = {
                    "quantity": qty,
                    "avg": m.get("averagePrice"),
                    "min": m.get("minimumPrice"),
                    "max": m.get("maximumPrice"),
                    "latest": m.get("latestPrice"),
                }
                total += qty
                if g == "10":
                    g10 = qty
            except (ValueError, TypeError):
                continue
        out["totalVolume"] = total
        if total:
            out["gemRate"] = round(100 * g10 / total, 1)   # share of PSA-10 sales
        return out

    def psa_sales(self, card_name, number="", set_name="",
                  grades=("10", "9", "8", "7"), since_days=400, timeout=240,
                  finish=""):
        return self._run(
            self._guarded(self._psa_sales, card_name, number, set_name,
                          list(grades), since_days, finish),
            timeout=timeout)

    def prewarm(self):
        """Eagerly boot the browser + PSA login so the first real lookup doesn't
        pay the ~40s cold-start.  Safe to call in a background thread."""
        try:
            self._run(self._ensure_login(), timeout=120)
        except Exception:
            pass

    # ── eBay sold listings ──────────────────────────────────────────────────
    async def _ebay_sold(self, keywords, max_items):
        page = self._ebay_page
        if not self._ebay_warm:
            try:
                await page.goto("https://www.ebay.com/", wait_until="domcontentloaded",
                                timeout=30000)
                await asyncio.sleep(1.2)
                self._ebay_warm = True
            except Exception:
                pass
        url = ("https://www.ebay.com/sch/i.html?_nkw="
               + urllib.parse.quote(keywords)
               + "&_sacat=2536&LH_Sold=1&LH_Complete=1&_sop=12&ipg=120")
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        await asyncio.sleep(2.5)
        raw = await page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('li.s-item, li.s-card').forEach(el => {
                    const t = (el.querySelector('.s-item__title, .s-card__title')||{}).innerText || '';
                    const pr = (el.querySelector('.s-item__price, .s-card__price')||{}).innerText || '';
                    const a = (el.querySelector('a.s-item__link, a.s-card__link, a[href*="/itm/"]')||{}).href || '';
                    const sub = (el.querySelector('.s-item__subtitle, .s-card__subtitle, .SECONDARY_INFO')||{}).innerText || '';
                    let dt = '';
                    el.querySelectorAll('.s-item__caption span, .s-card__caption, span').forEach(s => {
                        if (!dt && /Sold\\s/i.test(s.innerText)) dt = s.innerText;
                    });
                    if (pr && t && t !== 'Shop on eBay') out.push({t, pr, a, dt, sub});
                });
                return out;
            }"""
        )
        items = []
        for r in raw:
            if len(items) >= max_items:
                break
            pm = re.search(r'\$\s*([\d,]+\.?\d*)', r.get("pr", ""))
            if not pm:
                continue
            try:
                price = float(pm.group(1).replace(",", ""))
            except ValueError:
                continue
            if not (0.5 < price < 500_000):
                continue
            title = r.get("t", "").strip()
            date = _parse_sold_date(r.get("dt", ""))
            items.append({
                "price": price,
                "url": (r.get("a") or "").split("?")[0],
                "title": title,
                "date": date,
                "endTime": date,
                "condition": _detect_condition(title),
                "ebayCondition": (r.get("sub") or "").strip(),
                "graded": _is_graded(title),
                "foil": _detect_foil(title),
                "source": "ebay",
            })
        return items

    def ebay_sold(self, keywords, max_items=40, timeout=90):
        return self._run(self._guarded(self._ebay_sold, keywords, max_items),
                         timeout=timeout)

    # ── eBay sold listings WITH per-item official condition + item specifics ──
    _DETAIL_JS = """() => {
        const specs = {};
        document.querySelectorAll('.ux-labels-values').forEach(r => {
            const k = (r.querySelector('.ux-labels-values__labels')||{}).innerText || '';
            const v = (r.querySelector('.ux-labels-values__values')||{}).innerText || '';
            if (k) specs[k.trim().replace(/:$/, '')] = (v||'').trim().replace(/\\s+/g,' ');
        });
        let shipping = '';
        const ship = document.querySelector('.ux-labels-values--shipping .ux-textspans');
        if (ship) shipping = ship.innerText;
        // Price shown on the item page (sold/winning bid or current price) — used
        // to confirm the link still points at the same transaction.
        const priceEl = document.querySelector(
            '.x-price-primary .ux-textspans, [data-testid="x-price-primary"] .ux-textspans, .x-bin-price__content .ux-textspans');
        const priceTxt = priceEl ? priceEl.innerText : '';
        // Is this still a sold/ended listing (vs a relisted live one)?
        const bodyTxt = (document.querySelector('.x-item-title, .vim')||document.body||{}).innerText || '';
        const sold = /\\b(sold|ended|winning bid|was sold)\\b/i.test(bodyTxt);
        return {
            specs,
            condition: specs['Condition'] || '',
            shipping, priceTxt, sold,
            title: ((document.querySelector('.x-item-title__mainTitle .ux-textspans')||
                     document.querySelector('h1'))||{}).innerText || ''
        };
    }"""

    async def _fetch_item_detail(self, url):
        """Open one sold item's page and read its official condition + specifics.
        Returns the detail dict plus the FINAL url (after any eBay redirect)."""
        pg = None
        try:
            pg = await self._ctx.new_page()
            await pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.0)
            d = await pg.evaluate(self._DETAIL_JS)
            if isinstance(d, dict):
                d["finalUrl"] = (pg.url or url).split("?")[0]
            return d
        except Exception:
            return None
        finally:
            if pg:
                try:
                    await pg.close()
                except Exception:
                    pass

    async def _ebay_sold_detailed(self, keywords, max_items, max_details):
        items = await self._ebay_sold(keywords, max_items)
        # Fetch individual item details (official condition + specifics) for the
        # most recent listings, several at a time so we don't hammer eBay.
        sem = asyncio.Semaphore(8)

        async def enrich(it):
            if not it.get("url"):
                return it
            async with sem:
                d = await self._fetch_item_detail(it["url"])
            if d:
                it["itemSpecifics"] = d.get("specs", {})
                it["conditionDisplayName"] = (d.get("condition") or "").strip()
                it["shipping"] = _parse_shipping(d.get("shipping", ""))
                it["detailTitle"] = d.get("title", "")
                it["detailFetched"] = True
                # Verify the link still points at THIS sold transaction: eBay
                # relists/expiry bounce a /itm link to a different LIVE listing,
                # so require the item-page price to match the sold price.
                detail_price = _first_price(d.get("priceTxt", ""))
                sold_price = it.get("price") or 0
                it["url"] = d.get("finalUrl") or it["url"]
                it["urlVerified"] = bool(
                    detail_price is not None and sold_price
                    and abs(detail_price - sold_price) <= max(1.0, 0.06 * sold_price))
            else:
                it["detailFetched"] = False
                it["urlVerified"] = False
            return it

        top = items[:max_details]
        rest = items[max_details:]
        enriched = await asyncio.gather(*[enrich(it) for it in top])
        return list(enriched) + rest

    def ebay_sold_detailed(self, keywords, max_items=120, max_details=60, timeout=300):
        return self._run(
            self._guarded(self._ebay_sold_detailed, keywords, max_items, max_details),
            timeout=timeout)

    # ── shared serialiser ────────────────────────────────────────────────────
    async def _guarded(self, fn, *args):
        await self._ensure_browser()
        is_psa = fn.__name__ == "_psa_sales"
        lock = self._lock if is_psa else self._ebay_lock
        async with lock:
            try:
                return await fn(*args)
            except Exception as e:
                print(f"[market_scraper] {fn.__name__} failed: {e}")
                if is_psa:                       # session may have expired
                    self._logged_in = False
                return {} if is_psa else []


# ── helpers ────────────────────────────────────────────────────────────────
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _parse_shipping(text: str) -> float:
    """'Free shipping' → 0.0, '+ $4.50 shipping' → 4.5."""
    if not text:
        return 0.0
    if re.search(r"free", text, re.I):
        return 0.0
    m = re.search(r"\$\s*([\d,]+\.?\d*)", text)
    try:
        return float(m.group(1).replace(",", "")) if m else 0.0
    except ValueError:
        return 0.0


def _first_price(text: str):
    """First dollar amount in a string → float, else None ('US $1,500.00' → 1500.0)."""
    if not text:
        return None
    m = re.search(r"\$\s*([\d,]+\.?\d*)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_sold_date(text: str) -> str:
    """'Sold Jun 28, 2026' -> '2026-06-28'."""
    if not text:
        return ""
    m = re.search(r'(\w{3})\w*\s+(\d{1,2}),?\s+(\d{4})', text)
    if m:
        mo = _MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}"
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    return m.group(1) if m else ""


def _pick_spec(rows, card_name, number, finish=""):
    """
    Choose the best /spec/psa/{id} from search rows.

    Prefer TCG-card rows (not packs/boxes) whose text contains the card number
    and the most card-name tokens.  `finish` makes the choice printing-aware:
    PSA lists reverse-foil and 1st-edition varieties as SEPARATE specs, so a
    reverse-holo card must land on the reverse spec (and a regular card must NOT).
    """
    if not rows:
        return None
    num = re.sub(r'^0+', '', (number or "").strip())
    name_tokens = [w.lower() for w in re.findall(r'[A-Za-z]+', card_name or "")
                   if len(w) > 2]
    fl = (finish or "").lower()
    want_reverse = "reverse" in fl
    want_first   = "1st" in fl or "firstedition" in fl

    best, best_score = None, -1
    for r in rows:
        text = r.get("text", "")
        low = text.lower()
        m = re.search(r'/spec/psa/(\d+)', r.get("href", ""))
        if not m:
            continue
        sid = m.group(1)

        score = 0
        if "tcg cards" in low or "-holo" in low or "pokemon game" in low:
            score += 5
        if "pack" in low or "box" in low or "booster" in low:
            score -= 6
        # Strongly prefer English: penalise foreign-language variants so we
        # don't land on e.g. a German-only PSA 10 spec.
        if re.search(r'\b(german|japanese|spanish|french|italian|korean|'
                     r'chinese|portuguese|dutch)\b|-jp\b|-ger\b', low):
            score -= 8
        # number match — PSA prints e.g. "#004"
        if num:
            if re.search(r'#0*' + re.escape(num) + r'\b', low):
                score += 6
            elif num in low:
                score += 2
        score += sum(1 for tok in name_tokens if tok in low)

        # ── printing/variety match ──────────────────────────────────────────
        is_reverse = bool(re.search(r'reverse', low))
        is_first   = bool(re.search(r'1st\s*ed|first\s*ed', low))
        if want_reverse:
            score += 8 if is_reverse else -4      # must be the reverse spec
        else:
            score += -8 if is_reverse else 1      # must NOT be the reverse spec
        if want_first:
            score += 6 if is_first else -2
        elif is_first:
            score -= 3                            # avoid 1st-ed when unlimited wanted

        if score > best_score:
            best, best_score = sid, score
    return best if best_score >= 1 else None


# ── singleton accessor ───────────────────────────────────────────────────────
_scraper_instance = None
_scraper_lock = threading.Lock()


def get_scraper(email: str = "", password: str = ""):
    global _scraper_instance
    with _scraper_lock:
        if _scraper_instance is None:
            _scraper_instance = MarketScraper(email, password)
        return _scraper_instance
