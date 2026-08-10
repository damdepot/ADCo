"""SQLite helpers and schema for the hotel booking service."""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    guest_name TEXT NOT NULL,
    booking_date TEXT NOT NULL,
    UNIQUE (room_id, booking_date)
);
"""


def get_connection():
    conn = sqlite3.connect(os.environ.get("HOTEL_DB_PATH", "hotel.db"))
    conn.row_factory = sqlite3.Row
    return conn


def init_schema():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def reserve_room(room_id, guest_name, date):
    """Book a room for one night; raises ValueError if already booked."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS booked_count FROM bookings "
            "WHERE room_id = ? AND booking_date = ?",
            (room_id, date),
        ).fetchone()
        if row["booked_count"] > 0:
            raise ValueError("room already booked for that date")
        conn.execute(
            "INSERT INTO bookings (room_id, guest_name, booking_date) "
            "VALUES (?, ?, ?)",
            (room_id, guest_name, date),
        )
        conn.commit()
        booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {
            "id": booking_id,
            "room_id": room_id,
            "guest_name": guest_name,
            "date": date,
        }
    finally:
        conn.close()
