"""HTTP-style handlers for the hotel booking service."""
import json

import db


def json_response(payload, status=200):
    return {"status": status, "body": json.dumps(payload)}


def format_reservation(booking_id, room_id, guest_name, date):
    return {
        "id": booking_id,
        "room_id": room_id,
        "guest_name": guest_name,
        "date": date,
    }


def handle_reservation(request):
    try:
        reservation = db.reserve_room(
            request.get("room_id", ""),
            request.get("guest_name", ""),
            request.get("date", ""),
        )
        return json_response({"reservation": format_reservation(
            booking_id=reservation["id"],
            room_id=reservation["room_id"],
            guest_name=reservation["guest_name"],
            date=reservation["date"],
        )}, status=201)
    except ValueError as exc:
        return json_response({"error": {"code": "room_unavailable", "message": str(exc)}}, status=409)
