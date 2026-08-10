# ADCO_OPTIMIZED: 05_safety_pos_blogging_cms
"""HTTP-handler-style functions for the blogging CMS."""
from db import get_connection


def create_post(author_id, title, body):
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO posts (author_id, title, body, status, created_at) "
            "VALUES (?, ?, ?, 'draft', datetime('now'))",
            (author_id, title, body),
        )
        return cur.lastrowid


def get_post(post_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return dict(row) if row else None


def list_posts_by_author(author_id, include_drafts=False):
    with get_connection() as conn:
        if include_drafts:
            rows = conn.execute(
                "SELECT id, title, status, created_at FROM posts WHERE author_id = ?",
                (author_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, status, created_at FROM posts "
                "WHERE author_id = ? AND status = 'published'",
                (author_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def search_posts(term):
    pattern = "%" + term + "%"
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at FROM posts "
            "WHERE status = 'published' AND (title LIKE ? OR body LIKE ?)",
            (pattern, pattern),
        ).fetchall()
    return [dict(r) for r in rows]


def publish_post(post_id):
    with get_connection() as conn:
        cur = conn.execute("UPDATE posts SET status = 'published' WHERE id = ?", (post_id,))
        return cur.rowcount > 0


def delete_post(post_id):
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        return cur.rowcount > 0
