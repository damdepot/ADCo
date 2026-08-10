# ADCO_OPTIMIZED: 17_performance_regression_pos_weather_alert
import json
import time

from db import (ConditionsCache, get_cities_with_contacts,
                get_city_conditions_batch)

ALERT_TEMP_C = 35
ALERT_WIND_KMH = 60


def is_severe(cond):
    return cond["temp_c"] >= ALERT_TEMP_C or cond["wind_kmh"] >= ALERT_WIND_KMH


def send_alert(city, cond):
    print(json.dumps({
        "event": "weather_alert",
        "city": city["name"],
        "region": city["region"],
        "temp_c": cond["temp_c"],
        "wind_kmh": cond["wind_kmh"],
    }))


def resolve_conditions(cities, cache):
    resolved = {}
    missing = [c["id"] for c in cities if not cache.has(c["id"])]
    if missing:
        fetched = get_city_conditions_batch(missing)
        for city_id, cond in fetched.items():
            cache.put(city_id, cond)
        for city_id in missing:
            if city_id not in fetched:
                cache.put(city_id, None)
    for city in cities:
        if cache.has(city["id"]):
            resolved[city["id"]] = cache.get(city["id"])
    return resolved


def run_alert_scan():
    cache = ConditionsCache()
    cities = get_cities_with_contacts()
    conditions = resolve_conditions(cities, cache)
    for city in cities:
        cond = conditions.get(city["id"])
        if cond is not None and is_severe(cond):
            send_alert(city, cond)


def main():
    start = time.monotonic()
    run_alert_scan()
    elapsed = time.monotonic() - start
    print(f"scan complete in {elapsed:.3f}s")


if __name__ == "__main__":
    main()
