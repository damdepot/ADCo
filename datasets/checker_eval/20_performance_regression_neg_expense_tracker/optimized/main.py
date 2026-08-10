# ADCO_OPTIMIZED: 20_performance_regression_neg_expense_tracker
import json

from db import fetch_latest_rate, save_rate


def get_rate(base, quote):
    # rates are synced hourly and the table is small, so an indexed
    # lookup per conversion is cheap
    return fetch_latest_rate(base, quote)


def convert_amount(amount, base, quote):
    rate = get_rate(base, quote)
    if rate is None:
        raise ValueError(f"no exchange rate for {base}->{quote}")
    return amount * rate


def handle_report(entries):
    rows = []
    for e in entries:
        if e["currency"] != "USD":
            converted = convert_amount(e["amount"], e["currency"], "USD")
        else:
            converted = e["amount"]
        rows.append({
            "date": e["date"],
            "category": e["category"],
            "merchant": e["merchant"],
            "currency": e["currency"],
            "amount": e["amount"],
            "amount_usd": round(converted, 2),
        })
    return 200, {"count": len(rows), "items": rows}


def main():
    save_rate("EUR", "USD", 1.08)
    save_rate("JPY", "USD", 0.0067)
    entries = [
        {"date": "2026-07-01", "category": "food", "merchant": "Cafe",
         "currency": "EUR", "amount": 12.5},
        {"date": "2026-07-01", "category": "travel", "merchant": "Metro",
         "currency": "JPY", "amount": 780},
    ]
    status, body = handle_report(entries)
    print(status, json.dumps(body, indent=2))


if __name__ == "__main__":
    main()
