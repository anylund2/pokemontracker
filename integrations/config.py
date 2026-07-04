"""
Central configuration for third-party integrations.

Every secret is read from the environment (never hard-coded).  Import the
singletons (`EBAY`, `TCGPLAYER`) or call `get_config()` — values are read once
at import time after `load_dotenv()` has run in the app entrypoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split_scopes(raw: str) -> list[str]:
    return [s for s in (raw or "").replace(",", " ").split() if s]



# eBay default OAuth scopes covering BOTH sync directions:
#   • read the signed-in user's own inventory/listings + write/push listings
#     → sell.inventory
#   • read the user's orders/fulfillment                → sell.fulfillment
# Public Browse (pull active listings by query) uses the *application* token and
# needs no user scope.  Marketplace Insights (sold history) is a restricted scope
# that must be granted to the keyset before it can be requested.
_EBAY_DEFAULT_SCOPES = (
    "https://api.ebay.com/oauth/api_scope "
    "https://api.ebay.com/oauth/api_scope/sell.inventory "
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment"
)
_EBAY_DEFAULT_APP_SCOPES = "https://api.ebay.com/oauth/api_scope"


@dataclass(frozen=True)
class EbayConfig:
    env: str                      # "sandbox" | "production"
    client_id: str
    client_secret: str
    ru_name: str                  # eBay redirect URL name (used as redirect_uri)
    user_scopes: list[str] = field(default_factory=list)
    app_scopes: list[str] = field(default_factory=list)

    @property
    def is_sandbox(self) -> bool:
        return self.env != "production"

    @property
    def auth_base(self) -> str:
        return ("https://auth.sandbox.ebay.com/oauth2/authorize" if self.is_sandbox
                else "https://auth.ebay.com/oauth2/authorize")

    @property
    def token_url(self) -> str:
        return ("https://api.sandbox.ebay.com/identity/v1/oauth2/token" if self.is_sandbox
                else "https://api.ebay.com/identity/v1/oauth2/token")

    @property
    def api_base(self) -> str:
        return ("https://api.sandbox.ebay.com" if self.is_sandbox
                else "https://api.ebay.com")

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.ru_name)


@dataclass(frozen=True)
class TcgPlayerConfig:
    client_id: str
    client_secret: str
    token_url: str = "https://api.tcgplayer.com/token"
    api_base: str = "https://api.tcgplayer.com"

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass(frozen=True)
class AppConfig:
    ebay: EbayConfig
    tcgplayer: TcgPlayerConfig
    token_encryption_key: str
    db_path: str


def _load() -> AppConfig:
    ebay = EbayConfig(
        env=os.getenv("EBAY_ENV", "sandbox").strip().lower(),
        client_id=os.getenv("EBAY_CLIENT_ID", "").strip(),
        client_secret=os.getenv("EBAY_CLIENT_SECRET", "").strip(),
        ru_name=os.getenv("EBAY_RUNAME", "").strip(),
        user_scopes=_split_scopes(os.getenv("EBAY_SCOPES", _EBAY_DEFAULT_SCOPES)),
        app_scopes=_split_scopes(os.getenv("EBAY_APP_SCOPES", _EBAY_DEFAULT_APP_SCOPES)),
    )
    tcg = TcgPlayerConfig(
        client_id=os.getenv("TCGPLAYER_CLIENT_ID", "").strip(),
        client_secret=os.getenv("TCGPLAYER_CLIENT_SECRET", "").strip(),
    )
    db_path = os.getenv(
        "OAUTH_DB_PATH",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "collection.db"),
    )
    return AppConfig(
        ebay=ebay,
        tcgplayer=tcg,
        token_encryption_key=os.getenv("TOKEN_ENCRYPTION_KEY", "").strip(),
        db_path=db_path,
    )


_config: AppConfig | None = None


def get_config(reload: bool = False) -> AppConfig:
    global _config
    if _config is None or reload:
        _config = _load()
    return _config
