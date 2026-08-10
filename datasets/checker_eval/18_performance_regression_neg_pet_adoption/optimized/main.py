# ADCO_OPTIMIZED: 18_performance_regression_neg_pet_adoption
import json

from db import get_available_listings, get_pet_detail, get_shelter_by_id


def handle_listings():
    rows = get_available_listings()
    listings = []
    for row in rows:
        shelter = get_shelter_by_id(row["shelter_id"])
        listings.append({
            "id": row["id"],
            "pet_name": row["pet_name"],
            "species": row["species"],
            "breed": row["breed"],
            "age_months": row["age_months"],
            "location": row["location"],
            "fee": row["fee"],
            "shelter_name": shelter["name"] if shelter else "",
            "shelter_phone": shelter["phone"] if shelter else "",
        })
    return 200, {"count": len(listings), "listings": listings}


def handle_pet_detail(pet_id):
    row = get_pet_detail(pet_id)
    if row is None:
        return 404, {"error": "pet not found"}
    return 200, {"pet": row}


def route(method, path, query):
    if method == "GET" and path == "/api/listings":
        return handle_listings()
    if method == "GET" and path.startswith("/api/pets/"):
        pet_id = int(path.rsplit("/", 1)[1])
        return handle_pet_detail(pet_id)
    return 404, {"error": "not found"}


def main():
    requests = [
        ("GET", "/api/listings", {}),
        ("GET", "/api/pets/42", {}),
    ]
    for method, path, query in requests:
        status, body = route(method, path, query)
        print(status, json.dumps(body)[:200])


if __name__ == "__main__":
    main()
