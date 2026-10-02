"""Hotel-specific text formatting: hotel cards, list rows, the stay voucher, the cancellation policy."""
import re
from datetime import date, datetime, timedelta

from app.core.places import city
from app.core.utils import IST, inr, now_ist

CHECK_IN_HOUR, CHECK_IN_LABEL, CHECK_OUT_LABEL = 14, "2:00 PM", "11:00 AM"
FREE_CANCEL_HOURS = 24       # free cancellation until this long before check-in
STATUS_ICON = {"confirmed": "✅", "pending": "⏳", "cancelled": "❌"}


def fmt_day(d: date | str) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d:%a, %d %b}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def free_cancel_until(check_in: date | str) -> datetime:
    d = date.fromisoformat(check_in) if isinstance(check_in, str) else check_in
    return datetime.combine(d, datetime.min.time(), IST).replace(hour=CHECK_IN_HOUR) - timedelta(hours=FREE_CANCEL_HOURS)


def cancel_policy(check_in: date | str) -> str:
    until = free_cancel_until(check_in)
    if now_ist() <= until:
        return f"✅ Free cancellation until {until:%a, %d %b, %H:%M}"
    return "🚫 Free cancellation has ended for these dates"


def hotel_card(h: dict) -> str:
    lines = [f"🏨 *{h['name']}* · {h['stars']}★", f"📍 {h['area']}, {city(h['city_code'])} · ⭐ {h['rating']}"]
    if h.get("amenities"):
        lines.append("✨ " + " · ".join(h["amenities"][:5]))
    if h.get("description"):
        lines.append(f"_{h['description']}_")
    return "\n".join(lines)


# what people type -> the amenity name used in our data
AMENITY_WORDS = {"pool": "Pool", "swimming": "Pool", "gym": "Gym", "wifi": "Free WiFi", "wi-fi": "Free WiFi", "internet": "Free WiFi",
                 "breakfast": "Breakfast included", "nashta": "Breakfast included", "parking": "Parking", "spa": "Spa",
                 "restaurant": "Restaurant", "bar": "Bar", "shuttle": "Airport shuttle"}
_AMENITY_WORD = re.compile(r"\b(" + "|".join(re.escape(w) for w in AMENITY_WORDS) + r")\b")
_AMENITY_ASK = re.compile(r"\b(amenit\w*|facilit\w*|suvidha\w*|kya kya|inclusions?|what(?:'s| is| all)? (?:included|available|there))\b")


def amenities_asked(text: str) -> list[str]:
    """The amenities named in a message ("pool hai?", "gym and wifi"), as they appear in our data."""
    return list(dict.fromkeys(AMENITY_WORDS[w] for w in _AMENITY_WORD.findall(text.lower())))


def asks_amenities(text: str) -> bool:
    return bool(amenities_asked(text) or _AMENITY_ASK.search(text.lower()))


def amenity_answer(h: dict, asked: list[str]) -> str:
    """What this hotel has: a yes or no for each thing asked about, then everything it offers."""
    have = h.get("amenities") or []
    lines = [f"{'✅ Yes' if a in have else '❌ No'}, *{h['name']}* {'has' if a in have else 'does not have'} {a.lower()}." for a in asked]
    everything = " · ".join(have) if have else "no extra amenities listed"
    return "\n".join(lines + [f"✨ *{h['name']}* offers: {everything}."])


def hotel_tags(hotels: list[dict]) -> dict[str, list[str]]:
    """Playful labels so the list is easy to scan. Each hotel carries `rooms` (cheapest first)."""
    tags: dict[str, list[str]] = {h["id"]: [] for h in hotels}
    if len(hotels) > 1:
        tags[min(hotels, key=lambda h: h["rooms"][0]["price_inr"])["id"]].append("💸 Cheapest")
        tags[max(hotels, key=lambda h: (h["rating"], h["stars"]))["id"]].append("🏆 Top rated")
    for h in hotels:
        if sum(r["left"] for r in h["rooms"]) <= 2:
            tags[h["id"]].append("🔥 Few rooms")
    return tags


def hotel_row(h: dict, tags: list[str] | None = None) -> tuple[str, str, str]:
    parts = [f"from {inr(h['rooms'][0]['price_inr'])}/night", f"⭐ {h['rating']}", f"{h['stars']}★"] + (tags or [])
    return f"htl:{h['id']}", h["name"], " · ".join(parts)


def room_row(r: dict, nights: int) -> tuple[str, str, str]:
    left = " · 🔥 Last room!" if r["left"] == 1 else f" · 🔥 {r['left']} left" if r["left"] <= 3 else ""
    return (f"hroom:{r['id']}", f"{r['room_type']} · {inr(r['price_inr'])}/night",
            f"{r['bed']} · up to {r['max_guests']} guests · {inr(r['price_inr'] * nights)} total{left}")


def stay_countdown(check_in: date | str) -> str:
    d = date.fromisoformat(check_in) if isinstance(check_in, str) else check_in
    days = (d - now_ist().date()).days
    if days <= 0:
        return "That's *today*, time to pack up and head over! 🧳"
    if days == 1:
        return "That's *tomorrow*, start packing! 🧳"
    return f"That's in *{days} days*, plenty of time to get excited! 🧳"


def stay_text(b: dict) -> str:
    """The booking details, used by the voucher and My Stays."""
    h, r = b["hotels"], b["hotel_rooms"]
    nights = (date.fromisoformat(b["check_out"]) - date.fromisoformat(b["check_in"])).days
    guests = f"👥 {b['guests']} guests" if b["guests"] > 1 else "👤 1 guest"
    return (f"🎫 Ref: *{b['ref']}*\n{guests} · *{b['guest_name']}*\n━━━━━━━━━━━━━━━\n{hotel_card_short(h)}\n"
            f"🛏️ {r['room_type']} · {r['bed']}\n"
            f"🛎️ Check-in: *{fmt_day(b['check_in'])}* from {CHECK_IN_LABEL}\n"
            f"🚪 Check-out: *{fmt_day(b['check_out'])}* by {CHECK_OUT_LABEL}\n━━━━━━━━━━━━━━━\n"
            f"💰 Total: *{inr(b['total_price_inr'])}* ({plural(nights, 'night')})")


def hotel_card_short(h: dict) -> str:
    return f"🏨 *{h['name']}* · {h['stars']}★\n📍 {h['area']}, {city(h['city_code'])}"
