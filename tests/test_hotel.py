import asyncio
from datetime import timedelta

from fakes import CONTACT, DETAILS, Chat, FakeGateway, WA
from app.agents.hotel.formatting import cancel_policy
from app.core.utils import now_ist


class Off(FakeGateway):
    enabled = False  # no Razorpay keys: stays confirm instantly


def day(offset):
    return now_ist().date() + timedelta(days=offset)


def hotel_id(c, name):
    return next(h["id"] for h in c.hotel_repo.hotels.values() if h["name"] == name)


def search(c, city="GOI", check_in=5, nights=2, guests=2):
    """Menu -> Find a Hotel -> city, check-in `check_in` days from now, nights, guests. Returns the results list."""
    c.send("hi"); c.send(reply_id="svc:hotel"); c.send(reply_id="hotel:find"); c.send(reply_id=f"hcity:{city}")
    c.send(reply_id=f"hin:{day(check_in).isoformat()}"); c.send(reply_id=f"hnt:{nights}")
    return c.send(reply_id=f"hgst:{guests}")


def pick(c, hotel="Calangute Shores", room="Deluxe"):
    """Open a hotel, choose a room, book for self. Returns the 'last check' summary."""
    c.send(reply_id=f"htl:{hotel_id(c, hotel)}")
    c.send(reply_id=f"hroom:{c.hotel_repo.room_of(hotel, room)['id']}")
    return c.send(reply_id="hname:self")


def book(c, **kw):
    search(c, **kw)
    pick(c)
    out = c.send(reply_id="hcfm:yes")
    if "email" in out["body"] and "full name" in out["body"]:  # paid online: the first payment asks who is paying
        out = c.send(CONTACT)
    return out


def only_booking(c):
    return next(iter(c.hotel_repo.bookings.values()))


# ------------------------------------------------------------------------------ search
def test_results_are_listed_cheapest_first_with_tags():
    out = search(Chat(gateway=Off()))
    assert [r[1] for r in out["rows"][:2]] == ["Baga Backpackers", "Calangute Shores"]
    assert "from ₹1,500/night" in out["rows"][0][2] and "Cheapest" in out["rows"][0][2]
    assert "2 nights" in out["body"] and "Found *2* stays" in out["body"]


def test_only_rooms_that_fit_the_party_and_are_free_are_offered():
    c = Chat(gateway=Off())
    out = search(c, guests=4)  # only Calangute's Suite sleeps four
    assert [r[1] for r in out["rows"][:-1]] == ["Calangute Shores"]
    c.send(reply_id=out["rows"][0][0])
    rooms = c.last[-1]  # a photo of the hotel comes first, then the room list
    assert [r[0] for r in rooms["rows"][:-2]] == [f"hroom:{c.hotel_repo.room_of('Calangute Shores', 'Suite')['id']}"]
    assert "Last room" in rooms["rows"][0][2]


def test_no_rooms_for_the_party_says_so():
    out = search(Chat(gateway=Off()), city="BOM", guests=4)
    assert "No rooms free" in out["body"]


def test_free_text_fills_the_city_and_date():
    c = Chat()
    assert "Got it" in c.send("hotel in goa tomorrow")["body"]
    assert "How many nights" in c.last[-1]["body"]  # city and check-in came from the sentence: nights is next


def test_one_sentence_fills_everything_and_goes_straight_to_the_stays():
    c = Chat()
    out = c.send(f"hotel 2 nights in goa from {day(5):%d/%m} for 2 people")
    assert "Got it" in c.last[0]["body"] and "Found *2* stays" in c.last[-1]["body"] and c.last[-1]["type"] == "list"


def test_each_question_takes_several_answers_at_once_and_a_bare_number():
    c = Chat()
    c.send("hotel in goa")
    assert "check in" in c.last[-1]["body"]
    c.send("tomorrow for 3 nights")
    assert "How many guests" in c.last[-1]["body"]
    assert "Found" in c.send("2")["body"]


def test_changing_the_search_by_typing_while_looking_at_the_results():
    c = Chat()
    search(c)
    out = c.send("make it 3 nights")
    assert "3 nights" in out["body"] and out["type"] == "list"


