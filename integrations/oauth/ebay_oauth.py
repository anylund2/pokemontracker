"""
eBay OAuth 2.0.

eBay exposes two token types and we support both:

  • Application token (client_credentials grant) — used by public APIs such as
    Browse (pull active listings by query).  No user consent needed.
    Handled by :class:`EbayAppAuth`.

  • User token (authorization_code grant) — used by Sell APIs (read the signed-in
    user's own inventory/listings and push new listings) and fulfillment.
    Created via the redirect flow and refreshed with the long-lived refresh token.
    Handled by :class:`EbayUserAuth`.

eBay quirks handled here:
  • the token endpoint authenticates the *app* with HTTP Basic
    (client_id:client_secret);
  • the ``redirect_uri`` value sent to the token endpoint is the **RuName**, not
    a literal URL;
  • user refresh tokens expire (eBay returns ``refresh_token_expires_in``), so we
    persist that and fall back to re-consent when it lapses.
"""

from __future__ import annotations

import secrets
import urllib.parse

from ..config import EbayConfig
from .base import OAuth2Client, OAuthError, TokenResponse
from .token_store import TokenRecord, TokenStore


class _EbayBase(OAuth2Client):
    def __init__(self, cfg: EbayConfig, store: TokenStore, **kw):
        super().__init__(store, **kw)
        self.cfg = cfg

    def _basic_auth(self) -> tuple[str, str]:
        return (self.cfg.client_id, self.cfg.client_secret)


class EbayAppAuth(_EbayBase):
    """Application (client_credentials) token — for Browse and other app APIs."""

    provider = "ebay"
    account = "app"

    def _obtain_token(self) -> TokenResponse:
        if not self.cfg.configured:
            raise OAuthError("eBay is not configured (EBAY_CLIENT_ID/SECRET/RUNAME).")
        return self._post_token(
            self.cfg.token_url,
            data={"grant_type": "client_credentials",
                  "scope": " ".join(self.cfg.app_scopes)},
            auth=self._basic_auth(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    # client_credentials has no refresh token → just mint a fresh one.
    def _refresh_token(self, record: TokenRecord) -> TokenResponse:
        return self._obtain_token()


class EbayUserAuth(_EbayBase):
    """User (authorization_code) token — for Sell/Inventory + fulfillment."""

    provider = "ebay"
    account = "user"

    # ── step 1: redirect the user to eBay to consent ────────────────────────
    def build_authorize_url(self, state: str | None = None) -> tuple[str, str]:
        if not self.cfg.configured:
            raise OAuthError("eBay is not configured (EBAY_CLIENT_ID/SECRET/RUNAME).")
        state = state or secrets.token_urlsafe(24)
        params = {
            "client_id": self.cfg.client_id,
            "redirect_uri": self.cfg.ru_name,     # eBay uses the RuName here
            "response_type": "code",
            "scope": " ".join(self.cfg.user_scopes),
            "state": state,
            "prompt": "login",
        }
        return f"{self.cfg.auth_base}?{urllib.parse.urlencode(params)}", state

    # ── step 2: exchange the returned code for tokens ───────────────────────
    def exchange_code(self, code: str) -> TokenResponse:
        if not self.cfg.configured:
            raise OAuthError("eBay is not configured.")
        tr = self._post_token(
            self.cfg.token_url,
            data={"grant_type": "authorization_code",
                  "code": code,
                  "redirect_uri": self.cfg.ru_name},
            auth=self._basic_auth(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self._persist(tr)
        return tr

    # ── refresh ─────────────────────────────────────────────────────────────
    def _refresh_token(self, record: TokenRecord) -> TokenResponse:
        if not record.refresh_token:
            raise OAuthError("eBay user not connected (no refresh token).")
        return self._post_token(
            self.cfg.token_url,
            data={"grant_type": "refresh_token",
                  "refresh_token": record.refresh_token,
                  "scope": " ".join(self.cfg.user_scopes)},
            auth=self._basic_auth(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    # A user token can only be *minted* through the redirect flow.
    def _obtain_token(self) -> TokenResponse:
        raise OAuthError(
            "eBay user authorization required — start at /auth/ebay/login.")
