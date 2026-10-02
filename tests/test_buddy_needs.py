"""Buddy works out what the user needs next and offers it as buttons."""
from datetime import timedelta

from app.agents.buddy.brain import BuddyReply
from app.agents.buddy.needs import next_needs
from app.core.utils import now_ist
from fakes import Chat
from fakes_buddy import FakeBrain
from test_buddy_trip import booked_chat

NOW = now_ist()


def flight(frm="IDR", to="BOM", in_hours=10):
    dep = NOW + timedelta(hours=in_hours)
    return {"flights": {"from_code": frm, "to_code": to, "departure_time": dep.isoformat(), "arrival_time": (dep + timedelta(hours=2)).isoformat()}}


def services(needs):
    return [s for s, _ in needs]


def test_a_flight_tomorrow_means_a_cab_then_a_stay_at_the_destination():
    needs = next_needs(now=NOW, text="kal nikalna hai", flight=flight(), stay=None, trip=None, has_location=False)
    assert services(needs) == ["cab", "hotel", "events"] and needs[1][1] == "🏨 Stay in Mumbai"


def test_a_stay_already_booked_at_the_destination_is_not_suggested_again():
    stay = {"hotels": {"city_code": "BOM"}}
    assert "hotel" not in services(next_needs(now=NOW, text="hi", flight=flight(), stay=stay, trip=None, has_location=False))


def test_flying_abroad_adds_visa_and_forex():
    needs = next_needs(now=NOW, text="hello", flight=flight("BOM", "DXB", in_hours=60), stay=None, trip=None, has_location=False)
    assert services(needs) == ["hotel", "visa", "forex"]                    # far enough away that no airport cab yet


def test_a_question_about_the_hotel_leads_to_a_ride_and_food_nearby():
    stay = {"hotels": {"city_code": "GOI"}}
    needs = next_needs(now=NOW, text="hotel mein wifi hai?", flight=None, stay=stay, trip=None, has_location=False)
    assert services(needs) == ["cab", "nearby", "events"]


def test_bored_or_hungry_points_to_things_nearby():
    assert services(next_needs(now=NOW, text="bahut bore ho raha hu", flight=None, stay=None, trip=None, has_location=True))[0] == "nearby"
    assert next_needs(now=NOW, text="bhookh lagi hai", flight=None, stay=None, trip=None, has_location=True)[0][1] == "🍽️ Food near me"


def test_a_trip_booked_earlier_still_prompts_a_hotel():
    trip = {"to": "GOI", "city": "Goa", "intl": False}
    assert services(next_needs(now=NOW, text="hi", flight=None, stay=None, trip=trip, has_location=False)) == ["hotel", "cab"]


def test_nothing_is_suggested_without_a_trip_or_a_need():
    assert next_needs(now=NOW, text="mera din kharab tha", flight=None, stay=None, trip=None, has_location=False) == []


def test_buddy_attaches_the_buttons_to_a_plain_answer():
    c, brain = booked_chat()                                               # Indore to Mumbai
    for b in c.repo.bookings.values():                                     # make it leave in 6 hours, whatever time the test runs
        b["flights"]["departure_time"] = (now_ist() + timedelta(hours=6)).isoformat()
    brain.queue.append(BuddyReply("Kal subah 9 baje ki flight hai, 2 ghante pehle pahunch jana."))
    out = c.send("kal kitne baje nikalna chahiye")
    ids = [i for i, _ in out["buttons"]]
    assert ids[0] == "svc:cab" and "svc:hotel" in ids and len(ids) <= 3


def test_buddy_stays_quiet_when_someone_is_upset():
    brain = FakeBrain()
    c = Chat(brain=brain)
    c.send("hi")
    brain.queue.append(BuddyReply("Main yahin hoon.", risk="self_harm"))
    assert c.send("ab aur nahi ho raha")["type"] == "text"


def test_flights_opens_straight_on_the_route_with_the_last_trip_one_tap_away():
    c = Chat()
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")
    c.send(reply_id="nav:menu"); out = c.send(reply_id="svc:flight")
    assert out["type"] == "list" and "Flights" in out["body"] and out["rows"][1][0] == "trip:IDR-BOM"
