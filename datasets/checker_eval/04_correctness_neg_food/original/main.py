"""Per-order totals for food delivery restaurants."""
import json

from db import get_connection


def fetch_order_items(conn, restaurant_id, day):
    """Return the day's item rows for one restaurant."""
    return conn.execute(
        "SELECT oi.order_id, oi.quantity, oi.unit_price_cents "
        "FROM order_items oi "
        "JOIN orders o ON o.id = oi.order_id "
        "WHERE o.restaurant_id = ? AND o.created_at LIKE ?",
        (restaurant_id, day + "%"),
    ).fetchall()


def compute_totals(rows):
    """Sum quantity * unit_price per order id."""
    totals = {}
    for row in rows:
        totals[row["order_id"]] = (
            totals.get(row["order_id"], 0)
            + row["quantity"] * row["unit_price_cents"]
        )
    return totals


def order_totals(restaurant_id, day):
    """Return each order id mapped to its total in cents as JSON."""
    conn = get_connection()
    try:
        rows = fetch_order_items(conn, restaurant_id, day)
        return json.dumps(compute_totals(rows))
    finally:
        conn.close()


def daily_revenue(restaurant_id, day):
    """Return total revenue in cents for a restaurant on a day."""
    conn = get_connection()
    try:
        rows = fetch_order_items(conn, restaurant_id, day)
        return sum(r["quantity"] * r["unit_price_cents"] for r in rows)
    finally:
        conn.close()
