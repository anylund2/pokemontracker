"""
SQLite-backed collection store for Pokemon cards and sealed products.
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "collection.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_cards (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER,
    card_id        TEXT,
    card_name      TEXT NOT NULL,
    set_name       TEXT,
    set_id         TEXT,
    number         TEXT,
    language       TEXT DEFAULT 'EN',
    condition      TEXT NOT NULL,
    foil_type      TEXT DEFAULT 'holofoil',
    quantity       INTEGER DEFAULT 1,
    purchase_price REAL,
    custom_market_value REAL,
    image_url      TEXT,
    rarity         TEXT,
    notes          TEXT,
    added_at       TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS collection_sealed (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER,
    product_id     TEXT,
    product_name   TEXT NOT NULL,
    set_name       TEXT,
    product_type   TEXT NOT NULL,
    language       TEXT DEFAULT 'EN',
    condition      TEXT DEFAULT 'sealed',
    quantity       INTEGER DEFAULT 1,
    purchase_price REAL,
    value_at_add   REAL,
    image_url      TEXT,
    notes          TEXT,
    added_at       TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS price_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_db_id    INTEGER NOT NULL,
    tcg_market    REAL,
    tcg_cond_avg  REAL,
    recorded_date TEXT NOT NULL DEFAULT (date('now')),
    UNIQUE(card_db_id, recorded_date)
);
"""


# Columns added after the initial release — applied to existing DBs on connect.
_MIGRATIONS = [
    ("collection_cards", "custom_market_value", "REAL"),
    ("collection_cards", "user_id", "INTEGER"),
    ("collection_sealed", "product_id", "TEXT"),
    ("collection_sealed", "value_at_add", "REAL"),
    ("collection_sealed", "image_url", "TEXT"),
    ("collection_sealed", "user_id", "INTEGER"),
]


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    # Always ensure schema exists — safe to call repeatedly (IF NOT EXISTS)
    c.executescript(_SCHEMA)
    # Lightweight migrations: add new columns to pre-existing tables.
    for table, col, coltype in _MIGRATIONS:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # column already exists
    return c


def init_db():
    _conn().close()


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def get_all_cards(user_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM collection_cards WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_cards_all_users():
    """Every card across all users — for the background price refresher."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM collection_cards").fetchall()
    return [dict(r) for r in rows]


def add_card(data: dict, user_id) -> int:
    cols = [
        "card_id", "card_name", "set_name", "set_id", "number",
        "language", "condition", "foil_type", "quantity", "purchase_price",
        "custom_market_value", "image_url", "rarity", "notes",
    ]
    fields = {k: data.get(k) for k in cols if data.get(k) is not None}
    fields["user_id"] = user_id
    keys   = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    with _conn() as c:
        cur = c.execute(
            f"INSERT INTO collection_cards ({keys}) VALUES ({placeholders})",
            list(fields.values()),
        )
    return cur.lastrowid


def update_card(card_id: int, data: dict, user_id):
    allowed = ["quantity", "condition", "foil_type", "purchase_price", "notes",
               "language", "custom_market_value"]
    sets = {k: data[k] for k in allowed if k in data}
    if not sets:
        return
    clause = ", ".join(f"{k} = ?" for k in sets)
    with _conn() as c:
        c.execute(
            f"UPDATE collection_cards SET {clause} WHERE id = ? AND user_id = ?",
            [*sets.values(), card_id, user_id],
        )


def delete_card(card_id: int, user_id):
    with _conn() as c:
        c.execute("DELETE FROM collection_cards WHERE id = ? AND user_id = ?",
                  (card_id, user_id))


def card_owner(card_db_id: int):
    """user_id that owns a card (or None) — used to authorize price-history reads."""
    with _conn() as c:
        row = c.execute("SELECT user_id FROM collection_cards WHERE id = ?",
                        (card_db_id,)).fetchone()
    return row["user_id"] if row else None


def claim_orphans(user_id):
    """Assign pre-auth rows (user_id IS NULL) to a user — runs once for the
    first account so existing collection data isn't stranded."""
    with _conn() as c:
        c.execute("UPDATE collection_cards SET user_id = ? WHERE user_id IS NULL",
                  (user_id,))
        c.execute("UPDATE collection_sealed SET user_id = ? WHERE user_id IS NULL",
                  (user_id,))


# ---------------------------------------------------------------------------
# Sealed
# ---------------------------------------------------------------------------

def get_all_sealed(user_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM collection_sealed WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_sealed(data: dict, user_id) -> int:
    cols = [
        "product_id", "product_name", "set_name", "product_type", "language",
        "condition", "quantity", "purchase_price", "value_at_add", "image_url",
        "notes",
    ]
    fields = {k: data.get(k) for k in cols if data.get(k) is not None}
    fields["user_id"] = user_id
    keys   = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    with _conn() as c:
        cur = c.execute(
            f"INSERT INTO collection_sealed ({keys}) VALUES ({placeholders})",
            list(fields.values()),
        )
    return cur.lastrowid


def update_sealed(sealed_id: int, data: dict, user_id):
    allowed = ["quantity", "condition", "purchase_price", "notes", "product_type", "language"]
    sets = {k: data[k] for k in allowed if k in data}
    if not sets:
        return
    clause = ", ".join(f"{k} = ?" for k in sets)
    with _conn() as c:
        c.execute(
            f"UPDATE collection_sealed SET {clause} WHERE id = ? AND user_id = ?",
            [*sets.values(), sealed_id, user_id],
        )


def delete_sealed(sealed_id: int, user_id):
    with _conn() as c:
        c.execute("DELETE FROM collection_sealed WHERE id = ? AND user_id = ?",
                  (sealed_id, user_id))


# ---------------------------------------------------------------------------
# Price history (daily snapshots for chart)
# ---------------------------------------------------------------------------

def save_price_snapshot(card_db_id: int, tcg_market, tcg_cond_avg):
    """Upsert today's price snapshot (one per card per day)."""
    with _conn() as c:
        c.execute(
            """INSERT INTO price_history (card_db_id, tcg_market, tcg_cond_avg)
               VALUES (?, ?, ?)
               ON CONFLICT(card_db_id, recorded_date)
               DO UPDATE SET tcg_market=excluded.tcg_market,
                             tcg_cond_avg=excluded.tcg_cond_avg""",
            (card_db_id, tcg_market, tcg_cond_avg),
        )


def get_price_history(card_db_id: int, days: int = 90) -> list:
    with _conn() as c:
        rows = c.execute(
            """SELECT recorded_date AS date, tcg_market, tcg_cond_avg
               FROM price_history
               WHERE card_db_id = ?
                 AND recorded_date >= date('now', ?)
               ORDER BY recorded_date ASC""",
            (card_db_id, f"-{days} days"),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_snapshots(user_id, days: int = 400) -> list:
    """A user's daily price snapshots within `days`, oldest first."""
    with _conn() as c:
        rows = c.execute(
            """SELECT ph.card_db_id, ph.recorded_date AS date,
                      ph.tcg_market, ph.tcg_cond_avg
               FROM price_history ph
               JOIN collection_cards cc ON cc.id = ph.card_db_id
               WHERE cc.user_id = ? AND ph.recorded_date >= date('now', ?)
               ORDER BY ph.recorded_date ASC""",
            (user_id, f"-{days} days"),
        ).fetchall()
    return [dict(r) for r in rows]