def test_stay_after_a_flight_uses_the_trip():
    c = Chat(gateway=Off())
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")
    out = c.send(reply_id="svc:hotel")
    assert "hotel:trip" in [i for i, _ in out["buttons"]] and "Mumbai" in out["body"]
    c.send(reply_id="hotel:trip")
    assert "How many nights" in c.last[-1]["body"]  # Mumbai and the landing day are already known


def test_typed_date_nights_and_city():
    c = Chat(gateway=Off())
    c.send("hi"); c.send(reply_id="svc:hotel"); c.send(reply_id="hotel:find")
    assert "don't have stays" in c.send("atlantis")["body"]
    assert "I can book stays in" in c.send("which cities are operational?")["body"]
    c.send("I am going to dubai")  # a sentence works, not just a bare city name
    assert "check in" in c.last[-1]["body"]
    assert "out of range" in c.send("1/1/2031")["body"]
    c.send("tomorrow")
    assert "How many nights" in c.last[-1]["body"]
    assert "between 1 and 14" in c.send("30")["body"]
    c.send("3 nights")
    assert "How many guests" in c.last[-1]["body"]


def test_typed_guest_name():
    c = Chat(gateway=Off())
    search(c)
    c.send(reply_id=f"htl:{hotel_id(c, 'Calangute Shores')}")
    c.send(reply_id=f"hroom:{c.hotel_repo.room_of('Calangute Shores', 'Deluxe')['id']}")
    c.send(reply_id="hname:other")
    assert "letters only" in c.send("R2D2")["body"]
    assert "Priya Sharma" in c.send("priya sharma")["body"]


# ---------------------------------------------------------------------------- booking
def test_booking_without_razorpay_confirms_instantly():
    c = Chat(gateway=Off())
    search(c)
    summary = pick(c)
    assert "Last check" in summary["body"] and "₹4,500 × 2 = *₹9,000*" in summary["body"]
    out = c.send(reply_id="hcfm:yes")
    assert "Payment received" in out["body"] and "Internal Booking Ref" in out["body"]
    b = only_booking(c)
    assert b["status"] == "confirmed" and b["total_price_inr"] == 9000 and b["guests"] == 2
    assert [i for i, _ in c.last[1]["buttons"]] == ["hotel:stays", "svc:cab", "nav:menu"]


def test_confirm_holds_the_room_and_sends_a_payment_link():
    gw = FakeGateway()
    c = Chat(gateway=gw)
    out = book(c)
    assert [m["type"] for m in c.last] == ["cta", "buttons"]
    assert out["url"] == "https://rzp.io/i/plink_1" and out["button_text"] == "Pay ₹9,000"
    assert "Test mode" in out["body"]
    assert [i for i, _ in c.last[1]["buttons"]] == ["chk:check", "chk:cancel"]
    b = only_booking(c)
    assert b["status"] == "pending" and gw.links["plink_1"]["amount"] == 9000 and gw.links["plink_1"]["ref"] == b["ref"]
    assert gw.links["plink_1"]["phone"] == "+9876543210" and gw.links["plink_1"]["email"] == "aarav@example.com"
    rooms = c.hotel_repo.search_hotels("GOI", day(5), day(7), 2)
    assert next(r for h in rooms for r in h["rooms"] if r["room_type"] == "Deluxe")["left"] == 2  # held while unpaid


def test_ive_paid_confirms_once_razorpay_says_paid():
    gw = FakeGateway()
    c = Chat(gateway=gw)
    book(c)
    assert "haven't received the payment" in c.send(reply_id="chk:check")["body"]
    gw.paid.add("plink_1")
    assert "Payment received" in c.send(reply_id="chk:check")["body"] and only_booking(c)["status"] == "confirmed"
    assert c.send(reply_id="chk:check") is not None  # a stale tap after confirming is harmless


def test_webhook_confirms_and_is_idempotent():
    c = Chat(gateway=FakeGateway())
    book(c)
    number, messages = asyncio.run(c.concierge.confirm_payment("plink_1"))  # the Concierge finds the right agent
    assert number == WA and "Payment received" in messages[0]["body"] and only_booking(c)["status"] == "confirmed"
    assert all(m["type"] != "reaction" for m in messages)  # nothing to react to: the user didn't tap
    assert asyncio.run(c.concierge.confirm_payment("plink_1")) is None  # Razorpay retried: no second voucher
    assert "no payment waiting" in c.send(reply_id="chk:check")["body"]


