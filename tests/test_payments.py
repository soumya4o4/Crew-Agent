import asyncio
from datetime import timedelta

from fakes import Chat, FakeGateway, FakeRepo, WA
from app.core.utils import now_ist


def seats(repo, no):
    return next(f for f in repo.flights.values() if f["flight_no"] == no)["seats_left"]


def pay_flow(c, travellers=1):
    """Chat up to the payment request."""
    c.enter_flights()
    c.send(reply_id="menu:book"); c.send(reply_id="from:IDR"); c.send(reply_id="to:BOM")
    c.send(reply_id=c.ids()[1]); c.send(reply_id="sort:time")
    c.send(reply_id=next(i for i in c.ids() if i.startswith("flt:")))  # 6E-1000, Rs 4,000, 3 seats
    c.book(travellers)
    c.send(reply_id="name:self")
    if travellers > 1:
        c.send("priya sharma")
    return c.send(reply_id="cfm:yes")


def test_confirm_holds_seats_and_sends_a_payment_link():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    out = pay_flow(c)
    assert [m["type"] for m in c.last] == ["cta", "buttons"]
    assert out["url"] == "https://rzp.io/i/plink_1" and "Pay ₹4,000" == out["button_text"]
    assert "Test mode" in out["body"] and "held for 20 minutes" in out["body"]
    assert [i for i, _ in c.last[1]["buttons"]] == ["pay:check", "pay:cancel"]
    booking = next(iter(repo.bookings.values()))
    assert booking["status"] == "pending" and seats(repo, "6E-1000") == 2  # held, not yet confirmed
    assert gw.links["plink_1"]["amount"] == 4000 and gw.links["plink_1"]["ref"] == booking["pnr"]


def test_ive_paid_confirms_once_razorpay_says_paid():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    pay_flow(c)
    assert "haven't received the payment" in c.send(reply_id="pay:check")["body"]
    gw.paid.add("plink_1")
    done = c.send(reply_id="pay:check")
    assert "Booking Confirmed" in done["body"] and next(iter(repo.bookings.values()))["status"] == "confirmed"
    assert c.send(reply_id="pay:check") is not None  # a stale tap after confirming is harmless
    assert "Already" in c.last[0]["body"] or "no longer" in c.last[0]["body"] or "couldn't find" in c.last[0]["body"]


def test_webhook_confirms_and_is_idempotent():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    pay_flow(c, travellers=2)
    flight = c.concierge.agents["flight"]
    number, messages = asyncio.run(flight.confirm_payment("plink_1"))
    assert number == WA and "Booking Confirmed" in messages[0]["body"] and "2 travellers" in messages[0]["body"]
    assert all(m["type"] != "reaction" for m in messages)  # nothing to react to: the user didn't tap
    assert asyncio.run(flight.confirm_payment("plink_1")) is None  # Razorpay retried: no second ticket
    assert repo.convos["+" + WA]["context"]["trip"]["city"] == "Mumbai"  # cabs/hotels can use the trip
    assert "Already confirmed" in c.send(reply_id="pay:check")["body"]


def test_cancelling_before_paying_releases_the_seats():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    pay_flow(c)
    assert "released your seats" in c.send(reply_id="pay:cancel")["body"]
    assert next(iter(repo.bookings.values()))["status"] == "cancelled" and seats(repo, "6E-1000") == 3
    assert gw.cancelled == {"plink_1"} and repo.payments["plink_1"]["status"] == "cancelled"
    assert asyncio.run(c.concierge.agents["flight"].confirm_payment("plink_1")) is None  # a late payment can't revive it


def test_unpaid_bookings_expire_and_the_user_is_told():
    repo, gw = FakeRepo(), FakeGateway()
    c = Chat(repo, gateway=gw)
    pay_flow(c)
    repo.payments["plink_1"]["expires_at"] = now_ist() - timedelta(minutes=1)
    notices = asyncio.run(c.concierge.agents["flight"].release_expired())
    assert len(notices) == 1 and notices[0][0] == WA and "payment window" in notices[0][1][0]["body"]
    assert seats(repo, "6E-1000") == 3 and next(iter(repo.bookings.values()))["status"] == "cancelled"
    assert asyncio.run(c.concierge.agents["flight"].release_expired()) == []  # only once
    assert "window ended" in c.send(reply_id="pay:check")["body"]


def test_payment_service_down_releases_seats_instead_of_stranding_them():
    repo, gw = FakeRepo(), FakeGateway()
    gw.fail = True
    c = Chat(repo, gateway=gw)
    out = pay_flow(c)
    assert "couldn't set up the payment" in out["body"]
    assert seats(repo, "6E-1000") == 3 and next(iter(repo.bookings.values()))["status"] == "cancelled"


def test_without_razorpay_keys_bookings_confirm_instantly():
    class Off(FakeGateway):
        enabled = False

    c = Chat(gateway=Off())
    c.enter_flights(); c.pick_flight()
    assert "Booking Confirmed" in c.send(reply_id="cfm:yes")["body"]
