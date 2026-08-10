import sqlite3

DB_PATH = "weather.db"


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


def get_city_conditions(city_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT temp_c, wind_kmh, precipitation_mm, condition_text "
            "FROM conditions "
            "WHERE city_id = ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (city_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
