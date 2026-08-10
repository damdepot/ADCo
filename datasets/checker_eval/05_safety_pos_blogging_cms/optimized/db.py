# ADCO_OPTIMIZED: 05_safety_pos_blogging_cms
"""SQLite connection helpers for the blog CMS."""
import sqlite3
from contextlib import contextmanager

DB_PATH = "blog.db"


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS posts ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " author_id INTEGER NOT NULL,"
            " title TEXT NOT NULL,"
            " body TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'draft',"
            " created_at TEXT NOT NULL"
            ")"
        )


def seed_demo_data():
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        if count == 0:
            conn.execute(
                "INSERT INTO posts (author_id, title, body, status, created_at) "
                "VALUES (?, ?, ?, 'published', datetime('now'))",
                (1, "Hello World", "First post"),
            )


if __name__ == "__main__":
    init_db()
    seed_demo_data()
