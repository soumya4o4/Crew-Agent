"""Fakes for the location features: a scripted GeoService (no OpenStreetMap calls) and an in-memory EventsRepo."""
import uuid
from datetime import timedelta

from app.core.utils import now_ist


class FakeGeo:
    """Script it: .places_by_radius = {2500: [...], 6000: [...]}, .geocodes = {"vijay nagar": {...}}, .route_result = {...}."""

    def __init__(self):
        near = [{"name": "Joshi Coffee House", "lat": 22.7224, "lon": 75.8514, "dist_m": 350, "info": "coffee shop", "hours": "08:00-22:00"},
                {"name": "Cafe Kava", "lat": 22.7035, "lon": 75.8428, "dist_m": 1200, "info": "cafe", "hours": ""}]
        self.places_by_radius = {2500: near, 6000: near}
        self.geocodes = {"vijay nagar indore": {"lat": 22.7533, "lon": 75.8937, "name": "Vijay Nagar"}}
        self.route_result = {"km": 9.0, "minutes": 18}
        self.label = "Martand Chowk, Indore"
        self.calls = []

    async def reverse(self, lat, lon):
        self.calls.append(("reverse", lat, lon))
        return self.label

    async def geocode(self, query, near=None):
        self.calls.append(("geocode", query))
        return self.geocodes.get(query.lower().strip())

    async def places(self, lat, lon, category=None, text=None, radius_m=2500, limit=9):
        self.calls.append(("places", lat, lon, category, text, radius_m))
        return self.places_by_radius.get(radius_m, [])

    async def route(self, origin, dest):
        self.calls.append(("route", origin, dest))
        return self.route_result


class FakeEventsRepo:
    def __init__(self):
        tomorrow = (now_ist() + timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
        mk = lambda city, title, cat, venue, lat, lon, days, price: {
            "id": str(uuid.uuid4()), "city_code": city, "title": title, "category": cat, "venue": venue, "area": venue, "lat": lat,
            "lon": lon, "starts_at": (tomorrow + timedelta(days=days)).isoformat(), "price_inr": price, "description": f"{title} at {venue}."}
        self.events = {e["id"]: e for e in [
            mk("IDR", "Indie Music Night", "music", "Rajwada Palace", 22.7185, 75.8553, 1, 499),
            mk("IDR", "Stand-up Saturdays", "comedy", "Vijay Nagar", 22.7533, 75.8937, 0, 0),
            mk("IDR", "Street Food Carnival", "food", "Palasia Square", 22.7243, 75.8839, 2, 0),
            mk("BOM", "Sunset Acoustic Session", "music", "Bandra West", 19.0596, 72.8295, 1, 899),
            mk("DXB", "Beach Cricket Night", "sports", "Jumeirah Beach", 25.2048, 55.2420, 40, 300)]}

    def list_events(self, city_code, start, end):
        return sorted((dict(e) for e in self.events.values() if e["city_code"] == city_code
                       and start <= to_dt(e["starts_at"]) < end), key=lambda e: e["starts_at"])

    def get_event(self, event_id):
        return self.events.get(event_id)


def to_dt(ts):
    from datetime import datetime
    return datetime.fromisoformat(ts)