def test_cancelling_before_paying_releases_the_room():
    gw = FakeGateway()
    c = Chat(gateway=gw)
    book(c)
    assert "released everything" in c.send(reply_id="chk:cancel")["body"]
    assert only_booking(c)["status"] == "cancelled" and gw.cancelled == {"plink_1"}
    assert c.hotel_repo.payments["plink_1"]["status"] == "cancelled"
    assert asyncio.run(c.concierge.agents["hotel"].confirm_payment("plink_1")) is None  # a late payment can't revive it


def test_unpaid_stays_expire_and_the_user_is_told():
    c = Chat(gateway=FakeGateway())
    book(c)
    c.hotel_repo.payments["plink_1"]["expires_at"] = now_ist() - timedelta(minutes=1)
    notices = asyncio.run(c.concierge.release_expired())
    assert len(notices) == 1 and notices[0][0] == WA and "payment window" in notices[0][1][0]["body"]
    assert only_booking(c)["status"] == "cancelled"
    assert asyncio.run(c.concierge.release_expired()) == []  # only once
    assert "window ended" in c.send(reply_id="chk:check")["body"]


def test_payment_service_down_releases_the_room():
    gw = FakeGateway()
    gw.fail = True
    c = Chat(gateway=gw)
    out = book(c)
    assert "couldn't set up the payment" in out["body"] and only_booking(c)["status"] == "cancelled"


def test_a_room_taken_meanwhile_is_caught():
    c = Chat(gateway=Off())
    out = search(c, guests=4)
    c.send(reply_id=out["rows"][0][0])
    other = c.repo.get_or_create_user("+919000000000", "Other Person")
    hotel = c.hotel_repo.hotels[hotel_id(c, "Calangute Shores")]
    c.hotel_repo.create_booking(other["id"], hotel, c.hotel_repo.room_of("Calangute Shores", "Suite"), "Other Person", 4, day(5), day(7))
    out = c.send(reply_id=f"hroom:{c.hotel_repo.room_of('Calangute Shores', 'Suite')['id']}")
    assert "just taken" in out["body"]


# ------------------------------------------------------------------------- my stays
def test_my_stays_and_cancelling_with_a_refund():
    c = Chat(gateway=Off())
    assert "no stays yet" in c.send(reply_id="hotel:stays")["body"]
    book(c)
    stays = c.send(reply_id="hotel:stays")
    assert stays["type"] == "list" and stays["rows"][0][1] == "Calangute Shores"
    bid = stays["rows"][0][0].split(":", 1)[1]
    assert "Confirmed" in c.send(reply_id=f"hbk:{bid}")["body"] and f"hbkc:{bid}" in c.ids()
    assert "refunded" in c.send(reply_id=f"hbkc:{bid}")["body"]
    assert "refund has been initiated" in c.send(reply_id=f"hbkcy:{bid}")["body"]
    assert only_booking(c)["status"] == "cancelled"
    rooms = c.hotel_repo.search_hotels("GOI", day(5), day(7), 2)
    assert next(r for h in rooms for r in h["rooms"] if r["room_type"] == "Deluxe")["left"] == 3  # the room is free again
    assert "Cancelled" in c.send(reply_id=f"hbk:{bid}")["body"] and f"hbkc:{bid}" not in c.ids()


def test_cancelling_after_the_free_window_gives_no_refund():
    c = Chat(gateway=Off())
    book(c, check_in=0)  # checking in today: the free-cancellation window is already over
    bid = only_booking(c)["id"]
    assert "no refund applies" in c.send(reply_id=f"hbkc:{bid}")["body"]
    assert "no refund applies" in c.send(reply_id=f"hbkcy:{bid}")["body"]


def test_cancellation_policy_text():
    assert "Free cancellation until" in cancel_policy(day(5))
    assert "has ended" in cancel_policy(day(0))


