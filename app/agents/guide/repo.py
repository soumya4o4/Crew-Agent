"""Trip Guide data access: reads what the traveller has already booked with us, so the roadmap can tick steps off by itself.
It only reads, through the other agents' repos. Synchronous; callers run it in a thread."""
from datetime import date

from app.core.places import country_of
from app.core.utils import to_ist

LIVE = ("confirmed", "pending")
NEAR_DAYS = 5  # a booking within this many days of the trip date counts as being for this trip


def _near(d: date, travel: date) -> bool:
    return abs((d - travel).days) <= NEAR_DAYS


class GuideRepo:
    def __init__(self, flights, hotels, forex, visa, cabs=None):
        self.flights, self.hotels, self.forex, self.visa, self.cabs = flights, hotels, forex, visa, cabs

    def home_country(self, user_id: str) -> str:
        """Where this person usually flies from: their last booking, else the country with the most airports."""
        last = self.flights.list_user_bookings(user_id, 1)
        if last:
            return country_of(last[0]["flights"]["from_code"])
        counts: dict[str, int] = {}
        for a in self.flights.list_airports():
            counts[a["country"]] = counts.get(a["country"], 0) + 1
        return max(counts, key=counts.get) if counts else ""

    def origin_choices(self, user_id: str, limit: int = 8) -> list[dict]:
        """Airports in the traveller's home country to start from, the city they flew from last time first."""
        home = self.home_country(user_id)
        last = self.flights.list_user_bookings(user_id, 1)
        last_code = last[0]["flights"]["from_code"] if last else None
        rows = [a for a in self.flights.list_airports() if a["country"] == home]
        rows.sort(key=lambda a: (a["code"] != last_code, a["city"]))
        return [{"code": a["code"], "city": a["city"], "name": a["name"], "last": a["code"] == last_code} for a in rows[:limit]]

    def cab_cities(self) -> set[str]:
        """Airport codes of the cities where we book cabs."""
        return {c["code"] for c in self.cabs.list_cab_cities()} if self.cabs else set()

    def visa_rule(self, visa_code: str) -> dict | None:
        return self.visa.get_rule(visa_code, "tourist")

    def popular_destinations(self, limit: int = 3) -> list[str]:
        """Airport codes people fly to most, then the other airports we know (a fallback when there is no AI to suggest places).
        Flights are live, so every airport in our table can be booked."""
        out: list[str] = []
        for _, to in self.flights.popular_routes(12):
            if to not in out:
                out.append(to)
        out += [a["code"] for a in self.flights.list_airports() if a["code"] not in out]
        return out[:limit]

    def progress(self, user_id: str, *, dest_code: str | None, origin_code: str | None, travel: date, back: date | None,
                 currency: str | None, visa_code: str | None) -> dict:
        """What is already arranged for this trip:
        {"flight": there booked, "flight_back": return booked, "hotel": bool, "forex": bool, "visa": None | status}."""
        out: dict = {"flight": False, "flight_back": False, "hotel": False, "forex": False, "visa": None}
        if dest_code:
            bookings = [b for b in self.flights.list_user_bookings(user_id, 20) if b["status"] in LIVE]
            out["flight"] = any(b["flights"]["to_code"] == dest_code and _near(to_ist(b["flights"]["departure_time"]).date(), travel)
                                for b in bookings)
            out["flight_back"] = bool(back) and any(
                b["flights"]["from_code"] == dest_code and (not origin_code or b["flights"]["to_code"] == origin_code)
                and _near(to_ist(b["flights"]["departure_time"]).date(), back) for b in bookings)
            out["hotel"] = any(h["status"] in LIVE and h["hotels"]["city_code"] == dest_code
                               and _near(date.fromisoformat(str(h["check_in"])[:10]), travel)
                               for h in self.hotels.list_user_bookings(user_id, 20))
        if currency:
            out["forex"] = any(o["status"] != "cancelled" and o["currency"] == currency for o in self.forex.list_user_orders(user_id, 20))
        if visa_code:
            apps = [a for a in self.visa.list_applications(user_id, 20)
                    if a["country_code"] == visa_code and a["status"] not in ("cancelled", "rejected")]
            if apps:
                out["visa"] = "approved" if any(a["status"] == "approved" for a in apps) else apps[0]["status"]
        return out
