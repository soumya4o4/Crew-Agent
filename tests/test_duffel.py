"""Duffel live flights: the client turns the API's offers into our flights, keeps the quote fresh, issues and cancels tickets."""
import asyncio
import json
from datetime import date

import httpx
import pytest

from app.core.config import settings
from app.services.duffel import DuffelClient, DuffelError, minutes_of, utc_of
from fakes_forex import FakeRates

DAY = date(2026, 11, 2)


def segment(frm="DEL", to="BOM", dep="2026-11-02T09:30:00", arr="2026-11-02T11:40:00", carrier="6E", number="123", bags=1):
    zones = {"DEL": "Asia/Kolkata", "BOM": "Asia/Kolkata", "LHR": "Europe/London", "DXB": "Asia/Dubai"}
    return {"origin": {"iata_code": frm, "time_zone": zones[frm]}, "destination": {"iata_code": to, "time_zone": zones[to]},
            "departing_at": dep, "arriving_at": arr, "marketing_carrier": {"iata_code": carrier}, "marketing_carrier_flight_number": number,
            "passengers": [{"cabin_class": "economy", "baggages": [{"type": "checked", "quantity": bags}] if bags else []}]}


def offer(oid="off_1", total="50.00", currency="USD", segments=None, pax=1, owner="IndiGo", refund=True, docs=False, duration="PT2H10M"):
    return {"id": oid, "expires_at": "2026-11-01T12:00:00Z", "total_amount": total, "total_currency": currency,
            "owner": {"name": owner}, "passengers": [{"id": f"pas_{i}"} for i in range(pax)],
            "slices": [{"duration": duration, "segments": segments or [segment()]}],
            "conditions": {"refund_before_departure": {"allowed": refund}}, "passenger_identity_documents_required": docs}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setattr(settings, "DUFFEL_API_TOKEN", "duffel_test_x")


def client(offers=None, calls=None, routes=None):
    """A Duffel whose offer requests answer `offers` (a list the test may change later); `routes` maps "METHOD path" to a
    (status, body) answer and wins over that; anything else is "not found"."""
    offers = [offer()] if offers is None else offers

    def handler(request):
        if calls is not None:
            calls.append(request)
        key = f"{request.method} {request.url.path}"
        if routes and key in routes:
            status, body = routes[key]
            return httpx.Response(status, json=body)
        if key == "POST /air/offer_requests":
            return httpx.Response(200, json={"data": {"offers": offers}})
        return httpx.Response(404, json={"errors": [{"message": "not found", "code": "not_found"}]})
    return DuffelClient(FakeRates(), httpx.MockTransport(handler))


def search(c, pax=1, **kw):
    return asyncio.run(c.search("DEL", "BOM", DAY, pax, **kw))


def test_an_offer_becomes_our_flight_with_utc_times_and_rupees():
    [f] = search(client([offer(total="50.00")]))
    assert f["flight_no"] == "6E-123" and f["airline"] == "IndiGo" and f["from_code"] == "DEL" and f["to_code"] == "BOM"
    assert f["departure_time"] == "2026-11-02T04:00:00+00:00"  # 09:30 in Delhi is 04:00 UTC
    assert f["arrival_time"] == "2026-11-02T06:10:00+00:00" and f["duration_min"] == 130
    assert f["price_inr"] == 4000 and f["stops"] == 0 and f["checked_bags"] == 1 and f["refundable"] is True  # $50 at Rs 80
    assert f["duffel_offer_id"] == "off_1" and f["offer_total"] == 50.0 and f["offer_currency"] == "USD" and f["offer_pax"] == 1
    assert f["itinerary_key"] == "6E-123@2026-11-02T09:30:00"


def test_the_fare_is_per_traveller_and_the_markup_is_added(monkeypatch):
    monkeypatch.setattr(settings, "DUFFEL_MARKUP_PCT", 10.0)
    [f] = search(client([offer(total="100.00", pax=2)]), pax=2)
    assert f["price_inr"] == 4400 and f["offer_pax"] == 2 and f["offer_passenger_ids"] == ["pas_0", "pas_1"]  # $50 each, +10%


