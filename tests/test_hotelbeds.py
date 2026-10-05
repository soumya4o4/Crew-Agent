"""Hotelbeds live search: the client turns the API's answer into our hotels, and the hotel agent shows only those."""
import asyncio
import json
from datetime import timedelta

import httpx

from app.agents.hotel import HotelAgent
from app.core.config import settings
from app.core.utils import now_ist
from app.services.hotelbeds import HotelbedsClient, HotelbedsError, bed_of, room_type, short, split_name, stars_of
from fakes import Chat, FakeGateway, FakeRepo
from fakes_forex import FakeRates
from fakes_hotel import FakeHotelRepo

CHECK_IN = now_ist().date() + timedelta(days=5)
CHECK_OUT = CHECK_IN + timedelta(days=2)


class Off(FakeGateway):
    enabled = False  # no Razorpay keys: stays confirm instantly


HOTEL = {"code": 1234, "name": "GRAND BEACH RESORT", "categoryCode": "4EST", "zoneName": "CALANGUTE", "currency": "EUR",
         "rooms": [
             {"name": "DOUBLE STANDARD", "rates": [{"rateKey": "k-std-hi", "net": "300.00", "allotment": 4},
                                                    {"rateKey": "k-std-lo", "net": "200.00", "allotment": 2}]},
             {"name": "SUPERIOR TWIN", "rates": [{"rateKey": "k-dlx", "net": "400.00", "allotment": 12}]},
             {"name": "JUNIOR SUITE SEA VIEW", "rates": [{"rateKey": "k-ste", "net": "600.00", "allotment": 1}]}]}


FACILITIES = {"facilities": [{"code": 550, "facilityGroupCode": 70, "description": {"content": "Wi-fi"}},
                             {"code": 306, "facilityGroupCode": 60, "description": {"content": "Outdoor swimming pool"}},
                             {"code": 470, "facilityGroupCode": 70, "description": {"content": "Gym"}},
                             {"code": 500, "facilityGroupCode": 70, "description": {"content": "Secure parking"}},
                             {"code": 515, "facilityGroupCode": 70, "description": {"content": "Newspapers"}}]}
CONTENT = {"hotels": [{"code": 1234, "description": {"content": "Right on the beach. A short walk to the market. " + "More text. " * 40},
                       "images": [{"imageTypeCode": "RES", "path": "12/room.jpg", "visualOrder": 1},
                                  {"imageTypeCode": "GEN", "path": "12/front.jpg", "visualOrder": 5}],
                       "facilities": [{"facilityCode": 550, "facilityGroupCode": 70}, {"facilityCode": 306, "facilityGroupCode": 60},
                                      {"facilityCode": 470, "facilityGroupCode": 70, "indFee": True},  # paid gym: not listed
                                      {"facilityCode": 500, "facilityGroupCode": 70}, {"facilityCode": 515, "facilityGroupCode": 70}]}]}


def client(payload=None, calls=None, content=True):
    def handler(request):
        if calls is not None:
            calls.append(request)
        path = request.url.path
        if "content-api" in path:
            if not content:
                return httpx.Response(500)
            return httpx.Response(200, json=FACILITIES if path.endswith("types/facilities") else CONTENT)
        return httpx.Response(200, json={"hotels": {"hotels": [HOTEL] if payload is None else payload}})
    return HotelbedsClient(FakeRates(), httpx.MockTransport(handler))


def search(c, guests=2):
    return asyncio.run(c.search("GOI", CHECK_IN, CHECK_OUT, guests))


def test_helpers():
    assert (stars_of("5LUX"), stars_of("2EST"), stars_of("")) == (5, 2, 3)
    assert (room_type("JUNIOR SUITE"), room_type("SUPERIOR TWIN"), room_type("DOUBLE STANDARD")) == ("Suite", "Deluxe", "Standard")
    assert (bed_of("SUPERIOR TWIN"), bed_of("DOUBLE STANDARD"), bed_of("ROOM")) == ("Twin beds", "Double bed", "Hotel room")


