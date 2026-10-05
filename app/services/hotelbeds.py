"""Hotelbeds APItude: live hotel availability and rates for a city and dates.

Every call is signed with SHA-256(api key + secret + unix seconds). Results are cached for a few minutes: the same stay is
searched many times while a user picks a hotel and a room, and test keys only allow a small number of calls a day.
Prices come back in the hotel's currency (usually EUR) and are turned into rupees per night here.
Photos, amenities and the description come from the Content API, fetched once per hotel and kept in memory."""
import hashlib
import logging
import math
import re
import time
from datetime import date

import httpx

from app.core.config import settings
from app.core.places import AIRPORT_COORDS

logger = logging.getLogger(__name__)
CACHE_S = 10 * 60
MAX_HOTELS = 60
PHOTO_URL = "https://photos.hotelbeds.com/giata/bigger/{}"
FACILITY_GROUPS = {60, 70, 73, 80, 90}  # room, hotel, pool, catering and business facilities
AMENITIES = {  # our amenity name -> words in a Hotelbeds facility description
    "Pool": re.compile(r"\bpool\b"), "Gym": re.compile(r"\bgym\b|fitness"), "Free WiFi": re.compile(r"wi-?fi|wireless"),
    "Parking": re.compile(r"parking"), "Spa": re.compile(r"\bspa\b|wellness"), "Restaurant": re.compile(r"restaurant"),
    "Bar": re.compile(r"\bbar\b"), "Airport shuttle": re.compile(r"airport shuttle"),
}
SUITE = re.compile(r"\bsuites?\b")
DELUXE = re.compile(r"\b(deluxe|superior|premium|executive|club|junior|family|comfort|luxury)\b")


def room_type(name: str) -> str:
    low = name.lower()
    return "Suite" if SUITE.search(low) else "Deluxe" if DELUXE.search(low) else "Standard"


def bed_of(name: str) -> str:
    low = name.lower()
    for word, bed in (("twin", "Twin beds"), ("triple", "3 beds"), ("quad", "4 beds"), ("double", "Double bed"),
                      ("king", "King bed"), ("queen", "Queen bed"), ("single", "Single bed")):
        if word in low:
            return bed
    return "Hotel room"


def stars_of(category_code: str) -> int:
    m = re.match(r"(\d)", category_code or "")
    return min(5, max(1, int(m.group(1)))) if m else 3


class HotelbedsError(Exception):
    """Hotelbeds said no (rate gone, sold out, invalid request...). `str(error)` is its message."""


def split_name(full: str) -> tuple[str, str]:
    """"Aarav Kumar Sharma" -> ("Aarav Kumar", "Sharma"); a single word is used for both."""
    words = re.sub(r"[^A-Za-z .'\-]", "", full).split()
    return (" ".join(words[:-1]), words[-1]) if len(words) > 1 else ((words[0], words[0]) if words else ("Guest", "Guest"))


