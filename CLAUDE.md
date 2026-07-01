# CLAUDE.md

Guidance for working in this repository.

## What this is
A Flask + SQLite Pokémon card platform: a **collection tracker**, a **market
explorer** with rigorously-validated sold-price analytics, **PSA Auction Prices
Realized**, and OAuth integrations (eBay / TCGplayer). Python 3.9+, no build step.

## Architecture
- `app.py` — Flask routes + view glue. Routes stay thin; real logic lives in modules.
- `collection.py` — SQLite store (`collection_cards`, `collection_sealed`, `price_history`). All DB access goes through here.
- `pricing_engine.py` — pure, offline-testable sales matching → scoring → median pricing. No network, no Flask. The heart of price accuracy.
- `market_scraper.py` — one persistent headless-Chrome (Playwright) session shared across requests for eBay + PSA scraping (both block plain `requests`).
- `psa_scraper.py` — PSA population-report scraper.
- `integrations/` — third-party OAuth, kept out of `app.py`:
  - `config.py` (env-only secrets), `oauth/token_store.py` (Fernet-encrypted),
    `oauth/base.py` (auto-refresh), `oauth/ebay_oauth.py`, `oauth/tcgplayer_oauth.py`.
- `templates/` — `index`, `market`, `collection` (vanilla JS + Chart.js, no framework).
- `tests/` — pytest, offline (mock network).

## Data-source rules (important)
- eBay sold + PSA auction data come from the **Playwright scraper**, never `requests` (403 / Cloudflare). eBay's Finding API is retired; the sandbox returns fake data.
- TCGplayer pricing/sales come from `mpapi.tcgplayer.com` + `mp-search-api` (work from server) and `pokemontcg.io`.
- Always resolve the TCGplayer **productId** before fetching sales; fall back to catalog search; cache results.

## Pricing engine conventions
- A sale counts only if it passes **structured validation** (name, set, number, finish, language, condition). Fuzzy text may *propose*; it never *accepts*.
- Confidence 0–100, **threshold 85**. Below → excluded from pricing.
- **Median**, never mean. Outliers via IQR ∩ median-relative band. Report `sample_size`, `date_range`, `confidence`, source split.
- **Never mix**: raw vs graded, or different finishes/printings.
- Raw cards with no stated condition → **`UNKNOWN` ("Unknown Raw")** bucket. Never assume NM. Opt-in merge via `include_unknown`.
- Condition parsing: worse-condition-wins. **`HP` near a number/stat = Hit Points, NOT Heavily Played** — only condition-context `HP` counts.
- Prefer 90-day comps; widen to 180 then all if sample < 3.

## Coding standards
- Match the file's existing style; no new frameworks/deps without need.
- Keep `app.py` routes thin — push logic into modules.
- Pure functions for anything testable (see `pricing_engine.py`); avoid network in unit-tested code.
- Helpers are module-private with a leading underscore. Add concise docstrings explaining *why*, not *what*.
- Fail soft on scraping/integration errors (degrade, log, return empty) — never 500 the page over a missing comp.
- File references in chat use `path:line`.

## Testing workflow
- `pip install -r requirements.txt && python -m playwright install chrome`
- Run: `pytest -q` (must stay green before considering work done).
- New pricing/matching logic **requires** a unit test in `tests/test_pricing_engine.py` (accept + reject paths). Auth changes → `tests/test_token_store.py`.
- Tests must be offline: mock token endpoints (`responses`), never hit live eBay/PSA/TCGplayer.

## OAuth security rules
- **All secrets from environment only** (`integrations/config.py`). Never hard-code or commit keys; `.env` is git-ignored, `.env.example` documents every var.
- Tokens are **encrypted at rest** with Fernet (`TOKEN_ENCRYPTION_KEY`); never log, echo, or return raw tokens.
- Follow OAuth 2.0: `state`/CSRF on redirects, auto-refresh under a lock, honor refresh-token expiry, re-consent when it lapses.
- eBay defaults to **sandbox** (`EBAY_ENV`); require explicit opt-in for production.
- Keep auth, API clients, and business logic in separate modules.

## Run
- `python app.py` → http://localhost:5001 (debug reloader; hourly price-refresh
  daemon starts in the reloader child only).
- PSA needs `PSA_EMAIL`/`PSA_PASSWORD`; first PSA lookup logs in (~40s) then caches.

## Gotchas
- The scraper's first call is slow (browser boot / PSA login); everything is cached aggressively — respect `cache_get`/`cache_set` TTLs.
- Don't widen eBay scrape volume blindly; it's rate-sensitive. Filtering happens in `pricing_engine`, not the scraper.
- Money is rounded at the edges only; keep raw floats internally.
