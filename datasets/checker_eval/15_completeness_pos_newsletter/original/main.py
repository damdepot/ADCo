import json
import smtplib
from email.message import EmailMessage

from db import get_connection, init_db

SMTP_HOST = "localhost"


def send_email(recipient, subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = recipient
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST) as server:
        server.send_message(msg)


def send_to_one_endpoint(request_body):
    body = json.loads(request_body)
    email = body["email"]
    campaign = body["campaign"]
    subject = body["subject"]
    content = body["content"]

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, email, status FROM subscribers WHERE email = ?", (email,)
        ).fetchone()
        if row is None or row["status"] != "subscribed":
            return 404, {"error": "subscriber not found"}
        try:
            send_email(email, subject, content)
        except OSError:
            return 502, {"error": "smtp send failed"}
        conn.execute(
            "INSERT INTO sends (subscriber_id, campaign, status, error) "
            "VALUES (?, ?, 'sent', NULL)",
            (row["id"], campaign),
        )
        conn.commit()
        return 200, {"sent": True, "email": email}
    finally:
        conn.close()


def run():
    init_db()
    print("newsletter service ready")


if __name__ == "__main__":
    run()
