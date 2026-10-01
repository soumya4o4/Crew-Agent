"""Events agent: concerts, comedy, food fests and more, closest first when we know where the user is.

Button ids: events:* (list), evt:<id> (one event). Where to look comes from, in order: a city typed here, the location pin
the user shared (ctx["loc"], mapped to the nearest city we cover), or the city of the flight they just booked. With none of
these the agent asks for a pin or a city. Ticket booking is not built yet: entry is paid at the venue.
State lives in ctx["ev"] = {city, lat, lon, from}.
"""
import asyncio
from datetime import datetime, timedelta

from app.agents.base import Agent, Session
from app.core.geo import fmt_dist, fresh_location, haversine_m, maps_link, nearest_city
from app.core.messages import buttons_msg, cta_msg, list_msg, location_request_msg, text_msg
from app.core.places import CITIES, CITY_ALIASES, city
from app.core.utils import IST, inr, now_ist, to_ist

WINDOWS_DAYS = (7, 30)  # look a week ahead first, then a month
CATEGORY_ICON = {"music": "🎵", "comedy": "😂", "food": "🍜", "sports": "🏏", "art": "🎨", "festival": "🎪", "workshop": "🛠️"}


class EventsAgent(Agent):
    name = "events"
    title = "Events"
    emoji = "🎟️"
    menu_desc = "Shows, music & fests near you"
    owns = frozenset({"events", "evt"})

    def __init__(self, repo):
        self.repo = repo  # EventsRepo

    async def _db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def reset(self, s: Session) -> None:
        s.ctx.pop("ev", None)

    def expects_text(self, s: Session) -> bool:
        return s.step == "awaiting_event_where"

    def expects_location(self, s: Session) -> bool:
        return s.step == "awaiting_event_where"

    # ------------------------------------------------------------------ entry points
    async def on_enter(self, s: Session) -> list[dict]:
        s.ctx["agent"] = "events"
        return await self._show(s)

    async def start(self, s: Session, slots: dict) -> list[dict]:
        """Free text like "events in Goa": a named city skips the question."""
        s.ctx["agent"] = "events"
        code = next((slots[k] for k in ("to", "from") if slots.get(k) in CITIES), None)
        if code:
            s.ctx["ev"] = {"city": code}
        return await self._show(s)

    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        s.ctx["agent"] = "events"
        if reply_id:
            kind, _, val = reply_id.partition(":")
            if kind == "evt":
                return await self._show_event(s, val)
            if val == "city":
                self.reset(s)
                return self._ask_where(s)
            return await self._show(s)
        if s.step == "awaiting_event_where" and text:
            code = CITY_ALIASES.get(text.lower().strip())
            if code not in CITIES:
                return [text_msg("😕 I list events in: " + ", ".join(sorted(CITIES.values())) + ". Type one of these, or share your location.")]
            s.ctx["ev"] = {"city": code}
            return await self._show(s)
        return await self._show(s)

    async def on_location(self, s: Session, loc: dict) -> list[dict]:
        s.ctx.pop("ev", None)  # the fresh pin wins over a city typed earlier
        return await self._show(s)

    # ------------------------------------------------------------------- results
    @staticmethod
    def _anchor(s: Session) -> dict | None:
        """Where to look: {city, lat, lon, from}. lat/lon are only set when we know where the user stands."""
        if (ev := s.ctx.get("ev")) and ev.get("city"):
            return ev
        if loc := fresh_location(s.ctx):
            return {"city": nearest_city(loc["lat"], loc["lon"]), "lat": loc["lat"], "lon": loc["lon"]}
        trip = s.ctx.get("trip") or {}
        if trip.get("to") in CITIES:
            return {"city": trip["to"], "from": trip.get("date")}  # events around the day they land
        return None

    def _ask_where(self, s: Session, note: str = "") -> list[dict]:
        s.step = "awaiting_event_where"
        return [location_request_msg(f"{note}📍 Share your location and I'll show events closest to you. I only use it to search, "
                                     "and forget it after a few hours."),
                text_msg("Or type a city name.")]

    async def _show(self, s: Session) -> list[dict]:
        a = self._anchor(s)
        if not a:
            return self._ask_where(s)
        if not a.get("city"):
            return self._ask_where(s, "I don't list events around where you are yet. ")
        now = now_ist()
        start = now
        if a.get("from"):  # a flight lands on that day: look from then
            start = max(now, datetime.fromisoformat(a["from"][:10]).replace(tzinfo=IST))
        events, days = [], WINDOWS_DAYS[0]
        for days in WINDOWS_DAYS:
            events = await self._db(self.repo.list_events, a["city"], start, start + timedelta(days=days))
            if events:
                break
        s.step = "events_list"
        if not events:
            return [buttons_msg(f"😕 No events listed in {city(a['city'])} for the next {days} days.",
                                [("events:city", "🏙️ Other city"), ("nav:menu", "🏠 Menu")])]
        near = a.get("lat") is not None
        if near:
            for e in events:
                e["dist_m"] = haversine_m(a["lat"], a["lon"], e["lat"], e["lon"])
            events.sort(key=lambda e: (e["dist_m"], e["starts_at"]))
        rows = []
        for e in events[:9]:
            when = to_ist(e["starts_at"])
            parts = [f"{CATEGORY_ICON.get(e['category'], '🎟️')} {when:%a %H:%M}", fmt_dist(e["dist_m"]) if near else e["venue"],
                     "Free" if not e["price_inr"] else inr(e["price_inr"])]
            rows.append((f"evt:{e['id']}", e["title"], " · ".join(parts)))  # the full title: WhatsApp cuts list titles at 24 characters
        rows.append(("nav:menu", "🏠 Main menu", ""))
        order = "closest first" if near else "soonest first"
        return [list_msg(f"🎟️ *Events in {city(a['city'])}* · next {days} days, {order}\nFound *{len(events)}*. Pick one 👇",
                         "See events", rows, "Events")]

    async def _show_event(self, s: Session, event_id: str) -> list[dict]:
        e = await self._db(self.repo.get_event, event_id)
        if not e:
            return await self._show(s)
        when = to_ist(e["starts_at"])
        lines = [f"{CATEGORY_ICON.get(e['category'], '🎟️')} *{e['title']}*", f"📍 {e['venue']}, {city(e['city_code'])}"]
        if loc := fresh_location(s.ctx):
            lines[-1] += f" · {fmt_dist(haversine_m(loc['lat'], loc['lon'], e['lat'], e['lon']))} from you"
        lines += [f"🗓️ {when:%a, %d %b} · {when:%H:%M}",
                  "💰 Free entry" if not e["price_inr"] else f"💰 {inr(e['price_inr'])} (pay at the venue)", e["description"]]
        return [cta_msg("\n".join(x for x in lines if x), "🗺️ Directions", maps_link(e["lat"], e["lon"])),
                buttons_msg("Anything else?", [("events:list", "↩️ More events"), ("nav:menu", "🏠 Menu")])]
