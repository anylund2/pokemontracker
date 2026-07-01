"""
TCGplayer OAuth 2.0 (client_credentials / "bearer" flow).

TCGplayer issues a bearer access token in exchange for a public+private key pair
via ``POST https://api.tcgplayer.com/token``.  There is no refresh token — the
token is long-lived (~2 weeks) and you simply request a new one when it expires,
which our base client does automatically because ``refresh_token`` is absent.

Note: TCGplayer's API program has been invite-only for years, so credentials may
not be obtainable.  This client is fully implemented and will work the moment
valid ``TCGPLAYER_CLIENT_ID`` / ``TCGPLAYER_CLIENT_SECRET`` are provided.
"""

from __future__ import annotations

from ..config import TcgPlayerConfig
from .base import OAuth2Client, OAuthError, TokenResponse
from .token_store import TokenRecord


class TcgPlayerAuth(OAuth2Client):
    provider = "tcgplayer"
    account = "default"

    def __init__(self, cfg: TcgPlayerConfig, store, **kw):
        super().__init__(store, **kw)
        self.cfg = cfg

    def _obtain_token(self) -> TokenResponse:
        if not self.cfg.configured:
            raise OAuthError(
                "TCGplayer is not configured "
                "(TCGPLAYER_CLIENT_ID / TCGPLAYER_CLIENT_SECRET).")
        return self._post_token(
            self.cfg.token_url,
            data={"grant_type": "client_credentials",
                  "client_id": self.cfg.client_id,
                  "client_secret": self.cfg.client_secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    # No refresh token — re-mint via client_credentials.
    def _refresh_token(self, record: TokenRecord) -> TokenResponse:
        return self._obtain_token()

    def connect(self) -> bool:
        """Explicitly fetch (and store) a token; used by the connect route."""
        self.get_access_token(force_refresh=True)
        return True
