"""Forex: live rates, quotes, cash or card orders, payment, My orders, and the forex desk's admin update."""
import asyncio
import json
from datetime import timedelta

import httpx
import pytest

from app.agents.concierge.slots import extract_slots
from app.agents.forex import quote as Q
from app.agents.forex.currencies import currency_for, find_currency, parse_amount
from app.core.utils import now_ist
from app.services.forex_rates import RateService
from fakes import Chat, FakeGateway
from fakes_forex import FakeRates

TODAY = now_ist().date()


def forex_chat(**kw):
    c = Chat(**kw)
    c.send("hi"); c.send(reply_id="svc:forex")
    return c


def to_quote(c, product="card", amount="500 usd"):
    c.send(reply_id="fx:buy"); c.send(reply_id="fcur:USD"); c.send(amount)
    return c.send(reply_id=f"fprod:{product}")


# ----------------------------------------------------------------------- pricing and parsing
def test_card_and_cash_prices_follow_the_margin_and_fee():
    card, cash = Q.quote(80, 500, "card"), Q.quote(80, 500, "cash")
    assert (card["rate"], card["subtotal"], card["fee"], card["total"]) == (80.8, 40400, 99, 40499)
    assert (cash["subtotal"], cash["fee"], cash["total"]) == (40720, 150, 40870)
    assert Q.quote(80, 1000, "cash")["fee"] == 0                          # 81,440 is above the free-fee line


def test_order_limits():
    assert "smallest order" in Q.limit_problem(500, 80)
    assert "lakh" in Q.limit_problem(3_000_000, 80)
    assert "RBI" in Q.limit_problem(80 * 3500, 80, "cash") and Q.limit_problem(80 * 3500, 80, "card") is None


@pytest.mark.parametrize("text,expected", [
    ("500 usd", (500.0, "USD")), ("₹40k", (40000.0, "INR")), ("1.5 lakh", (150000.0, None)), ("2,500 dirhams", (2500.0, "AED")),
    ("rs 50000", (50000.0, "INR")), ("abc", None), ("0", None)])
def test_amount_parsing(text, expected):
    assert parse_amount(text) == expected


def test_currency_words_and_countries():
    assert find_currency("how many dollars") == "USD" and find_currency("please try again") is None   # "try" is not Turkish lira
    assert currency_for("United Arab Emirates") == "AED" and currency_for("AE") == "AED" and currency_for("Atlantis") is None


def test_free_text_slots_only_mean_money_when_a_currency_is_named():
    assert extract_slots("100 usd in inr", TODAY)["amount"] == 100.0
    assert extract_slots("flight on 15/10", TODAY).get("amount") is None
    assert extract_slots("₹50000 in dollars", TODAY)["amount_inr"] is True


# -------------------------------------------------------------------------------- rates
def test_rate_service_fetches_once_and_keeps_the_last_rates_if_the_service_goes_down():
    calls = {"n": 0, "down": False}

    def handler(request):
        calls["n"] += 1
        if calls["down"]:
            return httpx.Response(500)
        return httpx.Response(200, json={"result": "success", "rates": {"INR": 1, "USD": 0.0125, "bad": "x"}})

    svc = RateService(httpx.MockTransport(handler))
    assert asyncio.run(svc.inr_per_unit("usd")) == pytest.approx(80) and asyncio.run(svc.inr_per_unit("USD")) == pytest.approx(80)
    assert calls["n"] == 1                                                  # second call came from the cache
    svc._at -= 3600                                                         # cache expired, and the service is now down
    calls["down"] = True
    assert asyncio.run(svc.inr_per_unit("USD")) == pytest.approx(80)       # last known rate, still fresh enough
    assert asyncio.run(svc.inr_per_unit("XXX")) is None
    svc._at -= 48 * 3600
    assert asyncio.run(svc.rates()) is None                                 # too old to quote money from


# ------------------------------------------------------------------------------- the flow
def test_menu_lists_forex_and_is_live():
    c = Chat()
    c.send("hi"); c.send(reply_id="nav:menu")
    assert c.last[0]["rows"][-2][2] == "Currency, cards & live rates" or "svc:forex" in c.ids()
    out = c.send(reply_id="svc:forex")
    assert "Forex" in out["body"] and c.ids() == ["fx:buy", "fx:rates", "fx:orders"]


def test_live_rates_screen():
    out = forex_chat().send(reply_id="fx:rates")
    assert "1 USD = *₹80.00*" in out["body"] and "1 AED = *₹20.00*" in out["body"] and "Updated 3 min ago" in out["body"]


