"""Trip Guide: a newcomer is walked through the whole journey, from home and back home, with a self-updating roadmap."""
from datetime import timedelta

from app.agents.concierge.classifiers import KeywordClassifier
from app.agents.guide import roadmap as R
from app.agents.guide.agent import norm_country, same_country
from app.core.utils import now_ist
from app.services.travel_advisor import VisaAdvice, parse_ideas, parse_notes
from fakes import Chat

TODAY = now_ist().date()
NOTES = {"weather": "Hot and dry.", "packing": ["sunscreen", "light clothes"], "plugs": "Type G, 230V.", "sim_money": "Buy an eSIM.",
         "safety": "Safe city.", "health": "", "culture": ["Dress modestly in malls"], "must_do": ["Burj Khalifa"]}


class GuideAdvisor:
    """Stands in for the AI: scripted ideas, visa advice, notes and country lookup."""

    def __init__(self, advice=None, ideas=None, notes=None):
        self.advice, self.ideas, self.notes, self.calls = advice, ideas or [], notes, []

    async def visa_check(self, citizenship, country):
        self.calls.append(("visa", citizenship, country))
        return self.advice

    async def suggest_destinations(self, **kw):
        self.calls.append(("ideas", kw))
        return self.ideas

    async def trip_notes(self, **kw):
        return self.notes

    async def place_country(self, place):
        return {"bali": ("Bali", "Indonesia"), "paris": ("Paris", "France")}.get(place.lower())

    async def city_tip(self, city, country=""):
        return "A tip."

    async def currency_of(self, country):
        return None


def third_month(c):
    return [i for i in c.ids() if i.startswith("gwhen:") and i != "gwhen:more"][3]  # "In 3 months"


def plan_trip(c, place="dubai", origin="gorg:IDR", days="gdays:7", trip="gtrip:round"):
    """Passport -> known place -> where from -> when (3 months away) -> how long -> round trip -> the roadmap."""
    c.send("hi"); c.send(reply_id="svc:guide"); c.send(reply_id="guide:new")
    c.send(reply_id="gpass:home"); c.send(reply_id="gkn:yes"); c.send(place)
    c.send(reply_id=origin)
    c.send(reply_id=third_month(c))
    c.send(reply_id=days)
    return c.send(reply_id=trip)


# ----------------------------------------------------------------------- the pure roadmap
def test_a_round_trip_abroad_covers_the_whole_journey_and_passport_comes_first():
    travel = TODAY + timedelta(days=90)
    steps = R.build_steps(travel=travel, days=7, intl=True, visa_status="e_visa", visa_processing_days=4)
    assert steps[0].id == "passport"
    assert {s.id for s in steps} == {"passport", "visa", "flights", "hotel", "insurance", "forex", "rides", "pack", "return"}
    by_id = {s.id: s for s in steps}
    assert by_id["visa"].due == travel - timedelta(days=11) and by_id["flights"].due == travel - timedelta(days=45)
    assert by_id["return"].due == travel + timedelta(days=4) and "check-out" in by_id["hotel"].why and "ride home" in by_id["return"].why
    assert "there and back" in by_id["flights"].title


def test_one_way_has_no_return_day_and_domestic_skips_passport_visa_insurance_and_forex():
    one_way = R.build_steps(travel=TODAY + timedelta(days=60), days=5, intl=True, round_trip=False, visa_status="not_required")
    assert "return" not in {s.id for s in one_way} and {s.id for s in one_way} >= {"flights", "hotel", "rides"}
    domestic = R.build_steps(travel=TODAY + timedelta(days=30), days=3, intl=False, visa_status=None)
    assert [s.id for s in domestic] == ["flights", "hotel", "plan", "rides", "pack", "return"]


def test_visa_status_changes_the_visa_step():
    f = lambda status: [s.id for s in R.build_steps(travel=TODAY + timedelta(days=60), intl=True, visa_status=status)]
    assert "visa" not in f("not_required") and "visa_arrival" in f("on_arrival") and "visa" in f("required") and "visa" in f(None)
    assert R.visa_lead_days("required", None) == 37 and R.visa_lead_days("e_visa", 4) == 11


