"""HTTP-handler-style functions for the blogging CMS."""
from db import get_connection


def create_post(author_id, title, body):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO posts (author_id, title, body, status, created_at) "
        f"VALUES ({author_id}, '{title}', '{body}', 'draft', datetime('now'))"
    )
    conn.commit()
    post_id = cur.lastrowid
    conn.close()
    return post_id


def get_post(post_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM posts WHERE id = {post_id}")
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def list_posts_by_author(author_id, include_drafts=False):
    conn = get_connection()
    cur = conn.cursor()
    if include_drafts:
        cur.execute(
            f"SELECT id, title, status, created_at FROM posts WHERE author_id = {author_id}"
        )
    else:
        cur.execute(
            f"SELECT id, title, created_at FROM posts "
            f"WHERE author_id = {author_id} AND status = 'published'"
        )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def search_posts(term):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, title, created_at FROM posts "
        f"WHERE status = 'published' AND (title LIKE '%{term}%' OR body LIKE '%{term}%')"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def publish_post(post_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"UPDATE posts SET status = 'published' WHERE id = {post_id}")
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def delete_post(post_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM posts WHERE id = {post_id}")
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed
