"""Handlers for the social feed service."""
from db import get_connection


def search_posts(term):
    conn = get_connection()
    pattern = "%" + term + "%"
    rows = conn.execute(
        "SELECT id, author, body FROM posts "
        "WHERE body LIKE ? ORDER BY posted_at DESC LIMIT 50",
        (pattern,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_post(author, body):
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO posts (author, body, posted_at) VALUES (?, ?, datetime('now'))",
        (author, body),
    )
    conn.commit()
    post_id = cur.lastrowid
    conn.close()
    return post_id


def get_feed(user, limit=50):
    conn = get_connection()
    rows = conn.execute(
        "SELECT p.id, p.author, p.body, p.posted_at "
        "FROM posts p JOIN follows f ON f.followee = p.author "
        "WHERE f.follower = ? "
        "ORDER BY p.posted_at DESC LIMIT ?",
        (user, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def follow(follower, followee):
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO follows (follower, followee) VALUES (?, ?)",
        (follower, followee),
    )
    conn.commit()
    conn.close()
