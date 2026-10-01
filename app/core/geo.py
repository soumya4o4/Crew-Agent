"""Location helpers shared by every agent: distances, map links, the user's last shared location, place categories.
Pure functions only; the network lookups live in app/services/geo_service.py."""
import math
import re
from datetime import datetime, timedelta
from urllib.parse import quote_plus

from app.core.places import AIRPORT_COORDS
from app.core.utils import IST, now_ist

LOCATION_TTL = timedelta(hours=3)  # a shared pin is trusted for this long; people move

# key -> (emoji, label, OpenStreetMap (key, value) filters, Nominatim search word)
PLACE_CATEGORIES = {
    "cafe": ("☕", "Cafés", [("amenity", "cafe")], "cafe"),
    "food": ("🍽️", "Restaurants", [("amenity", "restaurant"), ("amenity", "fast_food")], "restaurant"),
    "atm": ("🏧", "ATMs & banks", [("amenity", "atm"), ("amenity", "bank")], "atm"),
    "pharmacy": ("💊", "Pharmacy & hospitals", [("amenity", "pharmacy"), ("amenity", "hospital"), ("amenity", "clinic")], "pharmacy"),
    "fuel": ("⛽", "Petrol pumps", [("amenity", "fuel")], "fuel"),
    "sights": ("🏛️", "Things to see", [("tourism", "attraction"), ("tourism", "museum"), ("historic", "monument"), ("historic", "fort")], "tourist attraction"),
    "park": ("🌳", "Parks", [("leisure", "park")], "park"),
    "mall": ("🛍️", "Malls", [("shop", "mall")], "mall"),
}
# What people type -> category key. Anything else is searched by name ("biryani", "bookstore").
PLACE_ALIASES = {
    "cafe": "cafe", "cafes": "cafe", "coffee": "cafe", "chai": "cafe", "tea": "cafe",
    "restaurant": "food", "restaurants": "food", "food": "food", "khana": "food", "dinner": "food", "lunch": "food", "breakfast": "food",
    "atm": "atm", "atms": "atm", "bank": "atm", "cash": "atm",
    "pharmacy": "pharmacy", "chemist": "pharmacy", "medical": "pharmacy", "medicine": "pharmacy", "hospital": "pharmacy", "doctor": "pharmacy",
    "petrol": "fuel", "fuel": "fuel", "pump": "fuel", "diesel": "fuel",
    "sightseeing": "sights", "attractions": "sights", "museum": "sights", "fort": "sights", "monument": "sights",
    "park": "park", "parks": "park", "garden": "park", "mall": "mall", "malls": "mall", "shopping": "mall",
}
_ALIAS_RE = re.compile(r"\b(" + "|".join(sorted(PLACE_ALIASES, key=len, reverse=True)) + r")\b")


def category_for(text: str) -> str | None:
    m = _ALIAS_RE.search(text.lower())
    return PLACE_ALIASES[m.group(1)] if m else None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return round(6371000 * 2 * math.asin(math.sqrt(a)))


def fmt_dist(meters: int) -> str:
    return f"{meters} m" if meters < 1000 else f"{meters / 1000:.1f} km"


def maps_link(lat: float | None = None, lon: float | None = None, query: str | None = None) -> str:
    """A Google Maps directions link. The phone's own GPS is the starting point, so no API key and no shared location needed."""
    dest = f"{lat},{lon}" if lat is not None and lon is not None else quote_plus(query or "")
    return f"https://www.google.com/maps/dir/?api=1&destination={dest}&travelmode=driving"


def nearest_city(lat: float, lon: float, max_km: int = 60) -> str | None:
    """The airport city (from the `airports` table) closest to a point, if within max_km."""
    if not AIRPORT_COORDS:
        return None
    code, dist = min(((c, haversine_m(lat, lon, *xy)) for c, xy in AIRPORT_COORDS.items()), key=lambda t: t[1])
    return code if dist <= max_km * 1000 else None


# ---- the user's last shared location lives in the conversation context: ctx["loc"]
def save_location(ctx: dict, lat: float, lon: float, name: str = "", address: str = "") -> dict:
    ctx["loc"] = {"lat": lat, "lon": lon, "name": name or "", "address": address or "", "at": now_ist().isoformat()}
    return ctx["loc"]


def fresh_location(ctx: dict) -> dict | None:
    loc = ctx.get("loc")
    if not loc:
        return None
    try:
        age = now_ist() - datetime.fromisoformat(loc["at"]).astimezone(IST)
    except (KeyError, ValueError):
        return None
    return loc if age <= LOCATION_TTL else None


def location_age_min(loc: dict) -> int:
    return max(0, int((now_ist() - datetime.fromisoformat(loc["at"]).astimezone(IST)).total_seconds() // 60))