def test_a_flight_across_time_zones_is_converted_at_both_ends():
    seg = segment("LHR", "DXB", "2026-11-02T10:00:00", "2026-11-02T20:30:00", "EK", "10")
    [f] = asyncio.run(client([offer(segments=[seg], duration="PT7H30M")]).search("LHR", "DXB", DAY))
    assert f["departure_time"] == "2026-11-02T10:00:00+00:00" and f["arrival_time"] == "2026-11-02T16:30:00+00:00"


def test_a_connecting_flight_counts_its_stops_and_ends_at_the_last_airport():
    segs = [segment("DEL", "BOM", "2026-11-02T09:00:00", "2026-11-02T11:00:00", "AI", "1"),
            segment("BOM", "DEL", "2026-11-02T13:00:00", "2026-11-02T15:00:00", "AI", "2")]
    [f] = search(client([offer(segments=segs, duration="PT6H")]))
    assert f["stops"] == 1 and f["to_code"] == "DEL" and f["flight_no"] == "AI-1" and f["itinerary_key"].count("|") == 1


def test_the_same_flight_at_several_fares_keeps_the_cheapest_and_results_are_cheapest_first():
    other = segment(number="456", dep="2026-11-02T18:00:00", arr="2026-11-02T20:00:00")
    flights = search(client([offer("a", "70.00"), offer("b", "50.00"), offer("c", "30.00", segments=[other])]))
    assert [f["duffel_offer_id"] for f in flights] == ["c", "b"]


def test_offers_we_cannot_sell_are_left_out_not_the_whole_search():
    broken = offer("bad")
    del broken["slices"][0]["segments"][0]["origin"]["time_zone"]
    flights = search(client([offer("needs_passport", docs=True), broken, offer("ok")]))
    assert [f["duffel_offer_id"] for f in flights] == ["ok"]


def test_without_an_exchange_rate_nothing_is_quoted():
    assert search(client([offer(currency="XYZ")])) == []


def test_the_same_search_is_answered_from_memory():
    calls = []
    c = client(calls=calls)
    search(c); search(c)
    assert len(calls) == 1
    search(c, fresh=True)
    assert len(calls) == 2


def test_the_search_asks_for_the_party_and_a_single_connection():
    calls = []
    search(client(calls=calls), pax=3)
    body = json.loads(calls[0].content)["data"]
    assert body["passengers"] == [{"type": "adult"}] * 3 and body["slices"] == [{"origin": "DEL", "destination": "BOM", "departure_date": "2026-11-02"}]
    assert body["max_connections"] == 1 and "return_offers=true" in str(calls[0].url) and calls[0].headers["authorization"] == "Bearer duffel_test_x"


def test_an_api_error_is_raised_with_its_message_and_code():
    c = client(routes={"POST /air/offer_requests": (422, {"errors": [{"message": "bad airport", "code": "validation_error"}]})})
    with pytest.raises(DuffelError) as err:
        search(c)
    assert "bad airport" in str(err.value) and err.value.code == "validation_error"


# --------------------------------------------------------------------------------------- keeping the quote fresh
def test_refresh_asks_for_the_offer_itself_when_it_is_still_alive():
    c = client(routes={"GET /air/offers/off_1": (200, {"data": offer("off_1", "55.00")})})
    [f] = search(c)
    fresh = asyncio.run(c.refresh(f, 1))
    assert fresh["price_inr"] == 4400 and fresh["duffel_offer_id"] == "off_1"


