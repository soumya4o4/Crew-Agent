"""Seed Supabase with realistic dummy data for the flight-booking feature.

Re-runnable: removes the previous dummy rows first, then re-inserts.
Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the project-root .env.

    python supabase/seed/seed_dummy_data.py
"""
import os
import random
import string
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]  # app.* and cab_data
import cab_data  # noqa: E402
import event_data  # noqa: E402
import hotel_data  # noqa: E402
import visa_data  # noqa: E402
from app.agents.cab.pricing import distance_km, quote  # noqa: E402
load_dotenv(ROOT / ".env")

IST = timezone(timedelta(hours=5, minutes=30))
DAYS_AHEAD = 30
BATCH = 500
rng = random.Random(42)  # stable schedule between runs; prices/status still vary per day

# --------------------------------------------------------------------- static data
AIRPORTS = [
    ("IDR", "Indore", "Devi Ahilya Bai Holkar Airport", "India", 22.7218, 75.8011),
    ("BOM", "Mumbai", "Chhatrapati Shivaji Maharaj International Airport", "India", 19.0887, 72.8679),
    ("DEL", "Delhi", "Indira Gandhi International Airport", "India", 28.5562, 77.1),
    ("BLR", "Bengaluru", "Kempegowda International Airport", "India", 13.1989, 77.7068),
    ("HYD", "Hyderabad", "Rajiv Gandhi International Airport", "India", 17.2403, 78.4294),
    ("MAA", "Chennai", "Chennai International Airport", "India", 12.9941, 80.1709),
    ("CCU", "Kolkata", "Netaji Subhas Chandra Bose International Airport", "India", 22.6547, 88.4467),
    ("PNQ", "Pune", "Pune Airport", "India", 18.5822, 73.9197),
    ("GOI", "Goa", "Manohar International Airport", "India", 15.7441, 73.8644),
    ("AMD", "Ahmedabad", "Sardar Vallabhbhai Patel International Airport", "India", 23.0772, 72.6347),
    ("DXB", "Dubai", "Dubai International Airport", "United Arab Emirates", 25.2532, 55.3657),
]

# (a, b, typical duration in minutes, base economy fare in INR). Each is generated both ways.
ROUTES = [
    ("IDR", "BOM", 90, 3800),
    ("IDR", "DEL", 110, 4300),
    ("IDR", "BLR", 130, 4900),
    ("IDR", "HYD", 105, 4300),
    ("IDR", "PNQ", 85, 3600),
    ("IDR", "AMD", 65, 3300),
    ("BOM", "DEL", 130, 5200),
    ("BOM", "BLR", 105, 4400),
    ("BOM", "GOI", 70, 3500),
    ("DEL", "BLR", 175, 6300),
    ("DEL", "HYD", 140, 5200),
    ("DEL", "CCU", 140, 5300),
    ("BLR", "MAA", 60, 3300),
    ("HYD", "BLR", 75, 3500),
    ("BOM", "DXB", 195, 15500),  # the one international route
]

# airline -> (code, digits in flight number, price multiplier, pick weight)
AIRLINES = {
    "IndiGo": ("6E", 4, 1.00, 45),
    "Air India": ("AI", 3, 1.12, 25),
    "Akasa Air": ("QP", 4, 0.95, 15),
    "SpiceJet": ("SG", 4, 0.92, 15),
}

# name, (earliest, latest) departure hour:minute, price multiplier
SLOTS = {
    "morning": ((5, 30), (10, 0), 1.15),
    "afternoon": ((11, 0), (15, 0), 0.95),
    "evening": ((16, 0), (19, 30), 1.15),
    "night": ((20, 0), (23, 45), 0.85),
}

USERS = [
    ("Aarav Sharma", "window", "veg"),
    ("Priya Patel", "aisle", "jain"),
    ("Rohan Verma", "window", "non-veg"),
    ("Ananya Iyer", "aisle", "veg"),
    ("Vikram Singh", "aisle", "non-veg"),
    ("Neha Gupta", "window", "veg"),
    ("Arjun Reddy", "middle", "non-veg"),
    ("Sneha Kulkarni", "window", "veg"),
    ("Karan Malhotra", "aisle", "none"),
    ("Divya Nair", "window", "jain"),
]
# +91 5xxxxxxxxx is not an allocated Indian mobile range, so these can't reach a real person.
DUMMY_PHONES = [f"+91500000{i:04d}" for i in range(1, len(USERS) + 1)]


# ------------------------------------------------------------------------ helpers
def pick_airline():
    names = list(AIRLINES)
    return rng.choices(names, weights=[AIRLINES[n][3] for n in names])[0]


