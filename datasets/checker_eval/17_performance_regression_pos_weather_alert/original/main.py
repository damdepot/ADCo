import json
import time

from db import get_cities_with_contacts, get_city_conditions

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


def run_alert_scan():
    cities = get_cities_with_contacts()
    for city in cities:
        cond = get_city_conditions(city["id"])
        if cond is not None and is_severe(cond):
            send_alert(city, cond)


def main():
    start = time.monotonic()
    run_alert_scan()
    elapsed = time.monotonic() - start
    print(f"scan complete in {elapsed:.3f}s")


if __name__ == "__main__":
    main()
