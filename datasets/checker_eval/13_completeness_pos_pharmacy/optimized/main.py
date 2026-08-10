# ADCO_OPTIMIZED: 13_completeness_pos_pharmacy
import json
import sqlite3

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


def batch_refill_endpoint(request_body):
    """Refill several prescriptions in one request.

    Each item is handled independently so a failure on one
    prescription does not cancel the rest of the batch.
    """
    body = json.loads(request_body)
    rx_ids = [int(item) for item in body["rx_ids"]]
    results = []

    conn = get_connection()
    try:
        for rx_id in rx_ids:
            try:
                conn.execute("SAVEPOINT refill_item")
                row = conn.execute(
                    "SELECT id, patient_id, refills_left, status FROM prescriptions WHERE id = ?",
                    (rx_id,),
                ).fetchone()
                if row is None:
                    conn.execute("RELEASE SAVEPOINT refill_item")
                    results.append({"rx_id": rx_id, "status": "not_found"})
                    continue
                if row["refills_left"] < 1 or row["status"] != "active":
                    conn.execute("RELEASE SAVEPOINT refill_item")
                    results.append({"rx_id": rx_id, "status": "no_refills"})
                    continue
                conn.execute(
                    "UPDATE prescriptions SET refills_left = refills_left - 1 WHERE id = ?",
                    (rx_id,),
                )
                conn.execute(
                    "INSERT INTO refill_log (rx_id, patient_id) VALUES (?, ?)",
                    (rx_id, row["patient_id"]),
                )
                conn.execute("RELEASE SAVEPOINT refill_item")
                results.append({"rx_id": rx_id, "status": "ok"})
            except sqlite3.Error:
                conn.execute("ROLLBACK TO SAVEPOINT refill_item")
                conn.execute("RELEASE SAVEPOINT refill_item")
                results.append({"rx_id": rx_id, "status": "failed"})
        conn.commit()
        succeeded = sum(1 for r in results if r["status"] == "ok")
        return 200, {"processed": len(rx_ids), "succeeded": succeeded, "results": results}
    finally:
        conn.close()


def run():
    init_db()
    print("pharmacy service ready")


if __name__ == "__main__":
    run()
