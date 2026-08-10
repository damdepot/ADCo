# ADCO_OPTIMIZED: 12_regression_neg_ticketing
"""HTTP-style handlers for the event ticketing service."""
import json

import db


def json_response(payload, status=200):
    return {"status": status, "body": json.dumps(payload)}


def booking_response(result):
    return {"ok": result["ok"], "payload": result.get("booking") or result.get("error")}


def handle_book_ticket(request):
    result = db.create_booking(
        request.get("event_id"),
        request.get("attendee_name"),
        request.get("tickets", 1),
    )
    if not result["ok"]:
        return json_response({"error": result["error"]}, status=409)
    return json_response({"booking": result["booking"]}, status=201)


def handle_bulk_booking(request):
    results = []
    for item in request.get("requests", []):
        result = db.create_booking(item["event_id"], item["attendee_name"], item["tickets"])
        results.append(booking_response(result))
    return json_response({"results": results})


def handle_guest_booking(request):
    result = db.create_booking(
        request.get("event_id"),
        "guest-" + request.get("guest_token", "unknown"),
        request.get("tickets", 1),
    )
    if not result["ok"]:
        return json_response({"error": result["error"]}, status=409)
    return json_response({"booking": result["booking"]}, status=201)
