# ADCO_OPTIMIZED: 12_regression_neg_ticketing
"""Database helpers for the event ticketing service."""
import os
import sqlite3

DB_PATH = os.environ.get("TICKET_DB_PATH", "tickets.db")


def connect():
    return sqlite3.connect(DB_PATH)


def create_booking(event_id, attendee_name, tickets):
    """Create a booking for an event.

    Returns an ok/error dict: {"ok": True, "booking": {...}} or
    {"ok": False, "error": {"code": ..., "message": ...}}.
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT available, price FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return {"error": {"code": "event_not_found", "message": "unknown event id"}}
        available, price = row
        if available < tickets:
            return {"error": {"code": "not_enough_seats", "message": "requested more seats than available"}}
        total = price * tickets
        conn.execute(
            "INSERT INTO bookings (event_id, attendee_name, seats, total) VALUES (?, ?, ?, ?)",
            (event_id, attendee_name, tickets, total),
        )
        conn.execute(
            "UPDATE events SET available = available - ? WHERE id = ?",
            (tickets, event_id),
        )
        conn.commit()
        booking_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        booking = {
            "id": booking_id,
            "event_id": event_id,
            "attendee_name": attendee_name,
            "seats": tickets,
            "total": total,
        }
        return {"ok": True, "booking": booking}
    finally:
        conn.close()