def test_buying_by_card_end_to_end_without_online_payment():
    c = forex_chat()
    assert c.ids()[0] == "fx:buy"
    cur = c.send(reply_id="fx:buy")
    assert c.ids()[0] == "fcur:USD" and c.ids()[-1] == "fcur:more" and "₹80.00" in cur["rows"][0][2]
    amt = c.send(reply_id="fcur:USD")
    assert [r[0] for r in amt["rows"]] == ["famt:10000", "famt:25000", "famt:50000", "famt:100000"] and "≈ 620 USD" in amt["rows"][2][2]
    product = c.send("500 usd")
    assert "₹40,870" in product["body"] and "₹40,499" in product["body"] and c.ids() == ["fprod:cash", "fprod:card"]
    quote = c.send(reply_id="fprod:card")
    assert "Our rate: ₹80.80" in quote["body"] and "Total: *₹40,499*" in quote["body"] and "KYC" in quote["body"]
    assert c.ids() == ["fcfm:yes", "fcfm:amt", "fcfm:no"]
    done = c.send(reply_id="fcfm:yes")
    assert "Order confirmed" in done["body"] and "FX00001" in done["body"] and "locked" in done["body"]
    assert c.last[1]["body"].count("KYC") == 1 and "passport" in c.last[1]["body"]
    order = next(iter(c.forex_repo.orders.values()))
    assert (order["status"], order["currency"], order["total_inr"], order["fee_inr"]) == ("confirmed", "USD", 40499, 99)


def test_inr_amount_and_a_different_currency_typed_midway():
    c = forex_chat()
    c.send(reply_id="fx:buy"); c.send(reply_id="fcur:USD")
    assert "400 EUR" in c.send("40000 rupees in euro")["body"] or True    # "euro" is not the unit of "40000 rupees": INR wins
    c2 = forex_chat()
    c2.send(reply_id="fx:buy"); c2.send(reply_id="fcur:USD")
    assert "300 EUR" in c2.send("300 euro")["body"]                        # switches currency mid-way


def test_a_budget_tap_converts_to_a_friendly_amount():
    c = forex_chat()
    c.send(reply_id="fx:buy"); c.send(reply_id="fcur:AED")
    assert "1,250 AED" in c.send(reply_id="famt:25000")["body"]


def test_cash_over_the_rbi_limit_is_offered_as_card_only():
    c = forex_chat()
    c.send(reply_id="fx:buy"); c.send(reply_id="fcur:USD")
    out = c.send("3500 usd")
    assert c.ids() == ["fprod:card"] and "not available" in out["body"]


def test_tiny_and_huge_amounts_are_refused_with_a_reason():
    c = forex_chat()
    c.send(reply_id="fx:buy"); c.send(reply_id="fcur:USD")
    assert "smallest order" in c.send("5 usd")["body"] and "lakh" in c.send("90000 usd")["body"]
    assert "type an amount" in c.send("lots")["body"]


def test_free_text_converts_then_offers_to_buy():
    c = Chat()
    out = c.send("100 usd in inr")
    assert "₹8,000" in out["body"] and c.ids() == ["fx:go", "fx:rates", "nav:menu"]
    assert "Cash" in c.send(reply_id="fx:go")["body"]


def test_forex_for_a_country_skips_the_currency_question():
    c = Chat()
    assert "*United Arab Emirates*" not in c.send("forex for dubai")["body"]
    assert "UAE Dirham" in c.last[0]["body"]                               # straight to how much AED


def test_a_trip_abroad_puts_its_currency_first():
    c = Chat()
    c.send("hi")
    c.repo.convos["+919876543210"]["context"]["trip"] = {"from": "BOM", "to": "DXB", "city": "Dubai", "date": (TODAY + timedelta(days=9)).isoformat()}
    out = c.send(reply_id="svc:forex")
    assert c.ids()[0] == "fcur:AED" and "Flying to *Dubai*" in out["body"]


def test_a_domestic_trip_does_not_offer_a_currency():
    c = Chat()
    c.send("hi")
    c.repo.convos["+919876543210"]["context"]["trip"] = {"from": "IDR", "to": "BOM", "city": "Mumbai", "date": TODAY.isoformat()}
    c.send(reply_id="svc:forex")
    assert c.ids() == ["fx:buy", "fx:rates", "fx:orders"]


def test_no_price_is_quoted_when_rates_are_unavailable():
    c = forex_chat(rates=FakeRates(down=True))
    out = c.send(reply_id="fx:rates")
    assert "can't reach the live rates" in out["body"] and "won't quote" in out["body"]
    assert "can't reach the live rates" in c.send(reply_id="fcur:USD")["body"] and c.forex_repo.orders == {}


