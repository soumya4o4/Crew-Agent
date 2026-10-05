"""Hotel agent data access: hotels, rooms, bookings and their payments. Synchronous; callers run it in a thread."""
import random
import string
from datetime import date, datetime, timezone

from app.db.repository import CoreRepo

BOOKING_SELECT = "*, hotels(*), hotel_rooms(*), users(phone, name)"


class HotelRepo(CoreRepo):
    def __init__(self, client):
        super().__init__(client)
        self._cities = None
        self._content_saved: set[int] = set()  # Hotelbeds hotels whose photo/amenities we already wrote this run

    # ---- search
    def list_hotel_cities(self) -> list[dict]:
        if self._cities is None:
            codes = {r["city_code"] for r in self.db.table("hotels").select("city_code").execute().data}
            if not codes:
                self._cities = []
                return self._cities
            rows = self.db.table("airports").select("code, city, country").in_("code", sorted(codes)).execute().data
            self._cities = sorted(rows, key=lambda r: (r["country"] != "India", r["city"]))
        return self._cities

    def list_airport_cities(self) -> list[dict]:
        """Every city we know coordinates for: with Hotelbeds a hotel search works anywhere (not only where we hold hotels)."""
        rows = self.db.table("airports").select("code, city, country, lat").execute().data
        rows = [r for r in rows if r.get("lat") is not None]
        return sorted(rows, key=lambda r: (r["country"] != "India", r["city"]))

    def _booked(self, room_ids: list[str], check_in: date, check_out: date) -> dict[str, int]:
        """Rooms already taken per room type for these nights (pending and confirmed bookings both hold a room)."""
        if not room_ids:
            return {}
        rows = (self.db.table("hotel_bookings").select("room_id").in_("room_id", room_ids).neq("status", "cancelled")
                .lt("check_in", check_out.isoformat()).gt("check_out", check_in.isoformat()).execute().data)
        taken: dict[str, int] = {}
        for r in rows:
            taken[r["room_id"]] = taken.get(r["room_id"], 0) + 1
        return taken

    def search_hotels(self, city_code: str, check_in: date, check_out: date, guests: int) -> list[dict]:
        """Hotels in a city that still have a room for `guests` on these nights. Each hotel carries `rooms`: the room
        types that fit, cheapest first, with `left` (how many are still free)."""
        hotels = self.db.table("hotels").select("*").eq("city_code", city_code).execute().data
        if not hotels:
            return []
        rooms = (self.db.table("hotel_rooms").select("*").in_("hotel_id", [h["id"] for h in hotels])
                 .gte("max_guests", guests).execute().data)
        taken = self._booked([r["id"] for r in rooms], check_in, check_out)
        out = []
        for h in hotels:
            free = [{**r, "left": r["rooms_total"] - taken.get(r["id"], 0)} for r in rooms if r["hotel_id"] == h["id"]]
            free = sorted((r for r in free if r["left"] > 0), key=lambda r: r["price_inr"])
            if free:
                out.append({**h, "rooms": free})
        return out

    def sync_live_hotels(self, city_code: str, live: list[dict]) -> set[str]:
        """Mirror Hotelbeds results into our tables (so bookings and payments keep pointing at real rows) and refresh their
        prices. Returns the ids of the hotels that were just found live."""
        if not live:
            return set()
        hotels = self.db.table("hotels").upsert(
            [{"city_code": city_code, "name": h["name"], "area": h["area"] or city_code, "stars": h["stars"],
              "rating": h["rating"], "hb_code": h["hb_code"]} for h in live], on_conflict="city_code,name").execute().data
        ids = {h["name"]: h["id"] for h in hotels}
        for h in live:  # photo, amenities, description: only for hotels that have them, so a failed fetch never wipes old ones
            extra = {k: h[k] for k in ("image_url", "amenities", "description") if h.get(k)}
            if extra and h["name"] in ids and h["hb_code"] not in self._content_saved:
                self.db.table("hotels").update(extra).eq("id", ids[h["name"]]).execute()
                self._content_saved.add(h["hb_code"])
        rooms = [{"hotel_id": ids[h["name"]], "room_type": r["room_type"], "bed": r["bed"], "max_guests": r["max_guests"],
                  "price_inr": r["price_inr"], "rooms_total": r["left"], "hb_rate_key": r["rate_key"]}
                 for h in live if h["name"] in ids for r in h["rooms"]]
        if rooms:
            self.db.table("hotel_rooms").upsert(rooms, on_conflict="hotel_id,room_type").execute()
        return set(ids.values())

    def get_hotel(self, hotel_id: str) -> dict | None:
        rows = self.db.table("hotels").select("*").eq("id", hotel_id).limit(1).execute().data
        return rows[0] if rows else None

    def get_room(self, room_id: str) -> dict | None:
        rows = self.db.table("hotel_rooms").select("*").eq("id", room_id).limit(1).execute().data
        return rows[0] if rows else None

    # ---- bookings
    def create_booking(self, user_id: str, hotel: dict, room: dict, guest_name: str, guests: int, check_in: date,
                       check_out: date, status: str = "confirmed") -> dict | None:
        """Insert the booking, then count who holds this room type on these nights. If we went over the number of
        rooms we back out (two people racing may both back out, but nobody is ever double-booked).
        Returns None if no room was left."""
        for _ in range(5):
            ref = "HB" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
            try:
                booking = self.db.table("hotel_bookings").insert({
                    "ref": ref, "user_id": user_id, "hotel_id": hotel["id"], "room_id": room["id"],
                    "guest_name": guest_name, "guests": guests, "check_in": check_in.isoformat(),
                    "check_out": check_out.isoformat(), "status": status,
                    "total_price_inr": room["price_inr"] * (check_out - check_in).days,
                }).execute().data[0]
                break
            except Exception:
                continue  # ref collision, try another
        else:
            raise RuntimeError("Could not create hotel booking")
        if self._booked([room["id"]], check_in, check_out).get(room["id"], 0) > room["rooms_total"]:
            self.cancel_booking(booking)
            return None
        return self.get_booking_with_user(booking["id"])

    def set_supplier_ref(self, booking_id: str, reference: str) -> None:
        """Remember the Hotelbeds booking reference (needed to cancel it there)."""
        self.db.table("hotel_bookings").update({"hb_reference": reference}).eq("id", booking_id).execute()

    def get_booking_with_user(self, booking_id: str) -> dict | None:
        rows = self.db.table("hotel_bookings").select(BOOKING_SELECT).eq("id", booking_id).limit(1).execute().data
        return rows[0] if rows else None

    def get_booking(self, booking_id: str, user_id: str) -> dict | None:
        rows = (self.db.table("hotel_bookings").select(BOOKING_SELECT).eq("id", booking_id).eq("user_id", user_id)
                .limit(1).execute().data)
        return rows[0] if rows else None

    def list_user_bookings(self, user_id: str, limit: int = 9) -> list[dict]:
        return (self.db.table("hotel_bookings").select(BOOKING_SELECT).eq("user_id", user_id)
                .order("created_at", desc=True).limit(limit).execute().data)

    def cancel_booking(self, booking: dict) -> bool:
        """True if this call did the cancelling (the room is free again either way: availability ignores cancelled)."""
        return bool(self.db.table("hotel_bookings").update({"status": "cancelled"})
                    .eq("id", booking["id"]).neq("status", "cancelled").execute().data)

    # ---- payments (a pending booking holds its room until the Razorpay link is paid or expires)
    def create_payment(self, booking_id: str, user_id: str, amount_inr: int, link_id: str, short_url: str,
                       expires_at: datetime) -> dict:
        return self.db.table("payments").insert({
            "kind": "hotel", "hotel_booking_id": booking_id, "user_id": user_id, "amount_inr": amount_inr,
            "link_id": link_id, "short_url": short_url, "expires_at": expires_at.isoformat(),
        }).execute().data[0]

    def mark_paid(self, link_id: str) -> dict | None:
        """Payment received: confirm the booking. Returns it, or None if it was already handled
        (webhook retries, or the user tapping 'I've paid' after the webhook)."""
        paid = (self.db.table("payments").update({"status": "paid", "paid_at": datetime.now(timezone.utc).isoformat()})
                .eq("link_id", link_id).eq("kind", "hotel").eq("status", "created").execute().data)
        if not paid:
            return None
        confirmed = (self.db.table("hotel_bookings").update({"status": "confirmed"})
                     .eq("id", paid[0]["hotel_booking_id"]).eq("status", "pending").execute().data)
        return self.get_booking_with_user(paid[0]["hotel_booking_id"]) if confirmed else None

    def cancel_payment(self, booking_id: str) -> None:
        self.db.table("payments").update({"status": "cancelled"}).eq("hotel_booking_id", booking_id) \
            .eq("kind", "hotel").eq("status", "created").execute()

    def expire_unpaid(self, now: datetime) -> list[dict]:
        """Cancel pending bookings whose payment window passed. Returns them."""
        due = (self.db.table("payments").select("hotel_booking_id, link_id").eq("kind", "hotel").eq("status", "created")
               .lt("expires_at", now.isoformat()).limit(50).execute().data)
        expired = []
        for p in due:
            if not self.db.table("payments").update({"status": "expired"}).eq("link_id", p["link_id"]) \
                    .eq("kind", "hotel").eq("status", "created").execute().data:
                continue  # paid or cancelled a moment ago
            booking = self.get_booking_with_user(p["hotel_booking_id"])
            if booking and booking["status"] == "pending":
                self.cancel_booking(booking)
                expired.append(booking)
        return expired
