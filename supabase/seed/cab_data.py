"""Dummy cab data: pickup/drop places (public landmarks), rate cards, and fake drivers.
Names, phones and plates are invented; phones use the unallocated +91 5... range."""
import random
import string
import uuid

# city -> [(name, kind, lat, lon)]   (approximate coordinates of well-known public places)
PLACES = {
    "IDR": [("Indore Airport", "airport", 22.7218, 75.8011), ("Indore Railway Station", "railway", 22.7196, 75.8686),
            ("Rajwada Palace", "landmark", 22.7185, 75.8553), ("Vijay Nagar", "area", 22.7533, 75.8937),
            ("Treasure Island Mall", "mall", 22.7237, 75.8809), ("Palasia Square", "area", 22.7243, 75.8839)],
    "BOM": [("Mumbai Airport T2", "airport", 19.0887, 72.8679), ("CSMT Railway Station", "railway", 18.9402, 72.8356),
            ("Bandra West", "area", 19.0596, 72.8295), ("Andheri East", "area", 19.1136, 72.8697),
            ("Powai", "area", 19.1176, 72.9060), ("Gateway of India", "landmark", 18.9220, 72.8347)],
    "DEL": [("Delhi Airport T3", "airport", 28.5562, 77.1000), ("New Delhi Station", "railway", 28.6430, 77.2197),
            ("Connaught Place", "landmark", 28.6315, 77.2167), ("Saket", "area", 28.5245, 77.2066),
            ("Cyber Hub Gurugram", "area", 28.4950, 77.0890), ("Noida Sector 18", "mall", 28.5700, 77.3260)],
    "BLR": [("Kempegowda Airport", "airport", 13.1989, 77.7068), ("Majestic Railway Station", "railway", 12.9767, 77.5713),
            ("MG Road", "landmark", 12.9756, 77.6068), ("Koramangala", "area", 12.9352, 77.6245),
            ("Whitefield", "area", 12.9698, 77.7500), ("Indiranagar", "area", 12.9719, 77.6412)],
    "HYD": [("Rajiv Gandhi Airport", "airport", 17.2403, 78.4294), ("Secunderabad Station", "railway", 17.4339, 78.5011),
            ("HITEC City", "area", 17.4435, 78.3772), ("Banjara Hills", "area", 17.4126, 78.4482),
            ("Charminar", "landmark", 17.3616, 78.4747), ("Gachibowli", "area", 17.4401, 78.3489)],
    "MAA": [("Chennai Airport", "airport", 12.9941, 80.1709), ("Chennai Central Station", "railway", 13.0827, 80.2757),
            ("T Nagar", "area", 13.0418, 80.2341), ("Marina Beach", "landmark", 13.0500, 80.2824),
            ("Adyar", "area", 13.0012, 80.2565), ("OMR Sholinganallur", "area", 12.9010, 80.2279)],
    "CCU": [("Kolkata Airport", "airport", 22.6547, 88.4467), ("Howrah Station", "railway", 22.5839, 88.3425),
            ("Park Street", "landmark", 22.5535, 88.3520), ("Salt Lake Sector V", "area", 22.5726, 88.4310),
            ("New Town", "area", 22.5958, 88.4797), ("Victoria Memorial", "landmark", 22.5448, 88.3426)],
    "PNQ": [("Pune Airport", "airport", 18.5822, 73.9197), ("Pune Railway Station", "railway", 18.5286, 73.8744),
            ("Koregaon Park", "area", 18.5362, 73.8940), ("Hinjewadi", "area", 18.5912, 73.7389),
            ("Shivajinagar", "area", 18.5308, 73.8475), ("Viman Nagar", "area", 18.5679, 73.9143)],
    "GOI": [("Goa Airport (Mopa)", "airport", 15.7441, 73.8644), ("Panjim", "area", 15.4909, 73.8278),
            ("Calangute Beach", "landmark", 15.5440, 73.7550), ("Baga Beach", "landmark", 15.5553, 73.7517),
            ("Anjuna", "area", 15.5736, 73.7413), ("Madgaon Railway Station", "railway", 15.2832, 73.9862)],
    "AMD": [("Ahmedabad Airport", "airport", 23.0772, 72.6347), ("Kalupur Railway Station", "railway", 23.0258, 72.6012),
            ("Sabarmati Riverfront", "landmark", 23.0390, 72.5800), ("SG Highway", "area", 23.0480, 72.5300),
            ("Navrangpura", "area", 23.0360, 72.5600), ("GIFT City", "area", 23.1600, 72.6800)],
}

