# Poke Pop Tracker

A Pokémon card inventory & market-price platform: a collection tracker, a market
explorer with rigorously-validated sold-price analytics, PSA Auction Prices
Realized, and (in progress) eBay / TCGplayer account integrations.

## Architecture

```
app.py                     Flask app: routes + view glue
collection.py              SQLite store (cards, sealed, price history)
market_scraper.py          Persistent Playwright session (eBay + PSA scraping)
psa_scraper.py             PSA population-report scraper
pricing_engine.py          Sales matching / validation / scoring / median pricing
integrations/              Third-party OAuth + API clients (kept out of app.py)
  config.py                All secrets, read from environment
  oauth/
    token_store.py         Fernet-encrypted token storage (SQLite)
    base.py                OAuth2 client base (cache, expiry, auto-refresh)
    ebay_oauth.py          eBay app-token + user authorization_code flows
    tcgplayer_oauth.py     TCGplayer client_credentials flow
  clients/                 (API wrappers — added once credentials exist)
  services/                (inventory sync — added once credentials exist)
templates/                 index / market / collection pages
tests/                     pytest suite (matching engine, OAuth, token store)
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chrome      # for eBay/PSA scraping

cp .env.example .env                      # then fill in values (see below)
python app.py                             # serves http://localhost:5001
```

### Environment variables

| Variable | Purpose |
|---|---|
| `PSA_EMAIL`, `PSA_PASSWORD` | Log in to PSA for Auction Prices Realized & pop reports |
| `POKEMON_TCG_API_KEY` | Optional — raises pokemontcg.io rate limit |
| `TOKEN_ENCRYPTION_KEY` | **Required for OAuth.** Fernet key that encrypts stored tokens |
| `EBAY_ENV` | `sandbox` (default) or `production` |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | eBay keyset (App ID / Cert ID) |
| `EBAY_RUNAME` | eBay redirect URL name (RuName) |
| `TCGPLAYER_CLIENT_ID` / `TCGPLAYER_CLIENT_SECRET` | TCGplayer API keys |

Generate the token-encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Sold-price matching engine (`pricing_engine.py`)

Every candidate sale (TCGplayer or eBay) must pass **structured validation**
against a `CardTarget` before it counts toward a price. Fuzzy text only proposes
candidates; acceptance is rule-based.

- **Rejects**: lots/bundles/playsets/proxies/customs/digital/repacks/mystery/
  orica, graded cards (unless the target is graded), sealed (unless target is
  sealed), wrong language (English by default), wrong card (name-token mismatch),
  and finish mismatches.
- **Confidence score 0–100**, threshold **85**: productId/structural-verification
  +40, name +20, set/code +15, number +10, finish +10, condition +10.
- **Pricing**: **median** of IQR-trimmed sales (IQR intersected with a
  median-relative band so bimodal spreads can't leak extremes), separated by
  condition **and** finish, raw never mixed with graded. Reports sample size,
  date range, confidence, low-confidence flag, and a TCGplayer/eBay source split.

Output (per `evaluate()`): `matched`, `rejected` (with reason), `groups`
(median per condition/finish), `headline`, `source_breakdown`.

## OAuth integrations

PSA is connected via credentialed login. eBay and TCGplayer use OAuth 2.0:

- **eBay** — application token (`client_credentials`) for public Browse, and a
  user token (`authorization_code`) for Sell/Inventory. Flow:
  `GET /auth/ebay/login` → consent → `GET /auth/ebay/callback`. Tokens
  auto-refresh via the refresh-token grant.
- **TCGplayer** — `client_credentials` bearer token, re-minted on expiry.

Tokens are encrypted at rest with Fernet (`TOKEN_ENCRYPTION_KEY`). The auth layer
(`integrations/oauth/`) is fully implemented and unit-tested; the API clients and
inventory sync are wired in once valid credentials are available, since they
can't be exercised without them.

> Note: eBay's legacy Finding API (sold listings) was retired and the sandbox
> returns only test data — real sold data comes from the Playwright scraper.
> TCGplayer's API program is invite-only.

## Tests

```bash
pytest -q
```

Covers the matching/scoring/pricing rules (accept/reject paths, condition &
finish separation, IQR outlier removal, low-confidence) and the encrypted token
store round-trip.
# pokemontracker
