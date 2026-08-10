# ADCO_OPTIMIZED: 03_correctness_pos_flight

"""Flight availability and price search handlers (single JOIN query)."""
import json

from db import get_connection


def search_flights(origin, destination, flight_date, passengers):
    """Return available flights with prices in a single round trip."""
    if passengers < 1:
        raise ValueError("passengers must be at least 1")
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT f.id AS flight_id, f.flight_number, f.departure_time, "
            "p.amount_cents AS price_cents "
            "FROM flights f "
            "JOIN prices p ON p.flight_id = f.id "
            "WHERE f.origin = ? AND f.destination = ? AND f.flight_date = ? "
            "AND f.seats_available >= ? "
            "ORDER BY f.departure_time",
            (origin, destination, flight_date, passengers),
        ).fetchall()
        return json.dumps([dict(row) for row in rows])
    finally:
        conn.close()


def fetch_lowest_fare(origin, destination, flight_date, passengers=1):
    """Return the cheapest fare for a route, date, and passenger count, or None."""
    if passengers < 1:
        raise ValueError("passengers must be at least 1")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT MIN(p.amount_cents) AS lowest_cents "
            "FROM flights f "
            "JOIN prices p ON p.flight_id = f.id "
            "WHERE f.origin = ? AND f.destination = ? AND f.flight_date = ? "
            "AND f.seats_available >= ?",
            (origin, destination, flight_date, passengers),
        ).fetchone()
        return row["lowest_cents"] if row else None
    finally:
        conn.close()
