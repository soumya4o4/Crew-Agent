import asyncio
from datetime import timedelta

from fakes import CONTACT, DETAILS, Chat, FakeGateway, FakeRepo, WA
from app.agents.checkout import parse_contact
from app.core.utils import now_ist


def pay_flow(c, travellers=1, contact=CONTACT):
    """Chat up to the payment request: request, pick, book, confirm, and (the first time) who is paying."""
    c.send("flight from indore to mumbai tomorrow")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))  # 6E-1000, Rs 4,000
    if travellers > 1:
        c.send("Aarav Sharma 14/03/1992 M; Priya Sharma 02/07/1994 F")
    else:
        c.send("book"); c.send(DETAILS)
    out = c.send("yes")
    return c.send(contact) if contact and "email" in out["body"] else out


def test_the_payment_asks_for_name_email_and_phone_once():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    c.send("flight from indore to mumbai tomorrow")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))
    c.send("book"); c.send(DETAILS)
    ask = c.send("yes")
    assert "full name, email and phone number" in ask["body"] and "4,000" in ask["body"] and gw.links == {}
    assert "still need your email" in c.send("Aarav Sharma, 9876543210")["body"]  # asks only for what is missing
    out = c.send("aarav@example.com")
    assert out["type"] == "cta" and gw.links["plink_1"]["email"] == "aarav@example.com"
    assert gw.links["plink_1"]["phone"] == "+9876543210" and gw.links["plink_1"]["name"] == "Aarav Sharma"
    # next purchase: the details are remembered, the link comes straight away
    gw.paid.add("plink_1"); c.send(reply_id="chk:check")
    c.send("flight from mumbai to indore tomorrow")
    if c.ids()[0].startswith("flt:"):
        c.send(reply_id=c.ids()[0]); c.send("book")  # their birth date and gender are remembered: straight to the summary
        assert c.send("yes")["type"] == "cta"


def test_contact_parser_takes_the_details_in_any_order():
    assert parse_contact("Rahul Verma, rahul@mail.com, 98765 43210") == {"name": "Rahul Verma", "email": "rahul@mail.com", "phone": "9876543210"}
    assert parse_contact("my email is r@x.in", WA)["email"] == "r@x.in"
    assert parse_contact("same", "+919876543210")["phone"] == "919876543210"
    assert parse_contact("98765 43210 rahul@mail.com Rahul") == {"name": "Rahul", "email": "rahul@mail.com", "phone": "9876543210"}


def test_confirm_holds_the_booking_and_sends_a_payment_link():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    out = pay_flow(c)
    assert [m["type"] for m in c.last] == ["cta", "buttons"]
    assert out["url"] == "https://rzp.io/i/plink_1" and "Pay ₹4,000" == out["button_text"]
    assert "Test mode" in out["body"] and "Held for 20 minutes" in out["body"]
    assert [i for i, _ in c.last[1]["buttons"]] == ["chk:check", "chk:cancel"]
    booking = next(iter(repo.bookings.values()))
    assert booking["status"] == "pending" and booking["duffel_order_id"] is None  # held, no airline ticket until it is paid
    assert booking["passenger_details"] == [{"name": "Aarav Sharma", "dob": "1992-03-14", "gender": "m"}]
    assert c.live.orders == {}
    assert gw.links["plink_1"]["amount"] == 4000 and gw.links["plink_1"]["ref"] == booking["pnr"]


def test_nothing_is_held_until_checkout():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    c.send("flight from indore to mumbai tomorrow")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))
    c.send("book"); c.send(DETAILS); c.send("yes")                       # waiting for contact details
    assert repo.bookings == {} and c.live.orders == {}


def test_ive_paid_confirms_once_razorpay_says_paid():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    pay_flow(c)
    assert "haven't received the payment" in c.send(reply_id="chk:check")["body"]
    gw.paid.add("plink_1")
    done = c.send(reply_id="chk:check")
    assert "Your booking has been recorded successfully" in done["body"] and "ZX9Q2K" in done["body"] and next(iter(repo.bookings.values()))["status"] == "confirmed"
    assert len(c.live.orders) == 1  # the airline ticket is issued once the money is in
    assert c.send(reply_id="chk:check") is not None  # a stale tap after confirming is harmless
    assert "no payment waiting" in c.last[0]["body"]


def test_webhook_confirms_and_is_idempotent():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    pay_flow(c, travellers=2)
    number, messages = asyncio.run(c.concierge.confirm_payment("plink_1"))
    assert number == WA and "Your booking has been recorded successfully" in messages[0]["body"] and "2 travellers" in messages[0]["body"]
    assert all(m["type"] != "reaction" for m in messages)  # nothing to react to: the user didn't tap
    assert asyncio.run(c.concierge.confirm_payment("plink_1")) is None  # Razorpay retried: no second ticket
    ctx = repo.convos["+" + WA]["context"]
    assert ctx["trip"]["city"] == "Mumbai" and "checkout" not in ctx  # cabs/hotels can use the trip; the checkout is done
    assert "no payment waiting" in c.send(reply_id="chk:check")["body"]


def test_cancelling_before_paying_releases_the_booking():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    pay_flow(c)
    assert "released everything" in c.send(reply_id="chk:cancel")["body"]
    assert next(iter(repo.bookings.values()))["status"] == "cancelled" and c.live.orders == {}
    assert gw.cancelled == {"plink_1"} and repo.payments["plink_1"]["status"] == "cancelled"
    assert asyncio.run(c.concierge.confirm_payment("plink_1")) is None  # a late payment can't revive it


