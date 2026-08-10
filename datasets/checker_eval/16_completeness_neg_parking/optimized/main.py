# ADCO_OPTIMIZED: 16_completeness_neg_parking
import json
from datetime import datetime

from db import get_connection, init_db
from reports import daily_summary
from utils import calculate_fee


def check_in_endpoint(request_body):
    body = json.loads(request_body)
    plate = body["plate"]
    zone = body["zone"]

    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO sessions (plate, zone, entry_time) VALUES (?, ?, ?)",
            (plate, zone, datetime.now().isoformat()),
        )
        conn.commit()
        return 201, {"session_id": cursor.lastrowid}
    finally:
        conn.close()


def check_out_endpoint(request_body):
    body = json.loads(request_body)
    session_id = int(body["session_id"])
    exit_time = datetime.now()

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, plate, zone, entry_time, fee FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return 404, {"error": "session not found"}

        fee = calculate_fee(datetime.fromisoformat(row["entry_time"]), exit_time)
        conn.execute(
            "UPDATE sessions SET exit_time = ?, fee = ? WHERE id = ?",
            (exit_time.isoformat(), fee, session_id),
        )
        conn.commit()
        return 200, {"session_id": session_id, "fee": fee}
    finally:
        conn.close()


def report_endpoint():
    conn = get_connection()
    try:
        sessions = conn.execute("SELECT * FROM sessions").fetchall()
        summary = daily_summary(sessions)
        return 200, {"report": summary}
    finally:
        conn.close()


def run():
    init_db()
    print("parking service ready")


if __name__ == "__main__":
    run()
