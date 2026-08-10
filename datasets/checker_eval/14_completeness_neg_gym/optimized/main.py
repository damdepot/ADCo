# ADCO_OPTIMIZED: 14_completeness_neg_gym
import json
from datetime import date, timedelta

from db import get_connection, init_db


def signup_endpoint(request_body):
    body = json.loads(request_body)
    name = body["name"]
    plan = body["plan"]
    months = int(body["months"])

    conn = get_connection()
    try:
        expires_on = date.today() + timedelta(days=30 * months)
        cursor = conn.execute(
            "INSERT INTO members (name, plan, expires_on) VALUES (?, ?, ?)",
            (name, plan, expires_on.isoformat()),
        )
        conn.commit()
        return 201, {"member_id": cursor.lastrowid}
    finally:
        conn.close()


def renew_member_endpoint(request_body):
    body = json.loads(request_body)
    member_id = int(body["member_id"])
    months = int(body["months"])

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, plan, expires_on FROM members WHERE id = ?", (member_id,)
        ).fetchone()
        if row is None:
            return 404, {"error": "member not found"}
        base = date.fromisoformat(row["expires_on"])
        if base < date.today():
            base = date.today()
        new_expiry = base + timedelta(days=30 * months)
        conn.execute(
            "UPDATE members SET expires_on = ? WHERE id = ?",
            (new_expiry.isoformat(), member_id),
        )
        conn.commit()
        return 200, {"member_id": member_id, "expires_on": new_expiry.isoformat()}
    finally:
        conn.close()


def bulk_renew_endpoint(request_body):
    """Renew multiple memberships from a list of member ids."""
    raise NotImplementedError


def run():
    init_db()
    print("gym service ready")


if __name__ == "__main__":
    run()