def test_refresh_searches_again_when_the_offer_has_expired_and_finds_the_flight_by_its_itinerary():
    gone = {"GET /air/offers/off_1": (422, {"errors": [{"message": "gone", "code": "offer_no_longer_available"}]})}
    offers = [offer("off_1", "50.00")]
    c = client(offers, routes=gone)
    [f] = search(c)
    offers[:] = [offer("off_2", "52.00")]  # the airline's new quote for the same flight
    fresh = asyncio.run(c.refresh(f, 1))
    assert fresh["duffel_offer_id"] == "off_2" and fresh["price_inr"] == 4160


def test_refresh_for_a_different_party_size_searches_for_that_many_instead_of_reusing_the_offer():
    calls = []
    offers = [offer("off_1", "50.00")]
    c = client(offers, calls, routes={"GET /air/offers/off_1": (200, {"data": offer("off_1")})})
    [f] = search(c)
    offers[:] = [offer("off_2", "104.00", pax=2)]
    two = asyncio.run(c.refresh(f, 2))
    assert two["offer_pax"] == 2 and two["price_inr"] == 4160 and not any(r.method == "GET" for r in calls)  # $52 each; no GET of the old offer


def test_refresh_returns_none_when_the_flight_is_no_longer_sold():
    offers = [offer("off_1")]
    c = client(offers, routes={"GET /air/offers/off_1": (404, {"errors": [{"message": "not found", "code": "not_found"}]})})
    [f] = search(c)
    offers.clear()
    assert asyncio.run(c.refresh(f, 1)) is None


# --------------------------------------------------------------------------------------------- ticket and cancel
def test_book_sends_the_travellers_and_pays_from_the_balance():
    calls = []
    c = client(calls=calls, routes={"POST /air/orders": (201, {"data": {"id": "ord_9", "booking_reference": "ABC123"}})})
    [f] = search(c, pax=1)
    f["offer_passenger_ids"] = ["pas_0"]
    made = asyncio.run(c.book(f, [{"name": "Aarav Kumar Sharma", "dob": "1992-03-14", "gender": "m"}],
                              {"email": "a@example.com", "phone": "919876543210"}, "XYZ789"))
    assert made == {"order_id": "ord_9", "booking_reference": "ABC123"}
    body = json.loads(calls[-1].content)["data"]
    assert body["type"] == "instant" and body["selected_offers"] == ["off_1"]
    assert body["payments"] == [{"type": "balance", "currency": "USD", "amount": "50.00"}]
    assert body["passengers"] == [{"id": "pas_0", "title": "mr", "gender": "m", "given_name": "Aarav Kumar", "family_name": "Sharma",
                                   "born_on": "1992-03-14", "email": "a@example.com", "phone_number": "+919876543210"}]


def test_book_refuses_a_party_that_does_not_match_the_offer():
    c = client()
    [f] = search(c, pax=1)
    with pytest.raises(DuffelError):
        asyncio.run(c.book(f, [{"name": "A B", "dob": "1990-01-01", "gender": "f"}] * 2, {"email": "a@b.co", "phone": "9198"}, "R"))


def test_cancelling_asks_for_a_quote_then_confirms_it():
    calls = []
    c = client(calls=calls, routes={"POST /air/order_cancellations": (201, {"data": {"id": "ore_1", "refund_amount": "42.50", "refund_currency": "USD"}}),
                                    "POST /air/order_cancellations/ore_1/actions/confirm": (200, {"data": {"id": "ore_1"}})})
    quote = asyncio.run(c.cancel_quote("ord_9"))
    assert quote == {"quote_id": "ore_1", "refund_amount": 42.5, "currency": "USD"}
    asyncio.run(c.cancel("ore_1"))
    assert [r.url.path for r in calls] == ["/air/order_cancellations", "/air/order_cancellations/ore_1/actions/confirm"]


def test_small_helpers():
    assert minutes_of("PT2H35M") == 155 and minutes_of("PT45M") == 45 and minutes_of("P1DT2H") == 1560 and minutes_of(None) is None
    assert utc_of("2026-11-02T09:30:00", "Asia/Kolkata").isoformat() == "2026-11-02T04:00:00+00:00"
    assert DuffelClient.configured() is True
