"""HTTP-style handlers for the e-commerce checkout flow."""
import json
import time

from db import get_connection


def create_order(customer_id, items):
    """Insert an order and its line items; returns the new order id."""
    conn = get_connection()
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cursor = conn.execute(
            "INSERT INTO orders (customer_id, status, created_at) "
            "VALUES (?, ?, ?)",
            (customer_id, "pending", now),
        )
        order_id = cursor.lastrowid
        for item in items:
            conn.execute(
                "INSERT INTO order_items "
                "(order_id, product_id, quantity, unit_price_cents) "
                "VALUES (?, ?, ?, ?)",
                (
                    order_id,
                    item["product_id"],
                    item["quantity"],
                    item["unit_price_cents"],
                ),
            )
        conn.commit()
        return order_id
    finally:
        conn.close()


def order_count_for_customer(customer_id):
    """Return how many orders a customer has placed."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM orders WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
        return row["total"]
    finally:
        conn.close()


def handle_checkout(customer_id, items):
    """Create the order and return a JSON summary for the caller."""
    order_id = create_order(customer_id, items)
    total_orders = order_count_for_customer(customer_id)
    return json.dumps({
        "order_id": order_id,
        "customer_total_orders": total_orders,
    })
