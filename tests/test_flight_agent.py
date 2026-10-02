from datetime import timedelta

from fakes import Chat, FakeRepo
from app.core.utils import now_ist, parse_date


def seats(repo, no):
    return next(f for f in repo.flights.values() if f["flight_no"] == no)["seats_left"]


def test_full_booking_and_cancel():
    repo = FakeRepo()
    c = Chat(repo)
    c.enter_flights()
    assert c.last[-1]["type"] == "text" and "flying from" in c.last[-1]["body"]  # one plain question, no list to scroll

    results = c.send("indore to mumbai tomorrow")  # route and date in one message: straight to the flights
    assert results["type"] == "list"
    c.send("cheapest")
    results = c.last[-1]
    flight_ids = [r[0] for r in results["rows"] if r[0].startswith("flt:")]
    assert len(flight_ids) == 2  # sold-out flight hidden
    assert flight_ids[0].endswith(next(f["id"] for f in repo.flights.values() if f["price_inr"] == 3500))  # cheapest first

    detail = c.send(reply_id=flight_ids[1])
    assert "6E-1000" in detail["body"] and "act:book" in c.ids()
    summary = c.book()  # one traveller we know by name: straight to the summary, no questions
    assert "Aarav Sharma" in summary["body"] and [i for i, _ in summary["buttons"]][0] == "cfm:yes"

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
    assert "type the traveller name" in c.last[-1]["body"]  # names can be typed right under the flight
    confirm = c.send("Aarav Sharma, priya sharma")
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
    assert c.send(reply_id="from:BOM")["type"] == "text"  # carries on to the date, Dubai remembered
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


# ------------------------------------------------------------------ natural language: few messages, no menus
def test_one_message_with_route_date_and_round_trip_goes_straight_to_the_flights():
    c = Chat()
    c.send("I want to fly to mumbai. Show me flights.")
    assert c.last[-1]["type"] == "text" and "Mumbai" in c.last[-1]["body"] and "flying from" in c.last[-1]["body"]
    out = c.send(f"Indore to Mumbai, tomorrow, round trip till {(now_ist().date() + timedelta(days=4)):%d/%m}")
    assert out["type"] == "list" and out["rows"][0][0].startswith("flt:")


def test_a_booking_takes_four_messages_from_hello_to_the_summary():
    c = Chat()
    c.send("flight from indore to mumbai tomorrow")           # 1: the request
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))  # 2: pick a flight
    summary = c.send("book")                                    # 3: book (the name is already known)
    assert "Last check" in summary["body"] and "Aarav Sharma" in summary["body"]
    assert "Booking Confirmed" in c.send("yes")["body"]          # 4: confirm


def test_names_typed_under_the_flight_card_set_the_party_and_the_total():
    c = Chat()
    c.send("flight from indore to mumbai tomorrow")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))
    summary = c.send("Rahul Verma and Priya Verma")
    assert "Rahul Verma" in summary["body"] and "Priya Verma" in summary["body"] and "×" in summary["body"]


def test_the_return_date_given_up_front_is_used_for_the_return_flight():
    from datetime import timedelta
    from app.core.utils import now_ist
    repo = FakeRepo()
    for f in list(repo.flights.values()):  # the reverse route, a few days later
        back = f.copy()
        back.update(id="r" + f["id"], from_code=f["to_code"], to_code=f["from_code"],
                    departure_time=(now_ist() + timedelta(days=4, hours=3)).isoformat(),
                    arrival_time=(now_ist() + timedelta(days=4, hours=5)).isoformat(), seats_left=5, status="scheduled")
        repo.flights[back["id"]] = back
    c = Chat(repo)
    back_day = now_ist().date() + timedelta(days=4)
    c.send(f"flight from indore to mumbai tomorrow and back on {back_day:%d/%m}")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))
    c.send("book"); out = c.send("yes")
    assert "coming back" in c.last[1]["body"]
    ret = c.send(reply_id="act:return")
    assert "Mumbai ➜ Indore" in ret["body"] and ret["type"] == "list"


def test_trip_parser_understands_ranges_and_party():
    from datetime import date
    from app.agents.flight.nlu import parse_trip
    today = date(2026, 10, 2)
    got = parse_trip("12-23 round trip for 2 people", today)
    assert got["date"] == "2026-10-12" and got["return_date"] == "2026-10-23" and got["round_trip"] and got["pax"] == 2
    assert "return_date" not in parse_trip("tomorrow one way", today)
