import json
from types import SimpleNamespace

from fakes import Chat
from fakes_buddy import FakeBrain
from app.agents.buddy.brain import BuddyReply, parse_reply

IDR_AIRPORT_LINK = "https://www.google.com/maps/dir/?api=1&destination=22.7218,75.8011&travelmode=driving"


def booked_chat():
    """A user with a confirmed flight tomorrow 09:00 (Indore to Mumbai), chatting with Buddy."""
    brain = FakeBrain()
    c = Chat(brain=brain)
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")  # no Razorpay in this chat: confirmed at once
    c.send("mera mood thoda off hai")
    brain.calls.clear()
    return c, brain


def stuck_in_traffic(c, brain, **reply):
    brain.queue.append(BuddyReply("Tension mat lo, abhi time hai.", **reply))
    return c.send("traffic mein fas gaya, flight miss na ho jaye")


# --------------------------------------------------------------------------- facts
def test_a_question_about_the_booked_flight_stays_with_buddy_and_gets_the_trip_facts():
    c, brain = booked_chat()
    stuck_in_traffic(c, brain)  # the word "flight" must not open the booking flow
    assert len(brain.calls) == 1
    context = brain.calls[0]["context"]
    for expect in ("Flight 6E-1000 Indore to Mumbai", "PNR ZX9Q2K", "Reach the airport by", "User location: not shared"):
        assert expect in context, expect


def test_a_clear_booking_request_still_leaves_buddy():
    c, brain = booked_chat()
    c.send("book a flight from indore to goa tomorrow")
    assert brain.calls == [] and "Goa" in c.last[-1]["body"]  # the flight flow answered (no Indore-Goa route in the fake)


def test_no_trip_means_no_trip_facts():
    brain = FakeBrain()
    c = Chat(brain=brain)
    c.send("mera mood off hai")
    assert "No upcoming flight booked" in brain.calls[0]["context"]


# ------------------------------------------------------------------ the way and the time
def test_lost_or_late_gets_a_maps_button_and_a_request_for_the_location():
    c, brain = booked_chat()
    stuck_in_traffic(c, brain, action="airport_route")
    assert [m["type"] for m in c.last] == ["text", "cta", "location_request"]
    cta = c.last[1]
    assert cta["url"] == IDR_AIRPORT_LINK and "Indore Airport" in cta["body"] and cta["button_text"] == "🗺️ Open Maps"
    assert c.repo.convos["+919876543210"]["current_step"] == "buddy_wait_location"


def test_sharing_the_location_adds_the_distance_the_drive_and_when_to_leave():
    c, brain = booked_chat()
    stuck_in_traffic(c, brain, action="airport_route")
    brain.queue.append(BuddyReply("Aap 9 km door ho, nikal jao."))
    c.send_location(22.7196, 75.8577)
    context = brain.calls[-1]["context"]
    assert brain.calls[-1]["text"] == "(I just shared my location)"
    for expect in ("near Martand Chowk, Indore", "9.0 km and 18 min", "Suggested time to leave for the airport"):
        assert expect in context, expect
    assert c.geo.calls[-1] == ("route", (22.7196, 75.8577), (22.7218, 75.8011))
    assert c.last[0]["body"] == "Aap 9 km door ho, nikal jao." and c.repo.convos["+919876543210"]["current_step"] == "buddy_chat"


def test_with_a_known_location_the_maps_button_shows_the_drive_time_and_nothing_more_is_asked():
    c, brain = booked_chat()
    c.send_location(22.7196, 75.8577)
    c.send("hi")
    c.send(reply_id="svc:buddy")
    out = stuck_in_traffic(c, brain, action="airport_route")
    assert [m["type"] for m in c.last] == ["text", "cta"] and "9.0 km · about 18 min" in c.last[1]["body"]


def test_the_way_to_the_hotel_works_from_the_phones_own_gps():
    c, brain = booked_chat()
    c.buddy_repo.stay = {"ref": "HB00001", "check_in": "2026-10-02", "check_out": "2026-10-04",
                         "hotels": {"name": "Calangute Shores", "area": "Calangute", "city_code": "GOI"}}
    brain.queue.append(BuddyReply("Hotel ka raasta bhej raha hoon.", action="stay_route"))
    c.send("hotel kaise jaun yaar")
    assert "Calangute+Shores%2C+Calangute%2C+Goa" in c.last[1]["url"] and "Hotel: Calangute Shores" in brain.calls[-1]["context"]


def test_buddy_can_ask_for_the_location_itself():
    c, brain = booked_chat()
    brain.queue.append(BuddyReply("Ruko, location bhej do.", action="ask_location"))
    c.send("raasta bhatak gaya yaar")
    assert c.last[1]["type"] == "location_request"
    c.send_location()
    assert brain.calls[-1]["text"] == "(I just shared my location)"


# --------------------------------------------------------------------- things to do nearby
def test_buddy_hands_over_to_around_me_and_the_results_follow_the_reply():
    c, brain = booked_chat()
    c.send_location(22.7196, 75.8577)
    c.send("hi"); c.send(reply_id="svc:buddy")
    brain.queue.append(BuddyReply("Chal chai peete hain.", action="nearby", query="coffee"))
    c.send("kuch karne ko nahi hai, chai milegi?")
    assert [m["type"] for m in c.last] == ["text", "list"] and c.ids()[0] == "nplc:0"
    assert c.geo.calls[-1][3] == "cafe"


