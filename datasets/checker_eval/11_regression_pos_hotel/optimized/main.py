# ADCO_OPTIMIZED: 11_regression_pos_hotel

"""HTTP-style handlers for the hotel booking service."""
import json

import db
import reservation_utils


def json_response(payload, status=200):
    return {"status": status, "body": json.dumps(payload)}


def handle_reservation(request):
    try:
        reservation = db.reserve_room(
            request.get("room_id", ""),
            request.get("guest_name", ""),
            request.get("date", ""),
        )
        return json_response({"reservation": reservation_utils.format_reservation(
            booking_id=reservation["id"],
            room_id=reservation["room_id"],
            guest_name=reservation["guest_name"],
            date=reservation["date"],
        )}, status=201)
    except ValueError as exc:
        return json_response({"error": {"code": "room_unavailable", "message": str(exc)}}, status=409)
