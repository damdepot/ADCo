"""HTTP-style handlers for the job board."""
import json

import db


def json_response(payload, status=200):
    return {"status": status, "body": json.dumps(payload)}


def handle_search(request):
    term = request.get("term", "")
    location = request.get("location", "")
    result = db.search_jobs(term, location)
    if not result["ok"]:
        return json_response({"error": result["error"]}, status=500)
    return json_response({"query": {"term": term, "location": location}, "jobs": result["jobs"]})


def handle_homepage(request):
    result = db.search_jobs("", "")
    if not result["ok"]:
        return json_response({"error": result["error"]}, status=500)
    return json_response({"featured": result["jobs"][:6]})


def handle_alert_digest(request):
    saved = request.get("saved_alerts", [])
    hits = []
    for alert in saved:
        result = db.search_jobs(alert["term"], alert["location"])
        if not result["ok"]:
            continue
        hits.extend(result["jobs"][:5])
    return json_response({"hits": hits})
