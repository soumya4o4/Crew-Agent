"""Cab agent data access: places, rate cards, drivers, rides. Synchronous; callers run it in a thread."""
import random
import string

from app.db.repository import CoreRepo

RIDE_SELECT = ("*, pickup:cab_places!pickup_id(name, kind), dropoff:cab_places!drop_id(name, kind), "
               "driver:cab_drivers(name, phone, vehicle_model, plate, rating)")
KIND_ORDER = {"airport": 0, "railway": 1, "landmark": 2, "mall": 3, "area": 4}


class CabRepo(CoreRepo):
    def __init__(self, client):
        super().__init__(client)
        self._cities, self._places, self._rates = None, {}, {}

    # ---- static-ish reference data (cached: it barely changes)
    def list_cab_cities(self) -> list[dict]:
        if self._cities is None:
            codes = {r["city_code"] for r in self.db.table("cab_places").select("city_code").execute().data}
            rows = self.db.table("airports").select("code, city, country").in_("code", sorted(codes)).execute().data
            self._cities = sorted(rows, key=lambda r: r["city"])
        return self._cities

    def list_places(self, city_code: str) -> list[dict]:
        if city_code not in self._places:
            rows = self.db.table("cab_places").select("*").eq("city_code", city_code).execute().data
            self._places[city_code] = sorted(rows, key=lambda p: (KIND_ORDER.get(p["kind"], 9), p["name"]))
        return self._places[city_code]

    def get_place(self, place_id: str) -> dict | None:
        rows = self.db.table("cab_places").select("*").eq("id", place_id).limit(1).execute().data
        return rows[0] if rows else None

    def get_rates(self, city_code: str) -> dict[str, dict]:
        if city_code not in self._rates:
            rows = self.db.table("cab_fares").select("*").eq("city_code", city_code).execute().data
            self._rates[city_code] = {r["vehicle_type"]: r for r in rows}
        return self._rates[city_code]

    # ---- rides
    def pick_driver(self, city_code: str, vehicle_type: str) -> dict | None:
        rows = (self.db.table("cab_drivers").select("*").eq("city_code", city_code)
                .eq("vehicle_type", vehicle_type).limit(20).execute().data)
        return random.choice(rows) if rows else None

    def create_ride(self, user_id: str, city_code: str, pickup_id: str, drop_id: str, pickup_time: str,
                    vehicle_type: str, distance_km: float, fare: int, driver_id: str | None) -> dict:
        for _ in range(5):
            ref = "CB" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
            try:
                row = self.db.table("cab_bookings").insert({
                    "ref": ref, "user_id": user_id, "city_code": city_code, "pickup_id": pickup_id, "drop_id": drop_id,
                    "pickup_time": pickup_time, "vehicle_type": vehicle_type, "distance_km": distance_km,
                    "fare_inr": fare, "driver_id": driver_id, "otp": f"{random.randint(0, 9999):04d}",
                }).execute().data[0]
                return self.get_ride(row["id"], user_id)
            except Exception:
                continue  # ref collision, try another
        raise RuntimeError("Could not create ride")

    def list_rides(self, user_id: str, limit: int = 9) -> list[dict]:
        return (self.db.table("cab_bookings").select(RIDE_SELECT).eq("user_id", user_id)
                .order("pickup_time", desc=True).limit(limit).execute().data)

    def get_ride(self, ride_id: str, user_id: str) -> dict | None:
        rows = (self.db.table("cab_bookings").select(RIDE_SELECT).eq("id", ride_id)
                .eq("user_id", user_id).limit(1).execute().data)
        return rows[0] if rows else None

    def cancel_ride(self, ride_id: str, fee: int) -> None:
        self.db.table("cab_bookings").update({"status": "cancelled", "cancel_fee_inr": fee}) \
            .eq("id", ride_id).neq("status", "cancelled").execute()
