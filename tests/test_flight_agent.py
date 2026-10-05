from datetime import date, timedelta

from fakes import DETAILS, Chat, FakeGateway, FakeRepo, CONTACT
from app.agents.flight.travellers import parse_travellers
from app.core.utils import now_ist, parse_date


def add_flight(repo, frm, to, days, hour, price, number, key=None):
    """Put one more flight on offer at the (fake) airlines."""
    dep = (now_ist() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    repo.inventory.append({
        "airline": "IndiGo", "flight_no": number, "from_code": frm, "to_code": to, "departure_time": dep.isoformat(),
        "arrival_time": (dep + timedelta(minutes=90)).isoformat(), "duration_min": 90, "price_inr": price, "class": "Economy",
        "checked_bags": 1, "baggage_kg": 0, "stops": 0, "refundable": True, "status": "scheduled", "duffel_offer_id": f"off_{number}",
        "offer_pax": 1, "offer_total": price / 80, "offer_currency": "USD", "offer_passenger_ids": ["pas_0"],
        "itinerary_key": key or f"{number}@{dep:%Y-%m-%dT%H:%M:%S}"})


def flight_ids(c):
    return [i for i in c.ids() if i.startswith("flt:")]


def book_first_flight(c, text="flight from indore to mumbai tomorrow"):
    c.send(text)
    c.send(reply_id=flight_ids(c)[0])
    return c.send("book")


def test_full_booking_and_cancel():
    c = Chat()
    c.enter_flights()
    assert c.last[-1]["type"] == "text" and "flying from" in c.last[-1]["body"]  # one plain question, no list to scroll

    results = c.send("indore to mumbai tomorrow")  # route and date in one message: straight to the live flights
    assert results["type"] == "list" and c.live.searches[0][:2] == ("IDR", "BOM")
    c.send("cheapest")
    ids = flight_ids(c)
    assert len(ids) == 3 and "₹3,000" in c.last[-1]["rows"][0][2]  # cheapest first

    detail = c.send(reply_id=ids[2])
    assert "6E-1000" in detail["body"] and "1 checked bag" in detail["body"] and "act:book" in c.ids()
    ask = c.book()  # the airline needs a birth date and a gender: one question for both
    assert "date of birth" in ask["body"] and "Aarav Sharma" in ask["body"]
    summary = c.send(DETAILS)
    assert "Aarav Sharma" in summary["body"] and "14 Mar 1992" in summary["body"] and [i for i, _ in summary["buttons"]][0] == "cfm:yes"

    done = c.send(reply_id="cfm:yes")
    assert "Your booking has been recorded successfully" in done["body"] and "ZX9Q2K" in done["body"]  # the airline's own reference
    order = next(iter(c.live.orders.values()))
    assert order["travellers"] == [{"name": "Aarav Sharma", "dob": "1992-03-14", "gender": "m"}]

    assert c.send(reply_id="menu:bookings")["type"] == "list"
    c.send(reply_id=c.ids()[0])
    assert "bkc:" in c.ids()[0]
    ask = c.send(reply_id=c.ids()[0])
    assert "ZX9Q2K" in ask["body"] and "refund" in ask["body"].lower()
    assert "has been cancelled" in c.send(reply_id=c.ids()[0])["body"]
    assert c.live.cancelled == ["cq_ord_1"] and next(iter(c.repo.bookings.values()))["status"] == "cancelled"


def test_the_refund_follows_what_the_airline_gives_back():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    book_first_flight(c); c.send(DETAILS); c.send("yes"); c.send(CONTACT)
    gw.paid.add("plink_1"); assert "Your booking has been recorded successfully" in c.send(reply_id="chk:check")["body"]
    c.live.refund_ratio = 0.5  # the airline keeps half
    c.send(reply_id="menu:bookings"); c.send(reply_id=c.ids()[0])
    ask = c.send(reply_id=c.ids()[0])
    assert "₹2,000" in ask["body"] and "of your ₹4,000" in ask["body"]
    done = c.send(reply_id=c.ids()[0])
    assert "refund has been initiated" in done["body"] and gw.refunds == [("plink_1", 2000)]


def test_a_ticket_the_airline_will_not_cancel_is_left_alone():
    c = Chat()
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")
    c.live.cant_cancel = True
    c.send(reply_id="menu:bookings"); c.send(reply_id=c.ids()[0])
    assert "can't be cancelled here" in c.send(reply_id=c.ids()[0])["body"]
    assert next(iter(c.repo.bookings.values()))["status"] == "confirmed"


def test_a_non_refundable_fare_says_so_and_refunds_nothing():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    book_first_flight(c); c.send(DETAILS); c.send("yes"); c.send(CONTACT)
    gw.paid.add("plink_1"); c.send(reply_id="chk:check")
    c.live.refund_ratio = 0
    c.send(reply_id="menu:bookings"); c.send(reply_id=c.ids()[0])
    assert "non-refundable" in c.send(reply_id=c.ids()[0])["body"].lower()
    assert "no refund applies" in c.send(reply_id=c.ids()[0])["body"] and gw.refunds == []


def test_the_birth_date_and_gender_are_asked_once_and_remembered():
    c = Chat()
    book_first_flight(c)
    assert "date of birth" in c.last[0]["body"]
    c.send(DETAILS); c.send("yes")
    c.send("flight from indore to mumbai tomorrow")
    c.send(reply_id=flight_ids(c)[0])
    summary = c.send("book")  # same traveller again: straight to the summary
    assert "Last check" in summary["body"] and "14 Mar 1992" in summary["body"]


def test_typed_date_and_other_passenger():
    c = Chat()
    c.enter_flights()
    c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    assert "understand that date" in c.send("blah")["body"]
    assert c.send("kal")["type"] == "list"  # straight to the flights
    c.send(reply_id=c.ids()[0])
    c.book()
    assert "date of birth" in c.last[0]["body"]  # asked for the user's own birth date and gender first; another name replaces them
    assert "letters" in c.send("R2D2")["body"]
    summary = c.send("rohan   verma 05-11-1988 male")
    assert "Rohan Verma" in summary["body"] and "05 Nov 1988" in summary["body"] and "Male" in summary["body"]


def test_changing_names_asks_for_everything_again():
    c = Chat()
    book_first_flight(c); c.send(DETAILS)
    assert "full name" in c.send(reply_id="cfm:name")["body"]
    assert "Priya Shah" in c.send("Priya Shah, 2 July 1994, F")["body"]


def test_a_missing_gender_or_birth_date_is_asked_for_by_name():
    c = Chat()
    c.send("flight from indore to mumbai tomorrow")
    c.send(reply_id=flight_ids(c)[0])
    ask = c.send("Rahul Verma and Priya Verma")
    assert "Rahul Verma" in ask["body"] and "Priya Verma" in ask["body"] and "date of birth and gender" in ask["body"]
    ask = c.send("14/03/1992 M")  # one nameless reply fills the first person still missing
    assert "*Priya Verma*" in ask["body"] and "Rahul" not in ask["body"]
    summary = c.send("Priya Verma 2/7/1994 F")
    assert "Rahul Verma" in summary["body"] and "Priya Verma" in summary["body"] and "×" in summary["body"] and "8,000" in summary["body"]


def test_duplicate_webhook_and_stale_button():
    c = Chat()
    c.send("hi")
    c.n -= 1  # same message id delivered again
    assert c.send("hi") is None
    assert "no longer valid" in c.send(reply_id="cfm:yes")["body"]  # stale button, no flight picked


def test_the_fare_is_asked_for_again_and_a_sold_out_flight_is_caught():
    c = Chat()
    c.enter_flights(); c.pick_flight()
    c.live.gone = True  # the airline stopped selling it between the summary and the tap
    assert "sold out" in c.send(reply_id="cfm:yes")["body"] and c.repo.bookings == {} and c.live.orders == {}


def test_the_airline_refusing_the_ticket_leaves_nothing_booked():
    c = Chat()
    c.enter_flights(); c.pick_flight()
    c.live.fail_book = True
    out = c.send(reply_id="cfm:yes")
    assert "sold out" in out["body"] and next(iter(c.repo.bookings.values()))["status"] == "cancelled"


def test_quick_trip_repeat_last_and_popular():
    c = Chat()
    c.enter_flights()
    c.send(reply_id="menu:quick")
    assert "trip:explore" in c.ids() and "trip:IDR-BOM" not in c.ids()  # nothing booked yet: no popular routes
    c.send(reply_id="menu:book"); c.send("indore to mumbai tomorrow"); c.send(reply_id=flight_ids(c)[0])
    c.book(); c.send(DETAILS); c.send(reply_id="cfm:yes")
    c.send(reply_id="menu:quick")
    assert c.last[0]["rows"][0][1].startswith("\U0001F501")  # "repeat last trip" row appears
    c.send(reply_id="trip:IDR-BOM")  # skips origin/destination, straight to the date
    assert c.last[0]["type"] == "list" and c.ids()[0].startswith("date:")


def test_explore_surprise_me():
    c = Chat()
    c.enter_flights(); c.send(reply_id="menu:quick")
    assert "Surprise" in c.send(reply_id="trip:explore")["body"]
    assert c.send(reply_id="from:IDR")["type"] == "list"  # goes to date, no destination step
    out = c.send(reply_id=c.ids()[1])
    assert "Getaways" in out["body"] and out["rows"][0][0].startswith("flt:")
    assert {s[0] for s in c.live.searches} == {"IDR"} and {s[1] for s in c.live.searches} <= {"BOM", "DXB"}


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


def test_multiple_travellers_are_priced_and_ticketed_together():
    c = Chat()
    c.send("flight from indore to mumbai tomorrow 2 people")  # the party size is known from the start: the search is for 2
    assert c.live.searches[-1][3] == 2
    c.send(reply_id=flight_ids(c)[0])  # 6E-1000, Rs 4,000 each
    assert "type the traveller name" in c.last[-1]["body"]  # names can be typed right under the flight card
    confirm = c.send("Aarav Sharma 14/03/1992 M, priya sharma 2/7/1994 f")
    assert "Aarav Sharma" in confirm["body"] and "Priya Sharma" in confirm["body"] and "8,000" in confirm["body"]
    done = c.send(reply_id="cfm:yes")
    assert "2 travellers" in done["body"]
    order = next(iter(c.live.orders.values()))
    assert [t["name"] for t in order["travellers"]] == ["Aarav Sharma", "Priya Sharma"] and order["flight"]["offer_pax"] == 2
    assert next(iter(c.repo.bookings.values()))["total_price_inr"] == 8000


def test_return_flight_starts_from_the_outbound_day():
    c = Chat()
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")
    assert "act:return" in c.ids()
    out = c.send(reply_id="act:return")
    assert "Return trip" in out["body"] and "Mumbai" in out["body"] and "Indore" in out["body"]
    assert c.ids()[0].startswith("date:") and c.ids()[0][5:] == c.concierge.repo.convos["+919876543210"]["context"]["min_date"]


def test_no_flights_suggests_the_next_days_that_have_some():
    repo = FakeRepo()
    for f in repo.inventory:  # move everything to 3 days from now
        for key in ("departure_time", "arrival_time"):
            f[key] = (now_ist() + timedelta(days=3, hours=2)).isoformat()
        f["itinerary_key"] += "x"
    c = Chat(repo)
    c.enter_flights(); c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    out = c.send(reply_id=c.ids()[1])  # tomorrow: nothing
    assert "No flights" in out["body"] and "has flights on these days" in out["body"] and c.ids()[0].startswith("date:")
    assert c.send(reply_id=c.ids()[0])["rows"][0][0].startswith("flt:")  # tapping a day shows its flights


def test_a_route_nobody_flies_says_so_and_offers_other_dates():
    repo = FakeRepo()
    repo.inventory.clear()
    c = Chat(repo)
    c.enter_flights()
    out = c.send("indore to mumbai tomorrow")
    assert "couldn't find flights" in out["body"] and "act:date" in c.ids() and "menu:book" in c.ids()
    assert c.send(reply_id="act:date")["type"] == "list"


def test_when_the_airlines_cannot_be_reached_the_traveller_is_told_and_can_retry():
    c = Chat()
    c.enter_flights()
    c.live.fail_search = True
    out = c.send("indore to mumbai tomorrow")
    assert "couldn't reach the airlines" in out["body"] and c.ids() == ["act:results", "nav:menu"]
    c.live.fail_search = False
    assert c.send(reply_id="act:results")["type"] == "list"


def test_asking_for_dates_shows_the_days_that_have_flights():
    repo = FakeRepo()
    add_flight(repo, "IDR", "BOM", 4, 10, 3300, "6E-2000")
    c = Chat(repo)
    c.enter_flights(); c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    out = c.send("suggest me date in which flights are available")
    assert "has flights on these days" in out["body"] and c.ids()[0].startswith("date:") and "from" in out["rows"][0][2]
    assert len(c.ids()) >= 3  # tomorrow, in four days, and "another date"
    assert c.send(reply_id=c.ids()[0])["rows"][0][0].startswith("flt:")


def test_typing_cheapest_or_fastest_resorts_the_flights():
    c = Chat()
    c.enter_flights(); c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    c.send(reply_id=c.ids()[1])
    out = c.send("cheapest")
    assert "cheapest first" in out["body"] and out["rows"][0][2].startswith("₹3,000")


def test_any_airport_in_the_table_can_be_searched_not_only_routes_with_a_timetable():
    repo = FakeRepo()
    add_flight(repo, "BOM", "DXB", 2, 9, 15000, "EK-501")
    c = Chat(repo)
    c.enter_flights(); c.send(reply_id="menu:book")
    c.send(f"mumbai to dubai {(now_ist().date() + timedelta(days=2)):%d/%m}")
    assert flight_ids(c) and c.live.searches[-1][:2] == ("BOM", "DXB")


# ------------------------------------------------------------------ natural language: few messages, no menus
def test_one_message_with_route_date_and_round_trip_goes_straight_to_the_flights():
    c = Chat()
    c.send("I want to fly to mumbai. Show me flights.")
    assert c.last[-1]["type"] == "text" and "Mumbai" in c.last[-1]["body"] and "flying from" in c.last[-1]["body"]
    out = c.send(f"Indore to Mumbai, tomorrow, round trip till {(now_ist().date() + timedelta(days=4)):%d/%m}")
    assert out["type"] == "list" and out["rows"][0][0].startswith("flt:")


def test_a_booking_takes_five_messages_from_hello_to_the_ticket_the_first_time():
    c = Chat()
    c.send("flight from indore to mumbai tomorrow")           # 1: the request
    c.send(reply_id=flight_ids(c)[0])                           # 2: pick a flight
    c.send("book")                                              # 3: book
    summary = c.send(DETAILS)                                   # 4: birth date and gender (once, they are remembered)
    assert "Last check" in summary["body"] and "Aarav Sharma" in summary["body"]
    assert "Your booking has been recorded successfully" in c.send("yes")["body"]         # 5: confirm


def test_names_typed_under_the_flight_card_set_the_party_and_the_total():
    c = Chat()
    c.send("flight from indore to mumbai tomorrow")
    c.send(reply_id=flight_ids(c)[0])
    summary = c.send("Rahul Verma 14/03/1992 M and Priya Verma 2/7/1994 F")
    assert "Rahul Verma" in summary["body"] and "Priya Verma" in summary["body"] and "×" in summary["body"]


def test_the_return_date_given_up_front_is_used_for_the_return_flight():
    repo = FakeRepo()
    add_flight(repo, "BOM", "IDR", 4, 12, 3600, "6E-4000")  # the reverse route, a few days later
    c = Chat(repo)
    back_day = now_ist().date() + timedelta(days=4)
    c.send(f"flight from indore to mumbai tomorrow and back on {back_day:%d/%m}")
    c.send(reply_id=flight_ids(c)[0])
    c.send("book"); c.send(DETAILS); 
    ret = c.send("yes")
    assert "Outbound flight added" in ret["body"] and "pick your return flight" in ret["body"]
    assert "Mumbai ➜ Indore" in c.last[-1]["body"] and c.last[-1]["type"] == "list"


def test_trip_parser_understands_ranges_and_party():
    from app.agents.flight.nlu import parse_trip
    today = date(2026, 10, 2)
    got = parse_trip("12-23 round trip for 2 people", today)
    assert got["date"] == "2026-10-12" and got["return_date"] == "2026-10-23" and got["round_trip"] and got["pax"] == 2
    assert "return_date" not in parse_trip("tomorrow one way", today)


# ------------------------------------------------------------------------------ who is travelling
def test_traveller_parser_takes_names_dates_and_gender_in_any_layout():
    today = date(2026, 10, 3)
    one = {"name": "Rahul Verma", "dob": "1992-03-14", "gender": "m"}
    assert parse_travellers("Rahul Verma, 14/03/1992, M", today) == [one]
    assert parse_travellers("rahul verma 14 Mar 1992 male", today) == [one]
    assert parse_travellers("Mr Rahul Verma 1992-03-14", today) == [one]
    two = parse_travellers("Rahul Verma, 14/03/1992, M, Priya Verma, 02/07/1994, F", today)
    assert [p["name"] for p in two] == ["Rahul Verma", "Priya Verma"] and two[1] == {"name": "Priya Verma", "dob": "1994-07-02", "gender": "f"}
    assert parse_travellers("Rahul, Priya", today) == [{"name": "Rahul"}, {"name": "Priya"}]  # details come later
    assert parse_travellers("14/03/1992 F", today) == [{"dob": "1992-03-14", "gender": "f"}]   # details for someone already named
    assert parse_travellers("book for me and Priya", today, "Aarav Sharma") == [{"name": "Aarav Sharma"}, {"name": "Priya"}]


def test_traveller_parser_rejects_what_is_not_a_person_or_a_real_date():
    today = date(2026, 10, 3)
    assert parse_travellers("R2D2", today) is None
    assert parse_travellers("me", today) is None  # no name known to stand for "me"
    assert parse_travellers("Rahul 31/02/1990", today) is None  # an impossible date is not a birth date, and not a name either
    assert parse_travellers("Rahul 14/03/2030", today) is None  # nor is one in the future
