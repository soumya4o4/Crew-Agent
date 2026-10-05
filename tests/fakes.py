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
from app.agents.guide import GuideAgent, GuideRepo
from fakes_forex import FakeForexRepo, FakeRates
from app.agents.hotel import HotelAgent
from app.agents.planner import TripPlannerAgent
from app.agents.visa import VisaAgent
from app.agents.buddy import BuddyAgent
from fakes_buddy import FakeBuddyRepo
from fakes_geo import FakeEventsRepo, FakeGeo
from fakes_hotel import FakeHotelRepo
from fakes_planner import FakeMedia, FakePlannerBrain
from fakes_visa import FakeVerifier, FakeVisaRepo, fake_fetch_media
from app.core.utils import IST, now_ist
from app.services.duffel import DuffelError

WA = "919876543210"


class FakeRepo:
    def __init__(self):
        self.airports = [
            {"code": "IDR", "city": "Indore", "name": "Indore Airport", "country": "India"},
            {"code": "BOM", "city": "Mumbai", "name": "Mumbai Airport", "country": "India"},
            {"code": "DXB", "city": "Dubai", "name": "Dubai Airport", "country": "UAE"},
        ]
        tomorrow = (now_ist() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        self.inventory = []  # what the airlines (FakeLive) offer; `flights` is only what searches have mirrored into our table
        for i, (price, refundable) in enumerate([(4000, True), (3500, False), (3000, False)]):
            dep = tomorrow + timedelta(hours=i * 3)
            self.inventory.append({
                "airline": "IndiGo", "flight_no": f"6E-{1000 + i}", "from_code": "IDR", "to_code": "BOM",
                "departure_time": dep.isoformat(), "arrival_time": (dep + timedelta(minutes=90)).isoformat(),
                "duration_min": 90, "price_inr": price, "class": "Economy", "checked_bags": 1, "baggage_kg": 0, "stops": 0,
                "refundable": refundable, "status": "scheduled", "duffel_offer_id": f"off_{i}", "offer_pax": 1, "offer_total": price / 80,
                "offer_currency": "USD", "offer_passenger_ids": ["pas_0"], "itinerary_key": f"6E-{1000 + i}@{dep:%Y-%m-%dT%H:%M:%S}",
            })
        self.flights = {}
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

    def sync_live_flights(self, live):
        rows = []
        for f in live:
            row = next((r for r in self.flights.values() if r.get("duffel_offer_id") == f["duffel_offer_id"]), None)
            if row is None:
                row = {"id": str(uuid.uuid4())}
                self.flights[row["id"]] = row
            row.update(f)
            rows.append(dict(row))
        return rows

    def popular_routes(self, limit=6):
        counts = {}
        for b in self.bookings.values():
            if b["status"] != "cancelled":
                key = (b["flights"]["from_code"], b["flights"]["to_code"])
                counts[key] = counts.get(key, 0) + 1
        return sorted(counts, key=counts.get, reverse=True)[:limit]

    def get_flight(self, fid):
        return self.flights.get(fid)

    def create_booking(self, user_id, flight, name, passengers=1, status="confirmed", details=None, contact=None):
        b = {"id": str(uuid.uuid4()), "pnr": "ABC123", "user_id": user_id, "flight_id": flight["id"],
             "passenger_name": name, "status": status, "total_price_inr": flight["price_inr"] * passengers,
             "passengers": passengers, "flights": self.flights[flight["id"]], "passenger_details": details or [],
             "contact": contact or {}, "duffel_order_id": None, "airline_pnr": None}
        self.bookings[b["id"]] = b
        return b

    def set_ticket(self, booking_id, order_id, airline_pnr):
        self.bookings[booking_id].update(duffel_order_id=order_id, airline_pnr=airline_pnr or None)

    def save_traveller(self, user_id, name, dob, gender):
        user = next(u for u in self.users.values() if u["id"] == user_id)
        user["preferences"].setdefault("travellers", {})[name.lower()] = {"dob": dob, "gender": gender}

    def list_user_bookings(self, user_id, limit=9):
        return [b for b in self.bookings.values() if b["user_id"] == user_id]

    def get_booking(self, bid, user_id):
        b = self.bookings.get(bid)
        return b if b and b["user_id"] == user_id else None

    def cancel_booking(self, b):
        stored = self.bookings[b["id"]]  # callers may hold a copy, like a real DB read
        if stored["status"] == "cancelled":
            return False
        stored["status"] = "cancelled"
        return True

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

    def get_payment_for_booking(self, booking_id):
        found = [p for p in self.payments.values() if p["booking_id"] == booking_id]
        return found[-1] if found else None

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


class FakeLive:
    """Stands in for Duffel: offers the flights in `repo.inventory`, issues tickets, and the test decides what goes wrong."""

    def __init__(self, repo):
        self.repo = repo
        self.searches, self.orders, self.cancelled = [], {}, []
        self.fail_search = self.fail_book = self.gone = self.cant_cancel = False
        self.price_bump = 0  # added to every fare when it is asked for again
        self.refund_ratio = 1.0

    async def search(self, origin, destination, day, pax=1, fresh=False):
        if self.fail_search:
            raise DuffelError("search failed")
        self.searches.append((origin, destination, day, pax))
        found = [f for f in self.repo.inventory if f["from_code"] == origin and f["to_code"] == destination
                 and datetime.fromisoformat(f["departure_time"]).astimezone(IST).date() == day]
        return [self._for(f, pax) for f in found]

    @staticmethod
    def _for(f, pax):
        return {**f, "offer_pax": pax, "offer_passenger_ids": [f"pas_{i}" for i in range(pax)], "offer_total": f["offer_total"] * pax}

    async def refresh(self, flight, pax):
        if self.gone:
            return None
        base = next((f for f in self.repo.inventory if f["itinerary_key"] == flight["itinerary_key"]), None)
        if base is None:
            return None
        fresh = self._for(base, pax)
        fresh["price_inr"] += self.price_bump
        fresh["duffel_offer_id"] += f"-fresh{pax}"
        return fresh

    async def book(self, flight, travellers, contact, reference):
        if self.fail_book:
            raise DuffelError("airline said no", "order_failed")
        order = f"ord_{len(self.orders) + 1}"
        self.orders[order] = {"flight": flight, "travellers": travellers, "contact": contact, "reference": reference}
        return {"order_id": order, "booking_reference": "ZX9Q2K"}

    async def cancel_quote(self, order_id):
        if self.cant_cancel:
            raise DuffelError("not cancellable")
        total = self.orders[order_id]["flight"]["offer_total"]
        return {"quote_id": f"cq_{order_id}", "refund_amount": total * self.refund_ratio, "currency": "USD"}

    async def cancel(self, quote_id):
        self.cancelled.append(quote_id)


DETAILS = "14/03/1992 M"  # what a traveller sends when asked for their date of birth and gender
CONTACT = "Aarav Sharma, aarav@example.com, 9876543210"  # what a traveller sends when asked for name, email and phone


class FakeGateway:
    """Stands in for Razorpay: records links, and the test decides when a link counts as paid."""
    enabled, test_mode = True, True

    def __init__(self):
        self.links, self.paid, self.cancelled, self.fail = {}, set(), set(), False
        self.refunds, self.refund_fails = [], False

    async def create_link(self, amount_inr, reference_id, description, phone, name, expire_minutes, email=""):
        if self.fail:
            raise RuntimeError("razorpay down")
        link_id = f"plink_{len(self.links) + 1}"
        self.links[link_id] = {"amount": amount_inr, "ref": reference_id, "phone": phone, "email": email, "name": name,
                               "description": description}
        return {"id": link_id, "short_url": f"https://rzp.io/i/{link_id}"}

    async def link_status(self, link_id):
        return "paid" if link_id in self.paid else "created"

    async def cancel_link(self, link_id):
        self.cancelled.add(link_id)

    async def refund(self, link_id, amount_inr):
        if self.refund_fails:
            raise RuntimeError("razorpay down")
        self.refunds.append((link_id, amount_inr))
        return True


def build_concierge(repo, router=None, gateway=None, visa_agent=None, hotel_agent=None, buddy_agent=None, geo=None, events_repo=None,
                    planner_agent=None, advisor=None, forex_agent=None, guide_agent=None, live=None):
    agents = [guide_agent, FlightAgent(repo, gateway, advisor, live), hotel_agent or HotelAgent(FakeHotelRepo(repo), gateway), CabAgent(repo),
              NearbyAgent(geo or FakeGeo()), planner_agent or TripPlannerAgent(repo), EventsAgent(events_repo or FakeEventsRepo()),
              visa_agent or VisaAgent(FakeVisaRepo(repo), gateway, None, fake_fetch_media), forex_agent or ForexAgent(FakeForexRepo(repo), gateway, FakeRates())]
    if buddy_agent:  # like the real app, Buddy only exists when there is an LLM for it
        agents.append(buddy_agent)
    return Concierge(repo, [a for a in agents if a], router or IntentRouter())


class Chat:
    """Simulates one WhatsApp user talking to the Concierge, and checks WhatsApp's size limits."""

    def __init__(self, repo=None, router=None, gateway=None, brain=None, planner=False, advisor=None, rates=None, guide_advisor=None):
        self.repo = repo or FakeRepo()
        self.live = FakeLive(self.repo)
        self.visa_repo, self.verifier = FakeVisaRepo(self.repo), FakeVerifier()
        visa = VisaAgent(self.visa_repo, gateway, self.verifier, fake_fetch_media)
        self.hotel_repo = FakeHotelRepo(self.repo)
        self.forex_repo, self.rates = FakeForexRepo(self.repo), rates or FakeRates()
        forex = ForexAgent(self.forex_repo, gateway, self.rates)
        self.guide_repo = GuideRepo(self.repo, self.hotel_repo, self.forex_repo, self.visa_repo, self.repo)
        guide = GuideAgent(self.guide_repo, guide_advisor if guide_advisor is not None else advisor)
        hotel = HotelAgent(self.hotel_repo, gateway)
        self.buddy_repo, self.geo, self.events_repo = FakeBuddyRepo(self.repo), FakeGeo(), FakeEventsRepo()
        buddy = BuddyAgent(self.buddy_repo, brain, self.geo, self.hotel_repo) if brain else None
        self.planner_brain, self.media = (FakePlannerBrain(), FakeMedia()) if planner else (None, None)
        planner_agent = (PlannerAgent(self.repo, self.planner_brain, self.media, self.media.download, self.media.send, self.media.video_parts,
                                      background=False) if planner else None)
        self.concierge, self.n, self.last = build_concierge(self.repo, router, gateway, visa, hotel, buddy, self.geo, self.events_repo,
                                                            planner_agent, advisor, forex, guide, self.live), 0, []

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

    def pick_flight(self, details=DETAILS):
        """From the flights menu: IDR -> BOM tomorrow, by time, first flight, book for the user. Ends at the summary."""
        self.send(reply_id="menu:book"); self.send("indore to mumbai tomorrow"); self.send(reply_id="sort:time")
        self.send(reply_id=next(i for i in self.ids() if i.startswith("flt:")))
        self.book()
        return self.send(details) if details else self.last[0]

    def book(self, travellers=1):
        """Tap Book Now; with more than one traveller, say how many."""
        out = self.send(reply_id="act:book")
        return self.send(reply_id=f"pax:{travellers}") if travellers > 1 else out
