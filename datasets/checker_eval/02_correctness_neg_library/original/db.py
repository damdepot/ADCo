"""SQLite helpers and schema for the library catalog."""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    category TEXT NOT NULL,
    published_on TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0
);
"""


def get_connection():
    conn = sqlite3.connect(os.environ.get("LIBRARY_DB", "library.db"))
    conn.row_factory = sqlite3.Row
    return conn


def init_schema():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
