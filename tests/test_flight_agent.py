from datetime import timedelta

from fakes import Chat, FakeRepo
from app.core.utils import now_ist, parse_date


def seats(repo, no):
    return next(f for f in repo.flights.values() if f["flight_no"] == no)["seats_left"]


def test_full_booking_and_cancel():
    repo = FakeRepo()
    c = Chat(repo)
    c.enter_flights()
    assert "from:loc" in c.ids()

    origin = c.send(reply_id="menu:book")
    assert {r[0] for r in origin["rows"]} == {"from:loc", "from:IDR", "from:BOM", "from:more"}  # own country first, others via "Another city"
    dest = c.send(reply_id="from:IDR")
    assert {r[0] for r in dest["rows"]} == {"to:BOM", "to:more"}  # only places a flight goes to, the rest can be typed
    c.send(reply_id="to:BOM")
    c.send(reply_id=c.ids()[1])  # tomorrow
    results = c.send(reply_id="sort:cheap")
    flight_ids = [r[0] for r in results["rows"] if r[0].startswith("flt:")]
    assert len(flight_ids) == 2  # sold-out flight hidden
    assert flight_ids[0].endswith(next(f["id"] for f in repo.flights.values() if f["price_inr"] == 3500))  # cheapest first

    detail = c.send(reply_id=flight_ids[1])
    assert "6E-1000" in detail["body"] and "act:book" in c.ids()
    assert c.book()["buttons"][0][0] == "name:self"
    assert "Aarav Sharma" in c.send(reply_id="name:self")["body"]

    done = c.send(reply_id="cfm:yes")
    assert "Booking Confirmed" in done["body"] and "ABC123" in done["body"]
    assert seats(repo, "6E-1000") == 2

    assert c.send(reply_id="menu:bookings")["type"] == "list"
    c.send(reply_id=c.ids()[0])
    assert "bkc:" in c.ids()[0]
    c.send(reply_id=c.ids()[0])
    assert "non-refundable" not in c.last[0]["body"].lower()  # first flight is refundable
    assert "has been cancelled" in c.send(reply_id=c.ids()[0])["body"]
    assert seats(repo, "6E-1000") == 3


def test_typed_date_and_other_passenger():
    c = Chat()
    c.enter_flights()
    c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    assert "understand that date" in c.send("blah")["body"]
    assert c.send("kal")["type"] == "list"  # straight to the flights
    c.send(reply_id=c.ids()[0])
    c.book()
    assert "full name" in c.send(reply_id="name:other")["body"].lower()
    assert "letters" in c.send("R2D2")["body"]
    assert "Rohan Verma" in c.send("rohan   verma")["body"]


def test_duplicate_webhook_and_stale_button():
    c = Chat()
    c.send("hi")
    c.n -= 1  # same message id delivered again
    assert c.send("hi") is None
    assert "no longer valid" in c.send(reply_id="cfm:yes")["body"]  # stale button, no flight picked


def test_sold_out_race():
    repo = FakeRepo()
    c = Chat(repo)
    c.enter_flights()
    c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    c.send(reply_id=c.ids()[1]); c.send(reply_id="sort:time")
    fid = next(i for i in c.ids() if i.startswith("flt:"))
    c.send(reply_id=fid); c.book(); c.send(reply_id="name:self")
    repo.flights[fid[4:]]["seats_left"] = 0  # someone else grabs the last seat
    assert "enough seats" in c.send(reply_id="cfm:yes")["body"]


def test_quick_trip_repeat_last_and_popular():
    c = Chat()
    c.enter_flights()
    c.send(reply_id="menu:quick")
    assert "trip:IDR-BOM" in c.ids() and "trip:explore" in c.ids()
    c.send(reply_id="trip:IDR-BOM")  # skips origin/destination, straight to the date
    assert c.last[0]["type"] == "list" and c.ids()[0].startswith("date:")
    c.send(reply_id=c.ids()[1]); c.send(reply_id="sort:time")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))
    c.book(); c.send(reply_id="name:self"); c.send(reply_id="cfm:yes")
    c.send(reply_id="menu:quick")
    assert c.last[0]["rows"][0][1].startswith("\U0001F501")  # "repeat last trip" row appears


