"""Dummy events: invented listings at real public spots (venues come from cab_data's places, with their coordinates).
Titles, dates and prices are made up. Titles stay under 24 characters (WhatsApp list titles)."""
import uuid
from datetime import datetime, timedelta, timezone

import cab_data

IST = timezone(timedelta(hours=5, minutes=30))
DAYS_AHEAD = 30

# category -> (titles, description, hours to start at, (min price, max price))
CATEGORIES = {
    "music": (["Indie Music Night", "Sunset Acoustic Session", "Live Band Friday"], "Live music from local artists.", [19, 20, 21], (499, 1999)),
    "comedy": (["Stand-up Saturdays", "Open Mic Comedy Hour"], "An evening of stand-up and open mic.", [19, 20], (399, 999)),
    "food": (["Street Food Carnival", "Weekend Food Truck Fest"], "Dozens of food stalls and trucks in one place.", [12, 17, 18], (0, 299)),
    "sports": (["Cricket Screening Night", "5K Fun Run"], "Watch on the big screen, or run it yourself.", [6, 18, 19], (0, 799)),
    "art": (["Weekend Art Walk", "Photography Exhibition"], "Local artists, open studios and a chai stall.", [11, 16], (0, 299)),
    "festival": (["Cultural Fest", "Craft & Flea Market"], "Music, crafts and street performances.", [11, 15, 17], (0, 499)),
    "workshop": (["Pottery Workshop", "Beginner Salsa Class"], "Hands-on session for beginners, all materials included.", [10, 17, 18], (299, 899)),
}
# Dubai is not in cab_data
EXTRA_PLACES = {"DXB": [("Dubai Marina Walk", "area", 25.0805, 55.1403), ("Downtown Dubai", "area", 25.1972, 55.2744),
                        ("Jumeirah Beach", "landmark", 25.2048, 55.2420), ("Deira City Centre", "mall", 25.2532, 55.3300)]}
EVENTS_PER_CITY = 10


def build_events(rng) -> list[dict]:
    now = datetime.now(IST)
    cities = {c: [p for p in (cab_data.PLACES.get(c) or []) if p[1] in ("area", "mall", "landmark")] for c in cab_data.PLACES}
    cities |= EXTRA_PLACES
    events = []
    for code, places in cities.items():
        for i in range(EVENTS_PER_CITY):
            category = list(CATEGORIES)[i % len(CATEGORIES)] if i < len(CATEGORIES) else rng.choice(list(CATEGORIES))
            titles, description, hours, (lo, hi) = CATEGORIES[category]
            venue, _, lat, lon = rng.choice(places)
            day = now.date() + timedelta(days=rng.randint(0, 2) if i < 4 else rng.randint(0, DAYS_AHEAD - 1))  # a few always soon
            starts = datetime.combine(day, datetime.min.time(), IST).replace(hour=rng.choice(hours), minute=rng.choice([0, 30]))
            if starts <= now:
                starts += timedelta(days=1)
            price = 0 if lo == 0 and rng.random() < 0.4 else rng.randrange(lo, hi + 1, 50) if hi else 0
            events.append({"id": str(uuid.uuid4()), "city_code": code, "title": rng.choice(titles), "category": category,
                           "venue": venue, "area": venue, "lat": lat, "lon": lon, "starts_at": starts.isoformat(),
                           "price_inr": price, "description": description})
    return events
