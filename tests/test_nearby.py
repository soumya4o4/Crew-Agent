from datetime import date, timedelta

from fakes import Chat, WA
from app.agents.concierge.classifiers import KeywordClassifier
from app.core.utils import now_ist

TODAY = date(2026, 10, 1)
CAFES = [{"name": "Joshi Coffee House", "lat": 22.7224, "lon": 75.8514, "dist_m": 350, "info": "coffee shop", "hours": "08:00-22:00"},
         {"name": "Cafe Kava", "lat": 22.7035, "lon": 75.8428, "dist_m": 1200, "info": "cafe", "hours": ""}]


def open_menu(c):
    c.send("hi")
    return c.send(reply_id="svc:nearby")


def test_menu_lists_every_category_search_and_events():
    c = Chat()
    out = open_menu(c)
    assert out["type"] == "list" and len(out["rows"]) == 10
    assert c.ids() == [f"near:{k}" for k in ("cafe", "food", "atm", "pharmacy", "fuel", "sights", "park", "mall")] + ["near:search", "svc:events"]
    assert "ask for your location" in out["body"]


def test_a_category_without_a_location_asks_for_a_pin_then_shows_the_closest():
    c = Chat()
    open_menu(c)
    c.send(reply_id="near:cafe")
    assert [m["type"] for m in c.last] == ["location_request", "text"] and "Cafés".lower() in c.last[0]["body"].lower()
    out = c.send_location()
    assert out["type"] == "list" and c.ids() == ["nplc:0", "nplc:1", "nav:menu"]
    assert out["rows"][0][1:] == ("Joshi Coffee House", "350 m · coffee shop") and "☕" in out["body"]
    assert c.geo.calls[-1][:4] == ("places", 22.7196, 75.8577, "cafe") and c.geo.calls[-1][5] == 2500


def test_a_fresh_location_is_reused_without_asking_again():
    c = Chat()
    out = c.send_location()  # nobody asked: the pin is kept and the user is offered what to do with it
    assert [i for i, _ in out["buttons"]] == ["svc:nearby", "nav:menu"]
    assert "shared you" not in open_menu(c)["body"] and "shared 0 min ago" in c.last[0]["body"]
    assert c.send(reply_id="near:food")["type"] == "list"  # straight to the results


def test_a_stale_location_is_forgotten():
    c = Chat()
    c.send_location()
    c.repo.convos["+" + WA]["context"]["loc"]["at"] = (now_ist() - timedelta(hours=4)).isoformat()
    assert "ask for your location" in open_menu(c)["body"]
    assert "loc" not in c.repo.convos["+" + WA]["context"]


def test_place_details_have_a_directions_button_and_a_way_back():
    c = Chat()
    c.send_location()
    c.send("hi"); c.send(reply_id="svc:nearby"); c.send(reply_id="near:cafe")
    out = c.send(reply_id="nplc:0")
    assert out["type"] == "cta" and out["url"] == "https://www.google.com/maps/dir/?api=1&destination=22.7224,75.8514&travelmode=driving"
    assert "Joshi Coffee House" in out["body"] and "350 m" in out["body"] and "08:00-22:00" in out["body"]
    assert [i for i, _ in c.last[1]["buttons"]] == ["near:again", "nav:menu"]
    assert c.send(reply_id="near:again")["type"] == "list"


def test_typing_an_area_instead_of_sharing_a_location():
    c = Chat()
    open_menu(c)
    c.send(reply_id="near:cafe")
    assert "couldn't find that place" in c.send("atlantis")["body"]
    out = c.send("vijay nagar indore")
    assert out["type"] == "list" and "near Vijay Nagar" in out["body"] and c.geo.calls[-1][1:3] == (22.7533, 75.8937)


def test_search_anything_by_name():
    c = Chat()
    c.send_location()
    open_menu(c)
    c.send(reply_id="near:search")
    c.send("biryani")
    assert c.geo.calls[-1][3:5] == (None, "biryani") and "“biryani”" in c.last[0]["body"]
    c.send(reply_id="near:search")
    c.send("good chai place")  # a word we know: handled as a café search
    assert c.geo.calls[-1][3] == "cafe"


def test_it_looks_further_when_nothing_is_close_and_says_when_nothing_is_found():
    c = Chat()
    c.send_location()
    c.geo.places_by_radius = {2500: [], 6000: CAFES}
    open_menu(c)
    assert "looked a bit further" in c.send(reply_id="near:cafe")["body"]
    c.geo.places_by_radius = {2500: [], 6000: []}
    out = c.send(reply_id="near:park")
    assert "couldn't find" in out["body"] and "near:again" in [i for i, _ in out["buttons"]]
    c.geo.places_by_radius = {2500: CAFES, 6000: CAFES}
    assert c.send(reply_id="near:again")["type"] == "list"  # retry after the service was slow


def test_free_text_goes_straight_to_the_search():
    c = Chat()
    c.send("coffee near me")
    assert c.last[0]["type"] == "location_request"
    assert c.send_location()["type"] == "list" and c.geo.calls[-1][3] == "cafe"
    c.send("hi")
    c.send("nearest atm")
    assert c.last[0]["type"] == "list" and c.geo.calls[-1][3] == "atm"  # the pin from a minute ago is still fresh


def test_near_me_next_to_a_service_stays_with_that_service():
    k = KeywordClassifier()
    assert (k.classify("hotel near me", TODAY).name, k.classify("hotel near me", TODAY).also) == ("hotel", [])
    assert k.classify("coffee near me", TODAY).name == "nearby" and k.classify("coffee near me", TODAY).slots["place"] == "coffee"
    assert k.classify("events near me", TODAY).name == "events"
