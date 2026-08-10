import sqlite3

DB_PATH = "adoptions.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_listings_with_shelters(limit=200):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT l.id, l.pet_name, l.species, l.breed, l.age_months, "
            "       l.location, l.fee, l.created_at, "
            "       s.name AS shelter_name, s.phone AS shelter_phone "
            "FROM listings l "
            "JOIN shelters s ON s.id = l.shelter_id "
            "WHERE l.status = 'available' "
            "ORDER BY l.created_at DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_pet_detail(pet_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT l.id, l.pet_name, l.species, l.breed, l.age_months, "
            "       l.location, l.fee, l.notes, l.status, "
            "       s.name AS shelter_name, s.phone AS shelter_phone, "
            "       s.address AS shelter_address "
            "FROM listings l "
            "JOIN shelters s ON s.id = l.shelter_id "
            "WHERE l.id = ?",
            (pet_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
