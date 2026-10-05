"""What Buddy knows about the user's trip right now, worked out by code (never guessed by the LLM): the next flight,
when to arrive at the airport, the hotel stay, where the user is, and when to leave. describe() returns it as a
TripInfo; its `text` goes into the model's prompt as facts."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.agents.flight.formatting import baggage_label, pnr_of
from app.agents.hotel.formatting import CHECK_IN_LABEL, CHECK_OUT_LABEL, free_cancel_until
from app.core.geo import location_age_min
from app.core.places import city, is_international
from app.core.utils import inr, to_ist

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
             f"(status: {f['status']}, {fmt_delta(dep - now)}). PNR {pnr_of(b)}, {b['passenger_name']}, {f['class']}, "
             f"baggage: {baggage_label(f)}.",
             f"Reach the airport by {dep - timedelta(minutes=buffer):%H:%M} ({buffer // 60}h before, "
             f"{'international' if buffer == 180 else 'domestic'}); check-in counters usually close about an hour before departure."]
    if dep > now and route:
        lv = leave_by(f, route["minutes"])
        lines.append(f"Suggested time to leave for the airport: {lv:%H:%M} ({fmt_delta(lv - now)}), counting {route['minutes']} min "
                     f"driving, the {buffer // 60}h airport buffer and {SLACK_MIN} min spare.")
    return lines


def _day(value) -> date:
    return date.fromisoformat(str(value)[:10])


def _stay_lines(now: datetime, stay: dict, options: list[dict]) -> list[str]:
    """Everything the user could ask about a booked hotel, as facts (missing details are simply left out)."""
    h, room = stay.get("hotels") or {}, stay.get("hotel_rooms") or {}
    where = ", ".join(x for x in (h.get("area"), city(h["city_code"]) if h.get("city_code") else "") if x)
    stars = f"{h['stars']}-star, " if h.get("stars") else ""
    head = f"Hotel: {h.get('name', 'unknown')}, {stars}{where}"
    if h.get("rating"):
        head += f" (rated {h['rating']})"
    check_in, check_out = stay.get("check_in"), stay.get("check_out")
    if check_in and check_out:
        a, b = _day(check_in), _day(check_out)
        days = (a - now.date()).days
        when = "starts today" if days == 0 else f"starts in {days} days" if days > 0 else "is on now"
        head += (f"; check-in {a:%a %d %b} from {CHECK_IN_LABEL}, check-out {b:%a %d %b} by {CHECK_OUT_LABEL} "
                 f"({(b - a).days} night(s); the stay {when})")
    lines = [head + f"; ref {stay.get('ref', '?')}, status {stay.get('status', 'confirmed')}."]
    details = []
    if room:
        details.append(f"room {room.get('room_type', '')} ({room.get('bed', '')})")
    if stay.get("guests"):
        details.append(f"{stay['guests']} guest(s), booked for {stay.get('guest_name', 'the guest')}")
    if stay.get("total_price_inr"):
        details.append(f"total {inr(stay['total_price_inr'])}")
    if check_in:
        until = free_cancel_until(check_in)
        details.append(f"free cancellation until {until:%a %d %b %H:%M}" if now <= until else "free cancellation has ended")
    if details:
        lines.append("  Booking: " + "; ".join(details) + ".")
    if h.get("amenities"):
        lines.append("  Amenities: " + ", ".join(h["amenities"]) + ".")
    if h.get("description"):
        lines.append(f"  About: {h['description']}")
    if options:
        picks = "; ".join(
            f"{o['name']} ({o.get('stars', '?')}-star, {o.get('area', '')}, rated {o.get('rating') or '?'}, from "
            f"{inr(o['rooms'][0]['price_inr'])}/night" + (f", {', '.join(o['amenities'][:3])}" if o.get("amenities") else "") + ")"
            for o in options)
        lines.append(f"  Other stays free in {city(h.get('city_code', ''))} on the same dates (from our search, suggest only these): {picks}.")
    elif check_in:
        lines.append("  No other stays found for the same dates.")
    return lines


def spend_lines(payments: list[dict]) -> list[str]:
    """The user's paid transactions as facts, so questions like "last payment" or "spend on 5 Oct" are answered from data."""
    if not payments:
        return ["Payments: no paid transactions yet."]
    rows = [(to_ist(p["paid_at"]), p) for p in payments if p.get("paid_at")]
    rows.sort(key=lambda r: r[0], reverse=True)
    total = sum(p["amount_inr"] for _, p in rows)
    last_at, last = rows[0]
    lines = [f"Payments (paid, newest first; last {len(rows)} shown, total {inr(total)}). Last transaction: {inr(last['amount_inr'])} "
             f"for {last['kind']} on {last_at:%a %d %b %Y, %H:%M}."]
    lines += [f"  {at:%d %b %Y}: {inr(p['amount_inr'])} ({p['kind']})" for at, p in rows]
    return lines


def describe(now: datetime, bookings: list[dict], stay: dict | None, loc: dict | None, label: str | None,
             route: dict | None, stays: list[dict] | None = None, options: dict | None = None,
             payments: list[dict] | None = None) -> TripInfo:
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
    stays = stays or ([stay] if stay else [])
    for st in stays:
        lines += _stay_lines(now, st, (options or {}).get(st.get("id") or st.get("ref"), []))
    if not stays:
        lines.append("No hotel booked with us.")
    lines += spend_lines(payments or [])
    if loc:
        where = f"near {label}" if label else "shared"
        lines.append(f"User location: {where}, shared {location_age_min(loc)} min ago."
                     + (f" About {route['km']} km and {route['minutes']} min by car (no live traffic) to the departure airport." if route else ""))
    else:
        lines.append("User location: not shared. If you need it (lost, ETA), use action ask_location.")
    return TripInfo("\n".join(lines), nxt, stay, loc, route)
