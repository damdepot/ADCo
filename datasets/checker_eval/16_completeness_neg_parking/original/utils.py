from datetime import datetime

HOURLY_RATE = 3.00
DAILY_MAX = 18.00
GRACE_MINUTES = 5


def _normalize(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def calculate_fee(entry_time, exit_time):
    entry = _normalize(entry_time)
    exit_t = _normalize(exit_time)
    duration = (exit_t - entry).total_seconds() / 3600.0
    if duration * 60 <= GRACE_MINUTES:
        return 0.0
    if duration <= 0:
        return 0.0
    return round(min(DAILY_MAX, duration * HOURLY_RATE), 2)


def format_duration(entry_time, exit_time):
    entry = _normalize(entry_time)
    exit_t = _normalize(exit_time)
    minutes = int((exit_t - entry).total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60}m"
