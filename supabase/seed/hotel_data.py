"""Dummy hotel data: invented hotels per city with Standard / Deluxe / Suite rooms.
Names are made up (any resemblance to a real hotel is a coincidence) and prices are indicative."""
import uuid

# city -> [(name, area, stars, rating, amenities, description)]. Names stay under 24 characters (WhatsApp list titles).
HOTELS = {
    "IDR": [("Rajwada Residency", "Rajwada", 4, 4.3, ["Free WiFi", "Breakfast included", "Restaurant", "Parking"], "Heritage-style stay a short walk from the old city."),
            ("Vijay Nagar Inn", "Vijay Nagar", 3, 4.0, ["Free WiFi", "Parking", "Gym"], "Clean, simple rooms near malls and cafés."),
            ("Sarafa Stay", "Sarafa Bazaar", 2, 3.8, ["Free WiFi", "Breakfast included"], "Budget rooms steps from the night food market."),
            ("The Malwa Grand", "Palasia", 5, 4.6, ["Free WiFi", "Pool", "Spa", "Gym", "Breakfast included", "Restaurant"], "Business-class luxury with a rooftop pool.")],
    "BOM": [("Marine Drive Suites", "Marine Drive", 5, 4.6, ["Free WiFi", "Pool", "Spa", "Restaurant", "Gym"], "Sea-facing rooms on the Queen's Necklace."),
            ("Bandra Boutique", "Bandra West", 4, 4.4, ["Free WiFi", "Breakfast included", "Restaurant"], "Boutique stay in the cafés-and-street-art district."),
            ("Airport Comfort Inn", "Andheri East", 3, 3.9, ["Free WiFi", "Airport shuttle", "Breakfast included"], "Ten minutes from the airport, ideal for short stays."),
            ("Juhu Beach Lodge", "Juhu", 3, 4.1, ["Free WiFi", "Parking", "Restaurant"], "Walk to the beach, vada pav included in the mood.")],
    "DEL": [("Connaught Crown", "Connaught Place", 5, 4.5, ["Free WiFi", "Pool", "Spa", "Gym", "Restaurant"], "Central luxury with metro at the doorstep."),
            ("Karol Bagh Inn", "Karol Bagh", 3, 3.9, ["Free WiFi", "Breakfast included"], "Good value near the metro and markets."),
            ("Aerocity Express", "Aerocity", 4, 4.2, ["Free WiFi", "Airport shuttle", "Gym", "Breakfast included"], "Minutes from T3, with a shuttle every 30 minutes."),
            ("Saket Stay", "Saket", 3, 4.0, ["Free WiFi", "Parking", "Restaurant"], "Quiet south-Delhi base near malls and hospitals.")],
    "BLR": [("Garden City Hotel", "MG Road", 4, 4.4, ["Free WiFi", "Pool", "Breakfast included", "Gym"], "Leafy courtyard in the heart of the city."),
            ("Koramangala Nest", "Koramangala", 3, 4.2, ["Free WiFi", "Restaurant", "Parking"], "Startup-district stay surrounded by cafés."),
            ("Whitefield Executive", "Whitefield", 4, 4.1, ["Free WiFi", "Gym", "Breakfast included", "Parking"], "Made for business trips to the tech parks."),
            ("Airport Pod Inn", "Devanahalli", 2, 3.7, ["Free WiFi", "Airport shuttle"], "Basic, clean and close to the airport.")],
    "HYD": [("Nizam Palace Hotel", "Banjara Hills", 5, 4.6, ["Free WiFi", "Pool", "Spa", "Restaurant", "Breakfast included"], "Royal-themed luxury, famous for its biryani buffet."),
            ("HITEC Business Inn", "HITEC City", 4, 4.2, ["Free WiFi", "Gym", "Breakfast included", "Parking"], "Next to the IT corridor."),
            ("Charminar Lodge", "Old City", 2, 3.8, ["Free WiFi"], "Simple stay near the monument and bazaars."),
            ("Gachibowli Comfort", "Gachibowli", 3, 4.0, ["Free WiFi", "Restaurant", "Parking"], "Comfortable mid-range rooms near the stadium.")],
    "MAA": [("Marina Bay Hotel", "Marina Beach", 4, 4.3, ["Free WiFi", "Pool", "Restaurant", "Breakfast included"], "Beach-side rooms with filter coffee at breakfast."),
            ("T Nagar Residency", "T Nagar", 3, 4.0, ["Free WiFi", "Parking", "Restaurant"], "Shopping-district stay with easy transit."),
            ("OMR Executive", "OMR", 4, 4.1, ["Free WiFi", "Gym", "Pool", "Breakfast included"], "Business hotel on the IT corridor."),
            ("Adyar Inn", "Adyar", 2, 3.8, ["Free WiFi", "Breakfast included"], "Quiet budget stay near the river.")],
    "CCU": [("Park Street Grand", "Park Street", 5, 4.5, ["Free WiFi", "Pool", "Spa", "Restaurant", "Gym"], "Colonial charm on the city's food street."),
            ("Salt Lake Comfort", "Salt Lake", 3, 4.0, ["Free WiFi", "Breakfast included", "Parking"], "Convenient for Sector V offices."),
            ("Howrah Bridge Inn", "Howrah", 2, 3.7, ["Free WiFi"], "Basic rooms near the station."),
            ("New Town Boutique", "New Town", 4, 4.3, ["Free WiFi", "Pool", "Breakfast included", "Gym"], "Modern stay with a quiet garden.")],
    "PNQ": [("Koregaon Park Suites", "Koregaon Park", 4, 4.4, ["Free WiFi", "Pool", "Breakfast included", "Restaurant"], "Boutique stay among cafés and bakeries."),
            ("Hinjewadi Executive", "Hinjewadi", 4, 4.1, ["Free WiFi", "Gym", "Parking", "Breakfast included"], "Convenient for the IT parks."),
            ("Shivajinagar Inn", "Shivajinagar", 3, 3.9, ["Free WiFi", "Breakfast included"], "Central and easy on the wallet."),
            ("Pune Airport Lodge", "Viman Nagar", 3, 4.0, ["Free WiFi", "Airport shuttle", "Restaurant"], "Five minutes from the airport.")],
    "GOI": [("Calangute Shores", "Calangute", 4, 4.3, ["Free WiFi", "Pool", "Bar", "Breakfast included"], "Beach access and a pool bar."),
            ("Baga Backpackers", "Baga", 2, 3.9, ["Free WiFi", "Bar"], "Social, budget-friendly and close to the nightlife."),
            ("Panjim Heritage Stay", "Panjim", 3, 4.2, ["Free WiFi", "Breakfast included", "Restaurant"], "Portuguese-era house in the Latin Quarter."),
            ("Anjuna Beach Resort", "Anjuna", 5, 4.6, ["Free WiFi", "Pool", "Spa", "Bar", "Breakfast included"], "Beachfront luxury with a sunset deck.")],
    "AMD": [("Riverfront Regency", "Sabarmati", 4, 4.3, ["Free WiFi", "Pool", "Restaurant", "Breakfast included"], "River views and a Gujarati thali on the house."),
            ("Old City Haveli", "Kalupur", 3, 4.2, ["Free WiFi", "Breakfast included"], "A restored haveli in the pol area."),
            ("SG Highway Suites", "SG Highway", 4, 4.0, ["Free WiFi", "Gym", "Parking", "Pool"], "Business-friendly with easy ring-road access."),
            ("Airport Express Inn", "Airport Road", 2, 3.7, ["Free WiFi", "Airport shuttle"], "Basic rooms five minutes from the airport.")],
    "DXB": [("Marina Skyline Hotel", "Dubai Marina", 5, 4.6, ["Free WiFi", "Pool", "Spa", "Gym", "Restaurant", "Breakfast included"], "High-rise rooms with marina views."),
            ("Deira City Hotel", "Deira", 3, 4.0, ["Free WiFi", "Breakfast included", "Airport shuttle"], "Close to the Gold Souk and the creek."),
            ("Downtown Boutique", "Downtown", 4, 4.4, ["Free WiFi", "Pool", "Gym", "Restaurant"], "Steps from the Dubai Mall."),
            ("Bur Dubai Budget Inn", "Bur Dubai", 2, 3.8, ["Free WiFi", "Breakfast included"], "No-frills stay near the metro.")],
}

