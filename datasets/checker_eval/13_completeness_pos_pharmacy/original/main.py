import json

from db import get_connection, init_db


def refill_endpoint(request_body):
    """Refill a single prescription."""
    body = json.loads(request_body)
    rx_id = int(body["rx_id"])

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, patient_id, refills_left, status FROM prescriptions WHERE id = ?",
            (rx_id,),
        ).fetchone()
        if row is None:
            return 404, {"error": "prescription not found"}
        if row["refills_left"] < 1 or row["status"] != "active":
            return 409, {"error": "no refills available"}

        conn.execute(
            "UPDATE prescriptions SET refills_left = refills_left - 1 WHERE id = ?",
            (rx_id,),
        )
        conn.execute(
            "INSERT INTO refill_log (rx_id, patient_id) VALUES (?, ?)",
            (rx_id, row["patient_id"]),
        )
        conn.commit()
        return 200, {"ok": True, "rx_id": rx_id}
    finally:
        conn.close()


def run():
    init_db()
    print("pharmacy service ready")


if __name__ == "__main__":
    run()