def test_signed_request_and_normalised_hotel(monkeypatch):
    monkeypatch.setattr(settings, "HOTELBEDS_API_KEY", "key")
    monkeypatch.setattr(settings, "HOTELBEDS_SECRET", "secret")
    calls = []
    (h,) = search(client(calls=calls))
    req = calls[0]
    assert "hotel-api" in req.url.path
    assert req.headers["Api-key"] == "key" and len(req.headers["X-Signature"]) == 64
    body = json.loads(req.content)
    assert body["stay"] == {"checkIn": CHECK_IN.isoformat(), "checkOut": CHECK_OUT.isoformat()}
    assert body["occupancies"] == [{"rooms": 1, "adults": 2, "children": 0}] and "geolocation" in body
    assert (h["hb_code"], h["name"], h["stars"], h["area"], h["rating"]) == (1234, "Grand Beach Resort", 4, "Calangute", None)
    # EUR = Rs 100 in FakeRates; 2 nights: cheapest standard rate 200 EUR -> Rs 10,000 a night
    assert [(r["room_type"], r["price_inr"], r["left"], r["rate_key"]) for r in h["rooms"]] == [
        ("Standard", 10000, 2, "k-std-lo"), ("Deluxe", 20000, 9, "k-dlx"), ("Suite", 30000, 1, "k-ste")]


def test_markup_and_cache(monkeypatch):
    monkeypatch.setattr(settings, "HOTELBEDS_MARKUP_PCT", 10.0)
    calls = []
    c = client(calls=calls)
    assert search(c)[0]["rooms"][0]["price_inr"] == 11000
    search(c)
    assert len(calls) == 3  # availability + facility types + hotel content; the second search came from the cache


def test_no_rate_no_price():
    c = HotelbedsClient(FakeRates(down=True), httpx.MockTransport(lambda r: httpx.Response(200, json={"hotels": {"hotels": [HOTEL]}})))
    assert search(c) == []


class Live:
    def __init__(self, c=None, fail=False):
        self.c, self.fail = c, fail

    async def search(self, *a):
        if self.fail:
            raise RuntimeError("Hotelbeds is down")
        return await self.c.search(*a)


def agent(live):
    repo = FakeHotelRepo(FakeRepo())
    return HotelAgent(repo, None, live=live), repo


STAY = {"city": "GOI", "check_in": CHECK_IN.isoformat(), "nights": 2, "guests": 2}


def test_agent_shows_only_live_hotels_and_mirrors_them():
    a, repo = agent(Live(client()))
    found = asyncio.run(a._search(STAY))
    assert [h["name"] for h in found] == ["Grand Beach Resort"]  # the seeded Goa hotels are hidden while live works
    assert repo.get_hotel(found[0]["id"])["hb_code"] == 1234  # bookings can point at a real row
    assert found[0]["rooms"][0]["price_inr"] == 10000


def test_agent_falls_back_to_own_hotels_when_hotelbeds_fails():
    a, _ = agent(Live(fail=True))
    assert {h["name"] for h in asyncio.run(a._search(STAY))} == {"Calangute Shores", "Baga Backpackers"}


def test_agent_without_hotelbeds_is_unchanged():
    a, _ = agent(None)
    assert {h["name"] for h in asyncio.run(a._search(STAY))} == {"Calangute Shores", "Baga Backpackers"}


def test_content_photo_amenities_description():
    (h,) = search(client())
    assert h["image_url"] == "https://photos.hotelbeds.com/giata/bigger/12/front.jpg"  # a general photo beats a room photo
    assert h["amenities"] == ["Pool", "Free WiFi", "Parking"] or set(h["amenities"]) == {"Pool", "Free WiFi", "Parking"}
    assert h["description"].startswith("Right on the beach.") and len(h["description"]) <= 220 and h["description"].endswith(".")


