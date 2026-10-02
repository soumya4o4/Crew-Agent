"""Buddy knows the user's booked hotels in detail and suggests other stays from real inventory."""
from datetime import timedelta

from fakes import Chat
from fakes_buddy import FakeBrain
from app.agents.buddy.brain import BuddyReply
from app.core.utils import now_ist


def with_stay(**extra):
    brain = FakeBrain()
    c = Chat(brain=brain)
    repo = c.hotel_repo
    hotel = next(h for h in repo.hotels.values() if h["name"] == "Calangute Shores")
    room = repo.room_of("Calangute Shores", "Deluxe")
    check_in = (now_ist() + timedelta(days=5)).date()
    c.buddy_repo.stay = {"id": "hb1", "ref": "HB12345", "status": "confirmed", "guest_name": "Aarav Sharma", "guests": 2,
                         "check_in": check_in.isoformat(), "check_out": (check_in + timedelta(days=2)).isoformat(),
                         "total_price_inr": 9000, "hotels": hotel, "hotel_rooms": room, **extra}
    c.send("hi")
    return c, brain


def test_a_question_about_my_hotel_goes_to_buddy_with_every_stay_detail():
    c, brain = with_stay()
    c.send("mera hotel kitne baje check-in hai?")
    assert len(brain.calls) == 1
    ctx = brain.calls[-1]["context"]
    assert "Hotel: Calangute Shores, 4-star, Calangute, Goa (rated 4.3)" in ctx
    assert "check-in" in ctx and "from 2:00 PM" in ctx and "by 11:00 AM" in ctx and "2 night(s)" in ctx and "starts in 5 days" in ctx
    assert "room Deluxe (Queen bed)" in ctx and "2 guest(s), booked for Aarav Sharma" in ctx and "total ₹9,000" in ctx
    assert "free cancellation until" in ctx and "Amenities: Free WiFi, Pool" in ctx and "ref HB12345" in ctx


def test_other_stays_come_from_real_inventory_and_exclude_the_booked_one():
    c, brain = with_stay()
    c.send("koi aur hotel option hai kya? mera hotel mehnga lag raha hai")
    ctx = brain.calls[-1]["context"]
    assert "Other stays free in Goa on the same dates" in ctx and "Baga Backpackers (2-star, Baga, rated 3.9, from ₹1,500/night" in ctx
    assert ctx.count("Calangute Shores (") == 0                            # the booked hotel is not offered again
    assert "Marine Drive Suites" not in ctx and "Marina Skyline" not in ctx  # other cities never leak in


def test_no_booked_hotel_is_stated_plainly():
    brain = FakeBrain()
    c = Chat(brain=brain)
    c.send("hi"); c.send("mera hotel kaha hai?")
    assert "No hotel booked with us." in brain.calls[-1]["context"]


def test_suggesting_a_hotel_opens_the_search_in_the_same_city():
    c, brain = with_stay()
    brain.queue.append(BuddyReply("Baga Backpackers sasta hai, ₹1,500 mein.", suggest=["hotel"]))
    out = c.send("mera hotel mehnga hai, sasta batao")
    assert [i for i, _ in out["buttons"]][0] == "svc:hotel"
    assert "A stay in Goa" in c.send(reply_id="svc:hotel")["body"]


def test_several_stays_are_all_listed():
    c, brain = with_stay()
    other = next(h for h in c.hotel_repo.hotels.values() if h["name"] == "Marine Drive Suites")
    first = c.buddy_repo.stay
    second = {**first, "id": "hb2", "ref": "HB99999", "hotels": other, "hotel_rooms": c.hotel_repo.room_of("Marine Drive Suites", "Standard")}
    c.buddy_repo.stays = [first, second]
    c.send("mere hotels ke baare mein batao, mera hotel kaisa hai")
    ctx = brain.calls[-1]["context"]
    assert "Hotel: Calangute Shores" in ctx and "Hotel: Marine Drive Suites" in ctx
