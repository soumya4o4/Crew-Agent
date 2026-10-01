import asyncio
from datetime import timedelta

from fakes import Chat, FakeGateway, WA
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
    return c.send(reply_id="hcfm:yes")


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
    rooms = c.send(reply_id=out["rows"][0][0])
    assert [r[0] for r in rooms["rows"][:-2]] == [f"hroom:{c.hotel_repo.room_of('Calangute Shores', 'Suite')['id']}"]
    assert "Last room" in rooms["rows"][0][2]


def test_no_rooms_for_the_party_says_so():
    out = search(Chat(gateway=Off()), city="BOM", guests=4)
    assert "No rooms free" in out["body"]


def test_free_text_fills_the_city_and_date():
    c = Chat()
    assert "Got it" in c.send("hotel in goa tomorrow")["body"]
    assert c.ids()[0] == "hnt:1"  # city and check-in came from the sentence: nights is next


def test_stay_after_a_flight_uses_the_trip():
    c = Chat(gateway=Off())
    c.enter_flights(); c.pick_flight(); c.send(reply_id="cfm:yes")
    out = c.send(reply_id="svc:hotel")
    assert "hotel:trip" in [i for i, _ in out["buttons"]] and "Mumbai" in out["body"]
    c.send(reply_id="hotel:trip")
    assert c.ids()[0] == "hnt:1"  # Mumbai and the landing day are already known


def test_typed_date_nights_and_city():
    c = Chat(gateway=Off())
    c.send("hi"); c.send(reply_id="svc:hotel"); c.send(reply_id="hotel:find")
    assert "don't have stays" in c.send("atlantis")["body"]
    c.send("dubai")
    c.send(reply_id="hin:more")
    assert "out of range" in c.send("1/1/2031")["body"]
    c.send("tomorrow")
    assert c.ids()[0] == "hnt:1"
    c.send(reply_id="hnt:more")
    assert "between 1 and 14" in c.send("30")["body"]
    c.send("3 nights")
    assert c.ids() == ["hgst:1", "hgst:2", "hgst:3", "hgst:4"]


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
    assert "Stay Confirmed" in out["body"] and "HB00001" in out["body"]
    b = only_booking(c)
    assert b["status"] == "confirmed" and b["total_price_inr"] == 9000 and b["guests"] == 2
    assert [i for i, _ in c.last[1]["buttons"]] == ["hotel:stays", "svc:cab", "nav:menu"]


def test_confirm_holds_the_room_and_sends_a_payment_link():
    gw = FakeGateway()
    c = Chat(gateway=gw)
    out = book(c)
    assert [m["type"] for m in c.last] == ["cta", "buttons"]
    assert out["url"] == "https://rzp.io/i/plink_1" and out["button_text"] == "Pay ₹9,000"
    assert "Test mode" in out["body"] and "held for 20 minutes" in out["body"]
    assert [i for i, _ in c.last[1]["buttons"]] == ["hpay:check", "hpay:cancel"]
    b = only_booking(c)
    assert b["status"] == "pending" and gw.links["plink_1"] == {"amount": 9000, "ref": b["ref"], "phone": "+" + WA}
    rooms = c.hotel_repo.search_hotels("GOI", day(5), day(7), 2)
    assert next(r for h in rooms for r in h["rooms"] if r["room_type"] == "Deluxe")["left"] == 2  # held while unpaid


def test_ive_paid_confirms_once_razorpay_says_paid():
    gw = FakeGateway()
    c = Chat(gateway=gw)
    book(c)
    assert "haven't received the payment" in c.send(reply_id="hpay:check")["body"]
    gw.paid.add("plink_1")
    assert "Stay Confirmed" in c.send(reply_id="hpay:check")["body"] and only_booking(c)["status"] == "confirmed"
    assert c.send(reply_id="hpay:check") is not None  # a stale tap after confirming is harmless


def test_webhook_confirms_and_is_idempotent():
    c = Chat(gateway=FakeGateway())
    book(c)
    number, messages = asyncio.run(c.concierge.confirm_payment("plink_1"))  # the Concierge finds the right agent
    assert number == WA and "Stay Confirmed" in messages[0]["body"] and only_booking(c)["status"] == "confirmed"
    assert all(m["type"] != "reaction" for m in messages)  # nothing to react to: the user didn't tap
    assert asyncio.run(c.concierge.confirm_payment("plink_1")) is None  # Razorpay retried: no second voucher
    assert "Already confirmed" in c.send(reply_id="hpay:check")["body"]


def test_cancelling_before_paying_releases_the_room():
    gw = FakeGateway()
    c = Chat(gateway=gw)
    book(c)
    assert "released the room" in c.send(reply_id="hpay:cancel")["body"]
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
    assert "window ended" in c.send(reply_id="hpay:check")["body"]


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
    pay_flow_chat.send(reply_id="menu:book"); pay_flow_chat.send(reply_id="from:IDR"); pay_flow_chat.send(reply_id="to:BOM")
    pay_flow_chat.send(reply_id=pay_flow_chat.ids()[1]); pay_flow_chat.send(reply_id="sort:time")
    pay_flow_chat.send(reply_id=next(i for i in pay_flow_chat.ids() if i.startswith("flt:")))
    pay_flow_chat.book(); pay_flow_chat.send(reply_id="name:self"); pay_flow_chat.send(reply_id="cfm:yes")
    c.repo.payments["plink_1"]["expires_at"] = now_ist() - timedelta(minutes=1)

    def broken(now):
        raise RuntimeError("column payments.hotel_booking_id does not exist")

    monkeypatch.setattr(c.hotel_repo, "expire_unpaid", broken)
    notices = asyncio.run(c.concierge.release_expired())
    assert len(notices) == 1 and notices[0][0] == WA and "payment window" in notices[0][1][0]["body"]  # the flight's notice survived
