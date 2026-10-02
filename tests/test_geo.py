import asyncio
from datetime import timedelta

import httpx
import pytest

from app.agents.buddy import trip as T
from app.core import geo
from app.core.utils import IST, now_ist
from app.services import geo_service
from app.services.geo_service import GeoService, clean_text, overpass_query, parse_overpass
from app.services.whatsapp_service import WhatsAppService


@pytest.fixture(autouse=True)
def _no_throttle_no_cache(monkeypatch):
    monkeypatch.setattr(geo_service, "NOMINATIM_GAP_S", 0)
    geo_service._cache.clear()


# ----------------------------------------------------------------------- pure helpers
def test_distance_links_and_cities():
    assert geo.haversine_m(22.7196, 75.8577, 22.7196, 75.8577) == 0
    assert 5700 < geo.haversine_m(22.7196, 75.8577, 22.7218, 75.8011) < 5900  # straight line, city centre to Indore airport
    assert (geo.fmt_dist(350), geo.fmt_dist(1200), geo.fmt_dist(9999)) == ("350 m", "1.2 km", "10.0 km")
    assert geo.maps_link(22.7218, 75.8011) == "https://www.google.com/maps/dir/?api=1&destination=22.7218,75.8011&travelmode=driving"
    assert "destination=Hotel+Raj%2C+Goa" in geo.maps_link(query="Hotel Raj, Goa")
    assert geo.nearest_city(22.75, 75.89) == "IDR" and geo.nearest_city(27.0, 70.0) is None


def test_a_shared_location_is_fresh_for_a_few_hours_only():
    ctx = {}
    assert geo.fresh_location(ctx) is None
    loc = geo.save_location(ctx, 22.7, 75.8, "Home")
    assert geo.fresh_location(ctx) == loc and geo.location_age_min(loc) == 0
    ctx["loc"]["at"] = (now_ist() - timedelta(hours=4)).isoformat()
    assert geo.fresh_location(ctx) is None


def test_place_words_map_to_categories():
    assert geo.category_for("any good chai nearby?") == "cafe" and geo.category_for("nearest ATM please") == "atm"
    assert geo.category_for("biryani") is None


# ---------------------------------------------------------------------- overpass parsing
def test_overpass_text_cannot_inject_query_syntax():
    assert clean_text('x"]; out;') == "x out"
    q = overpass_query(22.7, 75.8, 2000, [("amenity", "cafe")], 'x"]; node(1);')
    assert '"name"~"x node1"' in q and '"]; node' not in q


def test_parse_overpass_sorts_by_distance_and_skips_unnamed_or_duplicates():
    data = {"elements": [
        {"type": "node", "lat": 22.7300, "lon": 75.8600, "tags": {"name": "Far Cafe", "amenity": "cafe"}},
        {"type": "way", "center": {"lat": 22.7200, "lon": 75.8580}, "tags": {"name": "Near Cafe", "cuisine": "coffee_shop;dessert", "opening_hours": "Mo-Su 09:00-22:00"}},
        {"type": "node", "lat": 22.7200, "lon": 75.8580, "tags": {"amenity": "cafe"}},                       # no name
        {"type": "node", "lat": 22.7200, "lon": 75.8580, "tags": {"name": "Near Cafe", "amenity": "cafe"}},  # duplicate
        {"type": "relation", "tags": {"name": "No position"}}]}
    places = parse_overpass(data, (22.7196, 75.8577), 9)
    assert [p["name"] for p in places] == ["Near Cafe", "Far Cafe"]
    assert places[0]["info"] == "coffee shop, dessert" and places[0]["hours"] == "Mo-Su 09:00-22:00" and places[0]["dist_m"] < 100


# --------------------------------------------------------------------------- the service
def service(handler):
    return GeoService(transport=httpx.MockTransport(handler))


def test_reverse_geocode_gives_a_short_label_and_is_cached():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"address": {"neighbourhood": "Martand Chowk", "city": "Indore", "state": "Madhya Pradesh"}})

    geo_svc = service(handler)
    assert asyncio.run(geo_svc.reverse(22.7196, 75.8577)) == "Martand Chowk, Indore"
    assert asyncio.run(geo_svc.reverse(22.7196, 75.8577)) == "Martand Chowk, Indore" and len(calls) == 1