def test_hand_over_without_a_location_asks_for_one():
    c, brain = booked_chat()
    brain.queue.append(BuddyReply("Dekhte hain.", action="nearby", query="biryani"))
    c.send("bhookh lagi hai")
    assert [m["type"] for m in c.last] == ["text", "location_request", "text"]
    c.send_location()
    assert c.last[0]["type"] == "list" and c.geo.calls[-1][3:5] == (None, "biryani")


def test_buddy_can_show_events_too():
    c, brain = booked_chat()
    c.send_location(22.7533, 75.8937)
    c.send("hi"); c.send(reply_id="svc:buddy")
    brain.queue.append(BuddyReply("Kuch fun dhoondte hain.", action="events"))
    c.send("bore ho raha hoon")
    assert [m["type"] for m in c.last] == ["text", "list"] and "Events in Indore" in c.last[1]["body"]


# ------------------------------------------------------------------------ privacy
def test_forget_me_also_forgets_the_location():
    c, brain = booked_chat()
    c.send_location()
    assert "loc" in c.repo.convos["+919876543210"]["context"]
    c.send("forget me")
    c.send(reply_id="buddy:forgetyes")
    assert "loc" not in c.repo.convos["+919876543210"]["context"]


def test_a_pin_shared_out_of_the_blue_offers_around_me_and_buddy():
    c, _ = booked_chat()
    out = c.send_location()
    assert [i for i, _ in out["buttons"]] == ["svc:nearby", "svc:buddy", "nav:menu"]


# ------------------------------------------------------------------- what the model may ask
def test_model_actions_are_validated():
    ok = parse_reply(json.dumps({"reply": "x", "action": {"type": "nearby", "query": ' coffee"<b> '}}))
    assert ok.action == "nearby" and ok.query.startswith("coffee") and "<" not in ok.query and ">" not in ok.query
    assert parse_reply(json.dumps({"reply": "x", "action": {"type": "nearby"}})).action == "none"        # a search needs something to find
    assert parse_reply(json.dumps({"reply": "x", "action": {"type": "delete_everything"}})).action == "none"
    assert parse_reply(json.dumps({"reply": "x", "action": "airport_route"})).action == "none"           # must be an object
    assert parse_reply(json.dumps({"reply": "x"})).action == "none"


# ------------------------------------------------------------------------- spending
def test_paid_transactions_reach_buddy_as_facts_and_unpaid_ones_do_not():
    from datetime import timedelta
    from app.core.utils import now_ist
    brain = FakeBrain()
    c = Chat(brain=brain)
    c.send("mera mood thoda off hai")
    now = now_ist()
    c.buddy_repo.paid = [{"amount_inr": 5200, "kind": "hotel", "paid_at": (now - timedelta(days=1)).isoformat()},
                         {"amount_inr": 4100, "kind": "flight", "paid_at": (now - timedelta(days=9)).isoformat()}]
    brain.calls.clear()
    c.send("meri last transaction kitni thi?")
    context = brain.calls[0]["context"]
    assert "Last transaction: ₹5,200 for hotel" in context and "(flight)" in context and "total ₹9,300" in context


def test_no_payments_says_so():
    brain = FakeBrain()
    c = Chat(brain=brain)
    c.send("kitna spend hua?")
    assert "no paid transactions yet" in brain.calls[0]["context"]


# ------------------------------------------------------------------------ where am I
def test_where_am_i_without_a_pin_asks_for_one_and_then_names_the_place():
    c, brain = booked_chat()
    assert c.send("where am i")["type"] == "location_request" and brain.calls == []
    out = c.send_location()
    assert out["type"] == "cta" and "Martand Chowk, Indore" in out["body"] and "abhi" in out["body"]
    assert "google.com/maps?q=22.7196,75.8577" in out["url"] and brain.calls == []  # answered by the app, not the model


def test_where_am_i_in_hinglish_uses_a_pin_shared_earlier():
    c, brain = booked_chat()
    c.send_location()
    for words in ("meri location kya hai", "main kahan hu", "mai kaha hoon"):
        out = c.send(words)
        assert out["type"] == "cta" and "Martand Chowk, Indore" in out["body"], words
    assert brain.calls == []


def test_an_old_pin_is_reported_as_old_and_a_fresh_one_is_offered():
    from datetime import timedelta
    from app.core.utils import now_ist
    c, brain = booked_chat()
    c.send_location()
    for key in ("loc",):  # the pin was shared 40 minutes ago, still inside the 3 hour window
        convo = c.repo.convos[next(iter(c.repo.convos))]
        convo["context"][key]["at"] = (now_ist() - timedelta(minutes=40)).isoformat()
    c.send("where am i")
    assert "40 min pehle" in c.last[0]["body"] and c.last[1]["type"] == "location_request"
    out = c.send_location(19.07, 72.87)  # they moved: the new pin replaces the old one
    assert out["type"] == "cta" and "abhi" in out["body"] and "q=19.07,72.87" in out["url"]


def test_where_am_i_works_in_the_middle_of_another_flow():
    c, brain = booked_chat()
    c.send(reply_id="svc:hotel"); c.send(reply_id="hotel:find")
    assert c.send("where am i")["type"] == "location_request"