def test_one_broken_sweeper_does_not_hide_the_others_notices(monkeypatch):
    c = Chat(gateway=FakeGateway())
    pay_flow_chat = c  # a flight booking waiting for payment, plus a hotel agent whose database is broken
    pay_flow_chat.enter_flights()
    pay_flow_chat.send("flight from indore to mumbai tomorrow")
    pay_flow_chat.send(reply_id=next(i for i in pay_flow_chat.ids() if i.startswith("flt:")))
    pay_flow_chat.send("book"); pay_flow_chat.send(DETAILS); pay_flow_chat.send("yes"); pay_flow_chat.send(CONTACT)
    c.repo.payments["plink_1"]["expires_at"] = now_ist() - timedelta(minutes=1)

    def broken(now):
        raise RuntimeError("column payments.hotel_booking_id does not exist")

    monkeypatch.setattr(c.hotel_repo, "expire_unpaid", broken)
    notices = asyncio.run(c.concierge.release_expired())
    assert len(notices) == 1 and notices[0][0] == WA and "payment window" in notices[0][1][0]["body"]  # the flight's notice survived


# ------------------------------------------------------------------------------ photos
def test_opening_a_hotel_shows_its_photo_and_card_before_the_room_list():
    c = Chat(gateway=Off())
    search(c)
    c.send(reply_id=f"htl:{hotel_id(c, 'Calangute Shores')}")
    photo, rooms = c.last
    assert photo["type"] == "image" and photo["url"].startswith("https://") and "Calangute Shores" in photo["body"]
    assert rooms["type"] == "list" and "Calangute Shores" in rooms["body"]


def test_a_hotels_own_image_url_beats_the_stock_photo_and_the_same_hotel_always_gets_the_same_one():
    from app.agents.hotel.images import hotel_image
    h = {"name": "Calangute Shores", "stars": 4}
    assert hotel_image(h) == hotel_image(dict(h)) and hotel_image(h).startswith("https://images.unsplash.com/")
    assert hotel_image({**h, "image_url": "https://example.com/own.jpg"}) == "https://example.com/own.jpg"
    assert hotel_image({"name": "x", "stars": None}).startswith("https://")  # unknown star rating still gets a photo


def test_the_confirmed_stay_voucher_carries_the_photo():
    out = book(Chat(gateway=Off()))
    assert out["type"] == "image" and "Payment received" in out["body"] and "Internal Booking Ref" in out["body"]


def test_my_stays_detail_shows_the_photo_as_a_header():
    c = Chat(gateway=Off())
    book(c)
    c.send(reply_id="hotel:stays")
    shown = c.send(reply_id=f"hbk:{only_booking(c)['id']}")
    assert shown["type"] == "buttons" and shown["image"].startswith("https://")


# ------------------------------------------------------------------------------ near me
import pytest

GOA = (15.3808, 73.8314)    # the fake hotel repo only has Goa
JAIPUR = (26.9124, 75.7873)  # an airport city without hotels, far from Goa


@pytest.fixture(autouse=True)
def airports(monkeypatch):
    from app.core.places import AIRPORT_COORDS
    monkeypatch.setitem(AIRPORT_COORDS, "GOI", (15.3808, 73.8314))
    monkeypatch.setitem(AIRPORT_COORDS, "JAI", (26.8242, 75.8122))


def city_step(c):
    c.send("hi"); c.send(reply_id="svc:hotel")
    return c.send(reply_id="hotel:find")


def test_the_city_question_is_plain_text_that_names_the_cities_and_offers_location():
    out = city_step(Chat(gateway=Off()))
    assert out["type"] == "text" and "Goa" in out["body"] and "share your location" in out["body"]


def test_near_me_without_a_location_asks_for_it_and_then_picks_the_closest_city():
    c = Chat(gateway=Off())
    city_step(c)
    assert c.send(reply_id="hcity:near")["type"] == "location_request"
    out = c.send_location(*GOA)
    assert "near *Goa*" in out["body"]
    assert c.last[-1]["type"] == "text" and "Goa" in c.last[-1]["body"]  # on to the check-in date


def test_typing_near_me_or_current_location_does_the_same_instead_of_failing_as_an_unknown_city():
    for words in ("hotel near me", "meri current location dekh ke batao", "Find my current location"):
        c = Chat(gateway=Off())
        city_step(c)
        assert c.send(words)["type"] == "location_request", words
        c.send_location(*GOA)
        assert "Goa" in c.last[0]["body"], words


def test_near_me_uses_a_location_already_shared_without_asking_again():
    c = Chat(gateway=Off())
    c.send("hi"); c.send_location(*GOA)
    city_step(c)
    out = c.send(reply_id="hcity:near")
    assert out["type"] == "text" and "Goa" in out["body"]


