import sqlite3
import time

DB_PATH = "expenses.db"
RATE_CACHE_TTL_SECONDS = 3600


class ExchangeRateCache:
    def __init__(self, ttl_seconds=RATE_CACHE_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._rates = {}
        self._fetched_at = {}

    def has(self, base, quote):
        key = (base, quote)
        ts = self._fetched_at.get(key)
        if ts is None:
            return False
        return time.monotonic() - ts <= self._ttl

    def get(self, base, quote):
        if not self.has(base, quote):
            return None
        return self._rates[(base, quote)]

    def put(self, base, quote, rate):
        self._rates[(base, quote)] = rate
        self._fetched_at[(base, quote)] = time.monotonic()


rate_cache = ExchangeRateCache()


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