def test_progress_ticks_steps_off_and_flights_need_both_legs():
    steps = R.build_steps(travel=TODAY + timedelta(days=90), intl=True, visa_status="e_visa")
    R.apply_progress(steps, {"passport"}, {"flight": True, "flight_back": False, "forex": True, "visa": "in_review"})
    state = {s.id: (s.state, s.note) for s in steps}
    assert state["passport"][0] == "done" and state["visa"] == ("progress", "Application: in review") and state["forex"][0] == "done"
    assert state["flights"] == ("progress", "Way there booked, return still to book")
    assert R.next_step(steps).id == "flights"
    R.apply_progress(steps, set(), {"flight": True, "flight_back": True, "visa": "approved"})
    assert {s.id: s.state for s in steps}["flights"] == "done" and {s.id: s.state for s in steps}["visa"] == "done"
    one_way = R.apply_progress(R.build_steps(travel=TODAY + timedelta(days=90), intl=True, round_trip=False, visa_status="e_visa"),
                               set(), {"flight": True}, round_trip=False)
    assert {s.id: s.state for s in one_way}["flights"] == "done"


def test_a_late_start_is_flagged_and_dates_read_well():
    soon = TODAY + timedelta(days=8)
    steps = R.build_steps(travel=soon, intl=True, visa_status="required")
    assert "consider moving your date" in R.timeline_warning(steps, TODAY, soon)
    later = TODAY + timedelta(days=200)
    assert R.timeline_warning(R.build_steps(travel=later, intl=True, visa_status="e_visa"), TODAY, later) is None
    assert R.when_text(TODAY - timedelta(days=1), TODAY) == "⚠️ Do this now" and R.when_text(TODAY, TODAY) == "⏰ Today"
    assert R.when_text(TODAY + timedelta(days=20), TODAY).startswith("by ") and R.when_text(TODAY, TODAY, "done") == "Done"


def test_nationalities_and_countries_match():
    assert norm_country("Indian") == "india" and same_country("Indian", "India") and same_country("India", "india")
    assert not same_country("Indian", "United Arab Emirates") and not same_country("", "India")


def test_ai_answers_are_validated():
    ideas = parse_ideas('{"places": [{"place": "Bali <b>", "country": "Indonesia", "why": "Beaches", "cost_inr": 70000, "entry": "e-visa"},'
                        '{"place": "x"}, "junk", {"place": "Paris", "country": "France", "cost_inr": "lots"}]}')
    assert [i["place"] for i in ideas] == ["Bali (b)", "Paris"] and ideas[0]["cost_inr"] == 70000 and ideas[1]["cost_inr"] is None
    assert parse_ideas("not json") == [] and parse_notes("[]") is None and parse_notes('{"weather": ""}') is None
    notes = parse_notes('{"weather": "Hot", "packing": ["hat", "", 5, "x", "a", "b", "c", "d"], "culture": ["a", "b", "c", "d"]}')
    assert notes["weather"] == "Hot" and len(notes["packing"]) == 6 and len(notes["culture"]) == 3


# --------------------------------------------------------------------------------- routing
def test_newcomer_talk_about_travel_goes_to_the_guide_but_a_mood_does_not():
    k = KeywordClassifier()
    assert k.classify("i am new to travel, help me plan my first trip", TODAY).name == "guide"
    assert k.classify("pehli baar abroad ja raha hu kya kya karna padega", TODAY).name == "guide"
    assert k.classify("kuch samajh nahi aa raha, mood off hai", TODAY).name != "guide"


def test_the_menu_starts_with_the_guide_and_it_opens_with_a_friendly_intro():
    c = Chat()
    c.send("hi")
    assert c.ids()[0] == "svc:guide" and "svc:planner" not in c.ids()
    out = c.send(reply_id="svc:guide")
    assert "Trip Guide" in out["body"] and "getting back home" in out["body"] and c.ids() == ["guide:new", "svc:planner"]


