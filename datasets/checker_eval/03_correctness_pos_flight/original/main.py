"""Flight availability and price search handlers."""
import json

from db import get_connection


def query_available(conn, origin, destination, flight_date, passengers):
    """Return flights with enough seats for the route and date."""
    return conn.execute(
        "SELECT id, flight_number, departure_time "
        "FROM flights "
        "WHERE origin = ? AND destination = ? AND flight_date = ? "
        "AND seats_available >= ? "
        "ORDER BY departure_time",
        (origin, destination, flight_date, passengers),
    ).fetchall()


def query_price(conn, flight_id):
    """Return the amount for a flight's price row, or None."""
    row = conn.execute(
        "SELECT amount_cents FROM prices WHERE flight_id = ?",
        (flight_id,),
    ).fetchone()
    return row["amount_cents"] if row else None


def search_flights(origin, destination, flight_date, passengers):
    """Return available flights with prices, in departure order."""
    conn = get_connection()
    try:
        available = query_available(conn, origin, destination, flight_date, passengers)
        results = []
        for flight in available:
            price = query_price(conn, flight["id"])
            if price is None:
                continue
            results.append({
                "flight_id": flight["id"],
                "flight_number": flight["flight_number"],
                "departure_time": flight["departure_time"],
                "price_cents": price,
            })
        return json.dumps(results)
    finally:
        conn.close()


def fetch_lowest_fare(origin, destination, flight_date):
    """Return the cheapest fare for a route and date, or None."""
    conn = get_connection()
    try:
        flights = query_available(conn, origin, destination, flight_date, 1)
        fares = []
        for flight in flights:
            price = query_price(conn, flight["id"])
            if price is not None:
                fares.append(price)
        return min(fares) if fares else None
    finally:
        conn.close()