def test_geocode_finds_a_typed_place():
    def handler(request):
        assert request.url.params["q"] == "Vijay Nagar Indore"
        return httpx.Response(200, json=[{"lat": "22.7533", "lon": "75.8937", "name": "Vijay Nagar", "display_name": "Vijay Nagar, Indore"}])

    assert asyncio.run(service(handler).geocode("  Vijay   Nagar Indore ")) == {"lat": 22.7533, "lon": 75.8937, "name": "Vijay Nagar"}
    assert asyncio.run(service(lambda r: httpx.Response(200, json=[])).geocode("zzzz")) is None


def test_places_come_from_overpass():
    def handler(request):
        assert "overpass" in request.url.host and 'amenity"="cafe"' in request.content.decode().replace("%22", '"').replace("%3D", "=")
        return httpx.Response(200, json={"elements": [{"type": "node", "lat": 22.7224, "lon": 75.8514, "tags": {"name": "Joshi Coffee House", "amenity": "cafe"}}]})

    places = asyncio.run(service(handler).places(22.7196, 75.8577, category="cafe"))
    assert [p["name"] for p in places] == ["Joshi Coffee House"] and places[0]["dist_m"] > 0


def test_places_fall_back_to_nominatim_when_overpass_is_down():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if "overpass" in request.url.host:
            return httpx.Response(504, text="busy")
        assert request.url.params["bounded"] == "1" and request.url.params["q"] == "cafe"
        return httpx.Response(200, json=[{"lat": "22.7035", "lon": "75.8428", "name": "Cafe Kava", "display_name": "Cafe Kava, Indore", "type": "cafe"}])

    places = asyncio.run(service(handler).places(22.7196, 75.8577, category="cafe"))
    assert [p["name"] for p in places] == ["Cafe Kava"] and seen[-1] == "nominatim.openstreetmap.org" and len(seen) == 3


def test_route_and_failures():
    def ok(request):
        assert "75.8577,22.7196;75.8011,22.7218" in str(request.url)
        return httpx.Response(200, json={"routes": [{"distance": 8985.9, "duration": 935.4}]})

    assert asyncio.run(service(ok).route((22.7196, 75.8577), (22.7218, 75.8011))) == {"km": 9.0, "minutes": 16}
    assert asyncio.run(service(lambda r: httpx.Response(500)).route((1, 1), (2, 2))) is None
    assert asyncio.run(service(lambda r: httpx.Response(500)).reverse(1, 1)) is None


# ----------------------------------------------------------------------- WhatsApp location
def payload(message):
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {}, "contacts": [{"profile": {"name": "Aarav"}, "wa_id": "919876543210"}],
        "messages": [{"from": "919876543210", "id": "wamid.1", "timestamp": "1", **message}]}}]}]}


def test_incoming_location_pin_is_parsed():
    msg = WhatsAppService.extract_message_data(payload({"type": "location", "location": {"latitude": 22.7196, "longitude": 75.8577, "name": "Home"}}))
    assert msg["location"] == {"lat": 22.7196, "lon": 75.8577, "name": "Home", "address": ""} and msg["text"] == "" and msg["reply_id"] is None
    assert WhatsAppService.extract_message_data(payload({"type": "location", "location": {"latitude": 123, "longitude": 75}})) is None
    assert WhatsAppService.extract_message_data(payload({"type": "text", "text": {"body": "hi"}}))["location"] is None


def test_location_request_message_is_sent_as_an_interactive_button(monkeypatch):
    sent = {}

    async def fake_post(to, body):
        sent.update(to=to, body=body)

    monkeypatch.setattr(WhatsAppService, "_post", staticmethod(fake_post))
    asyncio.run(WhatsAppService.send("919876543210", {"type": "location_request", "body": "Share please"}))
    assert sent["body"] == {"type": "interactive", "interactive": {"type": "location_request_message", "body": {"text": "Share please"},
                                                                  "action": {"name": "send_location"}}}


# ------------------------------------------------------------------------- trip timing
def flight(dep="2026-10-02T09:00:00+05:30", frm="IDR", to="BOM", status="scheduled"):
    return {"id": "f1", "flight_no": "6E-1000", "from_code": frm, "to_code": to, "departure_time": dep, "arrival_time": "2026-10-02T10:30:00+05:30",
            "status": status, "class": "Economy", "baggage_kg": 15}


def test_leave_time_counts_the_airport_buffer_drive_and_spare_time():
    assert T.leave_by(flight(), 18).strftime("%H:%M") == "06:27"                       # 2h + 18 min + 15 min before 09:00
    assert T.leave_by(flight(frm="BOM", to="DXB"), 18).strftime("%H:%M") == "05:27"    # 3h buffer for international


