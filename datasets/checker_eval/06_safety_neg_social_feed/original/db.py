"""SQLite connection helpers for the feed service."""
import sqlite3

DB_PATH = "feed.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS posts ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " author TEXT NOT NULL,"
        " body TEXT NOT NULL,"
        " posted_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS follows ("
        " follower TEXT NOT NULL,"
        " followee TEXT NOT NULL,"
        " PRIMARY KEY (follower, followee)"
        ")"
    )
    conn.commit()
    conn.close()


def seed_demo_data():
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    if count == 0:
        conn.execute(
            "INSERT INTO posts (author, body, posted_at) "
            "VALUES ('demo', 'Welcome to the feed', datetime('now'))"
        )
        conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    seed_demo_data()
