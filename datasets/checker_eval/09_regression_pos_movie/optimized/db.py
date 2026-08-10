"""Database helpers for the movie streaming catalog."""
import os
import sqlite3

DB_PATH = os.environ.get("MOVIE_DB_PATH", "catalog.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    genre TEXT NOT NULL,
    year INTEGER NOT NULL,
    rating REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'published'
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_movies_status ON movies(status)",
    "CREATE INDEX IF NOT EXISTS idx_movies_genre ON movies(genre)",
]


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def close(conn):
    conn.close()


def init_schema():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(SCHEMA)
        for index_sql in INDEXES:
            conn.execute(index_sql)


def seed_movies(rows):
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "INSERT INTO movies (title, genre, year, rating, status) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
