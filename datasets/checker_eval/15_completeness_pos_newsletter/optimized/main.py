# ADCO_OPTIMIZED: 15_completeness_pos_newsletter
import json
import smtplib
from email.message import EmailMessage

from db import get_connection, init_db

SMTP_HOST = "localhost"
BATCH_SIZE = 50


def send_email(recipient, subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = recipient
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST) as server:
        server.send_message(msg)


def send_to_one_endpoint(request_body):
    body = json.loads(request_body)
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, email, status FROM subscribers WHERE email = ?",
            (body["email"],),
        ).fetchone()
        if row is None or row["status"] != "subscribed":
            return 404, {"error": "subscriber not found"}
        try:
            send_email(body["email"], body["subject"], body["content"])
        except OSError:
            return 502, {"error": "smtp send failed"}
        conn.execute(
            "INSERT INTO sends (subscriber_id, campaign, status, error) "
            "VALUES (?, ?, 'sent', NULL)",
            (row["id"], body["campaign"]),
        )
        conn.commit()
        return 200, {"sent": True, "email": body["email"]}
    finally:
        conn.close()


def send_batch_endpoint(request_body):
    """Send a campaign to every subscribed member, 50 at a time."""
    body = json.loads(request_body)
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, email FROM subscribers WHERE status = 'subscribed'"
        ).fetchall()
        total_sent = 0
        failures = []
        for start in range(0, len(rows), BATCH_SIZE):
            for row in rows[start : start + BATCH_SIZE]:
                status = "sent"
                error = None
                try:
                    send_email(row["email"], body["subject"], body["content"])
                except OSError as exc:
                    status = "failed"
                    error = str(exc)
                    failures.append({"subscriber_id": row["id"], "error": error})
                conn.execute(
                    "INSERT INTO sends (subscriber_id, campaign, status, error) "
                    "VALUES (?, ?, ?, ?)",
                    (row["id"], body["campaign"], status, error),
                )
                if status == "sent":
                    total_sent += 1
            conn.commit()
        return 200, {"total_subscribers": len(rows), "sent": total_sent,
                      "failed": len(failures), "failures": failures}
    finally:
        conn.close()
