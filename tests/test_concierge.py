import asyncio
from datetime import date
from types import SimpleNamespace

from fakes import Chat, FakeRepo
from app.agents.concierge.classifiers import KeywordClassifier, LLMClassifier, OpenAIClassifier
from app.agents.concierge.router import IntentRouter
from app.agents.concierge.slots import extract_slots

TODAY = date(2026, 10, 1)  # a Thursday


def test_main_menu_lists_every_service():
    c = Chat()
    assert c.send("hi")["type"] == "list"
    assert c.ids() == ["svc:flight", "svc:hotel", "svc:cab", "svc:nearby", "svc:planner", "svc:events",
                       "svc:visa", "svc:forex", "menu:bookings"]
    c.send(reply_id="svc:flight")
    assert "menu:book" in c.ids()


def test_free_text_flight_request_skips_known_steps():
    c = Chat()
    c.send("flight from indore to mumbai tomorrow")
    assert "Got it" in c.last[0]["body"] and c.ids() == ["sort:cheap", "sort:fast", "sort:time"]


def test_destination_only_asks_for_origin_then_continues():
    c = Chat()
    c.send("book a flight to mumbai")
    assert c.last[-1]["type"] == "list" and "from:IDR" in c.ids()
    c.send(reply_id="from:IDR")
    assert c.ids()[0].startswith("date:")  # destination was remembered


def test_hotel_request_routes_to_hotel_agent_and_skips_the_known_city():
    c = Chat()
    assert "Goa" in c.send("I need a hotel in goa")["body"]
    assert c.ids()[0].startswith("hin:")  # the city is known, so the next question is the check-in date


def test_planner_agent_is_coming_soon_and_events_agent_asks_where():
    c = Chat()
    assert "Trip Planner" in c.send("plan a trip for me")["body"]
    c.send(reply_id="planner:notify")
    assert next(iter(c.repo.users.values()))["preferences"]["interests"] == ["planner"]
    c.send("any concerts this weekend?")
    assert c.last[0]["type"] == "location_request"  # no location and no trip yet: events asks where


def test_trip_context_is_shared_between_agents():
    c = Chat()
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")
    assert "Mumbai" in c.send("need a cab")["body"]  # cab agent knows where the flight lands
    assert "Mumbai" in c.send("any events?")["body"]


def test_user_can_switch_topic_mid_question():
    c = Chat()
    c.enter_flights()
    c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")  # now asking for a date
    assert "Hotels" in c.send("actually I need a hotel")["body"]


def test_multi_part_request_does_flight_now_and_queues_the_rest():
    c = Chat()
    c.send("flight to mumbai and a hotel there")
    assert "start with flights" in c.last[0]["body"] and "hotels" in c.last[0]["body"]
    assert c.concierge.agents["flight"] and "from:IDR" in c.ids()  # flight flow is already running
    c.send(reply_id="from:IDR"); c.send(reply_id=c.ids()[1]); c.send(reply_id="sort:time")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))
    c.book(); c.send(reply_id="name:self"); c.send(reply_id="cfm:yes")
    assert "You also mentioned a hotel" in c.last[1]["body"] and "svc:hotel" in c.ids()
    c.send(reply_id="svc:hotel")  # tapping it moves on to the queued service, which knows where the flight lands
    assert "hotel:trip" in c.ids()


def test_vague_request_offers_relevant_options_and_keeps_details():
    c = Chat()
    out = c.send("I want to go to goa")
    assert out["type"] == "buttons" and "Goa" in out["body"]
    assert c.ids() == ["svc:flight", "svc:hotel", "svc:planner"]
    c.send(reply_id="svc:flight")  # "Goa" is remembered: only the origin is missing
    assert "from:IDR" in c.ids()
    c.send(reply_id="from:IDR")
    assert c.ids()[0].startswith("date:")


def test_llm_options_and_question_are_offered():
    llm = _fake_llm({"intent": "unknown", "to_city": "Dubai", "options": ["flight", "visa", "forex"],
                     "question": "Dubai trip! Where shall we start?"})
    c = Chat(router=IntentRouter(llm))
    assert "Where shall we start" in c.send("dubai plan hai")["body"]
    assert c.ids() == ["svc:flight", "svc:visa", "svc:forex"]


def test_small_talk_and_help_and_unknown():
    c = Chat()
    assert "Anytime" in c.send("thanks")["body"]
    assert "What I can do" in c.send("help")["body"]
    assert "I can help with flights" in c.send("asdfgh")["body"]