def test_far_from_every_hotel_city_says_so_and_offers_the_city_list():
    c = Chat(gateway=Off())
    city_step(c)
    c.send(reply_id="hcity:near")
    out = c.send_location(*JAIPUR)
    assert "don't have stays near you" in out["body"] and "Which city" in c.last[-1]["body"]


def test_saying_hotel_near_me_from_anywhere_goes_straight_to_the_location_request():
    c = Chat(gateway=Off())
    c.send("hi")
    assert c.send("hotel near me")["type"] == "location_request"


# ------------------------------------------------------------------------------ more options
def add_goa_hotels(c, n):
    for i in range(n):
        c.hotel_repo._hotel("GOI", f"Extra Stay {i:02d}", "Candolim", 3, 4.0, [("Standard", 2000 + i * 100, 2, 3)])


def test_more_than_nine_stays_show_an_overview_of_all_then_pages():
    c = Chat(gateway=Off())
    add_goa_hotels(c, 10)  # 12 in Goa
    search(c)
    overview, page1 = c.last
    assert overview["type"] == "text" and "All 12 stays" in overview["body"] and "Extra Stay 09" in overview["body"]
    assert len(page1["rows"]) == 10 and page1["rows"][-2][0] == "hact:more" and "(1/2)" in page1["rows"][-2][1]
    assert "showing 1-8" in page1["body"]
    page2 = c.send(reply_id="hact:more")
    assert "showing 9-12" in page2["body"] and "(2/2)" in page2["rows"][-2][1]
    assert [r[1] for r in page2["rows"][:-2]] != [r[1] for r in page1["rows"][:-2]]
    c.send(reply_id="hact:more")  # wraps around to the first page (with the overview again)
    assert "showing 1-8" in c.last[-1]["body"]


def test_nine_or_fewer_stays_stay_one_list_without_a_more_row():
    c = Chat(gateway=Off())
    add_goa_hotels(c, 5)
    out = search(c)
    assert all(r[0] != "hact:more" for r in out["rows"]) and len(c.last) == 1


def test_asking_for_more_options_in_words_shows_the_next_page_instead_of_starting_over():
    c = Chat(gateway=Off())
    add_goa_hotels(c, 10)
    search(c)
    out = c.send("Give me more hotel options")
    assert out["type"] == "list" and "showing 9-12" in out["body"]


def test_change_hotel_from_a_hotels_room_list_goes_back_to_the_results():
    c = Chat(gateway=Off())
    search(c)
    c.send(reply_id=f"htl:{hotel_id(c, 'Calangute Shores')}")
    out = c.send("I want to change hotel")
    assert out["type"] == "list" and out["rows"][0][1] in ("Baga Backpackers", "Calangute Shores") and "Found *2* stays" in out["body"]
    assert c.send("bhai dusri hotel chiye")["type"] == "list"


def test_other_words_while_looking_at_rooms_just_show_the_rooms_again():
    c = Chat(gateway=Off())
    search(c)
    c.send(reply_id=f"htl:{hotel_id(c, 'Calangute Shores')}")
    c.send("hmm which is best")
    assert c.last[-1]["type"] == "list" and c.last[-1]["button"] == "Choose room"


def test_change_hotel_before_the_search_is_ready_continues_with_the_next_question_not_an_error():
    c = Chat(gateway=Off())
    c.send("hi"); c.send(reply_id="svc:hotel"); c.send(reply_id="hotel:find"); c.send(reply_id="hcity:GOI")
    c.send(reply_id=f"hin:{day(5).isoformat()}")  # now it waits for the nights
    c.send("bhai dusri hotel chiye")
    assert c.last[0]["type"] == "text" and "nights" in c.last[-1]["body"].lower()


# ------------------------------------------------------------------------------ amenity questions
def test_asking_about_an_amenity_on_a_hotels_rooms_is_answered_for_that_hotel():
    c = Chat(gateway=Off())
    search(c)
    c.send(reply_id=f"htl:{hotel_id(c, 'Calangute Shores')}")
    out = c.send("isme pool hai?")
    assert "Yes" in out["body"] and "Calangute Shores" in out["body"] and "free wifi" not in out["body"].split("offers")[0].lower()
    no = c.send("gym hai kya aur parking?")["body"]
    assert "No, *Calangute Shores* does not have gym" in no and "does not have parking" in no and "Free WiFi · Pool" in no
    assert "Free WiFi · Pool" in c.send("what are the amenities?")["body"]