def test_unpaid_bookings_expire_and_the_user_is_told():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    pay_flow(c)
    repo.payments["plink_1"]["expires_at"] = now_ist() - timedelta(minutes=1)
    notices = asyncio.run(c.concierge.agents["flight"].release_expired())
    assert len(notices) == 1 and notices[0][0] == WA and "payment window" in notices[0][1][0]["body"]
    assert next(iter(repo.bookings.values()))["status"] == "cancelled" and c.live.orders == {}
    assert asyncio.run(c.concierge.agents["flight"].release_expired()) == []  # only once
    assert "window ended" in c.send(reply_id="chk:check")["body"]


def test_payment_service_down_releases_the_booking_instead_of_stranding_it():
    repo, gw = FakeRepo(), FakeGateway()
    gw.fail = True
    c = Chat(repo, gateway=gw)
    out = pay_flow(c)
    assert "couldn't set up the payment" in out["body"]
    assert next(iter(repo.bookings.values()))["status"] == "cancelled"


def test_a_fare_that_sold_out_meanwhile_is_caught_at_checkout_and_nothing_is_held():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    c.send("flight from indore to mumbai tomorrow")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))
    c.send("book"); c.send(DETAILS); c.send("yes")
    c.live.gone = True  # the airline no longer sells it
    assert "no longer available" in c.send(CONTACT)["body"] and repo.bookings == {} and gw.links == {}


def test_a_dearer_fare_at_checkout_counts_as_gone_but_a_few_rupees_are_absorbed():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    c.send("flight from indore to mumbai tomorrow")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))
    c.send("book"); c.send(DETAILS); c.send("yes")
    c.live.price_bump = 500
    assert "no longer available" in c.send(CONTACT)["body"] and repo.bookings == {}
    c.live.price_bump = 40
    c.send("flight from indore to mumbai tomorrow")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))
    c.send("book"); out = c.send("yes")
    assert out["type"] == "cta" and gw.links["plink_1"]["amount"] == 4040  # charged what the airline now asks


def test_paid_but_the_airline_refuses_the_ticket_cancels_and_refunds():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    pay_flow(c)
    c.live.fail_book = True
    gw.paid.add("plink_1")
    out = c.send(reply_id="chk:check")
    assert "couldn't issue this ticket" in out["body"] and "4,000" in out["body"] and "on its way back" in out["body"]
    assert gw.refunds == [("plink_1", 4000)] and next(iter(repo.bookings.values()))["status"] == "cancelled"


def test_paid_but_the_airline_refuses_and_the_refund_fails_says_so_honestly():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    pay_flow(c)
    c.live.fail_book = True
    gw.paid.add("plink_1"); gw.refund_fails = True
    number, messages = asyncio.run(c.concierge.confirm_payment("plink_1"))
    assert "Our team will contact you" in messages[0]["body"] and "on its way back" not in messages[0]["body"]


def test_the_ticket_is_issued_with_the_travellers_details_and_contact():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    pay_flow(c, travellers=2)
    gw.paid.add("plink_1"); c.send(reply_id="chk:check")
    pass
    assert [t["name"] for t in order["travellers"]] == ["Aarav Sharma", "Priya Sharma"]
    assert [t["dob"] for t in order["travellers"]] == ["1992-03-14", "1994-07-02"] and [t["gender"] for t in order["travellers"]] == ["m", "f"]
    assert order["contact"] == {"email": "aarav@example.com", "phone": "9876543210"} and order["flight"]["offer_pax"] == 2


def test_without_razorpay_keys_bookings_confirm_instantly():
    class Off(FakeGateway):
        enabled = False

    c = Chat(gateway=Off())
    c.enter_flights(); c.pick_flight()
    assert "Your booking has been recorded successfully" in c.send(reply_id="cfm:yes")["body"]


# ---------------------------------------------------------------------------- one payment for the whole trip
def test_flight_and_hotel_are_paid_together_with_one_link():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    c.send("flight from indore to mumbai tomorrow and a hotel there")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))
    c.send("book"); c.send(DETAILS)
    out = c.send("yes")                                          # flight added to the bundle, the hotel comes next
    assert "Added to your bundle" in c.last[0]["body"] and "hotel" in c.last[0]["body"].lower() and gw.links == {}
    assert repo.bookings == {}                                   # nothing held yet
    assert "How many nights" in c.last[-1]["body"]
    results = c.send("2 nights for 2 people")
    assert results["type"] == "list" and "Mumbai" in results["body"]
    c.send(reply_id=results["rows"][0][0])                       # a hotel
    c.send(reply_id=c.ids()[0])                                  # its first room
    c.send(reply_id="hname:self")
    ask = c.send(reply_id="hcfm:yes")                            # both chosen: one question about who is paying
    assert "Your bundle" in ask["body"] and "Total *₹" in ask["body"] and gw.links == {} and repo.bookings == {}
    pay = c.send(CONTACT)
    flight_total = 4000
    hotel_total = c.hotel_repo.bookings and next(iter(c.hotel_repo.bookings.values()))["total_price_inr"]
    assert pay["type"] == "cta" and len(gw.links) == 1                      # ONE link for everything
    assert gw.links["plink_1"]["amount"] == flight_total + hotel_total
    assert gw.links["plink_1"]["description"].count("+") == 1
    assert next(iter(repo.bookings.values()))["status"] == "pending" and next(iter(c.hotel_repo.bookings.values()))["status"] == "pending"
    gw.paid.add("plink_1")
    c.send(reply_id="chk:check")
    bodies = " ".join(m.get("body", "") for m in c.last)
    assert "Your booking has been recorded successfully" in bodies and "Stay Confirmed" in bodies                  # both confirmed by the one payment
    assert next(iter(repo.bookings.values()))["status"] == "confirmed" and next(iter(c.hotel_repo.bookings.values()))["status"] == "confirmed"
    assert bodies.count("Want me to arrange") == 0                                      # the hotel is not offered again
