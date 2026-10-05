"""International trips: passport question, visa advice, and the suggestions that follow a booking."""
import asyncio
import json
import uuid
from datetime import timedelta

from app.agents.base import Session
from app.agents.concierge.classifiers import Intent
from app.agents.concierge.slots import extract_slots
from app.agents.flight.formatting import suggest_steps
from app.core import places
from app.core.utils import now_ist
from app.services.travel_advisor import TravelAdvisor, VisaAdvice, parse_visa_advice
from fakes import DETAILS, Chat, FakeRepo


class FakeAdvisor:
    def __init__(self, advice=None, tip="Try the dosa 🥞"):
        self.advice, self.tip, self.calls = advice, tip, []

    async def visa_check(self, citizenship, country):
        self.calls.append((citizenship, country))
        return self.advice

    async def city_tip(self, city, country=""):
        return self.tip

    async def place_country(self, place):
        return {"new york": ("New York", "United States")}.get(place.lower())


def add_dubai_flight(repo):
    dep = (now_ist() + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    fid = str(uuid.uuid4())
    repo.flights[fid] = {"id": fid, "airline": "Emirates", "flight_no": "EK-501", "from_code": "BOM", "to_code": "DXB",
                         "departure_time": dep.isoformat(), "arrival_time": (dep + timedelta(hours=3)).isoformat(),
                         "duration_min": 180, "price_inr": 15000, "class": "Economy", "seats_left": 5, "baggage_kg": 25,
                         "stops": 0, "refundable": True, "status": "scheduled"}
    return fid


def open_dubai_flight(c, fid):
    c.send("hi"); c.send(reply_id="svc:flight")
    return c.send(reply_id=f"flt:{fid}")


def test_a_domestic_flight_never_asks_for_a_passport():
    c = Chat(advisor=FakeAdvisor())
    c.enter_flights(); c.pick_flight()
    assert c.send(reply_id="cfm:yes")["type"] == "text"
    assert c.last[1]["buttons"][0][0] == "svc:hotel" and c.last[1]["buttons"][1][0] == "svc:cab"
    assert "Try the dosa" in c.last[1]["body"] and "Heads up" not in c.last[1]["body"]


def test_an_international_flight_never_brings_up_the_visa_unasked():
    repo = FakeRepo()
    fid = add_dubai_flight(repo)
    advisor = FakeAdvisor(VisaAdvice("e_visa", "Apply online for a tourist e-visa.", 30))
    c = Chat(repo, advisor=advisor)
    card = open_dubai_flight(c, fid)
    assert "visa" not in card["body"].lower() and "passport" not in card["body"].lower()
    assert "svc:visa" not in c.ids() and advisor.calls == []
    c.send(reply_id="act:book"); c.send(DETAILS)
    c.send(reply_id="cfm:yes")
    assert "visa" not in c.last[1]["body"].lower() and "svc:visa" not in c.ids()
    assert c.ids()[0] == "svc:hotel"                                      # hotel first, then the airport cab and forex


def test_the_visa_agent_still_answers_when_asked_for_it():
    c = Chat(advisor=FakeAdvisor())
    out = c.send("I need a visa for Dubai")
    assert "UAE" in out["body"] or "visa" in out["body"].lower()


def test_suggestions_follow_the_trip():
    domestic = {"intl": False, "city": "Goa"}
    assert suggest_steps(domestic)[:2] == ["hotel", "cab"]
    abroad = {"intl": True, "city": "Paris"}
    assert suggest_steps(abroad)[:3] == ["hotel", "cab", "forex"] and "visa" not in suggest_steps(abroad)  # never unasked
    assert suggest_steps(domestic, queue=["events"])[0] == "events" and "cab" not in suggest_steps(domestic, done=("cab",))


def test_cities_come_from_the_registry_not_the_code():
    places.load_airports([{"code": "CDG", "city": "Paris", "name": "Charles de Gaulle", "country": "France", "lat": 49.0, "lon": 2.55}])
    assert places.city("CDG") == "Paris" and places.is_international("BOM", "CDG")
    assert extract_slots("flight from mumbai to paris tomorrow", now_ist().date())["to"] == "CDG"
    assert places.visa_code_for("United Arab Emirates") == "AE"


def test_a_trip_wish_to_a_place_we_cannot_book_offers_a_plan_without_visa_talk():
    c = Chat(advisor=FakeAdvisor())
    c.send("hi")
    s = Session("+919876543210", {"id": "u"}, "menu", "m1", c.repo.convos.get("+919876543210", {}).get("context", {}))
    out = asyncio.run(c.concierge._apply(s, Intent("flight", {"unknown_to": "New York"})))
    assert "can't book flights to New York" in out[0]["body"] and "visa" not in out[0]["body"].lower()
    assert [i for i, _ in out[0]["buttons"]] == ["svc:planner", "nav:menu"]


def test_origin_can_be_picked_by_sharing_a_location():
    c = Chat()
    c.enter_flights()
    c.send(reply_id="menu:book")
    assert "location" in c.send(reply_id="from:loc")["body"].lower()
    out = c.send_location(22.72, 75.86)                                    # Indore
    assert "Indore" in c.last[-1]["body"] and out is not None and "Tell me where to" in c.last[-1]["body"]


def test_visa_answer_is_validated():
    ok = parse_visa_advice(json.dumps({"status": "e_visa", "summary": "Apply <b>online</b>", "max_stay_days": 30}))
    assert ok.status == "e_visa" and ok.summary == "Apply (b)online(/b)" and ok.max_stay_days == 30 and ok.needs_visa
    assert parse_visa_advice(json.dumps({"status": "maybe", "summary": "x"})) is None
    assert parse_visa_advice("not json") is None
    assert parse_visa_advice(json.dumps({"status": "required", "summary": "x", "max_stay_days": 9999})).max_stay_days is None


def test_advisor_survives_a_failing_model():
    class Down:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    raise RuntimeError("down")

    advisor = TravelAdvisor(Down, "m")
    assert asyncio.run(advisor.visa_check("India", "Japan")) is None and asyncio.run(advisor.city_tip("Tokyo")) is None
