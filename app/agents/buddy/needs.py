"""What the user will probably need next, worked out from facts (never guessed by the model): the next flight, the hotel,
where they are going, and what they just said. Buddy turns the result into buttons, so one answer leads naturally to the
next step: a flight tomorrow means a ride to the airport, a landing with no hotel means a stay, a trip abroad means visa and forex.

Each need is (service key, button label); labels fit WhatsApp's 20 characters."""
import re
from datetime import datetime

from app.core.places import city, is_international
from app.core.utils import to_ist

BORED = re.compile(r"\b(bore|bored|boring|time ?pass|kuch (?:karne|dekhne)|free (?:hu|hoon|time)|what to do|kya karu|ghoomne|explore)\b")
HUNGRY = re.compile(r"\b(hungry|bhookh|bhuk|khana|khaana|food|eat|dinner|lunch|breakfast|cafe|coffee|chai)\b")
ABOUT_STAY = re.compile(r"\b(hotel|stay|room|check ?-?in|check ?-?out|amenit\w*|pool|wifi|breakfast)\b")
ABOUT_FLIGHT = re.compile(r"\b(flight|airport|boarding|gate|delay\w*|pnr|terminal|baggage|luggage)\b")


def _label(base: str, place: str = "") -> str:
    return f"{base} {place}".strip()[:20]


def next_needs(*, now: datetime, text: str, flight: dict | None, stay: dict | None, trip: dict | None,
               has_location: bool) -> list[tuple[str, str]]:
    """Up to 3 (service, label) pairs, most useful first. `flight` is a booking (with `flights`), `stay` a hotel booking,
    `trip` the trip remembered from the last booking in this chat (it may be further away than the 48 h Buddy sees)."""
    low = text.lower()
    needs: list[tuple[str, str]] = []
    f = flight["flights"] if flight else None
    hotel_city = ((stay or {}).get("hotels") or {}).get("city_code")

    if f and to_ist(f["departure_time"]) > now:
        hours = (to_ist(f["departure_time"]) - now).total_seconds() / 3600
        if hours <= 24:
            needs.append(("cab", "🚕 Cab to airport"))
        if f["to_code"] != hotel_city:
            needs.append(("hotel", _label("🏨 Stay in", city(f["to_code"]))))
        if is_international(f["from_code"], f["to_code"]):
            needs += [("forex", "💱 Forex")]
        needs.append(("events", _label("🎟️", city(f["to_code"]) + " events")))
    elif trip and trip.get("to") and not f:  # booked earlier, further away: still worth planning around
        if trip["to"] != hotel_city:
            needs.append(("hotel", _label("🏨 Stay in", trip.get("city") or city(trip["to"]))))
        if trip.get("intl"):
            needs += [("forex", "💱 Forex")]
        needs.append(("cab", "🚕 Airport cab"))

    if stay and ABOUT_STAY.search(low):
        needs = [("cab", "🚕 Ride to hotel"), ("nearby", "🧭 Food nearby"), ("events", "🎟️ Things to do")] + needs
    if BORED.search(low):
        needs = [("nearby", "🧭 Around me"), ("events", "🎟️ Events")] + needs
    elif HUNGRY.search(low):
        needs = [("nearby", "🍽️ Food near me")] + needs
    if f and ABOUT_FLIGHT.search(low) and not has_location:
        needs = [x for x in needs if x[0] != "events"]  # in a flight scramble, a concert is noise

    seen, out = set(), []
    for service, label in needs:
        if service not in seen:
            seen.add(service)
            out.append((service, label))
    return out[:3]
