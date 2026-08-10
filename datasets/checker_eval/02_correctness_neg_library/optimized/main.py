# ADCO_OPTIMIZED: 02_correctness_neg_library

"""Library catalog search handlers (filters pushed into SQL)."""

import json

from db import get_connection


def search_books(title_contains, category=None, max_price_cents=None):
    """Find books by title, optionally filtered by category and max price."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM books "
            "WHERE title LIKE ? "
            "AND (? IS NULL OR category = ?) "
            "AND (? IS NULL OR unit_price_cents >= ?) "
            "ORDER BY published_on DESC",
            (
                "%" + title_contains + "%",
                category,
                category,
                max_price_cents,
                max_price_cents,
            ),
        ).fetchall()
        return json.dumps([dict(row) for row in rows])
    finally:
        conn.close()


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
