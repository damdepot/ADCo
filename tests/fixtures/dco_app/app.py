"""Synthetic DCo fixture app: small but realistic PostgreSQL access patterns."""
from db import run_query

# TODO: this fixture deliberately shows common access anti-patterns


def get_user(user_id):
    return run_query("SELECT id, email FROM users WHERE id = %s", (user_id,))


def get_orders_for_users(user_ids):
    orders = {}
    for uid in user_ids:  # N+1: one query issued per user
        orders[uid] = run_query("SELECT id, total FROM orders WHERE user_id = %s", (uid,))
    return orders


def create_user(email):
    return run_query("INSERT INTO users (email) VALUES (%s) RETURNING id", (email,))


def orders_per_user():
    return run_query(
        "SELECT user_id, COUNT(*) AS order_count FROM orders GROUP BY user_id"
    )


if __name__ == "__main__":
    print(get_user(1))