class HotelbedsClient:
    def __init__(self, rates, transport: httpx.AsyncBaseTransport | None = None):
        self.rates = rates          # RateService: turns the quoted currency into INR
        self._transport = transport  # tests pass httpx.MockTransport
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._content: dict[int, dict] = {}       # hotel code -> {image_url, amenities, description}
        self._facilities: dict[tuple, str] | None = None  # (group, code) -> lowercase description

    @staticmethod
    def configured() -> bool:
        return bool(settings.HOTELBEDS_API_KEY and settings.HOTELBEDS_SECRET)

    @staticmethod
    def _headers() -> dict[str, str]:
        raw = f"{settings.HOTELBEDS_API_KEY}{settings.HOTELBEDS_SECRET}{int(time.time())}"
        return {"Api-key": settings.HOTELBEDS_API_KEY, "X-Signature": hashlib.sha256(raw.encode()).hexdigest(),
                "Accept": "application/json", "Content-Type": "application/json"}

    async def _call(self, method: str, path: str, **kw) -> dict:
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as http:
            resp = await http.request(method, f"{settings.HOTELBEDS_BASE_URL}/hotel-api/1.0/{path}", headers=self._headers(), **kw)
        if resp.status_code >= 400:
            try:
                message = resp.json()["error"]["message"]
            except Exception:
                message = resp.text[:200]
            raise HotelbedsError(f"{resp.status_code}: {message}")
        return resp.json()

    async def search(self, city_code: str, check_in: date, check_out: date, guests: int) -> list[dict]:
        """Hotels with a free room for these nights, in our own shape:
        {hb_code, name, area, stars, rating, rooms: [{room_type, bed, max_guests, price_inr, left, rate_key}]}.
        Rooms are the cheapest offer per room type, per night in INR. Raises on a network or API error."""
        key = (city_code, check_in, check_out, guests)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        lat_lon = AIRPORT_COORDS.get(city_code)
        if not lat_lon:
            return []
        body = {
            "stay": {"checkIn": check_in.isoformat(), "checkOut": check_out.isoformat()},
            "occupancies": [{"rooms": 1, "adults": guests, "children": 0}],
            "geolocation": {"latitude": lat_lon[0], "longitude": lat_lon[1], "radius": settings.HOTELBEDS_RADIUS_KM, "unit": "km"},
            "filter": {"maxHotels": MAX_HOTELS},
        }
        raw = ((await self._call("POST", "hotels", json=body)).get("hotels") or {}).get("hotels") or []
        hotels = await self._normalize(raw, (check_out - check_in).days, guests)
        await self._add_content(hotels)
        self._cache[key] = (time.monotonic(), hotels)
        return hotels

    async def _normalize(self, raw: list[dict], nights: int, guests: int) -> list[dict]:
        out = []
        for h in raw:
            per_inr = None
            if (cur := (h.get("currency") or "EUR").upper()) == "INR":
                per_inr = 1.0
            else:
                per_inr = await self.rates.inr_per_unit(cur)
            if not per_inr:  # no exchange rate: better to skip than to quote a made-up price
                logger.warning("No INR rate for %s, skipping Hotelbeds hotel %s", cur, h.get("code"))
                continue
            rooms: dict[str, dict] = {}
            for room in h.get("rooms") or []:
                for rate in room.get("rates") or []:
                    night = math.ceil(float(rate["net"]) * per_inr * (1 + settings.HOTELBEDS_MARKUP_PCT / 100) / nights)
                    kind = room_type(room.get("name", ""))
                    if night > 0 and (kind not in rooms or night < rooms[kind]["price_inr"]):
                        rooms[kind] = {"room_type": kind, "bed": bed_of(room.get("name", "")), "max_guests": min(6, guests),
                                       "price_inr": night, "left": max(1, min(int(rate.get("allotment") or 1), 9)),
                                       "rate_key": rate.get("rateKey")}
            if rooms:
                review = next((r for r in h.get("reviews") or [] if r.get("rate")), None)
                out.append({"hb_code": h["code"], "name": str(h["name"]).title()[:60],
                            "area": str(h.get("zoneName") or h.get("destinationName") or "").title(),
                            "stars": stars_of(h.get("categoryCode", "")), "rating": round(float(review["rate"]), 1) if review else None,
                            "rooms": sorted(rooms.values(), key=lambda r: r["price_inr"])})
        return out

    # ---- booking
    async def book(self, rate_key: str, guest_name: str, guests: int, reference: str) -> str:
        """Check the rate is still valid, then book one room for these guests. Returns the Hotelbeds booking reference.
        Raises HotelbedsError (or a network error) if the room could not be booked; nothing is held then."""
        try:
            checked = await self._call("POST", "checkrates", json={"rooms": [{"rateKey": rate_key}]})
            rate_key = checked["hotel"]["rooms"][0]["rates"][0]["rateKey"]
            name, surname = split_name(guest_name)
            paxes = [{"roomId": 1, "type": "AD", "name": name if i == 0 else "Guest", "surname": surname} for i in range(guests)]
            booking = (await self._call("POST", "bookings", json={
                "holder": {"name": name, "surname": surname}, "clientReference": reference[:20], "tolerance": 2.0,
                "rooms": [{"rateKey": rate_key, "paxes": paxes}]}))["booking"]
            if booking.get("status") != "CONFIRMED":
                raise HotelbedsError(f"booking came back {booking.get('status')}")
            return booking["reference"]
        except Exception:
            self._cache.clear()  # the rates we showed are stale: the next search must ask again
            raise

    async def cancel(self, reference: str) -> None:
        """Cancel a booking at Hotelbeds. Raises if it could not be cancelled (already cancelled counts as done)."""
        try:
            await self._call("DELETE", f"bookings/{reference}", params={"cancellationFlag": "CANCELLATION"})
        except HotelbedsError as exc:
            if "already" not in str(exc).lower() or "cancel" not in str(exc).lower():
                raise

    # ---- Content API: photo, amenities, description
    async def _get(self, http: httpx.AsyncClient, path: str, **params) -> dict:
        resp = await http.get(f"{settings.HOTELBEDS_BASE_URL}/hotel-content-api/1.0/{path}", params=params, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def _add_content(self, hotels: list[dict]) -> None:
        """Attach image_url, amenities and description to each hotel. Hotels we cannot get content for are left as they
        are (the search still works, the card just has the stock photo and no extras)."""
        missing = sorted({h["hb_code"] for h in hotels} - self._content.keys())
        try:
            async with httpx.AsyncClient(timeout=25, transport=self._transport) as http:
                if missing and self._facilities is None:
                    types = await self._get(http, "types/facilities", fields="all", language="ENG", **{"from": 1, "to": 1000})
                    self._facilities = {(f["facilityGroupCode"], f["code"]): f["description"]["content"].lower()
                                        for f in types.get("facilities") or []}
                for i in range(0, len(missing), 100):  # the API returns up to 100 hotels a call
                    batch = missing[i:i + 100]
                    data = await self._get(http, "hotels", codes=",".join(map(str, batch)), fields="all", language="ENG",
                                           **{"from": 1, "to": len(batch)})
                    for h in data.get("hotels") or []:
                        self._content[int(h["code"])] = self._content_of(h)
        except Exception as exc:
            logger.warning("Hotelbeds content failed: %s", type(exc).__name__)
        for h in hotels:
            h.update(self._content.get(h["hb_code"], {}))

    def _content_of(self, h: dict) -> dict:
        images = sorted(h.get("images") or [], key=lambda i: (i.get("imageTypeCode") != "GEN", i.get("visualOrder", 999)))
        have = {name for f in h.get("facilities") or [] if f.get("facilityGroupCode") in FACILITY_GROUPS and not f.get("indFee")
                for name, words in AMENITIES.items() if words.search((self._facilities or {}).get((f["facilityGroupCode"], f["facilityCode"]), ""))}
        out = {"amenities": [a for a in AMENITIES if a in have]}
        if images:
            out["image_url"] = PHOTO_URL.format(images[0]["path"])
        if text := ((h.get("description") or {}).get("content") or "").strip():
            out["description"] = short(text)
        return out


def short(text: str, limit: int = 220) -> str:
    """The first sentences of a description, cut at a full stop so it reads as a whole."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[:cut.rfind(". ") + 1] if ". " in cut else cut.rsplit(" ", 1)[0] + "…"
