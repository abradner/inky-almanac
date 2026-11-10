"""When the frame should next wake, and which day the poster should describe.

A full refresh takes ~40 s and the panel is unreadable meanwhile, so the frame
refreshes once an hour, a minute past the hour (Open-Meteo's data turns over on
the hour), from first_hour to last_hour inclusive, and holds still overnight.
"""

from datetime import timedelta, timezone

MIN_SLEEP = 5
MAX_SLEEP = 12 * 60


def next_wake(now, first_hour, last_hour):
    """The first HH:01 strictly after now whose hour is in [first_hour, last_hour]."""
    candidate = now.replace(minute=1, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(hours=1)
    for _ in range(48):
        if first_hour <= candidate.hour <= last_hour:
            return candidate
        candidate += timedelta(hours=1)
    raise ValueError("no refresh hour in range")


def sleep_minutes(now, first_hour, last_hour):
    wake = next_wake(now, first_hour, last_hour)
    # Subtract in UTC: aware datetimes sharing a tzinfo subtract as wall clock,
    # which is an hour out across a DST change.
    if now.tzinfo is not None:
        wake, now = wake.astimezone(timezone.utc), now.astimezone(timezone.utc)
    minutes = int((wake - now).total_seconds() // 60) + 1
    return max(MIN_SLEEP, min(MAX_SLEEP, minutes))


def poster_day(now, tomorrow_from_hour):
    """Evening renders describe tomorrow, so the last one of the night is still
    useful when you walk past in the morning."""
    if now.hour >= tomorrow_from_hour:
        return (now + timedelta(days=1)).date(), True
    return now.date(), False


def slot(now):
    """Renders are cached per hour."""
    return now.strftime("%Y-%m-%dT%H")

