from datetime import datetime, timedelta

from fakes import Chat, FakeRepo
from app.agents.cab.pricing import distance_km, multiplier, quote
from app.core.utils import IST, now_ist, parse_time_text


def place_id(repo, name):
    return next(p["id"] for p in repo.cab_places if p["name"] == name)


def start_cab(c):
    c.send("hi")
    return c.send(reply_id="svc:cab")


def book_ride(c, when="now", vehicle="Sedan"):
    """Mumbai Airport -> Bandra West."""
    repo = c.repo
    c.send(reply_id="cab:book"); c.send(reply_id="ccity:BOM")
    c.send(reply_id=f"cpick:{place_id(repo, 'Mumbai Airport T2')}")
    c.send(reply_id=f"cdrop:{place_id(repo, 'Bandra West')}")
    c.send(reply_id=f"cwhen:{when}")
    c.send(reply_id=f"cveh:{vehicle}")
    return c.send(reply_id="ccfm:yes")


def test_cab_menu_and_full_booking():
    c = Chat()
    assert "cab:book" in [i for i, _ in start_cab(c)["buttons"]]
    cities = c.send(reply_id="cab:book")
    assert {r[0] for r in cities["rows"]} == {"ccity:BOM", "ccity:IDR"}
    places = c.send(reply_id="ccity:BOM")
    assert places["rows"][0][1] == "Mumbai Airport T2"  # airport listed first
    drop = c.send(reply_id=f"cpick:{place_id(c.repo, 'Mumbai Airport T2')}")
    assert "Mumbai Airport T2" not in [r[1] for r in drop["rows"]]  # can't drop where you were picked up
    c.send(reply_id=f"cdrop:{place_id(c.repo, 'Bandra West')}")
    vehicles = c.send(reply_id="cwhen:now")
    assert [r[0] for r in vehicles["rows"]] == ["cveh:Mini", "cveh:Sedan", "cveh:SUV", "cveh:Luxury"]
    assert "airport fee" in vehicles["body"]
    assert vehicles["rows"][0][2].startswith("💸 Cheapest")
    summary = c.send(reply_id="cveh:Sedan")
    assert "Confirm your cab" in summary["body"] and "Bandra West" in summary["body"]
    ticket = c.send(reply_id="ccfm:yes")
    assert "Cab Confirmed" in ticket["body"] and "OTP" in ticket["body"] and "MH01 AB 1234" in ticket["body"]
    assert [m["type"] for m in c.last] == ["text", "buttons", "reaction"]


def test_typed_time_and_bad_time():
    c = Chat()
    start_cab(c)
    c.send(reply_id="cab:book"); c.send(reply_id="ccity:BOM")
    c.send(reply_id=f"cpick:{place_id(c.repo, 'Bandra West')}")
    c.send(reply_id=f"cdrop:{place_id(c.repo, 'Powai')}")
    assert "couldn't read that time" in c.send("whenever")["body"]
    out = c.send("tomorrow 6am")
    assert "Tomorrow · 06:00" in out["body"] and out["type"] == "list"
    assert "Night" not in out["body"]


def test_airport_pickup_uses_the_flight_arrival_time():
    c = Chat()
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")
    out = c.send(reply_id="svc:cab")  # suggested after the booking
    assert "You land in *Mumbai*" in out["body"] and "cab:airport" in c.ids()
    drop = c.send(reply_id="cab:airport")
    assert "Pickup: Mumbai Airport T2" in drop["body"]  # pickup is already the airport
    c.send(reply_id=f"cdrop:{place_id(c.repo, 'Powai')}")
    assert c.last[0]["type"] == "list" and "cwhen:arrival" in c.ids()


def test_airport_ride_offers_pickup_after_landing():
    c = Chat()
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")
    c.send(reply_id="svc:cab"); c.send(reply_id="cab:airport")
    out = c.send(reply_id=f"cdrop:{place_id(c.repo, 'Powai')}")
    arrival_row = next(r for r in out["rows"] if r[0] == "cwhen:arrival")
    assert "Pickup at" in arrival_row[2]
    assert c.send(reply_id="cwhen:arrival")["rows"][0][0] == "cveh:Mini"  # straight to choosing the car


def test_free_text_cab_request_jumps_to_pickup():
    c = Chat()
    out = c.send("need a cab in mumbai")
    assert "pick you up in Mumbai" in out["body"]


def test_my_rides_detail_and_free_cancellation():
    c = Chat()
    start_cab(c)
    book_ride(c, when="60")  # an hour from now -> free cancellation
    rides = c.send(reply_id="cab:rides")
    assert rides["rows"][0][1].startswith("CBTEST1")
    detail = c.send(reply_id=rides["rows"][0][0])
    assert "OTP" in detail["body"] and c.ids()[0].startswith("ccancel:")
    ask = c.send(reply_id=c.ids()[0])
    assert "Free cancellation" in ask["body"]
    done = c.send(reply_id=c.ids()[0])
    assert "is cancelled" in done["body"] and "No charges" in done["body"]


def test_late_cancellation_has_a_fee():
    c = Chat()
    start_cab(c)
    book_ride(c, when="now")  # pickup is within 15 minutes
    c.send(reply_id="cab:rides"); c.send(reply_id=c.ids()[0])
    assert "₹50 fee applies" in c.send(reply_id=c.ids()[0])["body"]
    assert "₹50 fee applies" in c.send(reply_id=c.ids()[0])["body"]


def test_pricing_rules():
    km = distance_km(19.0887, 72.8679, 19.0596, 72.8295)  # Mumbai airport -> Bandra
    assert 4 < km < 10
    rate = {"base_fare": 55, "per_km": 15, "min_fare": 160}
    noon = datetime(2026, 10, 3, 12, 0, tzinfo=IST)
    assert quote(rate, 1.0, noon, False)["fare"] == 160  # minimum fare
    assert quote(rate, 10.0, noon, True)["fare"] == 305  # 55+150+100 airport fee, rounded to 5
    assert multiplier(noon.replace(hour=9))[0] == 1.2 and multiplier(noon.replace(hour=23))[0] == 1.15
    assert quote(rate, 10.0, noon.replace(hour=9), False)["fare"] == 245  # 205 x 1.2


def test_parse_time_text():
    now = datetime(2026, 10, 1, 19, 0, tzinfo=IST)
    assert parse_time_text("6pm", now) == datetime(2026, 10, 2, 18, 0, tzinfo=IST)  # already past today
    assert parse_time_text("tomorrow 7:30am", now) == datetime(2026, 10, 2, 7, 30, tzinfo=IST)
    assert parse_time_text("kal 8 am", now) == datetime(2026, 10, 2, 8, 0, tzinfo=IST)
    assert parse_time_text("15/10 9pm", now) == datetime(2026, 10, 15, 21, 0, tzinfo=IST)
    assert parse_time_text("21:15", now) == datetime(2026, 10, 1, 21, 15, tzinfo=IST)
    assert parse_time_text("blah", now) is None and parse_time_text("25:00", now) is None


def test_free_text_time_is_remembered_and_not_asked_again():
    c = Chat()
    out = c.send("cab in mumbai at 7:30pm")
    assert "pick you up in Mumbai" in out["body"]
    c.send(reply_id=f"cpick:{place_id(c.repo, 'Bandra West')}")
    vehicles = c.send(reply_id=f"cdrop:{place_id(c.repo, 'Powai')}")  # went straight to choosing the car
    assert vehicles["rows"][0][0] == "cveh:Mini" and "19:30" in vehicles["body"]
