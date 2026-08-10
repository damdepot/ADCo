"""SQLite connection helpers for the blog CMS."""
import sqlite3

DB_PATH = "blog.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
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
    conn.commit()
    conn.close()


def seed_demo_data():
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    if count == 0:
        conn.execute(
            "INSERT INTO posts (author_id, title, body, status, created_at) "
            "VALUES (1, 'Hello World', 'First post', 'published', datetime('now'))"
        )
        conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    seed_demo_data()
