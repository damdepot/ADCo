import json

from db import get_listings_with_shelters, get_pet_detail


def handle_listings():
    rows = get_listings_with_shelters()
    listings = []
    for row in rows:
        listings.append({
            "id": row["id"],
            "pet_name": row["pet_name"],
            "species": row["species"],
            "breed": row["breed"],
            "age_months": row["age_months"],
            "location": row["location"],
            "fee": row["fee"],
            "shelter_name": row["shelter_name"],
            "shelter_phone": row["shelter_phone"],
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
