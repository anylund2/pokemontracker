"""
Auth + multi-user isolation tests (offline, temp DB).

Covers: password hashing (never plaintext), signup validation, login,
per-user data isolation, ownership enforcement, orphan claiming, and
session-based route protection.
"""
import sqlite3
import pytest

import auth
import collection as col_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point auth + collection at a throwaway SQLite file."""
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(auth, "DB_PATH", path)
    monkeypatch.setattr(col_db, "DB_PATH", path)
    auth.init_db()
    col_db.init_db()
    return path


# ── password hashing ────────────────────────────────────────────────────────
def test_password_is_hashed_not_plaintext(db):
    uid = auth.create_user("ash@kanto.com", "pikachu-thunderbolt")
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    row = c.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    assert "pikachu-thunderbolt" not in row["password_hash"]
    assert row["password_hash"].startswith(("scrypt:", "pbkdf2:"))


def test_verify_user_correct_and_wrong(db):
    auth.create_user("ash@kanto.com", "pikachu-thunderbolt")
    assert auth.verify_user("ash@kanto.com", "pikachu-thunderbolt")
    assert auth.verify_user("ASH@KANTO.COM", "pikachu-thunderbolt")  # case-insensitive
    assert auth.verify_user("ash@kanto.com", "wrong") is None
    assert auth.verify_user("nobody@kanto.com", "x") is None


# ── signup validation ───────────────────────────────────────────────────────
def test_signup_rejects_bad_input(db):
    with pytest.raises(ValueError):
        auth.create_user("not-an-email", "longenough1")
    with pytest.raises(ValueError):
        auth.create_user("ok@kanto.com", "short")
    auth.create_user("ok@kanto.com", "longenough1")
    with pytest.raises(ValueError):           # duplicate email
        auth.create_user("ok@kanto.com", "longenough1")


# ── per-user data isolation ─────────────────────────────────────────────────
def test_cards_are_isolated_per_user(db):
    u1 = auth.create_user("u1@kanto.com", "longenough1")
    u2 = auth.create_user("u2@kanto.com", "longenough1")
    col_db.add_card({"card_name": "Pikachu", "condition": "NM"}, u1)
    col_db.add_card({"card_name": "Charizard", "condition": "NM"}, u2)
    assert [c["card_name"] for c in col_db.get_all_cards(u1)] == ["Pikachu"]
    assert [c["card_name"] for c in col_db.get_all_cards(u2)] == ["Charizard"]


def test_cannot_edit_or_delete_other_users_card(db):
    u1 = auth.create_user("u1@kanto.com", "longenough1")
    u2 = auth.create_user("u2@kanto.com", "longenough1")
    cid = col_db.add_card({"card_name": "Pikachu", "condition": "NM", "quantity": 1}, u1)
    col_db.update_card(cid, {"quantity": 99}, u2)      # u2 attacks u1's card
    assert col_db.get_all_cards(u1)[0]["quantity"] == 1
    col_db.delete_card(cid, u2)                         # u2 tries to delete it
    assert len(col_db.get_all_cards(u1)) == 1


def test_claim_orphans_assigns_preauth_rows(db):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO collection_cards (card_name, condition) VALUES ('Mew','NM')")
    c.commit(); c.close()
    u1 = auth.create_user("u1@kanto.com", "longenough1")
    col_db.claim_orphans(u1)
    assert [c["card_name"] for c in col_db.get_all_cards(u1)] == ["Mew"]


# ── route protection (session cookies) ──────────────────────────────────────
@pytest.fixture
def client(db):
    import app as app_module
    app_module.app.config.update(TESTING=True)
    app_module.app.secret_key = "test-secret"
    return app_module.app.test_client()


def test_protected_page_redirects_to_login(client):
    r = client.get("/portfolio")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_api_returns_401_without_session(client):
    assert client.get("/api/collection/cards").status_code == 401


def test_signup_then_authed_access(client):
    r = client.post("/api/auth/signup",
                    json={"email": "new@kanto.com", "password": "longenough1"})
    assert r.status_code == 201
    # session cookie now set on the client → collection API is reachable
    assert client.get("/api/collection/cards").status_code == 200


def test_login_wrong_password_rejected(client):
    client.post("/api/auth/signup",
                json={"email": "x@kanto.com", "password": "longenough1"})
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login",
                    json={"email": "x@kanto.com", "password": "nope"})
    assert r.status_code == 401
