"""HTTP-style handlers for the movie streaming catalog."""
import json
import sqlite3

import db


def json_response(payload, status=200):
    return {"status": status, "body": json.dumps(payload)}


def error_response(code, message, status=500):
    payload = {"error": {"code": code, "message": message}}
    return {"status": status, "body": json.dumps(payload)}


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


def handle_catalog_list(request):
    try:
        conn = db.connect()
        rows = conn.execute(
            "SELECT id, title, genre, year, rating FROM movies "
            "WHERE status = 'published' ORDER BY year DESC"
        ).fetchall()
        return json_response({"movies": format_movies(rows)})
    except sqlite3.Error as exc:
        return error_response("catalog_unavailable", str(exc))
    finally:
        db.close(conn)


def handle_genre_browse(request):
    genre = request.get("genre", "")
    try:
        conn = db.connect()
        rows = conn.execute(
            "SELECT id, title, genre, year, rating FROM movies "
            "WHERE status = 'published' AND genre = ? ORDER BY rating DESC",
            (genre,),
        ).fetchall()
        return json_response({"genre": genre, "movies": format_movies(rows)})
    except sqlite3.Error as exc:
        return error_response("catalog_unavailable", str(exc))
    finally:
        db.close(conn)


def handle_catalog_detail(request):
    movie_id = request.get("id", "")
    try:
        conn = db.connect()
        row = conn.execute(
            "SELECT id, title, genre, year, rating FROM movies "
            "WHERE status = 'published' AND id = ?",
            (movie_id,),
        ).fetchone()
        if row is None:
            return json_response({"movie": None}, status=404)
        return json_response({"movie": format_movie(row)})
    except sqlite3.Error as exc:
        return error_response("catalog_unavailable", str(exc))
    finally:
        db.close(conn)
