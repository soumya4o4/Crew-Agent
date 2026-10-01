from fakes import Chat

VIJAY_NAGAR = (22.7533, 75.8937)


def titles(out):
    return [r[1] for r in out["rows"][:-1]]


def test_without_a_location_or_trip_it_asks_then_lists_by_time():
    c = Chat()
    c.send("hi")
    c.send(reply_id="svc:events")
    assert c.last[0]["type"] == "location_request" and "Or type a city" in c.last[1]["body"]
    assert "I list events in" in c.send("atlantis")["body"]
    out = c.send("indore")
    assert titles(out) == ["Stand-up Saturdays", "Indie Music Night", "Street Food Carnival"]
    assert "soonest first" in out["body"] and out["rows"][0][2].endswith("Free") and "₹499" in out["rows"][1][2]
    assert out["rows"][1][2].startswith("🎵 ")


def test_with_a_location_the_closest_events_come_first():
    c = Chat()
    c.send_location(*VIJAY_NAGAR)
    c.send("hi")
    out = c.send(reply_id="svc:events")
    assert titles(out) == ["Stand-up Saturdays", "Street Food Carnival", "Indie Music Night"]
    assert "closest first" in out["body"] and out["rows"][0][2].split(" · ")[1] == "0 m"


def test_sharing_a_pin_when_asked_shows_events_near_it():
    c = Chat()
    c.send("any concerts this weekend?")
    assert c.last[0]["type"] == "location_request"
    out = c.send_location(*VIJAY_NAGAR)
    assert out["type"] == "list" and "Events in Indore" in out["body"]


def test_event_details_show_price_distance_and_directions():
    c = Chat()
    c.send_location(*VIJAY_NAGAR)
    c.send("hi")
    c.send(reply_id="svc:events")
    free = c.send(reply_id=c.ids()[0])
    assert free["type"] == "cta" and "Free entry" in free["body"] and "0 m from you" in free["body"]
    assert free["url"] == "https://www.google.com/maps/dir/?api=1&destination=22.7533,75.8937&travelmode=driving"
    assert [i for i, _ in c.last[1]["buttons"]] == ["events:list", "nav:menu"]
    c.send(reply_id="events:list")
    assert "(pay at the venue)" in c.send(reply_id=c.ids()[2])["body"]  # the paid one


def test_after_a_flight_it_shows_events_in_the_city_you_land_in():
    c = Chat()
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")
    out = c.send("any events?")
    assert "Events in Mumbai" in out["body"] and titles(out) == ["Sunset Acoustic Session"]


def test_a_named_city_in_free_text_and_an_empty_city():
    c = Chat()
    assert "Events in Indore" in c.send("events in indore")["body"]
    out = c.send("events in dubai")  # the only Dubai event is more than 30 days away
    assert "No events listed in Dubai" in out["body"] and "events:city" in [i for i, _ in out["buttons"]]
    c.send(reply_id="events:city")
    assert c.last[0]["type"] == "location_request"


def test_a_location_outside_the_cities_we_cover_asks_for_a_city():
    c = Chat()
    c.send_location(27.0, 70.0)  # middle of the Thar desert
    c.send("hi")
    c.send(reply_id="svc:events")
    assert "don't list events around where you are" in c.last[0]["body"]
