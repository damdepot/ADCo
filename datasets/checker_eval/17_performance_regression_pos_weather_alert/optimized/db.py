# ADCO_OPTIMIZED: 17_performance_regression_pos_weather_alert
import sqlite3
import time

DB_PATH = "weather.db"
CACHE_TTL_SECONDS = 60


class ConditionsCache:
    def __init__(self, ttl_seconds=CACHE_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._data = {}
        self._timestamps = {}

    def has(self, city_id):
        ts = self._timestamps.get(city_id)
        if ts is None:
            return False
        return time.monotonic() - ts <= self._ttl

    def get(self, city_id):
        if not self.has(city_id):
            return None
        return self._data[city_id]

    def put(self, city_id, conditions):
        self._data[city_id] = conditions
        self._timestamps[city_id] = time.monotonic()


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_cities_with_contacts():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT c.id, c.name, c.region, u.phone "
            "FROM cities c "
            "JOIN users u ON u.city_id = c.id "
            "WHERE u.alerts_enabled = 1"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_city_conditions_batch(city_ids):
    if not city_ids:
        return {}
    conn = get_connection()
    try:
        placeholders = ",".join("?" * len(city_ids))
        query = (
            "SELECT c.city_id, c.temp_c, c.wind_kmh, c.precipitation_mm, "
            "c.condition_text "
            "FROM conditions c "
            "JOIN (SELECT city_id, MAX(recorded_at) AS latest "
            "      FROM conditions WHERE city_id IN (" + placeholders + ") "
            "      GROUP BY city_id) m "
            "ON m.city_id = c.city_id AND m.latest = c.recorded_at"
        )
        rows = conn.execute(query, tuple(city_ids)).fetchall()
        return {r["city_id"]: dict(r) for r in rows}
    finally:
        conn.close()
