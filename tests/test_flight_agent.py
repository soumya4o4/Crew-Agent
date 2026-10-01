from datetime import timedelta

from fakes import Chat, FakeRepo
from app.core.utils import now_ist, parse_date


def seats(repo, no):
    return next(f for f in repo.flights.values() if f["flight_no"] == no)["seats_left"]


def test_full_booking_and_cancel():
    repo = FakeRepo()
    c = Chat(repo)
    c.enter_flights()
    assert "menu:book" in c.ids()

    origin = c.send(reply_id="menu:book")
    assert {r[0] for r in origin["rows"]} == {"from:IDR", "from:BOM", "from:more"}  # own country first, others via "Another city"
    dest = c.send(reply_id="from:IDR")
    assert {r[0] for r in dest["rows"]} == {"to:BOM", "to:DXB"}
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
    assert c.send("kal")["type"] == "buttons"  # sort choice
    c.send(reply_id="sort:time")
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
    c.send(reply_id=c.ids()[1]); out = c.send(reply_id="sort:time")  # tomorrow: nothing
    assert "Next available" in out["body"] and c.ids()[0].startswith("date:")
    assert c.send(reply_id=c.ids()[0])["type"] == "buttons"  # tapping it goes straight to sorting


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
