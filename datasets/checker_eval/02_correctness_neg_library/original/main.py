"""Library catalog search handlers."""
import json

from db import get_connection


def fetch_candidates(title_contains):
    """Return books whose title contains the text, newest published first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM books WHERE title LIKE ? ORDER BY published_on DESC",
            ("%" + title_contains + "%",),
        ).fetchall()
        return rows
    finally:
        conn.close()


def apply_filters(rows, category, max_price_cents):
    """Apply the optional category and max-price filters in Python."""
    results = []
    for row in rows:
        if category is not None and row["category"] != category:
            continue
        if max_price_cents is not None and row["unit_price_cents"] > max_price_cents:
            continue
        results.append(row)
    return results


def search_books(title_contains, category=None, max_price_cents=None):
    """Find books by title, optionally filtered by category and max price."""
    rows = fetch_candidates(title_contains)
    return json.dumps([
        dict(row) for row in apply_filters(rows, category, max_price_cents)
    ])


def get_book_details(book_id):
    """Return a single book as JSON, or None when it does not exist."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM books WHERE id = ?",
            (book_id,),
        ).fetchone()
        return json.dumps(dict(row)) if row else None
    finally:
        conn.close()
