"""Minimal PostgreSQL access helper for the synthetic DCo fixture app."""
from __future__ import annotations

import configparser
from pathlib import Path

_CONFIG_PATH = Path(__file__).with_name("db.config")


def load_db_config(path: str | None = None) -> dict:
    parser = configparser.ConfigParser()
    parser.read(path or _CONFIG_PATH)
    pg = parser["postgres"]
    return {
        "host": pg.get("host", "127.0.0.1"),
        "port": pg.getint("port", 5432),
        "user": pg.get("user", "postgres"),
        "password": pg.get("password", ""),
        "database": pg.get("database", "dco_fixture"),
    }


def get_connection():
    import psycopg2

    return psycopg2.connect(**load_db_config())


def run_query(sql: str, params=None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            conn.commit()
            return []
    finally:
        conn.close()
