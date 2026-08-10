# ADCO_OPTIMIZED: 08_safety_neg_ride_sharing
"""Dispatch handlers for the ride-sharing service."""
import os

from db import get_connection


def request_ride(rider, pickup, dropoff):
    conn = get_connection()
    driver = conn.execute(
        "SELECT id, name, phone FROM drivers "
        "WHERE status = 'available' ORDER BY id LIMIT 1"
    ).fetchone()
    if driver is None:
        conn.close()
        return None
    cur = conn.execute(
        "INSERT INTO rides (rider, driver_id, pickup, dropoff, status) "
        "VALUES (?, ?, ?, ?, 'assigned')",
        (rider, driver["id"], pickup, dropoff),
    )
    conn.execute("UPDATE drivers SET status = 'busy' WHERE id = ?", (driver["id"],))
    conn.commit()
    ride_id = cur.lastrowid
    conn.close()
    notify_driver(driver["phone"], "New ride assigned: " + pickup + " -> " + dropoff)
    return ride_id


def notify_driver(driver_phone, message):
    # single-line notification; sms_send is a fixed internal CLI
    os.system("sms_send " + driver_phone + " " + message)


def update_driver_status(driver_id, status):
    conn = get_connection()
    cur = conn.execute("UPDATE drivers SET status = ? WHERE id = ?", (status, driver_id))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def register_driver(name, phone, vehicle):
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO drivers (name, phone, vehicle, status) "
        "VALUES (?, ?, ?, 'available')",
        (name, phone, vehicle),
    )
    conn.commit()
    driver_id = cur.lastrowid
    conn.close()
    return driver_id
