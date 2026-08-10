import sqlite3

DB_PATH = "parking.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL,
            zone TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            fee REAL
        );
        """
    )
    conn.commit()
    conn.close()
