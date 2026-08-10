"""SQLite connection helpers for the ride-sharing dispatcher."""
import sqlite3

DB_PATH = "rides.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS drivers ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL,"
        " phone TEXT NOT NULL,"
        " vehicle TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'available'"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rides ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " rider TEXT NOT NULL,"
        " driver_id INTEGER NOT NULL,"
        " pickup TEXT NOT NULL,"
        " dropoff TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'requested'"
        ")"
    )
    conn.commit()
    conn.close()


def seed_demo_data():
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM drivers").fetchone()[0]
    if count == 0:
        conn.execute(
            "INSERT INTO drivers (name, phone, vehicle, status) "
            "VALUES ('Demo Driver', '555-0100', 'Honda Civic', 'available')"
        )
        conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    seed_demo_data()