# ------------------------------------------------------------------------------- the journey
def test_a_newcomer_plans_a_round_trip_abroad_from_home_and_back():
    advisor = GuideAdvisor(VisaAdvice("e_visa", "Apply online.", 30), notes=NOTES)
    c = Chat(advisor=advisor)
    c.send("hi"); c.send(reply_id="svc:guide")
    ask = c.send(reply_id="guide:new")
    assert "which passport" in ask["body"] and c.ids() == ["gpass:home", "gpass:other"] and c.last[0]["buttons"][0][1] == "🛂 India"
    assert "know where" in c.send(reply_id="gpass:home")["body"] and c.ids() == ["gkn:yes", "gkn:no", "nav:menu"]
    assert "Type the place" in c.send(reply_id="gkn:yes")["body"]
    origin = c.send("dubai")
    assert "Which city will you start from" in origin["body"] and "*Dubai*" in origin["body"] and c.ids() == ["gorg:IDR", "gorg:BOM", "gorg:more"]
    when = c.send(reply_id="gorg:IDR")
    assert "When do you want to go to Dubai" in when["body"] and c.ids()[-1] == "gwhen:more"
    c.send(reply_id=third_month(c))
    assert c.send(reply_id="gdays:7")["type"] == "buttons" and c.ids() == ["gtrip:round", "gtrip:one"]
    road = c.send(reply_id="gtrip:round")
    assert road["type"] == "list" and "*Dubai, United Arab Emirates*" in road["body"] and "7 days" in road["body"]
    assert "Indore to Dubai" in road["body"] and ", back " in road["body"] and "India passport" in road["body"] and "e-visa needed" in road["body"]
    assert "0 of 9 steps done" in road["body"] and "👉 *Next:* Check your passport" in road["body"]
    ids = c.ids()
    assert ids[0] == "gstep:passport" and ids[-1] == "guide:notes" and set(ids) >= {"gstep:flights", "gstep:rides", "gstep:return"}
    assert len(road["rows"]) == 10 and all(len(r[1]) <= 24 for r in road["rows"])


def test_one_way_skips_the_return_day():
    c = Chat(advisor=GuideAdvisor(VisaAdvice("e_visa", "x", 30)))
    plan_trip(c, trip="gtrip:one")
    assert "0 of 8 steps done" in c.last[0]["body"] and "one way" in c.last[0]["body"] and "gstep:return" not in c.ids()


def test_the_flight_step_opens_there_and_back_with_the_trip_filled_in():
    c = Chat(advisor=GuideAdvisor(VisaAdvice("e_visa", "x", 30)))
    plan_trip(c)
    detail = c.send(reply_id="gstep:flights")
    assert "Book flights, there and back" in detail["body"] and c.ids() == ["gact:flights:0", "gact:flights:1", "gdone:flights", "guide:road"]
    assert detail["rows"][0][2].startswith("Indore to Dubai") and detail["rows"][1][2].startswith("Dubai to Indore")
    c.send(reply_id="gact:flights:0")
    assert "Flying from Indore to Dubai" in c.last[0]["body"]                # the flight search starts with the route known
    c.send(reply_id="nav:menu"); c.send(reply_id="svc:guide"); c.send(reply_id="guide:road"); c.send(reply_id="gstep:flights")
    c.send(reply_id="gact:flights:1")
    assert "Flying from Dubai to Indore" in c.last[0]["body"]               # and the way back goes the other direction


def test_the_rides_step_covers_home_to_airport_and_back_home_where_we_book_cabs():
    c = Chat(advisor=GuideAdvisor(VisaAdvice("e_visa", "x", 30)))
    plan_trip(c, origin="gorg:BOM")
    detail = c.send(reply_id="gstep:rides")
    assert "home to the airport" in detail["body"] and "airport to home" in detail["body"] and "can't book cabs in Dubai" in detail["body"]
    assert c.ids() == ["gact:rides:0", "gact:rides:1", "gdone:rides", "guide:road"]
    assert [r[1] for r in detail["rows"][:2]] == ["🚕 Home to airport", "🚕 Airport to home"]
    c.send(reply_id="gact:rides:1")
    assert c.last[0]["type"] in ("list", "buttons", "text")                  # the cab agent takes over, in Mumbai


def test_a_step_explains_itself_and_its_button_starts_the_visa_filled_in():
    c = Chat(advisor=GuideAdvisor(VisaAdvice("e_visa", "Apply online.", 30)))
    plan_trip(c)
    detail = c.send(reply_id="gstep:visa")
    assert "Apply for your e-Visa" in detail["body"] and "Single entry" in detail["body"]  # our rules table wins for Indian passports
    assert c.ids() == ["gact:visa:0", "gdone:visa", "guide:road"]
    assert "UAE" in c.send(reply_id="gact:visa:0")["body"]                  # the visa desk already knows the country