def test_explore_surprise_me():
    c = Chat()
    c.enter_flights(); c.send(reply_id="menu:quick")
    assert "Surprise" in c.send(reply_id="trip:explore")["body"]
    assert c.send(reply_id="from:IDR")["type"] == "list"  # goes to date, no destination step
    out = c.send(reply_id=c.ids()[1])
    assert "Getaways" in out["body"] and out["rows"][0][0].startswith("flt:")


def test_confirmation_has_ticket_tip_and_reaction():
    c = Chat()
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")
    assert [m["type"] for m in c.last] == ["text", "buttons", "reaction"]
    assert "Mumbai tip" in c.last[1]["body"]


def test_parse_date():
    today = now_ist().date()
    assert parse_date("15/10", today).month == 10
    assert parse_date("5 oct", today).day == 5
    assert parse_date("aaj", today) == today
    assert parse_date("31/02", today) is None
    assert parse_date("random", today) is None


def test_multiple_travellers_names_total_and_seats():
    repo = FakeRepo()
    c = Chat(repo)
    c.enter_flights()
    c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    c.send(reply_id=c.ids()[1]); c.send(reply_id="sort:time")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))  # 6E-1000: 3 seats, Rs 4,000
    pax = c.send(reply_id="act:book")
    assert [r[0] for r in pax["rows"]] == ["pax:1", "pax:2", "pax:3"] and "8,000" in pax["rows"][1][2]
    c.send(reply_id="pax:2")
    c.send(reply_id="name:self")
    assert "traveller 2 of 2" in c.last[0]["body"]
    confirm = c.send("priya sharma")
    assert "Aarav Sharma" in confirm["body"] and "Priya Sharma" in confirm["body"] and "8,000" in confirm["body"]
    done = c.send(reply_id="cfm:yes")
    assert "2 travellers" in done["body"] and seats(repo, "6E-1000") == 1
    c.send(reply_id="menu:bookings"); c.send(reply_id=c.ids()[0])
    c.send(reply_id=c.ids()[0]); c.send(reply_id=c.ids()[0])  # cancel -> confirm
    assert seats(repo, "6E-1000") == 3  # both seats come back


def test_return_flight_starts_from_the_outbound_day():
    c = Chat()
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")
    assert "act:return" in c.ids()
    out = c.send(reply_id="act:return")
    assert "Return trip" in out["body"] and "Mumbai" in out["body"] and "Indore" in out["body"]
    assert c.ids()[0].startswith("date:") and c.ids()[0][5:] == c.concierge.repo.convos["+919876543210"]["context"]["min_date"]


def test_no_flights_suggests_next_available_day():
    repo = FakeRepo()
    for f in repo.flights.values():  # move everything to 3 days from now
        for key in ("departure_time", "arrival_time"):
            f[key] = (now_ist() + timedelta(days=3, hours=2)).isoformat()
    c = Chat(repo)
    c.enter_flights(); c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    out = c.send(reply_id=c.ids()[1])  # tomorrow: nothing
    assert "No flights" in out["body"] and "has flights on these days" in out["body"] and c.ids()[0].startswith("date:")
    assert c.send(reply_id=c.ids()[0])["rows"][0][0].startswith("flt:")  # tapping a day shows its flights


def test_cheaper_nearby_day_tip():
    repo = FakeRepo()
    cheap = next(iter(repo.flights.values())).copy()
    cheap.update(id="cheap1", price_inr=2000, seats_left=5, status="scheduled", flight_no="6E-9999",
                 departure_time=(now_ist() + timedelta(days=2, hours=5)).isoformat(),
                 arrival_time=(now_ist() + timedelta(days=2, hours=7)).isoformat())
    repo.flights["cheap1"] = cheap
    c = Chat(repo)
    c.enter_flights(); c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    c.send(reply_id=c.ids()[1])  # tomorrow: cheapest is Rs 3,500, the day after has Rs 2,000
    assert "cheaper" in c.send(reply_id="sort:cheap")["body"]


