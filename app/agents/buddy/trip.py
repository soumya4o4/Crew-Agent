"""What Buddy knows about the user's trip right now, worked out by code (never guessed by the LLM): the next flight,
when to arrive at the airport, the hotel stay, where the user is, and when to leave. describe() returns it as a
TripInfo; its `text` goes into the model's prompt as facts."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.geo import location_age_min
from app.core.places import city, is_international
from app.core.utils import to_ist

SLACK_MIN = 15  # a cushion for parking, security queues and the unexpected


@dataclass
class TripInfo:
    text: str = ""
    flight: dict | None = None  # the booking Buddy is helping with (has `flights`)
    stay: dict | None = None
    loc: dict | None = None
    route: dict | None = None   # {km, minutes}: from the user's location to the airport they depart from


def airport_buffer_min(flight: dict) -> int:
    """How long before departure to be at the airport: 3 hours for international, 2 for domestic."""
    return 180 if is_international(flight["from_code"], flight["to_code"]) else 120


def pick_next_flight(bookings: list[dict], now: datetime) -> dict | None:
    """The first flight that has not left yet; failing that, the one that left most recently."""
    ordered = sorted(bookings, key=lambda b: b["flights"]["departure_time"])
    for b in ordered:
        if to_ist(b["flights"]["departure_time"]) >= now - timedelta(minutes=30):
            return b
    return ordered[-1] if ordered else None


def leave_by(flight: dict, eta_min: int) -> datetime:
    return to_ist(flight["departure_time"]) - timedelta(minutes=airport_buffer_min(flight) + eta_min + SLACK_MIN)


def fmt_delta(delta: timedelta) -> str:
    minutes = abs(int(delta.total_seconds() // 60))
    h, m = divmod(minutes, 60)
    span = f"{h}h {m:02d}m" if h else f"{m}m"
    return f"in {span}" if delta.total_seconds() >= 0 else f"{span} ago"


def _flight_lines(now: datetime, b: dict, route: dict | None) -> list[str]:
    f = b["flights"]
    dep, arr = to_ist(f["departure_time"]), to_ist(f["arrival_time"])
    buffer = airport_buffer_min(f)
    lines = [f"Flight {f['flight_no']} {city(f['from_code'])} to {city(f['to_code'])}: departs {dep:%a %d %b %H:%M}, arrives {arr:%H:%M} "
             f"(status: {f['status']}, {fmt_delta(dep - now)}). PNR {b['pnr']}, {b['passenger_name']}, {f['class']}, "
             f"{f['baggage_kg']} kg baggage.",
             f"Reach the airport by {dep - timedelta(minutes=buffer):%H:%M} ({buffer // 60}h before, "
             f"{'international' if buffer == 180 else 'domestic'}); check-in counters usually close about an hour before departure."]
    if dep > now and route:
        lv = leave_by(f, route["minutes"])
        lines.append(f"Suggested time to leave for the airport: {lv:%H:%M} ({fmt_delta(lv - now)}), counting {route['minutes']} min "
                     f"driving, the {buffer // 60}h airport buffer and {SLACK_MIN} min spare.")
    return lines


def describe(now: datetime, bookings: list[dict], stay: dict | None, loc: dict | None, label: str | None,
             route: dict | None) -> TripInfo:
    nxt = pick_next_flight(bookings, now)
    lines = [f"Now: {now:%a %d %b %Y, %H:%M} IST."]
    if nxt:
        lines += _flight_lines(now, nxt, route)
        extra = [b for b in bookings if b is not nxt][:1]
        for b in extra:
            f = b["flights"]
            lines.append(f"Also booked: {f['flight_no']} {city(f['from_code'])} to {city(f['to_code'])} departing {to_ist(f['departure_time']):%a %d %b %H:%M}.")
    else:
        lines.append("No upcoming flight booked with us.")
    if stay:
        h = stay["hotels"]
        lines.append(f"Hotel: {h['name']}, {h['area']}, {city(h['city_code'])}; check-in {stay['check_in']} from 2:00 PM, "
                     f"check-out {stay['check_out']} by 11:00 AM; ref {stay['ref']}.")
    if loc:
        where = f"near {label}" if label else "shared"
        lines.append(f"User location: {where}, shared {location_age_min(loc)} min ago."
                     + (f" About {route['km']} km and {route['minutes']} min by car (no live traffic) to the departure airport." if route else ""))
    else:
        lines.append("User location: not shared. If you need it (lost, ETA), use action ask_location.")
    return TripInfo("\n".join(lines), nxt, stay, loc, route)