# vehicle type -> (base fare, per km, minimum fare, seats, example model)
VEHICLES = {
    "Mini": (40, 12, 120, 4, "Swift or similar"),
    "Sedan": (55, 15, 160, 4, "Dzire or similar"),
    "SUV": (80, 20, 250, 6, "Ertiga or similar"),
    "Luxury": (150, 35, 600, 4, "Honda City / Camry"),
}
CITY_FARE_FACTOR = {"BOM": 1.1, "DEL": 1.1, "BLR": 1.1, "GOI": 1.15, "IDR": 0.9, "AMD": 0.9}  # default 1.0

DRIVER_NAMES = ["Ramesh Yadav", "Sunil Kumar", "Imran Sheikh", "Rajesh Patel", "Anil Chauhan", "Mohan Das", "Vijay Nair",
                "Farhan Ali", "Deepak Joshi", "Suresh Reddy", "Harpreet Singh", "Manoj Tiwari", "Arun Menon", "Salim Khan",
                "Prakash Rao", "Kiran Desai"]
MODELS = {"Mini": ["Maruti Swift", "Tata Tiago", "Hyundai i10"], "Sedan": ["Maruti Dzire", "Honda Amaze", "Hyundai Aura"],
          "SUV": ["Maruti Ertiga", "Toyota Innova", "Kia Carens"], "Luxury": ["Honda City", "Toyota Camry", "Skoda Superb"]}
RTO = {"IDR": "MP09", "BOM": "MH01", "DEL": "DL01", "BLR": "KA01", "HYD": "TS09", "MAA": "TN01",
       "CCU": "WB02", "PNQ": "MH12", "GOI": "GA03", "AMD": "GJ01"}


def build_places() -> list[dict]:
    return [{"id": str(uuid.uuid4()), "city_code": city, "name": n, "kind": k, "lat": lat, "lon": lon}
            for city, places in PLACES.items() for n, k, lat, lon in places]


def build_fares() -> list[dict]:
    rows = []
    for city in PLACES:
        f = CITY_FARE_FACTOR.get(city, 1.0)
        for vtype, (base, per_km, min_fare, seats, model) in VEHICLES.items():
            rows.append({"city_code": city, "vehicle_type": vtype, "base_fare": round(base * f),
                         "per_km": round(per_km * f, 2), "min_fare": round(min_fare * f), "seats": seats,
                         "example_model": model})
    return rows


def build_drivers(rng: random.Random, per_type: int = 3) -> list[dict]:
    rows, plates = [], set()
    for city in PLACES:
        for vtype in VEHICLES:
            for _ in range(per_type):
                while True:
                    plate = f"{RTO[city]} {''.join(rng.choices(string.ascii_uppercase, k=2))} {rng.randint(1000, 9999)}"
                    if plate not in plates:
                        plates.add(plate)
                        break
                rows.append({
                    "id": str(uuid.uuid4()), "name": rng.choice(DRIVER_NAMES),
                    "phone": f"+91510{rng.randint(1000000, 9999999)}",  # +91 5... is not an allocated mobile range
                    "city_code": city, "vehicle_type": vtype, "vehicle_model": rng.choice(MODELS[vtype]),
                    "plate": plate, "rating": round(rng.uniform(4.3, 4.9), 1), "trips": rng.randint(300, 4000),
                })
    phones = set()
    for r in rows:  # phones must be unique
        while r["phone"] in phones:
            r["phone"] = f"+91510{rng.randint(1000000, 9999999)}"
        phones.add(r["phone"])
    return rows