def test_the_return_day_has_a_checklist_and_a_ride_home():
    c = Chat(advisor=GuideAdvisor(VisaAdvice("e_visa", "x", 30)))
    plan_trip(c, origin="gorg:IDR")
    detail = c.send(reply_id="gstep:return")
    assert "Check out on time" in detail["body"] and c.ids() == ["gact:return:0", "gdone:return", "guide:road"]
    assert c.last[0]["buttons"][0][1] == "🚕 Airport to home"


def test_marking_a_step_done_updates_the_roadmap():
    c = Chat(advisor=GuideAdvisor(VisaAdvice("e_visa", "x", 30)))
    plan_trip(c)
    c.send(reply_id="gstep:passport")
    out = c.send(reply_id="gdone:passport")
    assert "1 of 9 steps done" in out["body"] and "✅ Check your passport"[:24] in [r[1] for r in out["rows"]]


def test_bookings_with_us_tick_steps_off_by_themselves():
    c = Chat(advisor=GuideAdvisor(VisaAdvice("e_visa", "x", 30)))
    plan_trip(c)
    user = c.repo.get_or_create_user("+919876543210", "Aarav Sharma")
    c.forex_repo.create_order(user["id"], "AED", 1000, "card", 20, 20.2, 99, 20299, "confirmed")
    c.send(reply_id="nav:menu"); c.send(reply_id="svc:guide")
    out = c.send(reply_id="guide:road")
    forex = next(r for r in out["rows"] if r[0] == "gstep:forex")
    assert forex[1].startswith("✅") and "Order placed" in forex[2] and "1 of 9" in out["body"]


def test_know_before_you_go_notes():
    c = Chat(advisor=GuideAdvisor(VisaAdvice("not_required", "x", 30), notes=NOTES))
    plan_trip(c)
    out = c.send(reply_id="guide:notes")
    assert "Dubai: know before you go" in out["body"] and "Hot and dry" in out["body"] and "Pack: sunscreen" in out["body"] and "Burj Khalifa" in out["body"]
    assert c.ids() == ["svc:planner", "guide:road", "guide:new"]


def test_a_domestic_round_trip_is_short_and_has_no_visa_or_passport():
    c = Chat()
    plan_trip(c, place="mumbai", origin="gorg:IDR")
    road = c.last[0]
    assert "0 of 6 steps done" in road["body"] and "passport" not in road["body"].lower() and "visa" not in road["body"].lower()
    assert [i for i in c.ids() if i.startswith("gstep:")] == ["gstep:flights", "gstep:hotel", "gstep:plan", "gstep:rides", "gstep:pack", "gstep:return"]
    c.send(reply_id="gstep:rides")
    assert c.ids()[:4] == ["gact:rides:0", "gact:rides:1", "gact:rides:2", "gact:rides:3"]  # both cities have cabs: all four rides


def test_an_unknown_place_still_gets_a_roadmap_and_is_honest_about_booking():
    c = Chat()
    plan_trip(c, place="narnia")
    assert "check the visa rules" in c.last[0]["body"]
    assert "can't book that for Narnia" in c.send(reply_id="gstep:flights")["body"]


def test_another_passport_can_be_typed():
    c = Chat(advisor=GuideAdvisor(VisaAdvice("required", "Embassy visa.", None)))
    c.send("hi"); c.send(reply_id="svc:guide"); c.send(reply_id="guide:new")
    assert "Type your passport country" in c.send(reply_id="gpass:other")["body"]
    assert "😕" in c.send("123")["body"]
    assert "know where" in c.send("Nepal")["body"]
    c.send(reply_id="gkn:yes"); c.send("dubai"); c.send(reply_id="gorg:IDR"); c.send(reply_id=third_month(c))
    c.send(reply_id="gdays:5"); c.send(reply_id="gtrip:round")
    assert "Nepal passport" in c.last[0]["body"] and "visa needed" in c.last[0]["body"]
    visa = c.send(reply_id="gstep:visa")
    assert "gact:visa:0" not in c.ids() and "Embassy visa" in visa["body"]  # our visa desk only handles Indian passports


