"""Duffel: live flight offers, tickets and cancellations for any route in the world.

A search returns offers (priced in the airline's currency, usually USD). They are turned into our own flight shape with the fare
in rupees per traveller, and kept for a few minutes: the same route is searched many times while a traveller compares flights.
An offer is only a quote. It expires, so before taking money we ask for it again (`refresh`), and a ticket (`book`) is only
issued after the traveller has paid us. Airport times arrive as local clock times; they are converted to UTC here."""
import logging
import math
import re
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.services.hotelbeds import split_name

logger = logging.getLogger(__name__)
CACHE_S = 10 * 60
MAX_OFFERS = 40
SUPPLIER_TIMEOUT_MS = 20_000
DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?")
GONE = ("offer_no_longer_available", "offer_expired", "price_changed")  # Duffel error codes: the quote cannot be used any more


class DuffelError(Exception):
    """Duffel said no (offer gone, bad passenger data, airline refused...). `str(error)` is its message; `.code` its error code."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def minutes_of(iso: str | None) -> int | None:
    """"PT2H35M" -> 155."""
    m = DURATION.fullmatch(iso or "")
    return int(m[1] or 0) * 1440 + int(m[2] or 0) * 60 + int(m[3] or 0) if m and any(m.groups()) else None


def utc_of(local: str, zone: str) -> datetime:
    """A local clock time at an airport ("2026-11-02T09:30:00", "Asia/Kolkata") as an aware UTC datetime."""
    return datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)


def baggage_of(passenger: dict) -> int:
    return sum(int(b.get("quantity") or 0) for b in passenger.get("baggages") or [] if b.get("type") == "checked")


class DuffelClient:
    def __init__(self, rates, transport: httpx.AsyncBaseTransport | None = None):
        self.rates = rates           # RateService: turns the quoted currency into INR
        self._transport = transport  # tests pass httpx.MockTransport
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}

    @staticmethod
    def configured() -> bool:
        return bool(settings.DUFFEL_API_TOKEN)

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.DUFFEL_API_TOKEN}", "Duffel-Version": "v2", "Accept": "application/json",
                "Content-Type": "application/json"}

    async def _call(self, method: str, path: str, timeout: float = 30, **kw) -> dict:
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as http:
            resp = await http.request(method, f"{settings.DUFFEL_BASE_URL}{path}", headers=self._headers(), **kw)
        if resp.status_code >= 400:
            try:
                err = resp.json()["errors"][0]
                raise DuffelError(err.get("message") or err.get("title") or resp.text[:200], err.get("code", ""))
            except (KeyError, IndexError, ValueError):
                raise DuffelError(f"{resp.status_code}: {resp.text[:200]}") from None
        return resp.json()["data"]

    # ---- search
    async def search(self, origin: str, destination: str, day: date, pax: int = 1, fresh: bool = False) -> list[dict]:
        """Flights on this day (the airports' local date), cheapest first, in our own shape (see `_normalize`).
        Raises DuffelError or a network error if Duffel could not be asked."""
        key = (origin, destination, day, pax)
        hit = self._cache.get(key)
        if hit and not fresh and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        body = {"data": {"slices": [{"origin": origin, "destination": destination, "departure_date": day.isoformat()}],
                         "passengers": [{"type": "adult"}] * pax, "cabin_class": "economy", "max_connections": 1}}
        data = await self._call("POST", f"/air/offer_requests?return_offers=true&supplier_timeout={SUPPLIER_TIMEOUT_MS}",
                                timeout=SUPPLIER_TIMEOUT_MS / 1000 + 15, json=body)
        flights = await self._normalize_all(data.get("offers") or [])
        self._cache[key] = (time.monotonic(), flights)
        return flights

    async def _normalize_all(self, offers: list[dict]) -> list[dict]:
        best: dict[str, dict] = {}  # itinerary -> its cheapest offer (an airline sells the same flight at several fares)
        for offer in offers:
            try:
                flight = await self._normalize(offer)
            except Exception as exc:
                logger.warning("Skipping a Duffel offer (%s): %s", offer.get("id"), exc)
                continue
            if flight and (flight["itinerary_key"] not in best or flight["price_inr"] < best[flight["itinerary_key"]]["price_inr"]):
                best[flight["itinerary_key"]] = flight
        return sorted(best.values(), key=lambda f: (f["price_inr"], f["departure_time"]))[:MAX_OFFERS]

    async def _normalize(self, offer: dict) -> dict | None:
        """One Duffel offer in our flight shape, or None if we cannot sell it (it needs passport details we do not collect)."""
        if offer.get("passenger_identity_documents_required"):
            return None
        currency = offer["total_currency"].upper()
        per_inr = 1.0 if currency == "INR" else await self.rates.inr_per_unit(currency)
        if not per_inr:  # no exchange rate: better to skip than to quote a made-up price
            raise DuffelError(f"no INR rate for {currency}")
        pax = len(offer["passengers"])
        total = float(offer["total_amount"])
        segments = offer["slices"][0]["segments"]
        first, last = segments[0], segments[-1]
        dep, arr = utc_of(first["departing_at"], first["origin"]["time_zone"]), utc_of(last["arriving_at"], last["destination"]["time_zone"])
        duration = minutes_of(offer["slices"][0].get("duration")) or max(1, int((arr - dep).total_seconds() // 60))
        refund = ((offer.get("conditions") or {}).get("refund_before_departure") or {})
        carrier = first.get("marketing_carrier") or {}
        passenger = (first.get("passengers") or [{}])[0]
        return {
            "airline": offer["owner"]["name"], "flight_no": f"{carrier.get('iata_code', '')}-{first.get('marketing_carrier_flight_number', '')}",
            "from_code": first["origin"]["iata_code"], "to_code": last["destination"]["iata_code"],
            "departure_time": dep.isoformat(), "arrival_time": arr.isoformat(), "duration_min": duration,
            "price_inr": math.ceil(total * per_inr * (1 + settings.DUFFEL_MARKUP_PCT / 100) / pax),  # per traveller
            "class": (passenger.get("cabin_class_marketing_name") or passenger.get("cabin_class") or "Economy").replace("_", " ").title(),
            "baggage_kg": 0, "checked_bags": baggage_of(passenger), "stops": len(segments) - 1,
            "refundable": bool(refund.get("allowed")), "status": "scheduled", "seats_left": 9,
            "duffel_offer_id": offer["id"], "offer_expires_at": offer["expires_at"],
            "offer_passenger_ids": [p["id"] for p in offer["passengers"]], "offer_pax": pax,
            "offer_total": total, "offer_currency": currency,
            "itinerary_key": "|".join(f"{s['marketing_carrier']['iata_code']}-{s['marketing_carrier_flight_number']}@{s['departing_at']}"
                                      for s in segments),
        }

    # ---- keep the quote fresh
    async def refresh(self, flight: dict, pax: int) -> dict | None:
        """The same flights, priced right now for `pax` travellers (our shape, with a new offer id), or None if they are gone.
        Asks for the offer itself when it is still alive for that many travellers, else searches again and finds the flights."""
        if flight.get("duffel_offer_id") and flight.get("offer_pax") == pax:
            try:
                return await self._normalize(await self._call("GET", f"/air/offers/{flight['duffel_offer_id']}"))
            except DuffelError as exc:
                if exc.code not in GONE and "not found" not in str(exc).lower():
                    raise
        key = flight.get("itinerary_key") or ""
        try:
            day = date.fromisoformat(key.split("@")[1][:10])
        except IndexError:
            return None
        found = [f for f in await self.search(flight["from_code"], flight["to_code"], day, pax, fresh=True) if f["itinerary_key"] == key]
        return found[0] if found else None

    # ---- ticket
    async def book(self, flight: dict, travellers: list[dict], contact: dict, reference: str) -> dict:
        """Issue the ticket, paying Duffel from our balance. `travellers` is [{name, dob, gender}] in the order of the offer's
        passengers. Returns {order_id, booking_reference}. Raises DuffelError if the airline would not issue it."""
        if len(travellers) != flight["offer_pax"]:
            raise DuffelError("the offer is for a different number of travellers")
        passengers = []
        for pid, t in zip(flight["offer_passenger_ids"], travellers):
            given, family = split_name(t["name"])
            passengers.append({"id": pid, "title": "mr" if t["gender"] == "m" else "ms", "gender": t["gender"], "given_name": given,
                               "family_name": family, "born_on": t["dob"], "email": contact["email"],
                               "phone_number": "+" + contact["phone"].lstrip("+")})
        body = {"data": {"type": "instant", "selected_offers": [flight["duffel_offer_id"]], "passengers": passengers,
                         "payments": [{"type": "balance", "currency": flight["offer_currency"], "amount": f"{flight['offer_total']:.2f}"}],
                         "metadata": {"crew_ref": reference[:50]}}}
        order = await self._call("POST", "/air/orders", timeout=60, json=body)
        return {"order_id": order["id"], "booking_reference": order.get("booking_reference") or ""}

    async def cancel_quote(self, order_id: str) -> dict:
        """What the airline gives back if this ticket is cancelled now: {quote_id, refund_amount, currency}. Raises DuffelError
        if the ticket cannot be cancelled."""
        quote = await self._call("POST", "/air/order_cancellations", json={"data": {"order_id": order_id}})
        return {"quote_id": quote["id"], "refund_amount": float(quote.get("refund_amount") or 0), "currency": quote.get("refund_currency") or ""}

    async def cancel(self, quote_id: str) -> None:
        await self._call("POST", f"/air/order_cancellations/{quote_id}/actions/confirm")

