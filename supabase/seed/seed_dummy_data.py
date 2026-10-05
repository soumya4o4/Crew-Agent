"""Seed Supabase with reference data (airports, cabs, events, visa rules) and a few dummy users.

Flights are NOT seeded: they come live from Duffel, and every search is mirrored into the `flights` table.

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
import visa_data  # noqa: E402
from app.agents.cab.pricing import distance_km, quote  # noqa: E402
load_dotenv(ROOT / ".env")

IST = timezone(timedelta(hours=5, minutes=30))
BATCH = 500
rng = random.Random(42)  # stable data between runs

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
    ("JFK", "New York", "John F. Kennedy International Airport", "United States", 40.6413, -73.7781),
    ("SFO", "San Francisco", "San Francisco International Airport", "United States", 37.6213, -122.379),
    ("LHR", "London", "Heathrow Airport", "United Kingdom", 51.47, -0.4543),
    ("CDG", "Paris", "Charles de Gaulle Airport", "France", 49.0097, 2.5479),
    ("SIN", "Singapore", "Changi Airport", "Singapore", 1.3644, 103.9915),
    ("BKK", "Bangkok", "Suvarnabhumi Airport", "Thailand", 13.69, 100.7501),
    ("NRT", "Tokyo", "Narita International Airport", "Japan", 35.772, 140.3929),
    ("SYD", "Sydney", "Sydney Kingsford Smith Airport", "Australia", -33.9399, 151.1753),
]

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


def build_users():
    now = datetime.now(IST)
    return [{
        "id": str(uuid.uuid4()),
        "name": name,
        "phone": phone,
        "preferences": {"seat": seat, "meal": meal},
        "created_at": (now - timedelta(days=rng.randint(5, 120))).isoformat(),
    } for (name, seat, meal), phone in zip(USERS, DUMMY_PHONES)]


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
    """Delete dummy data in FK order. Only touches rows belonging to the dummy users. (Hotels and flights are not seeded: they come live from Hotelbeds and Duffel.)"""
    res = client.table("users").select("id").in_("phone", DUMMY_PHONES).execute()
    ids = [r["id"] for r in res.data]
    if ids:
        client.table("cab_bookings").delete().in_("user_id", ids).execute()
        client.table("hotel_bookings").delete().in_("user_id", ids).execute()
        client.table("bookings").delete().in_("user_id", ids).execute()
    clear_cab_reference_data(client)
    client.table("conversations").delete().in_("phone", DUMMY_PHONES).execute()
    client.table("users").delete().in_("phone", DUMMY_PHONES).execute()
    print("  cleared old dummy data")


def clear_cab_reference_data(client):
    # places/drivers/fares are entirely generated; fails loudly if a real ride still references them
    client.table("cab_drivers").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    client.table("cab_places").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    client.table("cab_fares").delete().neq("city_code", "").execute()


def dummy_users(client):
    """The dummy users, recreated if they are missing."""
    users = sorted(client.table("users").select("*").in_("phone", DUMMY_PHONES).execute().data, key=lambda u: u["phone"])
    if not users:
        users = build_users()
        insert(client, "users", users)
    return users


def seed_events(client):
    """Events are reference data, regenerated from today."""
    client.table("events").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    insert(client, "events", event_data.build_events(rng))


def seed_cabs(client):
    """Cab data only: leaves bookings and real users untouched."""
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


def seed_international(client):
    """Add the world airports without touching anything else."""
    airports = [dict(zip(("code", "city", "name", "country", "lat", "lon"), a)) for a in AIRPORTS]
    client.table("airports").upsert(airports).execute()
    print(f"  upserted {len(airports):>4} rows into airports")


def main():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env")
    client = create_client(url, key)

    print("Seeding...")
    if "--international-only" in sys.argv:
        seed_international(client)
        return print("Done.")
    if "--visa-only" in sys.argv:
        seed_visa_rules(client)
        return print("Done.")
    if "--cabs-only" in sys.argv:
        seed_cabs(client)
        return print("Done.")
    if "--events-only" in sys.argv:
        seed_events(client)
        return print("Done.")
    clear_old_data(client)

    airports = [dict(zip(("code", "city", "name", "country", "lat", "lon"), a)) for a in AIRPORTS]
    client.table("airports").upsert(airports).execute()
    print(f"  upserted {len(airports):>4} rows into airports")

    users = build_users()
    insert(client, "users", users)

    insert_cab_data(client, users)
    seed_events(client)
    seed_visa_rules(client)
    insert(client, "conversations", build_conversations())
    print("Done.")


if __name__ == "__main__":
    main()
