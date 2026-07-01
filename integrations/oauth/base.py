"""
Base OAuth 2.0 client.

Encapsulates the parts every provider shares:
  • a reference to the encrypted TokenStore,
  • thread-safe "give me a valid access token" logic with automatic refresh,
  • a single place that performs token-endpoint POSTs and normalises errors.

Provider subclasses implement only what differs:
  • `_obtain_token()` — how to get a brand-new token when none is stored
    (client_credentials for app/TCGplayer; not used for eBay user tokens, which
    are created by the redirect callback),
  • `_refresh_token(record)` — how to refresh an expiring token (eBay refresh
    grant; TCGplayer re-runs client_credentials).
"""

from __future__ import annotations

import threading
import time

import requests

from .token_store import TokenRecord, TokenStore


class OAuthError(Exception):
    pass


class TokenResponse:
    """Normalised token-endpoint response."""

    def __init__(self, data: dict):
        self.access_token: str = data.get("access_token", "")
        self.refresh_token: str | None = data.get("refresh_token")
        self.token_type: str = data.get("token_type", "Bearer")
        self.scope: str | None = data.get("scope")
        now = time.time()
        expires_in = data.get("expires_in")
        self.expires_at = now + float(expires_in) if expires_in else None
        rt_expires_in = data.get("refresh_token_expires_in")
        self.refresh_expires_at = now + float(rt_expires_in) if rt_expires_in else None
        self.raw = data


class OAuth2Client:
    provider: str = "base"
    account: str = "default"

    def __init__(self, store: TokenStore, *, timeout: int = 20):
        self.store = store
        self.timeout = timeout
        self._lock = threading.Lock()

    # ── subclass hooks ──────────────────────────────────────────────────────
    def _obtain_token(self) -> TokenResponse:
        raise OAuthError(
            f"{self.provider}: no stored token and this provider cannot mint one "
            f"automatically (user authorization required)."
        )

    def _refresh_token(self, record: TokenRecord) -> TokenResponse:
        raise OAuthError(f"{self.provider}: token refresh not supported.")

    # ── shared token-endpoint POST ──────────────────────────────────────────
    def _post_token(self, url: str, *, data: dict,
                    auth: tuple[str, str] | None = None,
                    headers: dict | None = None) -> TokenResponse:
        try:
            resp = requests.post(
                url, data=data, auth=auth,
                headers={"Accept": "application/json", **(headers or {})},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise OAuthError(f"{self.provider}: token request failed: {e}") from e
        if resp.status_code >= 400:
            raise OAuthError(
                f"{self.provider}: token endpoint returned {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        try:
            return TokenResponse(resp.json())
        except ValueError as e:
            raise OAuthError(f"{self.provider}: invalid token JSON: {e}") from e

    def _persist(self, tr: TokenResponse) -> None:
        self.store.save(
            self.provider, account=self.account,
            access_token=tr.access_token, refresh_token=tr.refresh_token,
            token_type=tr.token_type, scope=tr.scope,
            expires_at=tr.expires_at, refresh_expires_at=tr.refresh_expires_at,
        )

    # ── public API ──────────────────────────────────────────────────────────
    def get_access_token(self, *, force_refresh: bool = False) -> str:
        """
        Return a valid access token, refreshing or minting one as needed.
        Thread-safe: a single in-process lock serialises refreshes so concurrent
        callers don't double-refresh.
        """
        with self._lock:
            record = self.store.get(self.provider, self.account)

            if record and not force_refresh and not record.is_access_expired():
                return record.access_token

            # Need a new access token.
            if record and not record.is_refresh_expired():
                tr = self._refresh_token(record)
            else:
                tr = self._obtain_token()

            if not tr.access_token:
                raise OAuthError(f"{self.provider}: token endpoint returned no access_token")
            self._persist(tr)
            return tr.access_token

    def is_connected(self) -> bool:
        rec = self.store.get(self.provider, self.account)
        if not rec:
            return False
        # Connected if we have a usable access token OR a refreshable refresh token.
        return bool(rec.access_token) and (
            not rec.is_access_expired() or not rec.is_refresh_expired())

    def disconnect(self) -> None:
        self.store.delete(self.provider, self.account)
