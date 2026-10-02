"""Flight agent data access: airports, flights, bookings. Synchronous; callers run it in a thread."""
import random
import string
from collections import Counter
from datetime import datetime, timezone

from app.db.repository import CoreRepo


def _utc_str(dt: datetime) -> str:
    # 'Z' form avoids a '+' in the PostgREST query string
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FlightRepo(CoreRepo):
    def __init__(self, client):
        super().__init__(client)
        self._airports = None

    # ---- airports
    def list_airports(self) -> list[dict]:
        if self._airports is None:
            rows = self.db.table("airports").select("*").execute().data
            # Indian airports first, alphabetical within each group
            self._airports = sorted(rows, key=lambda a: (a["country"] != "India", a["city"]))
        return self._airports

    # ---- flights
    def search_flights(self, from_code: str, to_code: str, day_start: datetime, day_end: datetime) -> list[dict]:
        return (
            self.db.table("flights").select("*")
            .eq("from_code", from_code).eq("to_code", to_code)
            .gte("departure_time", _utc_str(day_start)).lt("departure_time", _utc_str(day_end))
            .neq("status", "cancelled").gt("seats_left", 0)
            .order("departure_time").execute().data
        )

    def search_from(self, from_code: str, day_start: datetime, day_end: datetime) -> list[dict]:
        """Every bookable flight leaving an airport that day (any destination), for the explorer."""
        return (
            self.db.table("flights").select("*").eq("from_code", from_code)
            .gte("departure_time", _utc_str(day_start)).lt("departure_time", _utc_str(day_end))
            .neq("status", "cancelled").gt("seats_left", 0)
            .order("price_inr").execute().data
        )

    def popular_routes(self, limit: int = 6) -> list[tuple[str, str]]:
        """The routes with the most scheduled flights: a data-driven "popular right now" list."""
        rows = self.db.table("flights").select("from_code, to_code").neq("status", "cancelled").limit(1000).execute().data
        return [route for route, _ in Counter((r["from_code"], r["to_code"]) for r in rows).most_common(limit)]

    def origins_for(self, to_code: str) -> list[str]:
        """Airports that have a bookable flight to `to_code` from now on."""
        rows = (self.db.table("flights").select("from_code").eq("to_code", to_code).neq("status", "cancelled")
                .gt("seats_left", 0).gte("departure_time", _utc_str(datetime.now(timezone.utc))).limit(1000).execute().data)
        return sorted({r["from_code"] for r in rows})

    def destinations_from(self, from_code: str) -> list[str]:
        """Airports reachable from `from_code` by a bookable flight from now on."""
        rows = (self.db.table("flights").select("to_code").eq("from_code", from_code).neq("status", "cancelled")
                .gt("seats_left", 0).gte("departure_time", _utc_str(datetime.now(timezone.utc))).limit(1000).execute().data)
        return sorted({r["to_code"] for r in rows})

    def get_flight(self, flight_id: str) -> dict | None:
        rows = self.db.table("flights").select("*").eq("id", flight_id).limit(1).execute().data
        return rows[0] if rows else None

    # ---- bookings
    def create_booking(self, user_id: str, flight: dict, passenger_name: str, passengers: int = 1,
                       status: str = "confirmed") -> dict | None:
        """Reserve seats (compare-and-swap on seats_left) and insert the booking.
        Returns None if there aren't enough seats left."""
        current = flight
        for _ in range(3):
            if not current or current["seats_left"] < passengers or current["status"] == "cancelled":
                return None
            won = self.db.table("flights").update({"seats_left": current["seats_left"] - passengers}) \
                .eq("id", current["id"]).eq("seats_left", current["seats_left"]).execute().data
            if won:
                break
            current = self.get_flight(flight["id"])  # someone else booked; re-read and retry
        else:
            return None

        for _ in range(5):
            pnr = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            try:
                booking = self.db.table("bookings").insert({
                    "pnr": pnr, "user_id": user_id, "flight_id": flight["id"],
                    "passenger_name": passenger_name, "status": status,
                    "total_price_inr": flight["price_inr"] * passengers, "passengers": passengers,
                }).execute().data[0]
                booking["flights"] = current
                return booking
            except Exception:
                continue  # PNR collision, try another
        self._release_seat(flight["id"], passengers)
        raise RuntimeError("Could not create booking")

    def list_user_bookings(self, user_id: str, limit: int = 9) -> list[dict]:
        return (
            self.db.table("bookings").select("*, flights(*)").eq("user_id", user_id)
            .order("created_at", desc=True).limit(limit).execute().data
        )

    def get_booking(self, booking_id: str, user_id: str) -> dict | None:
        rows = (
            self.db.table("bookings").select("*, flights(*)")
            .eq("id", booking_id).eq("user_id", user_id).limit(1).execute().data
        )
        return rows[0] if rows else None

    def cancel_booking(self, booking: dict) -> None:
        done = self.db.table("bookings").update({"status": "cancelled"}) \
            .eq("id", booking["id"]).neq("status", "cancelled").execute().data
        if done:  # only give the seat back if this call did the cancelling
            self._release_seat(booking["flight_id"], booking.get("passengers") or 1)

    def _release_seat(self, flight_id: str, count: int = 1) -> None:
        for _ in range(3):
            f = self.get_flight(flight_id)
            if not f:
                return
            if self.db.table("flights").update({"seats_left": f["seats_left"] + count}) \
                    .eq("id", flight_id).eq("seats_left", f["seats_left"]).execute().data:
                return

    # ---- payments (a pending booking holds seats until its Razorpay link is paid or expires)
    def create_payment(self, booking_id: str, user_id: str, amount_inr: int, link_id: str, short_url: str,
                       expires_at: datetime) -> dict:
        return self.db.table("payments").insert({
            "booking_id": booking_id, "user_id": user_id, "amount_inr": amount_inr, "link_id": link_id,
            "short_url": short_url, "expires_at": expires_at.isoformat(),
        }).execute().data[0]

    def get_payment_for_booking(self, booking_id: str) -> dict | None:
        rows = (self.db.table("payments").select("*").eq("booking_id", booking_id)
                .order("created_at", desc=True).limit(1).execute().data)
        return rows[0] if rows else None

    def get_booking_with_user(self, booking_id: str) -> dict | None:
        rows = self.db.table("bookings").select("*, flights(*), users(phone, name)").eq("id", booking_id).limit(1).execute().data
        return rows[0] if rows else None

    def mark_paid(self, link_id: str) -> dict | None:
        """Payment received: confirm the booking. Returns the booking, or None if it was already handled
        (webhook retries, or the user tapping 'I've paid' after the webhook)."""
        paid = (self.db.table("payments").update({"status": "paid", "paid_at": datetime.now(timezone.utc).isoformat()})
                .eq("link_id", link_id).eq("kind", "flight").eq("status", "created").execute().data)
        if not paid:
            return None
        confirmed = (self.db.table("bookings").update({"status": "confirmed"})
                     .eq("id", paid[0]["booking_id"]).eq("status", "pending").execute().data)
        return self.get_booking_with_user(paid[0]["booking_id"]) if confirmed else None

    def cancel_payment(self, booking_id: str) -> None:
        self.db.table("payments").update({"status": "cancelled"}).eq("booking_id", booking_id).eq("status", "created").execute()

    def expire_unpaid(self, now: datetime) -> list[dict]:
        """Cancel pending bookings whose payment window passed and give their seats back. Returns them."""
        due = (self.db.table("payments").select("booking_id, link_id").eq("kind", "flight").eq("status", "created")
               .lt("expires_at", now.isoformat()).limit(50).execute().data)
        expired = []
        for p in due:
            if not self.db.table("payments").update({"status": "expired"}).eq("link_id", p["link_id"]) \
                    .eq("status", "created").execute().data:
                continue  # paid or cancelled a moment ago
            booking = self.get_booking_with_user(p["booking_id"])
            if booking and booking["status"] == "pending":
                self.cancel_booking(booking)
                expired.append(booking)
        return expired
