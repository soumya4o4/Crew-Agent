"""In-memory stand-in for FlightRepo so agents can be tested without Supabase or WhatsApp."""
import asyncio
import uuid
from datetime import datetime, timedelta

from app.agents.cab import CabAgent
from app.agents.concierge import Concierge
from app.agents.events import EventsAgent
from app.agents.nearby import NearbyAgent
from app.agents.planner import PlannerAgent
from app.agents.concierge.router import IntentRouter
from app.agents.flight import FlightAgent
from app.agents.forex import ForexAgent
from app.agents.hotel import HotelAgent
from app.agents.planner import TripPlannerAgent
from app.agents.visa import VisaAgent
from app.agents.buddy import BuddyAgent
from fakes_buddy import FakeBuddyRepo
from fakes_geo import FakeEventsRepo, FakeGeo
from fakes_hotel import FakeHotelRepo
from fakes_planner import FakeMedia, FakePlannerBrain
from fakes_visa import FakeVerifier, FakeVisaRepo, fake_fetch_media
from app.core.utils import now_ist

WA = "919876543210"


class FakeRepo:
    def __init__(self):
        self.airports = [
            {"code": "IDR", "city": "Indore", "name": "Indore Airport", "country": "India"},
            {"code": "BOM", "city": "Mumbai", "name": "Mumbai Airport", "country": "India"},
            {"code": "DXB", "city": "Dubai", "name": "Dubai Airport", "country": "UAE"},
        ]
        tomorrow = (now_ist() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        self.flights = {}
        for i, (price, seats, status) in enumerate([(4000, 3, "scheduled"), (3500, 1, "delayed"), (3000, 0, "scheduled")]):
            fid = str(uuid.uuid4())
            dep = tomorrow + timedelta(hours=i * 3)
            self.flights[fid] = {
                "id": fid, "airline": "IndiGo", "flight_no": f"6E-{1000 + i}", "from_code": "IDR", "to_code": "BOM",
                "departure_time": dep.isoformat(), "arrival_time": (dep + timedelta(minutes=90)).isoformat(),
                "duration_min": 90, "price_inr": price, "class": "Economy", "seats_left": seats,
                "baggage_kg": 15, "stops": 0, "refundable": i == 0, "status": status,
            }
        self.users, self.convos, self.bookings, self.messages, self.payments = {}, {}, {}, [], {}
        self._init_cabs()

    # --- core
    def get_or_create_user(self, phone, name):
        return self.users.setdefault(phone, {"id": str(uuid.uuid4()), "phone": phone, "name": name, "preferences": {}})

    def get_conversation(self, phone):
        return self.convos.get(phone)

    def save_conversation(self, phone, step, ctx):
        self.convos[phone] = {"current_step": step, "context": ctx}

    def log_message(self, user_id, role, agent, content):
        self.messages.append((user_id, role, agent, content))

    def recent_messages(self, user_id, limit=8):
        return [{"role": r, "agent": a, "content": c} for u, r, a, c in self.messages if u == user_id][-limit:]

    def add_interest(self, user_id, interest):
        user = next(u for u in self.users.values() if u["id"] == user_id)
        user["preferences"]["interests"] = sorted(set(user["preferences"].get("interests", [])) | {interest})

    # --- flight
    def list_airports(self):
        return self.airports

    def popular_routes(self, limit=6):
        counts = {}
        for f in self.flights.values():
            counts[(f["from_code"], f["to_code"])] = counts.get((f["from_code"], f["to_code"]), 0) + 1
        return sorted(counts, key=counts.get, reverse=True)[:limit]

    @staticmethod
    def _in_window(x, start, end):
        return start <= datetime.fromisoformat(x["departure_time"]) < end

    def search_flights(self, f, t, start, end):
        return [x for x in self.flights.values() if x["from_code"] == f and x["to_code"] == t
                and x["status"] != "cancelled" and x["seats_left"] > 0 and self._in_window(x, start, end)]

    def search_from(self, f, start, end):
        return [x for x in self.flights.values() if x["from_code"] == f
                and x["status"] != "cancelled" and x["seats_left"] > 0 and self._in_window(x, start, end)]

    def get_flight(self, fid):
        return self.flights.get(fid)

    def create_booking(self, user_id, flight, name, passengers=1, status="confirmed"):
        f = self.flights[flight["id"]]
        if f["seats_left"] < passengers:
            return None
        f["seats_left"] -= passengers
        b = {"id": str(uuid.uuid4()), "pnr": "ABC123", "user_id": user_id, "flight_id": f["id"],
             "passenger_name": name, "status": status, "total_price_inr": f["price_inr"] * passengers,
             "passengers": passengers, "flights": f}
        self.bookings[b["id"]] = b
        return b

    def list_user_bookings(self, user_id, limit=9):
        return [b for b in self.bookings.values() if b["user_id"] == user_id]

    def get_booking(self, bid, user_id):
        b = self.bookings.get(bid)
        return b if b and b["user_id"] == user_id else None

    def cancel_booking(self, b):
        stored = self.bookings[b["id"]]  # callers may hold a copy, like a real DB read
        if stored["status"] == "cancelled":
            return
        stored["status"] = "cancelled"
        self.flights[stored["flight_id"]]["seats_left"] += stored.get("passengers", 1)

    # --- payments
    def create_payment(self, booking_id, user_id, amount, link_id, short_url, expires_at):
        self.payments[link_id] = {"booking_id": booking_id, "user_id": user_id, "amount": amount, "link_id": link_id,
                                  "short_url": short_url, "status": "created", "expires_at": expires_at}

    def get_booking_with_user(self, bid):
        b = self.bookings.get(bid)
        if not b:
            return None
        user = next(u for u in self.users.values() if u["id"] == b["user_id"])
        return {**b, "users": {"phone": user["phone"], "name": user["name"]}}

    def mark_paid(self, link_id):
        p = self.payments.get(link_id)
        if not p or p["status"] != "created":
            return None
        p["status"] = "paid"
        b = self.bookings[p["booking_id"]]
        if b["status"] != "pending":
            return None
        b["status"] = "confirmed"
        return self.get_booking_with_user(b["id"])

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
                    out.append(self.get_booking_with_user(b["id"]))
        return out

    # --- cabs
    def _init_cabs(self):
        mk = lambda city, name, kind, lat, lon: {"id": str(uuid.uuid4()), "city_code": city, "name": name,
                                                  "kind": kind, "lat": lat, "lon": lon}
        self.cab_places = [mk("BOM", "Mumbai Airport T2", "airport", 19.0887, 72.8679), mk("BOM", "Bandra West", "area", 19.0596, 72.8295),
                           mk("BOM", "Powai", "area", 19.1176, 72.9060), mk("IDR", "Indore Airport", "airport", 22.7218, 75.8011),
                           mk("IDR", "Rajwada Palace", "landmark", 22.7185, 75.8553)]
        self.cab_rates = {c: {v: {"city_code": c, "vehicle_type": v, "base_fare": b, "per_km": pk, "min_fare": mn, "seats": 4,
                                  "example_model": "Test Car"} for v, (b, pk, mn) in
                              {"Mini": (40, 12, 120), "Sedan": (55, 15, 160), "SUV": (80, 20, 250), "Luxury": (150, 35, 600)}.items()}
                          for c in ("BOM", "IDR")}
        self.cab_rides = {}

    def list_cab_cities(self):
        return [{"code": "BOM", "city": "Mumbai", "country": "India"}, {"code": "IDR", "city": "Indore", "country": "India"}]

    def list_places(self, city):
        return [p for p in self.cab_places if p["city_code"] == city]

    def get_place(self, pid):
        return next((p for p in self.cab_places if p["id"] == pid), None)

    def get_rates(self, city):
        return self.cab_rates.get(city, {})

    def pick_driver(self, city, vehicle):
        return {"id": "drv1", "name": "Ramesh Yadav", "phone": "+915100000000", "vehicle_model": "Maruti Swift",
                "plate": "MH01 AB 1234", "rating": 4.8}

    def create_ride(self, user_id, city, pickup_id, drop_id, when, vehicle, km, fare, driver_id):
        rid = str(uuid.uuid4())
        ride = {"id": rid, "ref": "CBTEST1", "user_id": user_id, "city_code": city, "pickup_time": when,
                "vehicle_type": vehicle, "distance_km": km, "fare_inr": fare, "otp": "1234", "status": "confirmed",
                "cancel_fee_inr": 0, "pickup": self.get_place(pickup_id), "dropoff": self.get_place(drop_id),
                "driver": self.pick_driver(city, vehicle)}
        self.cab_rides[rid] = ride
        return ride

    def list_rides(self, user_id, limit=9):
        return [r for r in self.cab_rides.values() if r["user_id"] == user_id]

    def get_ride(self, rid, user_id):
        r = self.cab_rides.get(rid)
        return r if r and r["user_id"] == user_id else None

    def cancel_ride(self, rid, fee):
        self.cab_rides[rid].update(status="cancelled", cancel_fee_inr=fee)


class FakeGateway:
    """Stands in for Razorpay: records links, and the test decides when a link counts as paid."""
    enabled, test_mode = True, True

    def __init__(self):
        self.links, self.paid, self.cancelled, self.fail = {}, set(), set(), False

    async def create_link(self, amount_inr, reference_id, description, phone, name, expire_minutes):
        if self.fail:
            raise RuntimeError("razorpay down")
        link_id = f"plink_{len(self.links) + 1}"
        self.links[link_id] = {"amount": amount_inr, "ref": reference_id, "phone": phone}
        return {"id": link_id, "short_url": f"https://rzp.io/i/{link_id}"}

    async def link_status(self, link_id):
        return "paid" if link_id in self.paid else "created"

    async def cancel_link(self, link_id):
        self.cancelled.add(link_id)


def build_concierge(repo, router=None, gateway=None, visa_agent=None, hotel_agent=None, buddy_agent=None, geo=None, events_repo=None,
                    planner_agent=None, advisor=None):
    agents = [FlightAgent(repo, gateway, advisor), hotel_agent or HotelAgent(FakeHotelRepo(repo), gateway), CabAgent(repo),
              NearbyAgent(geo or FakeGeo()), planner_agent or TripPlannerAgent(repo), EventsAgent(events_repo or FakeEventsRepo()),
              visa_agent or VisaAgent(FakeVisaRepo(repo), gateway, None, fake_fetch_media), ForexAgent(repo)]
    if buddy_agent:  # like the real app, Buddy only exists when there is an LLM for it
        agents.append(buddy_agent)
    return Concierge(repo, agents, router or IntentRouter())


class Chat:
    """Simulates one WhatsApp user talking to the Concierge, and checks WhatsApp's size limits."""

    def __init__(self, repo=None, router=None, gateway=None, brain=None, planner=False, advisor=None):
        self.repo = repo or FakeRepo()
        self.visa_repo, self.verifier = FakeVisaRepo(self.repo), FakeVerifier()
        visa = VisaAgent(self.visa_repo, gateway, self.verifier, fake_fetch_media)
        self.hotel_repo = FakeHotelRepo(self.repo)
        hotel = HotelAgent(self.hotel_repo, gateway)
        self.buddy_repo, self.geo, self.events_repo = FakeBuddyRepo(self.repo), FakeGeo(), FakeEventsRepo()
        buddy = BuddyAgent(self.buddy_repo, brain, self.geo) if brain else None
        self.planner_brain, self.media = (FakePlannerBrain(), FakeMedia()) if planner else (None, None)
        planner_agent = (PlannerAgent(self.repo, self.planner_brain, self.media, self.media.download, self.media.send, self.media.video_parts,
                                      background=False) if planner else None)
        self.concierge, self.n, self.last = build_concierge(self.repo, router, gateway, visa, hotel, buddy, self.geo, self.events_repo,
                                                            planner_agent, advisor), 0, []

    def send(self, text="", reply_id=None):
        self.n += 1
        self.last = asyncio.run(self.concierge.handle(WA, "Aarav Sharma", f"m{self.n}", text, reply_id))
        for m in self.last:
            if m["type"] == "buttons":
                assert len(m["buttons"]) <= 3 and all(len(t) <= 20 for _, t in m["buttons"])
            if m["type"] == "list":
                assert len(m["rows"]) <= 10 and all(len(t) <= 24 and len(d) <= 72 for _, t, d in m["rows"])
        return self.last[0] if self.last else None

    def send_file(self, caption="", mime="image/jpeg", kind="image"):
        self.n += 1
        media = {"id": f"media{self.n}", "mime": mime, "filename": "", "kind": kind}
        self.last = asyncio.run(self.concierge.handle(WA, "Aarav Sharma", f"m{self.n}", caption, None, media))
        return self.last[0] if self.last else None

    def send_location(self, lat=22.7196, lon=75.8577, name=""):
        """The user shares a location pin (WhatsApp's location message)."""
        self.n += 1
        loc = {"lat": lat, "lon": lon, "name": name, "address": ""}
        self.last = asyncio.run(self.concierge.handle(WA, "Aarav Sharma", f"m{self.n}", "", None, None, loc))
        return self.last[0] if self.last else None

    def ids(self):
        m = [x for x in self.last if x["type"] != "reaction"][-1]
        return [i for i, _ in m["buttons"]] if m["type"] == "buttons" else [r[0] for r in m["rows"]]

    def enter_flights(self):
        self.send("hi")
        return self.send(reply_id="svc:flight")

    def pick_flight(self, name_reply="name:self"):
        """From the flights menu: IDR -> BOM tomorrow, cheapest-first, first flight, book for self."""
        self.send(reply_id="menu:book"); self.send(reply_id="from:IDR"); self.send(reply_id="to:BOM")
        self.send(reply_id=self.ids()[1]); self.send(reply_id="sort:time")
        self.send(reply_id=next(i for i in self.ids() if i.startswith("flt:")))
        self.book()
        return self.send(reply_id=name_reply)

    def book(self, travellers=1):
        """Tap Book Now; answers the 'how many travellers' list when it appears."""
        out = self.send(reply_id="act:book")
        return self.send(reply_id=f"pax:{travellers}") if out["type"] == "list" else out
