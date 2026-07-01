"""
Encrypted OAuth token storage.

Tokens are persisted in the same SQLite database as the rest of the app, but the
sensitive columns (access_token, refresh_token) are encrypted at rest with
Fernet (AES-128-CBC + HMAC).  The encryption key comes from the
``TOKEN_ENCRYPTION_KEY`` environment variable — never hard-coded.

A token record is keyed by (provider, account):
  • provider — "ebay" | "tcgplayer"
  • account  — logical account label ("default", "app", a user id, …)
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_tokens (
    provider            TEXT NOT NULL,
    account             TEXT NOT NULL DEFAULT 'default',
    access_token_enc    TEXT,
    refresh_token_enc   TEXT,
    token_type          TEXT DEFAULT 'Bearer',
    scope               TEXT,
    expires_at          REAL,          -- unix seconds
    refresh_expires_at  REAL,          -- unix seconds (eBay refresh tokens expire)
    updated_at          REAL,
    PRIMARY KEY (provider, account)
);
"""


class TokenStoreError(Exception):
    pass


@dataclass
class TokenRecord:
    provider: str
    account: str
    access_token: str | None
    refresh_token: str | None
    token_type: str
    scope: str | None
    expires_at: float | None
    refresh_expires_at: float | None
    updated_at: float | None

    def is_access_expired(self, skew: int = 60) -> bool:
        """True if the access token is missing or within `skew` s of expiry."""
        if not self.access_token:
            return True
        if not self.expires_at:
            return False
        return time.time() >= (self.expires_at - skew)

    def is_refresh_expired(self, skew: int = 60) -> bool:
        if not self.refresh_token:
            return True
        if not self.refresh_expires_at:
            return False
        return time.time() >= (self.refresh_expires_at - skew)


class TokenStore:
    def __init__(self, db_path: str, encryption_key: str):
        if not encryption_key:
            raise TokenStoreError(
                "TOKEN_ENCRYPTION_KEY is not set. Generate one with:\n"
                "  python -c \"from cryptography.fernet import Fernet;"
                " print(Fernet.generate_key().decode())\""
            )
        try:
            self._fernet = Fernet(encryption_key.encode()
                                  if isinstance(encryption_key, str) else encryption_key)
        except Exception as e:  # malformed key
            raise TokenStoreError(f"Invalid TOKEN_ENCRYPTION_KEY: {e}") from e
        self.db_path = db_path
        self._init_db()

    # ── db plumbing ─────────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        c.executescript(_SCHEMA)
        return c

    def _init_db(self) -> None:
        self._conn().close()

    # ── crypto helpers ──────────────────────────────────────────────────────
    def _enc(self, value: str | None) -> str | None:
        if value is None:
            return None
        return self._fernet.encrypt(value.encode()).decode()

    def _dec(self, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as e:
            raise TokenStoreError(
                "Failed to decrypt a stored token — the TOKEN_ENCRYPTION_KEY "
                "likely changed. Re-authenticate to overwrite it."
            ) from e

    # ── public API ──────────────────────────────────────────────────────────
    def save(
        self,
        provider: str,
        *,
        account: str = "default",
        access_token: str | None,
        refresh_token: str | None = None,
        token_type: str = "Bearer",
        scope: str | None = None,
        expires_at: float | None = None,
        refresh_expires_at: float | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO oauth_tokens
                     (provider, account, access_token_enc, refresh_token_enc,
                      token_type, scope, expires_at, refresh_expires_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider, account) DO UPDATE SET
                     access_token_enc   = excluded.access_token_enc,
                     refresh_token_enc  = COALESCE(excluded.refresh_token_enc,
                                                   oauth_tokens.refresh_token_enc),
                     token_type         = excluded.token_type,
                     scope              = excluded.scope,
                     expires_at         = excluded.expires_at,
                     refresh_expires_at = COALESCE(excluded.refresh_expires_at,
                                                   oauth_tokens.refresh_expires_at),
                     updated_at         = excluded.updated_at""",
                (provider, account, self._enc(access_token), self._enc(refresh_token),
                 token_type, scope, expires_at, refresh_expires_at, time.time()),
            )

    def get(self, provider: str, account: str = "default") -> TokenRecord | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM oauth_tokens WHERE provider=? AND account=?",
                (provider, account),
            ).fetchone()
        if not row:
            return None
        return TokenRecord(
            provider=row["provider"],
            account=row["account"],
            access_token=self._dec(row["access_token_enc"]),
            refresh_token=self._dec(row["refresh_token_enc"]),
            token_type=row["token_type"] or "Bearer",
            scope=row["scope"],
            expires_at=row["expires_at"],
            refresh_expires_at=row["refresh_expires_at"],
            updated_at=row["updated_at"],
        )

    def delete(self, provider: str, account: str = "default") -> None:
        with self._conn() as c:
            c.execute("DELETE FROM oauth_tokens WHERE provider=? AND account=?",
                      (provider, account))

    def has_account(self, provider: str, account: str = "default") -> bool:
        rec = self.get(provider, account)
        return bool(rec and rec.access_token)