def build_schedule():
    """A fixed daily timetable per directed route: 4-6 flights across the day."""
    used_numbers = set()
    schedule = []
    for a, b, base_min, base_fare in ROUTES:
        for src, dst in ((a, b), (b, a)):
            slots = list(SLOTS) + rng.sample(list(SLOTS), rng.randint(0, 2))
            for slot in slots:
                (h1, m1), (h2, m2), slot_mult = SLOTS[slot][0], SLOTS[slot][1], SLOTS[slot][2]
                lo, hi = h1 * 60 + m1, h2 * 60 + m2
                dep_min = rng.randrange(lo, hi + 1, 5)

                airline = pick_airline()
                code, digits, air_mult, _ = AIRLINES[airline]
                while True:
                    number = f"{code}-{rng.randint(10 ** (digits - 1), 10 ** digits - 1)}"
                    if number not in used_numbers:
                        used_numbers.add(number)
                        break

                stops = 1 if rng.random() < 0.07 else 0
                duration = base_min + rng.randrange(-5, 6, 5)
                fare_mult = 1.0
                if stops:
                    duration = int(duration * 1.7) + 60  # layover + longer routing
                    fare_mult = 0.85
                business = airline == "Air India" and rng.random() < 0.2
                international = "DXB" in (src, dst)

                schedule.append({
                    "airline": airline, "flight_no": number,
                    "from_code": src, "to_code": dst,
                    "dep_min": dep_min, "duration": duration, "stops": stops,
                    "base_fare": base_fare * air_mult * slot_mult * fare_mult,
                    "class": "Business" if business else "Economy",
                    "baggage": (35 if international else 25) if business else (30 if international else 15),
                    "refund_p": 0.9 if business else (0.5 if airline == "Air India" else 0.2),
                    "international": international,
                })
    return schedule


def price_for(entry, days_ahead, dep_dt):
    advance = 1 + 0.30 * max(0, 10 - days_ahead) / 10  # up to +30% on the closest dates
    weekend = 1.05 if dep_dt.weekday() in (4, 6) else 1.0
    price = entry["base_fare"] * advance * weekend * rng.uniform(0.93, 1.07)
    lo, hi = (12000, 25000) if entry["international"] else (3000, 9000)
    price = min(max(price, lo), hi)  # clamp the Economy fare, then apply the cabin multiplier
    if entry["class"] == "Business":
        price *= 2.2
    return int(round(price / 10) * 10)


def build_flights():
    now = datetime.now(IST)
    today = now.date()
    flights = []
    for entry in build_schedule():
        for d in range(DAYS_AHEAD):
            dep = datetime.combine(today + timedelta(days=d), datetime.min.time(), IST) + timedelta(
                minutes=entry["dep_min"])
            if dep <= now:
                continue  # don't list flights that already left today
            roll = rng.random()
            status, seats = "scheduled", rng.randint(2, 60) if d > 7 else rng.randint(1, 25)
            if roll < 0.03:
                seats = 0  # sold out
            elif roll < 0.06:
                status = "delayed"
            elif roll < 0.08:
                status = "cancelled"
            flights.append({
                "id": str(uuid.uuid4()),
                "airline": entry["airline"], "flight_no": entry["flight_no"],
                "from_code": entry["from_code"], "to_code": entry["to_code"],
                "departure_time": dep.isoformat(),
                "arrival_time": (dep + timedelta(minutes=entry["duration"])).isoformat(),
                "duration_min": entry["duration"],
                "price_inr": price_for(entry, d, dep),
                "class": entry["class"],
                "seats_left": seats,
                "baggage_kg": entry["baggage"],
                "stops": entry["stops"],
                "refundable": rng.random() < entry["refund_p"],
                "status": status,
            })
    return flights


def build_users():
    now = datetime.now(IST)
    return [{
        "id": str(uuid.uuid4()),
        "name": name,
        "phone": phone,
        "preferences": {"seat": seat, "meal": meal},
        "created_at": (now - timedelta(days=rng.randint(5, 120))).isoformat(),
    } for (name, seat, meal), phone in zip(USERS, DUMMY_PHONES)]


def build_bookings(users, flights):
    now = datetime.now(IST)
    horizon = (now + timedelta(days=3)).isoformat(), (now + timedelta(days=25)).isoformat()
    upcoming = [f for f in flights if horizon[0] <= f["departure_time"] <= horizon[1]]
    bookable = [f for f in upcoming if f["status"] == "scheduled" and f["seats_left"] > 0]
    delayed = [f for f in upcoming if f["status"] == "delayed" and f["seats_left"] > 0]

    statuses = ["confirmed"] * 8 + ["pending"] * 3 + ["cancelled"] * 4
    rng.shuffle(statuses)

    pnrs, bookings = set(), []
    for i, status in enumerate(statuses):
        user = users[i % len(users)]  # every user gets at least one booking
        # make one confirmed booking land on a delayed flight, to exercise delay messaging
        flight = rng.choice(delayed) if i == 0 and delayed and status == "confirmed" else rng.choice(bookable)
        while True:
            pnr = "".join(rng.choices(string.ascii_uppercase + string.digits, k=6))
            if pnr not in pnrs:
                pnrs.add(pnr)
                break
        bookings.append({
            "id": str(uuid.uuid4()),
            "pnr": pnr,
            "user_id": user["id"],
            "flight_id": flight["id"],
            "passenger_name": user["name"] if rng.random() < 0.7 else rng.choice(USERS)[0],
            "status": status,
            "total_price_inr": flight["price_inr"],
            "created_at": (now - timedelta(days=rng.randint(0, 10), hours=rng.randint(0, 23))).isoformat(),
        })
    return bookings


