# ADCO_OPTIMIZED: 09_regression_pos_movie
"""Movie catalog formatting helpers.

These two functions were moved verbatim from main.py. Their names,
signatures, return shapes, and behavior are unchanged — they only
format sqlite3.Row rows into plain dicts and have no side effects.
"""


def format_movie(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "genre": row["genre"],
        "year": row["year"],
        "rating": row["rating"],
    }


def format_movies(rows):
    return [format_movie(r) for r in rows]