def test_content_is_fetched_once_per_hotel():
    calls = []
    c = client(calls=calls)
    search(c)
    search(c, guests=3)  # a new search: availability again, but the hotel content is already known
    assert sum("content-api" in r.url.path for r in calls) == 2 and sum("hotel-api" in r.url.path for r in calls) == 2


def test_search_still_works_when_content_fails():
    (h,) = search(client(content=False))
    assert h["name"] == "Grand Beach Resort" and "image_url" not in h and "amenities" not in h


def test_short_description():
    assert short("One. Two.") == "One. Two."
    assert short("Alpha. " * 60).endswith(".") and len(short("Alpha. " * 60)) <= 220
    assert short("word " * 100).endswith("…")


def test_agent_stores_content_for_the_card():
    a, repo = agent(Live(client()))
    (h,) = asyncio.run(a._search(STAY))
    assert "swimming" not in h["amenities"] and "Pool" in h["amenities"] and h["image_url"].endswith("front.jpg")


# ---------------------------------------------------------------- booking and cancelling at Hotelbeds
def booking_client(book_status=200, calls=None, cancel_status=200):
    def handler(request):
        if calls is not None:
            calls.append((request.method, request.url.path, request.content))
        path = request.url.path
        if path.endswith("/checkrates"):
            return httpx.Response(200, json={"hotel": {"rooms": [{"rates": [{"rateKey": "fresh-key", "net": "200.00"}]}]}})
        if path.endswith("/bookings") and request.method == "POST":
            if book_status != 200:
                return httpx.Response(book_status, json={"error": {"code": "X", "message": "Room not available"}})
            return httpx.Response(200, json={"booking": {"reference": "1-9999", "status": "CONFIRMED"}})
        if request.method == "DELETE":
            if cancel_status != 200:
                return httpx.Response(cancel_status, json={"error": {"message": "Booking is already cancelled" if cancel_status == 409 else "Boom"}})
            return httpx.Response(200, json={"booking": {"reference": "1-9999", "status": "CANCELLED"}})
        raise AssertionError(path)
    return HotelbedsClient(FakeRates(), httpx.MockTransport(handler))


def test_split_name():
    assert split_name("Aarav Kumar Sharma") == ("Aarav Kumar", "Sharma")
    assert split_name("Aarav") == ("Aarav", "Aarav")


def test_book_rechecks_the_rate_then_books():
    calls = []
    ref = asyncio.run(booking_client(calls=calls).book("old-key", "Aarav Sharma", 2, "HBABCDE"))
    assert ref == "1-9999"
    assert [c[1].rsplit("/", 1)[1] for c in calls] == ["checkrates", "bookings"]
    body = json.loads(calls[1][2])
    assert body["holder"] == {"name": "Aarav", "surname": "Sharma"} and body["clientReference"] == "HBABCDE"
    assert body["rooms"][0]["rateKey"] == "fresh-key" and len(body["rooms"][0]["paxes"]) == 2


def test_book_failure_raises_and_forgets_cached_rates():
    c = booking_client(book_status=409)
    c._cache["x"] = (0, [])
    try:
        asyncio.run(c.book("k", "Aarav Sharma", 1, "HBABCDE"))
        raise AssertionError("should have failed")
    except HotelbedsError as exc:
        assert "Room not available" in str(exc)
    assert not c._cache


def test_cancel():
    calls = []
    asyncio.run(booking_client(calls=calls).cancel("1-9999"))
    assert calls[0][0] == "DELETE" and calls[0][1].endswith("/bookings/1-9999")
    asyncio.run(booking_client(cancel_status=409).cancel("1-9999"))  # already cancelled counts as done
    try:
        asyncio.run(booking_client(cancel_status=500).cancel("1-9999"))
        raise AssertionError("should have failed")
    except HotelbedsError:
        pass


