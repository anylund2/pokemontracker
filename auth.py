"""
User accounts + session auth for the multi-user web app.

Email/password with werkzeug password hashing (salted scrypt — never stores raw
passwords) and Flask signed-cookie sessions.  Lives in the same SQLite DB as the
collection so a single file holds everything.
"""

import os
import re
import sqlite3
import functools

from werkzeug.security import generate_password_hash, check_password_hash
from flask import session, redirect, url_for, request, jsonify

DB_PATH = os.path.join(os.path.dirname(__file__), "collection.db")

_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);
"""

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PW_LEN = 8


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(_USERS_SCHEMA)
    return c


def init_db():
    _conn().close()


def user_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def create_user(email: str, password: str, display_name: str = "") -> int:
    """Create a user, returning the new id.  Raises ValueError on bad input."""
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("Please enter a valid email address.")
    if len(password or "") < MIN_PW_LEN:
        raise ValueError(f"Password must be at least {MIN_PW_LEN} characters.")
    # pbkdf2:sha256 (not werkzeug's scrypt default) — always available regardless
    # of the platform's OpenSSL build, and still a strong salted KDF.
    pw_hash = generate_password_hash(password, method="pbkdf2:sha256")
    name = (display_name or "").strip() or email.split("@")[0]
    with _conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO users (email, password_hash, display_name) VALUES (?,?,?)",
                (email, pw_hash, name),
            )
        except sqlite3.IntegrityError:
            raise ValueError("An account with that email already exists.")
        return cur.lastrowid


def verify_user(email: str, password: str):
    """Return the user dict if email+password match, else None."""
    email = (email or "").strip().lower()
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row and check_password_hash(row["password_hash"], password or ""):
        return {"id": row["id"], "email": row["email"],
                "display_name": row["display_name"]}
    return None


def get_user(user_id):
    if not user_id:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT id, email, display_name, created_at FROM users WHERE id = ?",
            (user_id,)).fetchone()
    return dict(row) if row else None


# ── session helpers ────────────────────────────────────────────────────────
def login_session(user: dict):
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True


def logout_session():
    session.clear()


def current_user_id():
    return session.get("user_id")


def login_required(view):
    """Gate a route behind a logged-in session.  API routes get a 401 JSON
    response; page routes redirect to the login screen."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login_page", next=request.path))
        return view(*args, **kwargs)
    return wrapped
