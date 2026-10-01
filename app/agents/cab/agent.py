"""Cab agent: book airport transfers and city rides, see and cancel rides.

Button ids: cab:*, ccity:, cpick:, cdrop:, cwhen:, cveh:, ccfm:, cride:, ccancel:, ccancely:
Working state lives in ctx["cab"] so it never collides with other agents.
"""
import asyncio
from datetime import date, datetime, timedelta

from app.agents.base import Agent, Session
from app.agents.cab.pricing import distance_km, eta_min, quote
from app.core.messages import buttons_msg, list_msg, reaction_msg, text_msg
from app.core.places import CITIES, city, country_of
from app.core.utils import IST, inr, now_ist, parse_time_text, to_ist

VEHICLES = ["Mini", "Sedan", "SUV", "Luxury"]
KIND_LABEL = {"airport": "✈️ Airport", "railway": "🚆 Railway station", "landmark": "🏛️ Landmark",
              "mall": "🛍️ Mall", "area": "📍 Area"}
CANCEL_FREE_BEFORE_MIN = 15
LATE_CANCEL_FEE = 50
CANCEL_GRACE_MIN = 10  # a ride booked 'now' can still be cancelled while the driver is on the way
ARRIVAL_BUFFER_MIN = 20  # time to get off the plane and collect bags


def when_label(dt: datetime) -> str:
    today = now_ist().date()
    if dt <= now_ist() + timedelta(minutes=3):
        return "Now"
    day = "Today" if dt.date() == today else "Tomorrow" if dt.date() == today + timedelta(days=1) else f"{dt:%a, %d %b}"
    return f"{day} · {dt:%H:%M}"


