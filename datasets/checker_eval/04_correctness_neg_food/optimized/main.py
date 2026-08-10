# ADCO_OPTIMIZED: 04_correctness_neg_food

"""Per-order totals for food delivery restaurants (SQL aggregation)."""
import json

from db import get_connection


def order_totals(restaurant_id, day):
    """Sum each order's items with one aggregation query."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT MAX(oi.order_id) AS order_id, SUM(oi.quantity * oi.unit_price_cents) AS total_cents "
            "FROM order_items oi "
            "JOIN orders o ON o.id = oi.order_id "
            "WHERE o.restaurant_id = ? AND o.created_at LIKE ?",
            (restaurant_id, day + "%"),
        ).fetchall()
        totals = {row["order_id"]: row["total_cents"] for row in rows}
        return json.dumps(totals)
    finally:
        conn.close()


def daily_revenue(restaurant_id, day):
    """Return total revenue in cents for a restaurant on a day."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT SUM(oi.quantity * oi.unit_price_cents) AS total_cents "
            "FROM order_items oi "
            "JOIN orders o ON o.id = oi.order_id "
            "WHERE o.restaurant_id = ? AND o.created_at LIKE ?",
            (restaurant_id, day + "%"),
        ).fetchone()
        total = row["total_cents"]
        return total if total is not None else 0
    finally:
        conn.close()