def to_dxb_chat():
    """A user who picked Indore and then typed Dubai: nothing flies Indore to Dubai, but Mumbai does."""
    repo = FakeRepo()
    dep = (now_ist() + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    repo.flights["x1"] = {"id": "x1", "airline": "Emirates", "flight_no": "EK-501", "from_code": "BOM", "to_code": "DXB",
                          "departure_time": dep.isoformat(), "arrival_time": (dep + timedelta(hours=3)).isoformat(),
                          "duration_min": 180, "price_inr": 15000, "class": "Economy", "seats_left": 5, "baggage_kg": 25,
                          "stops": 0, "refundable": True, "status": "scheduled"}
    c = Chat(repo)
    c.enter_flights(); c.send(reply_id="menu:book"); c.send(reply_id="from:IDR")
    return c


def test_a_route_with_no_flights_offers_the_cities_that_do_fly_there():
    c = to_dxb_chat()
    c.send(reply_id="to:more")
    out = c.send("dubai")
    assert "no direct flights from *Indore* to *Dubai*" in out["body"] and c.ids() == ["from:BOM"]
    assert "km away" in out["rows"][0][2] and "from ₹15,000" in out["rows"][0][2] and "nearest to you" in out["body"]
    assert "Got it" not in c.send(reply_id="from:BOM")["body"] and c.last[0]["type"] == "list"  # carries on to the date, Dubai remembered
    assert "Mumbai ➜ Dubai" in c.last[0]["body"]


def test_asking_for_dates_shows_the_days_that_have_flights():
    c = Chat()
    c.enter_flights(); c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    out = c.send("suggest me date in which flights are available")
    assert "has flights on these days" in out["body"] and c.ids()[0].startswith("date:") and "from" in out["rows"][0][2]
    assert c.send(reply_id=c.ids()[0])["rows"][0][0].startswith("flt:")


def test_typing_cheapest_or_fastest_resorts_the_flights():
    c = Chat()
    c.enter_flights(); c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    c.send(reply_id=c.ids()[1])
    out = c.send("cheapest")
    assert "cheapest first" in out["body"] and out["rows"][0][2].startswith("₹3,500")


def test_when_nothing_flies_to_the_destination_nearby_places_are_offered():
    c = Chat()
    c.enter_flights(); c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:more")
    out = c.send("pune")                                                    # nobody flies to Pune in the fake data, but Mumbai is close
    assert "can't fly to *Pune*" in out["body"] and c.ids() == ["to:BOM"] and "km from Pune" in out["rows"][0][2]
    assert "Indore ➜ Mumbai" in c.send(reply_id="to:BOM")["body"]           # picking it carries on to the date


def test_a_city_with_no_departures_offers_the_nearest_airports_that_have_some():
    c = Chat()
    c.enter_flights(); c.send(reply_id="menu:book")
    out = c.send(reply_id="from:DXB") if False else None
    c.concierge.repo.flights.clear()                                       # nothing flies from anywhere...
    f = {"id": "z1", "airline": "IndiGo", "flight_no": "6E-1", "from_code": "BOM", "to_code": "IDR", "seats_left": 3, "status": "scheduled",
         "departure_time": (now_ist() + timedelta(days=1)).isoformat(), "arrival_time": (now_ist() + timedelta(days=1, hours=2)).isoformat(),
         "duration_min": 90, "price_inr": 4000, "class": "Economy", "baggage_kg": 15, "stops": 0, "refundable": False}
    c.concierge.repo.flights["z1"] = f                                     # ...except Mumbai
    out = c.send(reply_id="from:IDR")
    assert "no flights leaving *Indore*" in out["body"] and c.ids() == ["from:BOM"] and "km away" in out["rows"][0][2]