class CabAgent(Agent):
    name = "cab"
    title = "Cabs"
    emoji = "🚕"
    menu_desc = "Airport rides & city cabs"
    owns = frozenset({"cab", "ccity", "cpick", "cdrop", "cwhen", "cveh", "ccfm", "cride", "ccancel", "ccancely"})

    def __init__(self, repo):
        self.repo = repo  # CabRepo

    async def _db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def reset(self, s: Session) -> None:
        s.ctx.pop("cab", None)

    def expects_text(self, s: Session) -> bool:
        return s.step == "awaiting_cab_when"

    # ------------------------------------------------------------------ entry points
    async def on_enter(self, s: Session) -> list[dict]:
        self.reset(s)
        s.step = "cab_menu"
        buttons = [("cab:book", "🚕 Book a Cab"), ("cab:rides", "📋 My Rides")]
        trip, body = s.ctx.get("trip"), "🚕 *Cabs*\nAirport pickups, drops and city rides, at upfront fares."
        if trip and trip.get("arrival") and to_ist(trip["arrival"]) > now_ist():
            arr = to_ist(trip["arrival"])
            body += f"\n\n✈️ You land in *{trip['city']}* on {arr:%a, %d %b} at {arr:%H:%M}. Want a cab waiting for you?"
            buttons.insert(0, ("cab:airport", "✈️ Airport Pickup"))
        return [buttons_msg(body, buttons)]

    async def start(self, s: Session, slots: dict) -> list[dict]:
        """Free text like "cab in Goa": jump straight to choosing the pickup spot."""
        self.reset(s)
        cities = {c["code"] for c in await self._db(self.repo.list_cab_cities)}
        code = next((slots[k] for k in ("to", "from") if slots.get(k) in cities), None)
        if not code:
            return await self.on_enter(s)
        s.ctx["cab"] = {"city": code}
        if hhmm := slots.get("time"):  # "6pm" / "kal shaam 6 baje": remember it so we don't ask again
            today = now_ist()
            day = date.fromisoformat(slots["date"]) if slots.get("date") else today.date()
            when = datetime.combine(day, datetime.min.time(), IST).replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))
            if when < today and not slots.get("date"):
                when += timedelta(days=1)
            s.ctx["cab"]["when_hint"] = when.isoformat()
        return await self._ask_pickup(s)

    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        if not reply_id:
            return await self._on_when_text(s, text) if self.expects_text(s) else await self.on_enter(s)
        kind, _, val = reply_id.partition(":")
        c = s.ctx.setdefault("cab", {})
        if kind == "cab":
            if val == "book":
                return await self._ask_city(s)
            if val == "rides":
                return await self._show_rides(s)
            if val == "airport":
                return await self._airport_pickup(s)
        elif kind == "ccity":
            c.clear()
            c["city"] = val
            return await self._ask_pickup(s)
        elif kind == "cpick" and c.get("city"):
            c["pickup"] = await self._db(self.repo.get_place, val)
            return await self._ask_drop(s)
        elif kind == "cdrop" and c.get("pickup"):
            c["drop"] = await self._db(self.repo.get_place, val)
            if c.get("when_hint"):  # they already told us the time
                c["when"] = c.pop("when_hint")
                return await self._ask_vehicle(s)
            return await self._ask_when(s)
        elif kind == "cwhen" and c.get("drop"):
            return await self._on_when(s, val)
        elif kind == "cveh" and c.get("quotes") and val in c["quotes"]:
            c["vehicle"] = val
            return self._confirm(s)
        elif kind == "ccfm" and c.get("vehicle"):
            if val == "yes":
                return await self._book(s)
            if val == "vehicle":
                return await self._ask_vehicle(s)
            return await self.on_enter(s)
        elif kind == "cride":
            return await self._show_ride(s, val)
        elif kind == "ccancel":
            return await self._ask_cancel(s, val)
        elif kind == "ccancely":
            return await self._do_cancel(s, val)
        return await self.on_enter(s)

    # ---------------------------------------------------------------------- booking steps
    async def _ask_city(self, s: Session) -> list[dict]:
        s.step = "awaiting_cab_city"
        cities = await self._db(self.repo.list_cab_cities)
        rows = [(f"ccity:{c['code']}", c["city"], country_of(c["code"])) for c in cities]
        return [list_msg("🏙️ *Which city do you need the cab in?*", "Choose city", rows, "Cities")]

    async def _ask_pickup(self, s: Session) -> list[dict]:
        s.step = "awaiting_cab_pickup"
        code = s.ctx["cab"]["city"]
        rows = [(f"cpick:{p['id']}", p["name"], KIND_LABEL.get(p["kind"], "")) for p in await self._db(self.repo.list_places, code)]
        return [list_msg(f"📍 *Where should we pick you up in {city(code)}?*", "Pickup spot", rows, "Popular spots")]

    async def _ask_drop(self, s: Session) -> list[dict]:
        s.step = "awaiting_cab_drop"
        c = s.ctx["cab"]
        rows = [(f"cdrop:{p['id']}", p["name"], KIND_LABEL.get(p["kind"], ""))
                for p in await self._db(self.repo.list_places, c["city"]) if p["id"] != c["pickup"]["id"]]
        return [list_msg(f"🏁 *Pickup: {c['pickup']['name']}*\nWhere to?", "Drop spot", rows, "Popular spots")]

    async def _airport_pickup(self, s: Session) -> list[dict]:
        trip = s.ctx.get("trip") or {}
        places = await self._db(self.repo.list_places, trip.get("to", ""))
        airport = next((p for p in places if p["kind"] == "airport"), None)
        if not airport:
            return await self._ask_city(s)
        c = s.ctx["cab"] = {"city": trip["to"], "pickup": airport}
        if trip.get("arrival") and to_ist(trip["arrival"]) > now_ist():
            c["when"] = (to_ist(trip["arrival"]) + timedelta(minutes=ARRIVAL_BUFFER_MIN)).isoformat()
        return await self._ask_drop(s)

    async def _ask_when(self, s: Session) -> list[dict]:
        s.step = "awaiting_cab_when"
        c, now = s.ctx["cab"], now_ist()
        rows = []
        trip = s.ctx.get("trip") or {}
        if trip.get("to") == c["city"] and trip.get("arrival") and to_ist(trip["arrival"]) > now:
            pick = to_ist(trip["arrival"]) + timedelta(minutes=ARRIVAL_BUFFER_MIN)
            rows.append(("cwhen:arrival", "✈️ After my flight lands", f"Pickup at {pick:%H:%M}, we track your landing"))
        rows += [("cwhen:now", "🚕 Now", "Driver arrives in a few minutes"),
                 ("cwhen:30", "In 30 minutes", f"{now + timedelta(minutes=30):%H:%M}"),
                 ("cwhen:60", "In 1 hour", f"{now + timedelta(hours=1):%H:%M}"),
                 ("cwhen:t6", "Tomorrow 6:00 AM", "Early start"), ("cwhen:t9", "Tomorrow 9:00 AM", ""),
                 ("cwhen:custom", "Pick a time…", "Or just type it, like 6pm")]
        route = f"📍 {c['pickup']['name']} ➜ {c['drop']['name']}"
        return [list_msg(f"{route}\n\n🕐 *When do you need the cab?* (you can also type a time, e.g. *7:30pm* or *tomorrow 6am*)",
                         "Choose time", rows, "Pickup time")]

    async def _on_when(self, s: Session, val: str) -> list[dict]:
        c, now = s.ctx["cab"], now_ist()
        trip = s.ctx.get("trip") or {}
        if val == "custom":
            return [text_msg("🕐 Type the pickup time, like *7:30pm*, *tomorrow 6am* or *15/10 9pm*.")]
        when = {"now": now, "30": now + timedelta(minutes=30), "60": now + timedelta(hours=1),
                "t6": (now + timedelta(days=1)).replace(hour=6, minute=0), "t9": (now + timedelta(days=1)).replace(hour=9, minute=0),
                "arrival": to_ist(trip["arrival"]) + timedelta(minutes=ARRIVAL_BUFFER_MIN) if trip.get("arrival") else None}.get(val)
        if when is None:
            return await self._ask_when(s)
        c["when"] = when.replace(second=0, microsecond=0).isoformat()
        return await self._ask_vehicle(s)

    async def _on_when_text(self, s: Session, text: str) -> list[dict]:
        if not s.ctx.get("cab", {}).get("drop"):
            return await self.on_enter(s)
        when = parse_time_text(text, now_ist())
        if when is None:
            return [text_msg("😕 I couldn't read that time. Try *6pm*, *18:30*, *tomorrow 7:30am* or *15/10 9pm* (within 30 days).")]
        s.ctx["cab"]["when"] = when.replace(second=0, microsecond=0).isoformat()
        return await self._ask_vehicle(s)

    async def _ask_vehicle(self, s: Session) -> list[dict]:
        s.step = "awaiting_cab_vehicle"
        c = s.ctx["cab"]
        rates = await self._db(self.repo.get_rates, c["city"])
        when = to_ist(c["when"]) if isinstance(c["when"], str) else c["when"]
        km = distance_km(c["pickup"]["lat"], c["pickup"]["lon"], c["drop"]["lat"], c["drop"]["lon"])
        airport = "airport" in (c["pickup"]["kind"], c["drop"]["kind"])
        c["quotes"] = {v: quote(rates[v], km, when, airport) for v in VEHICLES if v in rates}
        asap = when <= now_ist() + timedelta(minutes=3)
        cheapest = min(q["fare"] for q in c["quotes"].values())
        rows = []
        for v, q in c["quotes"].items():
            r = rates[v]
            tag = "💸 Cheapest · " if q["fare"] == cheapest else "👑 Premium · " if v == "Luxury" else ""
            eta = f"{eta_min(c['city'], v)} min away" if asap else "driver assigned later"
            rows.append((f"cveh:{v}", f"{v} · {inr(q['fare'])}", f"{tag}{r['example_model']} · {r['seats']} seats · {eta}"))
        note = next((q["note"] for q in c["quotes"].values() if q["note"]), "")
        return [list_msg(f"🚘 *Choose your ride*\n📍 {c['pickup']['name']} ➜ {c['drop']['name']}\n"
                         f"🕐 {when_label(when)} · {km} km" + (f"\n⚡ {note}" if note else "") +
                         ("\n✈️ Includes airport fee" if airport else ""), "See cabs", rows, "Available now")]

    def _confirm(self, s: Session) -> list[dict]:
        s.step = "awaiting_cab_confirm"
        c = s.ctx["cab"]
        q = c["quotes"][c["vehicle"]]
        return [buttons_msg(
            f"📝 *Confirm your cab*\n\n📍 {c['pickup']['name']}\n🏁 {c['drop']['name']}\n"
            f"🕐 {when_label(to_ist(c['when']))}\n🚘 {c['vehicle']} · {q['km']} km\n💰 *{inr(q['fare'])}* (pay the driver by cash or UPI)\n\nBook it?",
            [("ccfm:yes", "✅ Confirm"), ("ccfm:vehicle", "🔁 Change cab"), ("ccfm:no", "❌ Cancel")])]

    async def _book(self, s: Session) -> list[dict]:
        c = s.ctx["cab"]
        q = c["quotes"][c["vehicle"]]
        driver = await self._db(self.repo.pick_driver, c["city"], c["vehicle"])
        if driver is None:
            return [buttons_msg(f"😕 No {c['vehicle']} cabs are free right now. Try another type?",
                                [("ccfm:vehicle", "🔁 Change cab"), ("nav:menu", "🏠 Menu")])]
        ride = await self._db(self.repo.create_ride, s.user["id"], c["city"], c["pickup"]["id"], c["drop"]["id"],
                              c["when"], c["vehicle"], q["km"], q["fare"], driver["id"])
        self.reset(s)
        s.step = "menu"
        when = to_ist(ride["pickup_time"])
        asap = when <= now_ist() + timedelta(minutes=3)
        arrive = f"🕐 Arriving in ~{eta_min(ride['city_code'], ride['vehicle_type'])} min" if asap else f"🕐 Pickup: {when_label(when)}"
        ticket = (f"🎉 *Cab Confirmed!*\n━━━━━━━━━━━━━━━\n🎫 Ref: *{ride['ref']}*\n"
                  f"📍 {ride['pickup']['name']}\n🏁 {ride['dropoff']['name']}\n{arrive}\n━━━━━━━━━━━━━━━\n"
                  f"🚘 {driver['vehicle_model']} · *{driver['plate']}*\n👨‍✈️ {driver['name']} · ⭐ {driver['rating']}\n"
                  f"📞 {driver['phone']}\n🔐 OTP: *{ride['otp']}* (tell the driver at pickup)\n━━━━━━━━━━━━━━━\n"
                  f"💰 *{inr(ride['fare_inr'])}* · cash or UPI to the driver")
        return [text_msg(ticket),
                buttons_msg("Free cancellation until 15 minutes before pickup. Safe ride! 🛡️",
                            [("cab:rides", "📋 My Rides"), ("cab:book", "🚕 Book Another"), ("nav:menu", "🏠 Menu")]),
                reaction_msg(s.msg_id, "🚕")]

    # ------------------------------------------------------------------------ my rides
    async def _show_rides(self, s: Session) -> list[dict]:
        rides = await self._db(self.repo.list_rides, s.user["id"])
        if not rides:
            return [buttons_msg("No rides yet. Your first one is a few taps away! 🙂",
                                [("cab:book", "🚕 Book a Cab"), ("nav:menu", "🏠 Menu")])]
        s.step = "awaiting_ride"
        icon = {"confirmed": "✅", "completed": "🏁", "cancelled": "❌"}
        rows = [(f"cride:{r['id']}", f"{r['ref']} · {r['pickup']['name']}",
                 f"{icon.get(r['status'], '')} {r['status'].title()} · {to_ist(r['pickup_time']):%d %b, %H:%M}") for r in rides]
        rows.append(("nav:menu", "🏠 Main menu", ""))
        return [list_msg("🚕 *Your rides*\nPick one for details 👇", "View rides", rows, "Recent rides")]

    @staticmethod
    def _ride_text(r: dict) -> str:
        d = r.get("driver") or {}
        text = (f"🎫 *{r['ref']}* · {r['status'].title()}\n📍 {r['pickup']['name']}\n🏁 {r['dropoff']['name']}\n"
                f"🕐 {when_label(to_ist(r['pickup_time']))} · {r['vehicle_type']} · {r['distance_km']} km\n💰 {inr(r['fare_inr'])}")
        if d and r["status"] == "confirmed":
            text += f"\n\n🚘 {d['vehicle_model']} · *{d['plate']}*\n👨‍✈️ {d['name']} · ⭐ {d['rating']} · 📞 {d['phone']}\n🔐 OTP: *{r['otp']}*"
        if r["status"] == "cancelled" and r["cancel_fee_inr"]:
            text += f"\n\nCancellation fee: {inr(r['cancel_fee_inr'])}"
        return text

    async def _show_ride(self, s: Session, ride_id: str) -> list[dict]:
        r = await self._db(self.repo.get_ride, ride_id, s.user["id"])
        if not r:
            return await self.on_enter(s)
        buttons = [("cab:rides", "📋 All rides"), ("nav:menu", "🏠 Menu")]
        if r["status"] == "confirmed" and now_ist() < to_ist(r["pickup_time"]) + timedelta(minutes=CANCEL_GRACE_MIN):
            buttons.insert(0, (f"ccancel:{r['id']}", "❌ Cancel ride"))
        return [buttons_msg(self._ride_text(r), buttons)]

    @staticmethod
    def _cancel_fee(r: dict) -> int:
        return 0 if to_ist(r["pickup_time"]) - now_ist() > timedelta(minutes=CANCEL_FREE_BEFORE_MIN) else LATE_CANCEL_FEE

    async def _ask_cancel(self, s: Session, ride_id: str) -> list[dict]:
        r = await self._db(self.repo.get_ride, ride_id, s.user["id"])
        if not r or r["status"] != "confirmed":
            return await self.on_enter(s)
        fee = self._cancel_fee(r)
        note = "Free cancellation. ✅" if fee == 0 else f"Pickup is within {CANCEL_FREE_BEFORE_MIN} min, so a {inr(fee)} fee applies."
        return [buttons_msg(f"⚠️ Cancel ride *{r['ref']}* ({r['pickup']['name']} ➜ {r['dropoff']['name']})?\n\n{note}",
                            [(f"ccancely:{r['id']}", "Yes, cancel it"), (f"cride:{r['id']}", "No, keep it")])]

    async def _do_cancel(self, s: Session, ride_id: str) -> list[dict]:
        r = await self._db(self.repo.get_ride, ride_id, s.user["id"])
        if not r or r["status"] != "confirmed":
            return await self.on_enter(s)
        fee = self._cancel_fee(r)
        await self._db(self.repo.cancel_ride, r["id"], fee)
        s.step = "menu"
        extra = f" A {inr(fee)} fee applies." if fee else " No charges."
        return [buttons_msg(f"😢 Ride *{r['ref']}* is cancelled.{extra}",
                            [("cab:book", "🚕 Book Another"), ("cab:rides", "📋 My Rides"), ("nav:menu", "🏠 Menu")])]