# Standard-room price (INR per night) by star rating; a city factor is applied on top.
STAR_BASE = {2: 1600, 3: 2800, 4: 4800, 5: 8500}
CITY_FACTOR = {"BOM": 1.35, "DEL": 1.15, "BLR": 1.1, "GOI": 1.25, "DXB": 3.2, "IDR": 0.85, "AMD": 0.85}  # default 1.0

# (room type, price multiplier, bed, max guests, rooms of that type: (low, high) by star band)
ROOM_TYPES = [("Standard", 1.0, "Queen bed", 2, (6, 12)), ("Deluxe", 1.45, "King bed", 3, (4, 8)), ("Suite", 2.3, "King bed + sofa bed", 4, (1, 3))]


def build_hotels(rng) -> tuple[list[dict], list[dict]]:
    """Returns (hotels, rooms). Budget (2 star) hotels have no Suite."""
    hotels, rooms = [], []
    for code, entries in HOTELS.items():
        for name, area, stars, rating, amenities, description in entries:
            hotel_id = str(uuid.uuid4())
            hotels.append({"id": hotel_id, "city_code": code, "name": name, "area": area, "stars": stars,
                           "rating": rating, "amenities": amenities, "description": description})
            base = STAR_BASE[stars] * CITY_FACTOR.get(code, 1.0)
            for room_type, mult, bed, guests, (lo, hi) in ROOM_TYPES:
                if stars == 2 and room_type == "Suite":
                    continue
                price = int(round(base * mult * rng.uniform(0.95, 1.08) / 50) * 50)
                rooms.append({"id": str(uuid.uuid4()), "hotel_id": hotel_id, "room_type": room_type, "bed": bed,
                              "max_guests": guests, "price_inr": price, "rooms_total": rng.randint(lo, hi)})
    return hotels, rooms