def build_cab_rides(users, places, drivers, rates):
    """6 dummy rides: upcoming, past and cancelled, in each user's home-ish cities."""
    now = datetime.now(IST)
    by_city = {}
    for p in places:
        by_city.setdefault(p["city_code"], []).append(p)
    rides, refs = [], set()
    for i, (days, status) in enumerate([(2, "confirmed"), (5, "confirmed"), (-3, "completed"), (-8, "completed"),
                                         (3, "cancelled"), (1, "confirmed")]):
        user = users[i % len(users)]
        city = rng.choice(list(by_city))
        pickup, drop = rng.sample(by_city[city], 2)
        vtype = rng.choice(list(cab_data.VEHICLES))
        driver = rng.choice([d for d in drivers if d["city_code"] == city and d["vehicle_type"] == vtype])
        when = (now + timedelta(days=days)).replace(hour=rng.choice([7, 9, 14, 18, 21]), minute=rng.choice([0, 15, 30, 45]),
                                                    second=0, microsecond=0)
        km = distance_km(pickup["lat"], pickup["lon"], drop["lat"], drop["lon"])
        rate = next(r for r in rates if r["city_code"] == city and r["vehicle_type"] == vtype)
        q = quote(rate, km, when, "airport" in (pickup["kind"], drop["kind"]))
        while True:
            ref = "CB" + "".join(rng.choices(string.ascii_uppercase + string.digits, k=5))
            if ref not in refs:
                refs.add(ref)
                break
        rides.append({"id": str(uuid.uuid4()), "ref": ref, "user_id": user["id"], "city_code": city,
                      "pickup_id": pickup["id"], "drop_id": drop["id"], "pickup_time": when.isoformat(),
                      "vehicle_type": vtype, "distance_km": km, "fare_inr": q["fare"], "driver_id": driver["id"],
                      "otp": f"{rng.randint(0, 9999):04d}", "status": status,
                      "cancel_fee_inr": 0, "created_at": (now - timedelta(days=rng.randint(1, 6))).isoformat()})
    return rides


def build_hotel_bookings(users, hotels, rooms):
    """4 dummy stays: upcoming, past and cancelled."""
    now = datetime.now(IST)
    rooms_by_hotel = {}
    for r in rooms:
        rooms_by_hotel.setdefault(r["hotel_id"], []).append(r)
    bookings, refs = [], set()
    for i, (days, nights, status) in enumerate([(4, 2, "confirmed"), (9, 3, "confirmed"), (-6, 2, "confirmed"), (6, 1, "cancelled")]):
        user = users[i % len(users)]
        hotel = rng.choice(hotels)
        room = rng.choice(rooms_by_hotel[hotel["id"]])
        check_in = (now + timedelta(days=days)).date()
        while True:
            ref = "HB" + "".join(rng.choices(string.ascii_uppercase + string.digits, k=5))
            if ref not in refs:
                refs.add(ref)
                break
        bookings.append({"id": str(uuid.uuid4()), "ref": ref, "user_id": user["id"], "hotel_id": hotel["id"],
                         "room_id": room["id"], "guest_name": user["name"], "guests": rng.randint(1, min(2, room["max_guests"])),
                         "check_in": check_in.isoformat(), "check_out": (check_in + timedelta(days=nights)).isoformat(),
                         "total_price_inr": room["price_inr"] * nights, "status": status,
                         "created_at": (now - timedelta(days=rng.randint(1, 8))).isoformat()})
    return bookings


def build_conversations():
    return [
        {"phone": DUMMY_PHONES[0], "current_step": "awaiting_flight_selection",
         "context": {"from": "IDR", "to": "BOM", "date": (datetime.now(IST) + timedelta(days=5)).date().isoformat()}},
        {"phone": DUMMY_PHONES[1], "current_step": "awaiting_passenger_name",
         "context": {"from": "DEL", "to": "BLR"}},
        {"phone": DUMMY_PHONES[2], "current_step": "start", "context": {}},
    ]


def insert(client, table, rows):
    for i in range(0, len(rows), BATCH):
        client.table(table).insert(rows[i:i + BATCH]).execute()
    print(f"  inserted {len(rows):>5} rows into {table}")


