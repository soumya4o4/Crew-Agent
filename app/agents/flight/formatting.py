"""Flight-specific text formatting: flight cards, list rows, tags."""
from datetime import datetime

from app.core.places import city, country_of
from app.core.utils import dur, inr, now_ist, to_ist


def stops_label(n: int) -> str:
    return "Non-stop" if n == 0 else f"{n} stop"


def baggage_label(f: dict) -> str:
    """"1 checked bag", "Cabin bag only", or (for flights we stored with a weight) "15 kg"."""
    bags = f.get("checked_bags") or 0
    if bags:
        return f"{bags} checked bag{'s' if bags > 1 else ''}"
    return f"{f['baggage_kg']} kg" if f.get("baggage_kg") else "Cabin bag only"


def pnr_of(booking: dict) -> str:
    """The reference to show: the airline's once the ticket is issued, else ours."""
    return booking.get("airline_pnr") or booking["pnr"]


def countdown(dep: datetime) -> str:
    days = (dep.date() - now_ist().date()).days
    if days <= 0:
        return "That's *today*, time to head to the airport! 🏃"
    if days == 1:
        return "That's *tomorrow*, start packing! 🧳"
    return f"That's in *{days} days*, plenty of time to get excited! 🧳"


def flight_card(f: dict) -> str:
    dep, arr = to_ist(f["departure_time"]), to_ist(f["arrival_time"])
    lines = [
        f"✈️ *{f['flight_no']}* · {f['airline']}",
        f"*{city(f['from_code'])}* ({f['from_code']}) ➜ *{city(f['to_code'])}* ({f['to_code']})",
        f"🛫 {dep:%a, %d %b} · *{dep:%H:%M}*",
        f"🛬 *{arr:%H:%M}* · ⏱ {dur(f['duration_min'])} · {stops_label(f['stops'])}",
        f"🧳 {baggage_label(f)} · {f['class']} · " + ("↩️ Refundable" if f["refundable"] else "🚫 Non-refundable"),
    ]
    if f["status"] == "delayed":
        lines.append("⚠️ This flight is running late")
    return "\n".join(lines)


def flight_row(f: dict, tags: list[str] | None = None) -> tuple[str, str, str]:
    dep = to_ist(f["departure_time"])
    parts = [inr(f["price_inr"]), dur(f["duration_min"]), stops_label(f["stops"])]
    if f["class"] == "Business":
        parts.append("Business")
    if f["status"] == "delayed":
        parts.append("⚠️ Delayed")
    parts += tags or []
    return f"flt:{f['id']}", f"{dep:%H:%M} · {f['flight_no']}", " · ".join(parts)


def flight_tags(flights: list[dict]) -> dict[str, list[str]]:
    """Playful labels so the list is easy to scan."""
    tags: dict[str, list[str]] = {f["id"]: [] for f in flights}
    if len(flights) > 1:
        tags[min(flights, key=lambda f: f["price_inr"])["id"]].append("💸 Cheapest")
        tags[min(flights, key=lambda f: f["duration_min"])["id"]].append("⚡ Fastest")
    for f in flights:
        hour = to_ist(f["departure_time"]).hour
        if hour < 7:
            tags[f["id"]].append("🌅 Early bird")
        elif hour >= 22:
            tags[f["id"]].append("🌙 Late night")
    return tags


# What to suggest after a booking: button label and how to name it in a sentence ({city} is filled in).
NEXT_STEPS = {
    "hotel": ("🏨 Add Hotel", "a hotel"), "cab": ("🚕 Airport Cab", "a cab"), "events": ("🎟️ Events", "events"),
    "planner": ("🗺️ Plan My Trip", "a trip plan"), "visa": ("🛂 Visa", "a visa"), "forex": ("💱 Forex", "forex"),
}
PITCH = {
    "hotel": "a stay in {city}", "cab": "a cab to or from the airport", "visa": "your visa", "forex": "currency for the trip",
    "events": "things to do in {city}", "planner": "a day-by-day plan",
}


def suggest_steps(trip: dict, queue: list | None = None, visa: dict | None = None, done: tuple = ()) -> list[str]:
    """What this traveller most likely needs next, in order, worked out from the trip itself:
    what they asked for, then stay, transfers, money. Visa is never suggested: it only comes up when the traveller asks."""
    order = [x for x in queue or [] if x in NEXT_STEPS]
    if trip.get("intl"):
        order += ["hotel", "cab", "forex"]
    else:
        order += ["hotel", "cab"]
    order += ["events", "planner"]
    return [x for x in dict.fromkeys(order) if x not in done]


async def city_tip(advisor, code: str, default: str) -> str:
    """A short local tip for a city: from the advisor (cached) when there is one, else the plain default."""
    tip = await advisor.city_tip(city(code), country_of(code)) if advisor else None
    return tip or default
