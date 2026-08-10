# ADCO_OPTIMIZED: 20_performance_regression_neg_expense_tracker
import sqlite3

DB_PATH = "expenses.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_latest_rate(base, quote):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT rate FROM exchange_rates "
            "WHERE base = ? AND quote = ? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (base, quote),
        ).fetchone()
        return row["rate"] if row else None
    finally:
        conn.close()


def save_rate(base, quote, rate):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO exchange_rates (base, quote, rate, fetched_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (base, quote, rate),
        )
        conn.commit()
    finally:
        conn.close()