def test_a_typed_starting_city_is_understood_and_an_unknown_one_is_refused():
    c = Chat()
    c.send("hi"); c.send(reply_id="svc:guide"); c.send(reply_id="guide:new"); c.send(reply_id="gpass:home"); c.send(reply_id="gkn:yes"); c.send("goa")
    assert "Type the city" in c.send(reply_id="gorg:more")["body"]
    assert "can't find" in c.send("atlantis")["body"]
    assert "When do you want to go to Goa" in c.send("indore")["body"]


def test_not_knowing_where_to_go_leads_to_ideas_then_a_plan():
    ideas = [{"place": "Bali", "country": "Indonesia", "why": "Beaches and temples in the dry season.", "cost_inr": 85000, "entry": "visa on arrival"},
             {"place": "Phuket", "country": "Thailand", "why": "Cheap and easy.", "cost_inr": 60000, "entry": "visa free"}]
    advisor = GuideAdvisor(VisaAdvice("on_arrival", "Pay at the airport.", 30), ideas=ideas)
    c = Chat(advisor=advisor)
    c.send("hi"); c.send(reply_id="svc:guide"); c.send(reply_id="guide:new"); c.send(reply_id="gpass:home")
    assert "Per person" in c.send(reply_id="gkn:no")["body"] and c.ids()[0] == "gbud:30000"
    c.send(reply_id="gbud:70000")
    assert c.ids()[0] == "gvibe:beach" and c.send(reply_id="gvibe:beach")["rows"][0][0] == "gdur:4"
    c.send(reply_id="gdur:6")
    months = c.ids()
    assert len(months) == 6 and months[0].startswith("gmon:")
    out = c.send(reply_id=months[2])
    assert advisor.calls[0][1] == {"citizen": "India", "budget_inr": 70000, "vibe": "beach", "days": 6, "month": advisor.calls[0][1]["month"]}
    assert "Bali, Indonesia" in out["body"] and "₹85,000" in out["body"] and "visa on arrival" in out["body"] and c.ids() == ["gdest:0", "gdest:1"]
    assert "start from" in c.send(reply_id="gdest:0")["body"]               # the destination is chosen; now where they begin
    c.send(reply_id="gorg:IDR")
    assert c.ids() == ["gtrip:round", "gtrip:one"]                          # month and days were already answered
    road = c.send(reply_id="gtrip:round")
    assert "*Bali, Indonesia*" in road["body"] and "6 days" in road["body"] and "visa on arrival" in road["body"]


def test_without_an_ai_the_ideas_are_places_we_actually_fly_to():
    c = Chat()
    c.send("hi"); c.send(reply_id="svc:guide"); c.send(reply_id="guide:new"); c.send(reply_id="gpass:home"); c.send(reply_id="gkn:no")
    c.send(reply_id="gbud:30000"); c.send(reply_id="gvibe:any"); c.send(reply_id="gdur:4")
    out = c.send(reply_id=c.ids()[1])
    assert "Mumbai" in out["body"] and c.ids() == ["gdest:0"]


def test_the_roadmap_survives_the_main_menu_and_reopens():
    c = Chat(advisor=GuideAdvisor(VisaAdvice("e_visa", "x", 30)))
    plan_trip(c)
    c.send(reply_id="nav:menu"); c.send(reply_id="svc:guide")
    assert c.ids() == ["guide:new", "guide:road", "svc:planner"] and c.last[0]["buttons"][1][1] == "📋 Dubai plan"
    assert "*Dubai" in c.send(reply_id="guide:road")["body"]


def test_typing_a_bad_date_or_a_past_date_is_handled():
    c = Chat()
    c.send("hi"); c.send(reply_id="svc:guide"); c.send(reply_id="guide:new"); c.send(reply_id="gpass:home"); c.send(reply_id="gkn:yes"); c.send("goa")
    c.send(reply_id="gorg:IDR")
    assert "Type the date" in c.send(reply_id="gwhen:more")["body"]
    assert "couldn't read" in c.send("someday")["body"]
    assert "between tomorrow" in c.send((TODAY - timedelta(days=3)).strftime("%d/%m/%Y"))["body"]