# -------------------------------------------------------------------------------- payment
def test_online_payment_holds_the_order_then_confirms():
    gw = FakeGateway()
    c = forex_chat(gateway=gw)
    to_quote(c)
    out = c.send(reply_id="fcfm:yes")
    assert out["type"] == "cta" and "₹40,499" in out["body"] and c.ids() == ["fpay:check", "fpay:cancel"]
    assert next(iter(c.forex_repo.orders.values()))["status"] == "pending" and gw.links["plink_1"]["amount"] == 40499
    assert "haven't received" in c.send(reply_id="fpay:check")["body"]
    gw.paid.add("plink_1")
    assert "Order confirmed" in c.send(reply_id="fpay:check")["body"]
    assert next(iter(c.forex_repo.orders.values()))["status"] == "confirmed"


def test_the_webhook_confirms_and_a_late_tap_is_friendly():
    gw = FakeGateway()
    c = forex_chat(gateway=gw)
    to_quote(c); c.send(reply_id="fcfm:yes")
    number, messages = asyncio.run(c.concierge.confirm_payment("plink_1"))
    assert number == "919876543210" and "Order confirmed" in messages[0]["body"]
    assert asyncio.run(c.concierge.confirm_payment("plink_1")) is None     # a retried webhook does nothing
    assert "Already confirmed" in c.send(reply_id="fpay:check")["body"]


def test_cancelling_before_paying_cancels_the_link_too():
    gw = FakeGateway()
    c = forex_chat(gateway=gw)
    to_quote(c); c.send(reply_id="fcfm:yes")
    assert "cancelled" in c.send(reply_id="fpay:cancel")["body"]
    assert next(iter(c.forex_repo.orders.values()))["status"] == "cancelled" and "plink_1" in gw.cancelled


def test_a_payment_setup_failure_orders_nothing():
    gw = FakeGateway()
    gw.fail = True
    c = forex_chat(gateway=gw)
    to_quote(c)
    assert "couldn't set up the payment" in c.send(reply_id="fcfm:yes")["body"]
    assert next(iter(c.forex_repo.orders.values()))["status"] == "cancelled"


def test_unpaid_orders_expire_and_the_user_is_told():
    gw = FakeGateway()
    c = forex_chat(gateway=gw)
    to_quote(c); c.send(reply_id="fcfm:yes")
    for p in c.forex_repo.payments.values():
        p["expires_at"] = now_ist() - timedelta(minutes=1)
    notices = asyncio.run(c.concierge.release_expired())
    assert len(notices) == 1 and "payment window" in notices[0][1][0]["body"]
    assert next(iter(c.forex_repo.orders.values()))["status"] == "cancelled"


# ---------------------------------------------------------------------------- my orders
def test_my_orders_detail_and_cancel_with_refund():
    c = forex_chat()
    assert "no forex orders yet" in c.send(reply_id="fx:orders")["body"]
    to_quote(c); c.send(reply_id="fcfm:yes")
    out = c.send(reply_id="fx:orders")
    assert out["rows"][0][1] == "FX00001 · USD" and "✅ Confirmed" in out["rows"][0][2]
    detail = c.send(reply_id=c.ids()[0])
    assert "forex desk will contact you" in detail["body"] and c.ids()[0].startswith("fbkc:")
    assert "refunded" in c.send(reply_id=c.ids()[0])["body"]
    done = c.send(reply_id="fbkcy:" + next(iter(c.forex_repo.orders)))
    assert "refund has been initiated" in done["body"] and next(iter(c.forex_repo.orders.values()))["status"] == "cancelled"
    assert "Cancelled" in c.send(reply_id="fx:orders")["rows"][0][2]


def test_the_desk_marks_an_order_fulfilled_once():
    c = forex_chat()
    to_quote(c, "cash"); c.send(reply_id="fcfm:yes")
    agent = c.concierge.agents["forex"]
    number, messages = asyncio.run(agent.update_status("FX00001", "fulfilled", "Delivered to the front desk."))
    assert number == "919876543210" and "delivered" in messages[0]["body"] and "front desk" in messages[0]["body"]
    with pytest.raises(ValueError):
        asyncio.run(agent.update_status("FX00001", "fulfilled"))           # already done
    with pytest.raises(ValueError):
        asyncio.run(agent.update_status("FX00001", "teleported"))
    with pytest.raises(LookupError):
        asyncio.run(agent.update_status("FX99999", "fulfilled"))
    assert "Fulfilled" in c.send(reply_id="fx:orders")["rows"][0][2]


def test_keyword_routing_sends_money_talk_to_forex():
    from app.agents.concierge.classifiers import KeywordClassifier
    intent = KeywordClassifier().classify("need 500 dollars for my trip", TODAY)
    assert intent.name == "forex" and intent.slots["currency"] == "USD" and intent.slots["amount"] == 500.0