def clear_old_data(client):
    """Delete dummy data in FK order. Only touches rows belonging to the dummy users."""
    res = client.table("users").select("id").in_("phone", DUMMY_PHONES).execute()
    ids = [r["id"] for r in res.data]
    if ids:
        client.table("cab_bookings").delete().in_("user_id", ids).execute()
        client.table("hotel_bookings").delete().in_("user_id", ids).execute()
        client.table("bookings").delete().in_("user_id", ids).execute()
    clear_cab_reference_data(client)
    clear_hotel_reference_data(client)
    client.table("conversations").delete().in_("phone", DUMMY_PHONES).execute()
    client.table("users").delete().in_("phone", DUMMY_PHONES).execute()
    # flights are entirely generated; this fails loudly if a real booking still references one
    client.table("flights").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    print("  cleared old dummy data")


def clear_cab_reference_data(client):
    # places/drivers/fares are entirely generated; fails loudly if a real ride still references them
    client.table("cab_drivers").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    client.table("cab_places").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    client.table("cab_fares").delete().neq("city_code", "").execute()


def clear_hotel_reference_data(client):
    # hotels and rooms are entirely generated; fails loudly if a real stay still references them
    client.table("hotel_rooms").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    client.table("hotels").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()


def dummy_users(client):
    """The dummy users, recreated (with flight bookings from the flights already stored) if they are missing."""
    users = sorted(client.table("users").select("*").in_("phone", DUMMY_PHONES).execute().data, key=lambda u: u["phone"])
    if not users:
        now = datetime.now(IST)
        flights = (client.table("flights").select("*").gte("departure_time", (now + timedelta(days=3)).isoformat())
                   .lte("departure_time", (now + timedelta(days=25)).isoformat()).limit(1000).execute().data)
        if not flights:
            sys.exit("No flights found. Run the full seed first (without --cabs-only / --hotels-only).")
        users = build_users()
        insert(client, "users", users)
        insert(client, "bookings", build_bookings(users, flights))
    return users


def seed_events(client):
    """Events are reference data, regenerated from today."""
    client.table("events").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    insert(client, "events", event_data.build_events(rng))


def seed_hotels(client):
    """Hotel data only: leaves flights, bookings and real users untouched."""
    users = dummy_users(client)
    client.table("hotel_bookings").delete().in_("user_id", [u["id"] for u in users]).execute()
    clear_hotel_reference_data(client)
    insert_hotel_data(client, users)


def insert_hotel_data(client, users):
    hotels, rooms = hotel_data.build_hotels(rng)
    insert(client, "hotels", hotels)
    insert(client, "hotel_rooms", rooms)
    insert(client, "hotel_bookings", build_hotel_bookings(users, hotels, rooms))


def seed_cabs(client):
    """Cab data only: leaves flights, bookings and real users untouched."""
    users = dummy_users(client)
    client.table("cab_bookings").delete().in_("user_id", [u["id"] for u in users]).execute()
    clear_cab_reference_data(client)
    insert_cab_data(client, users)


def insert_cab_data(client, users):
    places, rates = cab_data.build_places(), cab_data.build_fares()
    drivers = cab_data.build_drivers(rng)
    insert(client, "cab_places", places)
    insert(client, "cab_fares", rates)
    insert(client, "cab_drivers", drivers)
    insert(client, "cab_bookings", build_cab_rides(users, places, drivers, rates))


def seed_visa_rules(client):
    """Visa rules are reference data: upsert, so re-running never touches applications."""
    rows = visa_data.build_rules()
    client.table("visa_rules").upsert(rows, on_conflict="country_code,purpose").execute()
    print(f"  upserted {len(rows):>4} rows into visa_rules")


def main():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env")
    client = create_client(url, key)

    print("Seeding...")
    if "--visa-only" in sys.argv:
        seed_visa_rules(client)
        return print("Done.")
    if "--cabs-only" in sys.argv:
        seed_cabs(client)
        return print("Done.")
    if "--events-only" in sys.argv:
        seed_events(client)
        return print("Done.")
    if "--hotels-only" in sys.argv:
        seed_hotels(client)
        return print("Done.")
    clear_old_data(client)

    airports = [dict(zip(("code", "city", "name", "country", "lat", "lon"), a)) for a in AIRPORTS]
    client.table("airports").upsert(airports).execute()
    print(f"  upserted {len(airports):>4} rows into airports")

    flights = build_flights()
    users = build_users()
    bookings = build_bookings(users, flights)

    insert(client, "flights", flights)
    insert(client, "users", users)
    insert(client, "bookings", bookings)

    insert_cab_data(client, users)
    insert_hotel_data(client, users)
    seed_events(client)
    seed_visa_rules(client)
    insert(client, "conversations", build_conversations())
    print("Done.")


if __name__ == "__main__":
    main()