def test_conversation_history_is_saved():
    repo = FakeRepo()
    Chat(repo).send("hi")
    roles = [m[1] for m in repo.messages]
    assert roles[0] == "user" and "assistant" in roles


def test_slot_extraction():
    assert extract_slots("indore to goa tomorrow", TODAY) == {"from": "IDR", "to": "GOI", "date": "2026-10-02"}
    assert extract_slots("to delhi from mumbai on 15 oct", TODAY) == {"from": "BOM", "to": "DEL", "date": "2026-10-15"}
    assert extract_slots("flights to bangalore", TODAY) == {"to": "BLR"}
    assert extract_slots("goa this saturday", TODAY)["date"] == "2026-10-03"
    assert extract_slots("5th nov", TODAY)["date"] == "2026-11-05"


def test_keyword_classifier_intents():
    k = KeywordClassifier()
    assert k.classify("any cheap flights?", TODAY).name == "flight"
    assert k.classify("do I need a visa for dubai", TODAY).name == "visa"
    assert k.classify("convert to aed", TODAY).name == "forex"
    assert k.classify("show my bookings", TODAY).name == "bookings"
    assert k.classify("delhi to goa tomorrow", TODAY).name == "flight"
    assert k.classify("plan my trip to goa", TODAY).name == "planner"
    assert k.classify("concert tickets in mumbai", TODAY).name == "events"
    multi = k.classify("flight and hotel to goa", TODAY)
    assert (multi.name, multi.also) == ("flight", ["hotel"])


def _fake_llm(payload=None, error=None):
    async def create(**kwargs):
        if error:
            raise error
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=payload)])
    return LLMClassifier(SimpleNamespace(messages=SimpleNamespace(create=create)), "test-model")


def test_llm_classifier_maps_cities_and_dates():
    llm = _fake_llm({"intent": "flight", "from_city": "Bombay", "to_city": "Goa", "date": "2026-10-04"})
    intent = asyncio.run(llm.classify("x", TODAY, [], None))
    assert (intent.name, intent.slots) == ("flight", {"from": "BOM", "to": "GOI", "date": "2026-10-04"})


def test_llm_multi_part_request():
    llm = _fake_llm({"intent": "flight", "also": ["hotel", "cab", "flight", "teleport"], "to_city": "Goa"})
    intent = asyncio.run(llm.classify("x", TODAY, [], None, {"to": "GOI", "from": "IDR", "city": "Goa", "date": "2026-10-04"}))
    assert (intent.name, intent.also) == ("flight", ["hotel", "cab"])


def test_llm_bad_output_is_sanitised():
    llm = _fake_llm({"intent": "teleport", "from_city": "Narnia", "to_city": "goa", "date": "soon"})
    intent = asyncio.run(llm.classify("x", TODAY, [], None))
    assert (intent.name, intent.slots) == ("unknown", {"to": "GOI"})


def test_router_falls_back_to_keywords_when_llm_fails():
    router = IntentRouter(_fake_llm(error=RuntimeError("api down")))
    assert asyncio.run(router.classify("need a hotel", [], None)).name == "hotel"


def test_concierge_uses_llm_smalltalk_reply():
    llm = _fake_llm({"intent": "smalltalk", "reply": "Doing great, ready to plan a trip? ✈️"})
    c = Chat(router=IntentRouter(llm))
    assert "Doing great" in c.send("how are you?")["body"]


def test_openai_classifier_reads_tool_call():
    import json

    async def create(**kwargs):
        assert kwargs["tool_choice"]["function"]["name"] == "route_message"
        args = json.dumps({"intent": "hotel", "to_city": "Goa"})
        call = SimpleNamespace(function=SimpleNamespace(arguments=args))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    intent = asyncio.run(OpenAIClassifier(client, "gpt-test").classify("x", TODAY, [], None))
    assert (intent.name, intent.slots) == ("hotel", {"to": "GOI"})


def test_missing_messages_table_does_not_break_the_bot():
    class NoHistoryRepo(FakeRepo):
        def recent_messages(self, *a, **k):
            raise RuntimeError("table messages not found")

        def log_message(self, *a, **k):
            raise RuntimeError("table messages not found")

    llm = _fake_llm({"intent": "smalltalk", "reply": "Hello! ✈️"})
    c = Chat(NoHistoryRepo(), router=IntentRouter(llm))
    c.enter_flights()  # an active agent makes the Concierge read history
    assert "Hello" in c.send("how are you?")["body"]