class LiveBooking(Live):
    def __init__(self, c, fail_book=False, fail_cancel=False):
        super().__init__(c)
        self.fail_book, self.fail_cancel, self.booked, self.cancelled = fail_book, fail_cancel, [], []

    async def book(self, rate_key, name, guests, ref):
        if self.fail_book:
            raise HotelbedsError("409: Room not available")
        self.booked.append((rate_key, name, guests, ref))
        return "1-9999"

    async def cancel(self, reference):
        if self.fail_cancel:
            raise HotelbedsError("500: Boom")
        self.cancelled.append(reference)


def live_chat(**kw):
    """A user chatting with a hotel agent that searches (and books) at Hotelbeds."""
    live = LiveBooking(client(), **kw)
    c = Chat(gateway=Off())
    hotel = c.concierge.agents["hotel"]
    hotel.live = live
    return c, live


def book_live(c, name="Grand Beach Resort", room="Standard"):
    c.send("hi"); c.send(reply_id="svc:hotel"); c.send(reply_id="hotel:find"); c.send(reply_id="hcity:GOI")
    c.send(reply_id=f"hin:{CHECK_IN.isoformat()}"); c.send(reply_id="hnt:2"); c.send(reply_id="hgst:2")
    hid = next(h["id"] for h in c.hotel_repo.hotels.values() if h["name"] == name)
    c.send(reply_id=f"htl:{hid}")
    c.send(reply_id=f"hroom:{c.hotel_repo.room_of(name, room)['id']}")
    c.send(reply_id="hname:self")
    return c.send(reply_id="hcfm:yes")


def test_booking_a_live_hotel_books_it_at_hotelbeds():
    c, live = live_chat()
    out = book_live(c)
    assert "Stay Confirmed" in out["body"] or "Confirmed" in out["body"]
    assert live.booked == [("k-std-lo", "Aarav Sharma", 2, next(iter(c.hotel_repo.bookings.values()))["ref"])]
    (b,) = c.hotel_repo.bookings.values()
    assert b["status"] == "confirmed" and b["hb_reference"] == "1-9999"


def test_sold_out_at_hotelbeds_releases_the_room_and_apologises():
    c, live = live_chat(fail_book=True)
    out = book_live(c)
    assert "just taken" in out["body"]
    (b,) = c.hotel_repo.bookings.values()
    assert b["status"] == "cancelled" and "hb_reference" not in b


def test_cancelling_a_stay_cancels_it_at_hotelbeds():
    c, live = live_chat()
    book_live(c)
    (b,) = c.hotel_repo.bookings.values()
    c.send(reply_id=f"hbkc:{b['id']}")
    c.send(reply_id=f"hbkcy:{b['id']}")
    assert live.cancelled == ["1-9999"] and b["status"] == "cancelled"


def test_stay_stays_booked_if_hotelbeds_will_not_cancel():
    c, live = live_chat(fail_cancel=True)
    book_live(c)
    (b,) = c.hotel_repo.bookings.values()
    c.send(reply_id=f"hbkc:{b['id']}")
    out = c.send(reply_id=f"hbkcy:{b['id']}")
    assert "still booked" in out["body"] and b["status"] == "confirmed"


def test_own_database_hotels_never_call_hotelbeds():
    live = LiveBooking(client())
    a, repo = agent(live)
    room = repo.room_of("Calangute Shores", "Deluxe")  # a seeded room: no Hotelbeds rate key
    booking = {"id": "b1", "ref": "HBAAAAA", "guest_name": "Aarav Sharma", "guests": 2}
    assert asyncio.run(a._book_supplier(booking, room)) is booking and live.booked == []


def test_with_hotelbeds_every_known_city_can_be_searched():
    a, _ = agent(Live(client()))
    assert "DEL" in asyncio.run(a._hotel_cities())  # Delhi has no stored hotels, but Hotelbeds can search it
    a2, _ = agent(None)
    assert "DEL" not in asyncio.run(a2._hotel_cities())  # without Hotelbeds only cities with our own hotels
