"""In-memory HotelRepo (shares users/conversations with the main FakeRepo), so the hotel agent runs without Supabase."""
import uuid

from app.core.utils import now_ist


class FakeHotelRepo:
    def __init__(self, core):
        self.core, self.bookings, self.payments = core, {}, {}
        self.cities = [{"code": "BOM", "city": "Mumbai", "country": "India"}, {"code": "GOI", "city": "Goa", "country": "India"},
                       {"code": "DXB", "city": "Dubai", "country": "United Arab Emirates"}]
        self.hotels, self.rooms = {}, {}
        self._hotel("GOI", "Calangute Shores", "Calangute", 4, 4.3, [("Standard", 3000, 2, 2), ("Deluxe", 4500, 3, 3), ("Suite", 7000, 4, 1)])
        self._hotel("GOI", "Baga Backpackers", "Baga", 2, 3.9, [("Standard", 1500, 2, 1)])
        self._hotel("BOM", "Marine Drive Suites", "Marine Drive", 5, 4.6, [("Standard", 9000, 2, 5), ("Deluxe", 13000, 3, 2)])
        self._hotel("DXB", "Marina Skyline Hotel", "Dubai Marina", 5, 4.6, [("Standard", 27000, 2, 5)])

    def __getattr__(self, name):  # users, conversations, chat history, interests...
        return getattr(self.core, name)

    def _hotel(self, code, name, area, stars, rating, rooms):
        hid = str(uuid.uuid4())
        self.hotels[hid] = {"id": hid, "city_code": code, "name": name, "area": area, "stars": stars, "rating": rating,
                            "amenities": ["Free WiFi", "Pool"], "description": "A test hotel."}
        for room_type, price, guests, total in rooms:
            rid = str(uuid.uuid4())
            self.rooms[rid] = {"id": rid, "hotel_id": hid, "room_type": room_type, "bed": "Queen bed", "max_guests": guests,
                               "price_inr": price, "rooms_total": total}

    def room_of(self, hotel_name, room_type):
        hid = next(h["id"] for h in self.hotels.values() if h["name"] == hotel_name)
        return next(r for r in self.rooms.values() if r["hotel_id"] == hid and r["room_type"] == room_type)

    # --- search
    def list_hotel_cities(self):
        return self.cities

    def _taken(self, room_id, check_in, check_out):
        return sum(1 for b in self.bookings.values() if b["room_id"] == room_id and b["status"] != "cancelled"
                   and b["check_in"] < check_out.isoformat() and b["check_out"] > check_in.isoformat())

    def search_hotels(self, city_code, check_in, check_out, guests):
        out = []
        for h in self.hotels.values():
            if h["city_code"] != city_code:
                continue
            free = [{**r, "left": r["rooms_total"] - self._taken(r["id"], check_in, check_out)} for r in self.rooms.values()
                    if r["hotel_id"] == h["id"] and r["max_guests"] >= guests]
            free = sorted((r for r in free if r["left"] > 0), key=lambda r: r["price_inr"])
            if free:
                out.append({**h, "rooms": free})
        return out

    def get_hotel(self, hotel_id):
        return self.hotels.get(hotel_id)

    def get_room(self, room_id):
        return self.rooms.get(room_id)

    # --- bookings
    def _joined(self, b):
        user = next(u for u in self.core.users.values() if u["id"] == b["user_id"])
        return {**b, "hotels": self.hotels[b["hotel_id"]], "hotel_rooms": self.rooms[b["room_id"]],
                "users": {"phone": user["phone"], "name": user["name"]}}

    def create_booking(self, user_id, hotel, room, guest_name, guests, check_in, check_out, status="confirmed"):
        if self._taken(room["id"], check_in, check_out) >= room["rooms_total"]:
            return None
        b = {"id": str(uuid.uuid4()), "ref": f"HB{len(self.bookings) + 1:05d}", "user_id": user_id, "hotel_id": hotel["id"],
             "room_id": room["id"], "guest_name": guest_name, "guests": guests, "check_in": check_in.isoformat(),
             "check_out": check_out.isoformat(), "status": status,
             "total_price_inr": room["price_inr"] * (check_out - check_in).days, "created_at": now_ist().isoformat()}
        self.bookings[b["id"]] = b
        return self._joined(b)

    def get_booking_with_user(self, booking_id):
        b = self.bookings.get(booking_id)
        return self._joined(b) if b else None

    def get_booking(self, booking_id, user_id):
        b = self.bookings.get(booking_id)
        return self._joined(b) if b and b["user_id"] == user_id else None

    def list_user_bookings(self, user_id, limit=9):
        return [self._joined(b) for b in self.bookings.values() if b["user_id"] == user_id]

    def cancel_booking(self, b):
        stored = self.bookings[b["id"]]  # callers may hold a copy, like a real DB read
        if stored["status"] == "cancelled":
            return False
        stored["status"] = "cancelled"
        return True

    # --- payments
    def create_payment(self, booking_id, user_id, amount, link_id, short_url, expires_at):
        self.payments[link_id] = {"booking_id": booking_id, "amount": amount, "status": "created", "expires_at": expires_at}

    def mark_paid(self, link_id):
        p = self.payments.get(link_id)
        if not p or p["status"] != "created":
            return None
        p["status"] = "paid"
        b = self.bookings[p["booking_id"]]
        if b["status"] != "pending":
            return None
        b["status"] = "confirmed"
        return self._joined(b)

    def cancel_payment(self, booking_id):
        for p in self.payments.values():
            if p["booking_id"] == booking_id and p["status"] == "created":
                p["status"] = "cancelled"

    def expire_unpaid(self, now):
        out = []
        for p in self.payments.values():
            if p["status"] == "created" and p["expires_at"] < now:
                p["status"] = "expired"
                b = self.bookings[p["booking_id"]]
                if b["status"] == "pending":
                    self.cancel_booking(b)
                    out.append(self._joined(b))
        return out