def test_asking_for_an_amenity_in_the_results_lists_the_stays_that_have_it():
    c = Chat(gateway=Off())
    search(c)
    out = c.send("which hotel has a pool")["body"]
    assert "Stays with pool" in out and "Baga Backpackers" in out and "Calangute Shores" in out
    assert "None of these stays list gym" in c.send("gym wala hotel")["body"]


def test_amenity_questions_do_not_break_the_booking_flow():
    c = Chat(gateway=Off())
    search(c)
    c.send(reply_id=f"htl:{hotel_id(c, 'Calangute Shores')}")
    c.send("pool hai?")
    c.send(reply_id=f"hroom:{c.hotel_repo.room_of('Calangute Shores', 'Deluxe')['id']}")  # the room list still works
    assert c.send(reply_id="hname:self")["type"] == "buttons"


def ctx_of(c):
    return next(iter(c.repo.convos.values()))["context"]


def test_reset_forgets_everything_mid_search():
    c = Chat(gateway=Off())
    c.send("hi"); c.send(reply_id="svc:hotel"); c.send(reply_id="hotel:find"); c.send(reply_id="hcity:GOI")
    assert "hotel" in ctx_of(c)
    c.send("reset")
    assert "reset" in c.last[0]["body"].lower()
    assert next(iter(c.repo.convos.values()))["current_step"] == "menu" and not ctx_of(c).get("hotel") and "agent" not in ctx_of(c)


def test_reset_releases_a_payment_still_waiting():
    gw = FakeGateway()
    c = Chat(gateway=gw)
    book(c)
    c.send("Reset!")
    assert only_booking(c)["status"] == "cancelled" and gw.cancelled == {"plink_1"}
    assert "checkout" not in ctx_of(c)


def test_reset_never_cancels_a_stay_that_is_paid():
    gw = FakeGateway()
    c = Chat(gateway=gw)
    book(c)
    gw.paid.add("plink_1")  # paid on Razorpay, the webhook has not reached us yet
    c.send("reset")
    assert only_booking(c)["status"] == "pending" and gw.cancelled == set() and "checkout" not in ctx_of(c)
    asyncio.run(c.concierge.confirm_payment("plink_1"))  # the webhook arrives: the stay is confirmed
    assert only_booking(c)["status"] == "confirmed"
    c.send("reset")
    assert only_booking(c)["status"] == "confirmed"


def test_hotel_request_for_a_city_we_cannot_search_says_so_not_a_flight_message():
    from app.agents.base import Session
    from app.agents.concierge.classifiers import Intent
    c = Chat(gateway=Off())
    c.send("hi")
    s = Session(f"+{WA}", {"id": "u"}, "menu", "m1", {})
    out = asyncio.run(c.concierge._apply(s, Intent("hotel", {"unknown_to": "manali"}, ), "hotel in manali"))
    assert "can't find stays in Manali" in out[0]["body"] and "flights" not in out[0]["body"] and "Which city" in out[1]["body"]


def test_misspelt_city_is_understood():
    from app.core.places import fuzzy_city
    assert fuzzy_city("ahemdabad") == "AMD" and fuzzy_city("bangalor") == "BLR" and fuzzy_city("xyz") is None
    c = Chat(gateway=Off())
    c.send("hi"); c.send(reply_id="svc:hotel"); c.send(reply_id="hotel:find")
    out = c.send("ahemdabad")  # asked for the city: the typo is accepted
    assert "Ahmedabad" in out["body"]


def test_next_weekend_on_a_saturday_is_a_week_away():
    from datetime import date
    from app.agents.concierge.slots import extract_date
    sat, sun, wed = date(2026, 10, 3), date(2026, 10, 4), date(2026, 10, 7)
    assert extract_date("this weekend", sat) == "2026-10-03" and extract_date("next weekend", sat) == "2026-10-10"
    assert extract_date("next weekend", sun) == "2026-10-10" and extract_date("weekend", wed) == "2026-10-10"
