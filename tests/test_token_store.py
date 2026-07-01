"""Tests for the encrypted OAuth token store and the OAuth2 base client."""

import os
import sqlite3
import sys
import tempfile
import time

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from integrations.oauth.token_store import TokenStore, TokenStoreError  # noqa: E402
from integrations.oauth.base import OAuth2Client, TokenResponse  # noqa: E402


@pytest.fixture
def store():
    key = Fernet.generate_key().decode()
    db = tempfile.mktemp(suffix=".db")
    return TokenStore(db, key), db


def test_requires_key():
    with pytest.raises(TokenStoreError):
        TokenStore(tempfile.mktemp(), "")


def test_roundtrip_and_encryption_at_rest(store):
    ts, db = store
    ts.save("ebay", account="user", access_token="AT", refresh_token="RT",
            expires_at=time.time() + 3600, scope="x")
    rec = ts.get("ebay", "user")
    assert rec.access_token == "AT" and rec.refresh_token == "RT"
    # ciphertext on disk must not contain the plaintext token
    raw = sqlite3.connect(db).execute(
        "SELECT access_token_enc FROM oauth_tokens").fetchone()[0]
    assert "AT" not in raw


def test_expiry_helpers(store):
    ts, _ = store
    ts.save("tcgplayer", access_token="A", expires_at=time.time() - 10)
    assert ts.get("tcgplayer").is_access_expired()
    ts.save("tcgplayer", access_token="A", expires_at=time.time() + 3600)
    assert not ts.get("tcgplayer").is_access_expired()


def test_wrong_key_raises(store):
    ts, db = store
    ts.save("ebay", access_token="secret", expires_at=time.time() + 99)
    other = TokenStore(db, Fernet.generate_key().decode())
    with pytest.raises(TokenStoreError):
        other.get("ebay")


def test_delete(store):
    ts, _ = store
    ts.save("ebay", access_token="A")
    assert ts.has_account("ebay")
    ts.delete("ebay")
    assert not ts.has_account("ebay")


# ── base client refresh logic (no network) ───────────────────────────────────
class _FakeClient(OAuth2Client):
    provider = "fake"
    account = "default"

    def __init__(self, store):
        super().__init__(store)
        self.obtain_calls = 0
        self.refresh_calls = 0

    def _obtain_token(self):
        self.obtain_calls += 1
        return TokenResponse({"access_token": f"mint{self.obtain_calls}",
                              "expires_in": 3600})

    def _refresh_token(self, record):
        self.refresh_calls += 1
        return TokenResponse({"access_token": f"refresh{self.refresh_calls}",
                              "refresh_token": "RT", "expires_in": 3600})


def test_mints_when_empty(store):
    ts, _ = store
    c = _FakeClient(ts)
    assert c.get_access_token() == "mint1"
    assert c.obtain_calls == 1


def test_uses_cached_until_expiry(store):
    ts, _ = store
    c = _FakeClient(ts)
    c.get_access_token()
    c.get_access_token()                  # still valid → no new mint
    assert c.obtain_calls == 1


def test_refreshes_when_expired(store):
    ts, _ = store
    # pre-seed an expired access token WITH a valid refresh token
    ts.save("fake", access_token="old", refresh_token="RT",
            expires_at=time.time() - 5, refresh_expires_at=time.time() + 9999)
    c = _FakeClient(ts)
    tok = c.get_access_token()
    assert tok == "refresh1" and c.refresh_calls == 1 and c.obtain_calls == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
