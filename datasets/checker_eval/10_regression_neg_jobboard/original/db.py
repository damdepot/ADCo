"""Database helpers for the job board."""
import os
import sqlite3

DB_PATH = os.environ.get("JOB_DB_PATH", "jobs.db")


def connect():
    return sqlite3.connect(DB_PATH)


def search_jobs(term, location):
    """Search published jobs by keyword and location.

    Returns an ok/error dict:
    {"ok": True, "jobs": [{"id", "title", "company", "location"}]} or
    {"ok": False, "error": {"code": "search_failed", "message": ...}}.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, company, location FROM jobs "
            "WHERE status = 'published' "
            "AND (title LIKE ? OR description LIKE ?) "
            "AND location LIKE ? "
            "ORDER BY posted_at DESC",
            ("%" + term + "%", "%" + term + "%", "%" + location + "%"),
        ).fetchall()
        jobs = [
            {"id": row[0], "title": row[1], "company": row[2], "location": row[3]}
            for row in rows
        ]
        return {"ok": True, "jobs": jobs}
    except sqlite3.Error as exc:
        return {"ok": False, "error": {"code": "search_failed", "message": str(exc)}}
    finally:
        conn.close()