def test_trip_facts_for_the_model():
    now = now_ist().replace(year=2026, month=10, day=2, hour=5, minute=30, second=0, microsecond=0)
    booking = {"pnr": "ABC123", "passenger_name": "Aarav Sharma", "flights": flight(status="delayed")}
    info = T.describe(now, [booking], None, {"at": now.isoformat()}, "Martand Chowk, Indore", {"km": 9.0, "minutes": 18})
    assert info.flight is booking
    for expect in ("Flight 6E-1000 Indore to Mumbai", "status: delayed", "PNR ABC123", "Reach the airport by 07:00", "Suggested time to leave for the airport: 06:27",
                   "near Martand Chowk, Indore", "9.0 km and 18 min"):
        assert expect in info.text, expect
    assert "No upcoming flight" in T.describe(now, [], None, None, None, None).text
    assert "not shared" in T.describe(now, [], None, None, None, None).text
    assert T.fmt_delta(timedelta(minutes=80)) == "in 1h 20m" and T.fmt_delta(timedelta(minutes=-25)) == "25m ago"


# ------------------------------------------------------------------------- photos in messages
def _capture(monkeypatch, fail_when=lambda payload: False, uploadable=lambda url: True):
    sent = []

    async def fake_post(to, body):
        sent.append(body)
        return not fail_when(body)

    async def fake_media_id(url):
        return "media-" + url.rsplit("/", 1)[-1] if uploadable(url) else None

    monkeypatch.setattr(WhatsAppService, "_post", staticmethod(fake_post))
    monkeypatch.setattr(WhatsAppService, "_media_id", staticmethod(fake_media_id))
    return sent


def test_image_message_goes_out_as_an_uploaded_photo_with_its_caption(monkeypatch):
    sent = _capture(monkeypatch)
    asyncio.run(WhatsAppService.send("91987", {"type": "image", "url": "https://x.test/a.jpg", "body": "Nice hotel"}))
    assert sent == [{"type": "image", "image": {"id": "media-a.jpg", "caption": "Nice hotel"}}]


def test_buttons_and_cta_carry_the_photo_as_a_header(monkeypatch):
    sent = _capture(monkeypatch)
    asyncio.run(WhatsAppService.send("91987", {"type": "buttons", "body": "Hi", "buttons": [("a", "A")], "image": "https://x.test/a.jpg"}))
    asyncio.run(WhatsAppService.send("91987", {"type": "cta", "body": "Pay", "button_text": "Pay", "url": "https://p.test", "image": "https://x.test/a.jpg"}))
    for payload in sent:
        assert payload["interactive"]["header"] == {"type": "image", "image": {"id": "media-a.jpg"}}
    asyncio.run(WhatsAppService.send("91987", {"type": "buttons", "body": "Hi", "buttons": [("a", "A")]}))
    assert "header" not in sent[-1]["interactive"]


def test_a_photo_that_cannot_be_uploaded_never_costs_the_user_the_message(monkeypatch):
    sent = _capture(monkeypatch, uploadable=lambda url: False)
    asyncio.run(WhatsAppService.send("91987", {"type": "image", "url": "https://dead.test/a.jpg", "body": "Hotel card"}))
    asyncio.run(WhatsAppService.send("91987", {"type": "buttons", "body": "Your stay", "buttons": [("a", "A")], "image": "https://dead.test/a.jpg"}))
    assert sent[0] == {"type": "text", "text": {"body": "Hotel card"}}
    assert sent[1]["interactive"]["type"] == "button" and "header" not in sent[1]["interactive"]


def test_the_photo_is_uploaded_to_whatsapp_once_and_reused(monkeypatch):
    import httpx
    from app.services import whatsapp_service as ws
    from app.core.config import settings
    ws._media_ids.clear()
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "123")
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, content=b"JPEGDATA", headers={"content-type": "image/jpeg"})
        return httpx.Response(200, json={"id": "MEDIA1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(ws.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert asyncio.run(WhatsAppService._media_id("https://x.test/h.jpg")) == "MEDIA1"
    assert asyncio.run(WhatsAppService._media_id("https://x.test/h.jpg")) == "MEDIA1"
    assert calls == [("GET", "/h.jpg"), ("POST", "/v19.0/123/media")]


def test_something_that_is_not_a_photo_is_not_uploaded(monkeypatch):
    import httpx
    from app.services import whatsapp_service as ws
    from app.core.config import settings
    ws._media_ids.clear()
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "123")
    real = httpx.AsyncClient
    html = lambda request: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    monkeypatch.setattr(ws.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(html), **kw))
    assert asyncio.run(WhatsAppService._media_id("https://x.test/blocked")) is None
