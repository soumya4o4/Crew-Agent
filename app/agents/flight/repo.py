"""Flight agent data access: airports, mirrored live flights, bookings. Synchronous; callers run it in a thread."""
import logging
import random
import string
import time
from collections import Counter
from datetime import datetime, timezone

from app.db.repository import CoreRepo

logger = logging.getLogger(__name__)
PRUNE_EVERY_S = 6 * 3600


class FlightRepo(CoreRepo):
    def __init__(self, client):
        super().__init__(client)
        self._airports = None
        self._pruned_at = 0.0

    # ---- airports
    def list_airports(self) -> list[dict]:
        if self._airports is None:
            rows = self.db.table("airports").select("*").execute().data
            # Indian airports first, alphabetical within each group
            self._airports = sorted(rows, key=lambda a: (a["country"] != "India", a["city"]))
        return self._airports

    # ---- flights (live from Duffel: every search is mirrored into `flights`, so bookings point at a real row)
    def sync_live_flights(self, live: list[dict]) -> list[dict]:
        """Store the offers a search returned and give them back with their ids (an offer seen before keeps its row)."""
        if not live:
            return []
        rows = self.db.table("flights").upsert(live, on_conflict="duffel_offer_id").execute().data
        self._maybe_prune()
        order = {f["duffel_offer_id"]: i for i, f in enumerate(live)}
        return sorted(rows, key=lambda r: order.get(r["duffel_offer_id"], 0))

    def _maybe_prune(self) -> None:
        """Now and then drop old offers nobody booked, so the mirror does not grow forever."""
        now = time.monotonic()
        if now - self._pruned_at < PRUNE_EVERY_S:
            return
        self._pruned_at = now
        try:
            self.db.rpc("prune_flights").execute()
        except Exception:
            logger.warning("Could not prune old flight offers (is prune_flights created?)")

    def popular_routes(self, limit: int = 6) -> list[tuple[str, str]]:
        """The routes travellers booked most: a data-driven "popular right now" list."""
        rows = self.db.table("bookings").select("flights(from_code, to_code)").neq("status", "cancelled")             .order("created_at", desc=True).limit(500).execute().data
        return [route for route, _ in Counter((r["flights"]["from_code"], r["flights"]["to_code"]) for r in rows if r.get("flights"))
                .most_common(limit)]

    def get_flight(self, flight_id: str) -> dict | None:
        rows = self.db.table("flights").select("*").eq("id", flight_id).limit(1).execute().data
        return rows[0] if rows else None

    # ---- bookings
    def create_booking(self, user_id: str, flight: dict, passenger_name: str, passengers: int = 1, status: str = "confirmed",
                       details: list[dict] | None = None, contact: dict | None = None) -> dict:
        """Insert the booking. `details` is [{name, dob, gender}] and `contact` {email, phone}: what the airline needs for the ticket."""
        for _ in range(5):
            pnr = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            try:
                booking = self.db.table("bookings").insert({
                    "pnr": pnr, "user_id": user_id, "flight_id": flight["id"],
                    "passenger_name": passenger_name, "status": status,
                    "total_price_inr": flight["price_inr"] * passengers, "passengers": passengers,
                    "passenger_details": details or [], "contact": contact or {},
                }).execute().data[0]
                booking["flights"] = flight
                return booking
            except Exception:
                continue  # PNR collision, try another
        raise RuntimeError("Could not create booking")

    def set_ticket(self, booking_id: str, order_id: str, airline_pnr: str) -> None:
        """Remember the airline ticket (needed to cancel it) and its booking reference."""
        self.db.table("bookings").update({"duffel_order_id": order_id, "airline_pnr": airline_pnr or None}).eq("id", booking_id).execute()

    def save_traveller(self, user_id: str, name: str, dob: str, gender: str) -> None:
        """Remember a traveller's date of birth and gender (users.preferences.travellers), so next time only the name is needed."""
        row = self.db.table("users").select("preferences").eq("id", user_id).limit(1).execute().data
        prefs = (row[0]["preferences"] if row else None) or {}
        prefs.setdefault("travellers", {})[name.lower()] = {"dob": dob, "gender": gender}
        self.db.table("users").update({"preferences": prefs}).eq("id", user_id).execute()

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

    def cancel_booking(self, booking: dict) -> bool:
        """True if this call did the cancelling."""
        return bool(self.db.table("bookings").update({"status": "cancelled"})
                    .eq("id", booking["id"]).neq("status", "cancelled").execute().data)

    # ---- payments (a pending booking waits for its Razorpay link to be paid; unpaid ones are cancelled when the window ends)
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
        """Cancel pending bookings whose payment window passed. Returns them."""
        due = (self.db.table("payments").select("booking_id, link_id").eq("kind", "flight").eq("status", "created")
               .lt("expires_at", now.isoformat()).limit(50).execute().data)
        expired = []
        for p in due:
            if not self.db.table("payments").update({"status": "expired"}).eq("link_id", p["link_id"]) \
                    .eq("kind", "flight").eq("status", "created").execute().data:
                continue  # paid or cancelled a moment ago
            booking = self.get_booking_with_user(p["booking_id"])
            if booking and booking["status"] == "pending":
                self.cancel_booking(booking)
                expired.append(booking)
        return expired
