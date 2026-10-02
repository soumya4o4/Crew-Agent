"""Hotel agent: find a stay, pick a room, pay on Razorpay, and manage your stays.

Button/list ids look like "kind:value"; `owns` lists the kinds:
hotel:* (menu), hcity:, hin: (check-in), hnt: (nights), hgst: (guests), htl: (a hotel), hroom:, hname:, hcfm:,
hpay:, hbk: (a stay), hbkc: / hbkcy: (cancel), hact: (back / change)
The booking being built lives in ctx["hotel"]; a payment waiting to be completed lives in ctx["hpay"].
"""
import asyncio
import math
import re
import logging
import re
from datetime import date, datetime, timedelta

from app.agents.base import Agent, Session
from app.agents.flight.formatting import NEXT_STEPS, city_tip
from app.agents.hotel.images import hotel_image
from app.agents.hotel.nlu import parse_stay
from app.agents.hotel.formatting import (STATUS_ICON, amenities_asked, amenity_answer, asks_amenities, cancel_policy, fmt_day, free_cancel_until, hotel_card,
                                         hotel_card_short, hotel_row, hotel_tags, plural, room_row, stay_countdown,
                                         stay_text)
from app.core.geo import NEAR_ME, fresh_location, nearest_city
from app.core.messages import (buttons_msg, cta_msg, image_msg, list_msg, location_request_msg, reaction_msg,
                               text_msg)
from app.core.places import find_city, country_of, city
from app.core.utils import IST, inr, now_ist, parse_date

logger = logging.getLogger(__name__)
MAX_DAYS_AHEAD = 29
MAX_NIGHTS = 14
MAX_GUESTS = 9
WHICH_CITIES = re.compile(r"\b(cit(?:y|ies)|where|kahan|kaun|which|available|operational|list)\b")
PAGE_SIZE = 8  # stays per page when there are more than fit in one WhatsApp list (10 rows: stays, "More", "Main menu")
# "give me more hotel options", "I want to change hotel", "dusra hotel chahiye": the user wants to see other stays
MORE_HOTELS = re.compile(r"\b(?:more|other|another|different|else|alternatives?|options?|change|switch|dusr[aei]|doosr[aei]|aur|badal\w*)\b"
                         r"[^.?!]*\b(?:hotels?|stays?|rooms?|options?)\b|\b(?:hotels?|stays?)\b[^.?!]*\b(?:more|other|another|different|"
                         r"change|dusr[aei]|doosr[aei]|aur|options?|badal\w*)\b")
NEAR_KM = 80  # "near me" picks the closest city with hotels if it is within this distance
PAYMENT_WINDOW_MIN = 20  # Razorpay needs payment links to live at least 15 min
DETAIL_STEPS = ("awaiting_hotel_city", "awaiting_hotel_loc", "awaiting_hotel_date", "awaiting_hotel_nights", "awaiting_hotel_guests")
TEXT_STEPS = ("awaiting_hotel_city", "awaiting_hotel_loc", "awaiting_hotel_date", "awaiting_hotel_nights", "awaiting_hotel_guests", "awaiting_hotel_pick",
              "awaiting_hotel_room", "awaiting_hotel_name")


class HotelAgent(Agent):
    name = "hotel"
    bundleable = True
    title = "Hotels"
    emoji = "🏨"
    menu_desc = "Find & book stays"
    owns = frozenset({"hotel", "hcity", "hin", "hnt", "hgst", "htl", "hroom", "hname", "hcfm", "hpay", "hbk", "hbkc",
                      "hbkcy", "hact"})

    def __init__(self, repo, payments=None, advisor=None):
        self.repo = repo          # HotelRepo
        self.payments = payments  # RazorpayGateway (or None: stays confirm instantly)
        self.advisor = advisor    # TravelAdvisor (or None: a plain tip)

    async def _db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def reset(self, s: Session) -> None:
        s.ctx.pop("hotel", None)

    def expects_text(self, s: Session) -> bool:
        return s.step in TEXT_STEPS

    def expects_location(self, s: Session) -> bool:
        return s.step == "awaiting_hotel_loc"

    async def on_location(self, s: Session, loc: dict) -> list[dict]:
        """A shared pin picks the city: the closest one with hotels."""
        s.ctx.setdefault("hotel", {})
        return await self._near_me(s)

    # ------------------------------------------------------------------ entry points
    async def on_enter(self, s: Session) -> list[dict]:
        self.reset(s)
        s.step = "hotel_menu"
        buttons = [("hotel:find", "🏨 Find a Hotel"), ("hotel:stays", "📋 My Stays")]
        body = "🏨 *Hotels*\nFind a stay at a fair price and book it right here."
        trip = s.ctx.get("trip") or {}
        if trip.get("to") in await self._hotel_cities() and self._trip_checkin(trip):
            body += f"\n\n✈️ You land in *{trip['city']}* on {fmt_day(self._trip_checkin(trip))}. Want a place to stay?"
            buttons.insert(0, ("hotel:trip", f"🏨 Stay in {trip['city']}"))
        return [buttons_msg(body, buttons)]

    async def start(self, s: Session, slots: dict) -> list[dict]:
        """Free text like "hotel in Goa tomorrow": pre-fill what we heard, ask only for the rest."""
        self.reset(s)
        cities = await self._hotel_cities()
        h: dict = {}
        if code := next((slots[k] for k in ("to", "from") if slots.get(k) in cities), None):
            h["city"] = code
        said = parse_stay(slots["text"], now_ist().date()) if slots.get("text") else {}
        if said.get("city") in cities:
            h["city"] = said["city"]
        if (d := slots.get("date") or said.get("check_in")) and self._in_range(date.fromisoformat(d)):
            h["check_in"] = d
        if 1 <= said.get("nights", 0) <= MAX_NIGHTS:
            h["nights"] = said["nights"]
        if 1 <= said.get("guests", 0) <= MAX_GUESTS:
            h["guests"] = said["guests"]
        if slots.get("near_me") and not h.get("city"):
            s.ctx["hotel"] = h
            return await self._near_me(s)
        if not h:
            return await self.on_enter(s)
        s.ctx["hotel"] = h
        heard = " ".join(x for x in (f"in {city(h['city'])}" if h.get("city") else "",
                                     f"from {fmt_day(h['check_in'])}" if h.get("check_in") else "",
                                     plural(h["nights"], "night") if h.get("nights") else "",
                                     plural(h["guests"], "guest") if h.get("guests") else "") if x)
        return [text_msg(f"Got it! 🏨 A stay {heard}.")] + await self._advance(s)

    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        return await (self._on_reply(s, reply_id) if reply_id else self._on_text(s, text.strip()))

    # ---------------------------------------------------------------------- helpers
    async def _hotel_cities(self) -> set[str]:
        return {c["code"] for c in await self._db(self.repo.list_hotel_cities)}

    @staticmethod
    def _in_range(d: date) -> bool:
        today = now_ist().date()
        return today <= d <= today + timedelta(days=MAX_DAYS_AHEAD)

    def _trip_checkin(self, trip: dict) -> date | None:
        """The day the user lands (from the flight just booked), if it is still ahead of us."""
        try:
            d = datetime.fromisoformat(trip["arrival"]).astimezone(IST).date() if trip.get("arrival") else date.fromisoformat(trip["date"])
        except (KeyError, ValueError):
            return None
        return d if self._in_range(d) else None

    @staticmethod
    def _stay(h: dict) -> tuple[date, date]:
        check_in = date.fromisoformat(h["check_in"])
        return check_in, check_in + timedelta(days=h["nights"])

    def _home(self, s: Session, note: str) -> list[dict]:
        self.reset(s)
        s.step = "hotel_menu"
        return [buttons_msg(note, [("hotel:find", "🏨 Find a Hotel"), ("hotel:stays", "📋 My Stays"), ("nav:menu", "🏠 Menu")])]

    # -------------------------------------------------------------------- routing
    async def _on_text(self, s: Session, text: str) -> list[dict]:
        h = s.ctx.get("hotel")
        if s.step not in TEXT_STEPS or h is None:
            return await self.on_enter(s)
        if s.step == "awaiting_hotel_name":
            return await self._on_name_text(s, text)
        if s.step in DETAIL_STEPS:
            return await self._on_details_text(s, text)
        if not h.get("city"):
            return await self.on_enter(s)
        low = text.lower()
        wants_more = bool(MORE_HOTELS.search(low))
        if not wants_more and not asks_amenities(text):  # "make it 3 nights" / "for 4 people": change the search, show again
            said = parse_stay(text, now_ist().date())
            if any(k in said for k in ("city", "check_in", "nights", "guests")):
                return await self._on_details_text(s, text)
        return await self._on_more_text(s, text)

    async def _on_details_text(self, s: Session, text: str) -> list[dict]:
        """Any answer can fill several details at once ("2 nights from tomorrow for 3"); then we ask for what is missing."""
        h = s.ctx.setdefault("hotel", {})
        asking = s.step
        said = parse_stay(text, now_ist().date(), {"awaiting_hotel_nights": "nights", "awaiting_hotel_guests": "guests"}.get(asking))
        cities = {c["code"]: c["city"] for c in await self._db(self.repo.list_hotel_cities)}
        if said.get("near_me") and asking in ("awaiting_hotel_city", "awaiting_hotel_loc"):
            return await self._near_me(s)
        notes: list[str] = []
        if code := said.get("city"):
            if code in cities:
                if h.get("city") != code:
                    h.pop("page", None), h.pop("hotel_id", None), h.pop("room_id", None)
                h["city"] = code
            else:
                notes.append(f"😕 I don't have stays in {city(code)} yet. I can book in: " + ", ".join(sorted(cities.values())) + ".")
        elif asking in ("awaiting_hotel_city", "awaiting_hotel_loc") and not h.get("city"):
            if WHICH_CITIES.search(text.lower()):
                notes.append("🏙️ I can book stays in: " + ", ".join(sorted(cities.values())) + ". Which one?")
            else:
                notes.append("😕 I don't have stays there yet. I can book in: " + ", ".join(sorted(cities.values())) + ".")
        if d := said.get("check_in"):
            if self._in_range(date.fromisoformat(d)):
                h["check_in"] = d
            else:
                notes.append("😕 That date is out of range. I can book stays within the next 30 days.")
        if n := said.get("nights"):
            if 1 <= n <= MAX_NIGHTS:
                h["nights"] = n
            else:
                notes.append(f"😕 I can book between 1 and {MAX_NIGHTS} nights.")
        if g := said.get("guests"):
            if 1 <= g <= MAX_GUESTS:
                h["guests"] = g
            else:
                notes.append(f"😕 I can book for 1 to {MAX_GUESTS} guests.")
        if not h.get("city"):
            return [text_msg(n) for n in notes] if notes else await self._ask_city(s)
        if not notes and not any(k in said for k in ("city", "check_in", "nights", "guests")):
            notes.append("😕 Sorry, I didn't catch that.")
        return [text_msg(n) for n in notes] + await self._advance(s)

    async def _on_more_text(self, s: Session, text: str) -> list[dict]:
        """Typed words while looking at stays. "More options" / "change hotel" moves to other stays (the next page of the
        results, or back to them from a hotel's rooms); anything else just shows the same choices again."""
        h = s.ctx["hotel"]
        wants_more = bool(MORE_HOTELS.search(text.lower()))
        if h.get("guests") and not wants_more and asks_amenities(text) and (answer := await self._amenities(s, text)):
            return answer
        if not h.get("guests"):  # we cannot search yet: carry on with the next question instead of an error
            return [text_msg("👍 Sure, first I need a few details to show you the stays.")] + await self._advance(s)
        if s.step == "awaiting_hotel_pick" and wants_more:
            h["page"] = h.get("page", 0) + 1
        if s.step == "awaiting_hotel_room" and not wants_more and h.get("hotel_id"):
            return await self._show_hotel(s, h["hotel_id"])
        return await self._show_results(s)

    async def _amenities(self, s: Session, text: str) -> list[dict] | None:
        """"Does it have a pool?" / "amenities?": answered from our data. On a hotel's rooms: that hotel. In the results:
        which of these stays have it. None if there is nothing to answer from."""
        h, asked = s.ctx["hotel"], amenities_asked(text)
        found = await self._search(h)
        if s.step == "awaiting_hotel_room" and h.get("hotel_id"):
            hotel = next((x for x in found if x["id"] == h["hotel_id"]), None)
            return [text_msg(amenity_answer(hotel, asked) + "\n\nPick a room from the list above. 👆")] if hotel else None
        if s.step == "awaiting_hotel_pick" and asked:
            match = sorted((x for x in found if all(a in x["amenities"] for a in asked)), key=lambda x: x["rooms"][0]["price_inr"])
            what = " + ".join(a.lower() for a in asked)
            if not match:
                return [text_msg(f"😕 None of these stays list {what}. Try *more options*, or another city.")]
            return [text_msg(f"✅ Stays with {what}:\n" + "\n".join(
                f"• {x['name']} · {x['stars']}★ · from {inr(x['rooms'][0]['price_inr'])}/night" for x in match) +
                "\n\nPick one from the list above. 👆")]
        return None

    async def _on_reply(self, s: Session, reply_id: str) -> list[dict]:
        kind, _, val = reply_id.partition(":")
        h = s.ctx.setdefault("hotel", {})
        if kind == "hotel":
            if val == "find":
                s.ctx["hotel"] = {}
                return await self._ask_city(s)
            if val == "trip":
                return await self._stay_for_trip(s)
            if val == "stays":
                return await self._show_stays(s)
        elif kind == "hcity":
            if val == "more":
                s.step = "awaiting_hotel_city"
                return [text_msg("🏙️ Type the city name, e.g. *Pune*.")]
            if val == "near":
                return await self._near_me(s)
            if val not in await self._hotel_cities():
                return await self._ask_city(s)
            s.ctx["hotel"] = {"city": val}
            return await self._advance(s)
        elif kind == "hin" and h.get("city"):
            if val == "more":
                s.step = "awaiting_hotel_date"
                return [text_msg("📅 Type the check-in date, e.g. *15/10* or *15 Oct* (within the next 30 days).")]
            h["check_in"] = val
            return await self._advance(s)
        elif kind == "hnt" and h.get("check_in"):
            if val == "more":
                s.step = "awaiting_hotel_nights"
                return [text_msg(f"🌙 How many nights? Type a number from 1 to {MAX_NIGHTS}.")]
            if val.isdigit() and 1 <= int(val) <= MAX_NIGHTS:
                h["nights"] = int(val)
                return await self._advance(s)
        elif kind == "hgst" and h.get("nights") and val.isdigit():
            h["guests"] = int(val)
            return await self._advance(s)
        elif kind == "htl" and h.get("guests"):
            return await self._show_hotel(s, val)
        elif kind == "hroom" and h.get("hotel_id"):
            return await self._on_room(s, val)
        elif kind == "hname" and h.get("room_id"):
            if val == "self":
                h["name"] = s.user["name"]
                return await self._confirm(s)
            return self._prompt_name(s)
        elif kind == "hcfm" and h.get("room_id"):
            if val == "yes" and h.get("name"):
                return await self._do_booking(s)
            if val == "name":
                h.pop("name", None)
                return self._ask_guest(s)
            return self._home(s, "No worries, I've dropped that booking. 👍")
        elif kind == "hact" and h.get("city"):
            return await self._on_action(s, val)
        elif kind == "hpay" and val in ("check", "cancel"):
            return await self._on_payment_tap(s, val)
        elif kind == "hbk":
            return await self._show_stay(s, val)
        elif kind == "hbkc":
            return await self._ask_cancel(s, val)
        elif kind == "hbkcy":
            return await self._do_cancel(s, val)
        return await self.on_enter(s)

    async def _on_action(self, s: Session, val: str) -> list[dict]:
        h = s.ctx["hotel"]
        if val == "results" and h.get("guests"):
            return await self._show_results(s)
        if val == "more" and h.get("guests"):
            h["page"] = h.get("page", 0) + 1
            return await self._show_results(s)
        if val == "dates":
            for k in ("check_in", "nights", "hotel_id", "room_id", "page"):
                h.pop(k, None)
            return self._ask_date(s)
        if val == "city":
            s.ctx["hotel"] = {}
            return await self._ask_city(s)
        return await self.on_enter(s)

    async def _advance(self, s: Session) -> list[dict]:
        """Jump to the first step whose answer we don't have yet."""
        h = s.ctx["hotel"]
        if not h.get("city"):
            return await self._ask_city(s)
        if not h.get("check_in"):
            return self._ask_date(s)
        if not h.get("nights"):
            return self._ask_nights(s)
        if not h.get("guests"):
            return self._ask_guests(s)
        return await self._show_results(s)

    async def _stay_for_trip(self, s: Session) -> list[dict]:
        """"Stay in <city>" after a flight: city and check-in come from the trip."""
        trip = s.ctx.get("trip") or {}
        h = s.ctx["hotel"] = {}
        if trip.get("to") in await self._hotel_cities():
            h["city"] = trip["to"]
        if d := self._trip_checkin(trip):
            h["check_in"] = d.isoformat()
        plan = s.ctx.get("plan") or {}
        if plan.get("days") and plan.get("city_code") == trip.get("to"):  # "3 days" in the trip plan means 2 nights
            h["nights"] = max(1, min(MAX_NIGHTS, plan["days"] - 1))
        return await self._advance(s)

    # ------------------------------------------------------------------ search steps
    async def _ask_city(self, s: Session) -> list[dict]:
        s.step = "awaiting_hotel_city"
        names = sorted(c["city"] for c in await self._db(self.repo.list_hotel_cities))
        return [text_msg("🏙️ *Which city do you need a stay in?*\nI can book in: " + ", ".join(names) + ".\n"
                         "Just tell me in your own words, like *2 nights in Goa from tomorrow for 2 people*, "
                         "or share your location for stays near you.")]

    async def _near_me(self, s: Session) -> list[dict]:
        """"Near me": use the shared location (ask for it if we have none) to pick the closest city we have hotels in."""
        h = s.ctx.setdefault("hotel", {})
        loc = fresh_location(s.ctx)
        if not loc:
            s.step = "awaiting_hotel_loc"
            return [location_request_msg("📍 Share your location and I'll find stays in the closest city. "
                                         "I only use it for this, and forget it after a few hours."),
                    text_msg("Or just type a city name, like *Pune*.")]
        code = nearest_city(loc["lat"], loc["lon"], max_km=NEAR_KM, among=await self._hotel_cities())
        if not code:
            return [text_msg("📍 I don't have stays near you yet, so please tell me which city.")] + await self._ask_city(s)
        h["city"] = code
        return [text_msg(f"📍 You're near *{city(code)}*, so here are stays there.")] + await self._advance(s)

    def _ask_date(self, s: Session) -> list[dict]:
        s.step = "awaiting_hotel_date"
        return [text_msg(f"📅 *Stay in {city(s.ctx['hotel']['city'])}*\nWhen do you check in? Say *tomorrow*, *this Saturday* or a date like *15/10*.")]

    def _ask_nights(self, s: Session) -> list[dict]:
        s.step = "awaiting_hotel_nights"
        check_in = date.fromisoformat(s.ctx["hotel"]["check_in"])
        return [text_msg(f"🌙 Check-in *{fmt_day(check_in)}*. How many nights are you staying? (up to {MAX_NIGHTS})")]

    def _ask_guests(self, s: Session) -> list[dict]:
        s.step = "awaiting_hotel_guests"
        return [text_msg("👥 How many guests will stay?")]

    # -------------------------------------------------------------------- results
    async def _search(self, h: dict) -> list[dict]:
        check_in, check_out = self._stay(h)
        return await self._db(self.repo.search_hotels, h["city"], check_in, check_out, h["guests"])

    async def _show_results(self, s: Session) -> list[dict]:
        h = s.ctx["hotel"]
        if date.fromisoformat(h["check_in"]) < now_ist().date():  # the chat sat idle past the chosen day
            h.pop("check_in")
            return self._ask_date(s)
        check_in, check_out = self._stay(h)
        hotels = sorted(await self._search(h), key=lambda x: x["rooms"][0]["price_inr"])
        if not hotels:
            s.step = "hotel_menu"
            return [buttons_msg(f"😕 No rooms free in {city(h['city'])} for {fmt_day(check_in)} to {fmt_day(check_out)} "
                                f"with {plural(h['guests'], 'guest')}. Try other dates?",
                                [("hact:dates", "📅 Other dates"), ("hact:city", "🏙️ Other city"), ("nav:menu", "🏠 Menu")])]
        tags = hotel_tags(hotels)
        s.step = "awaiting_hotel_pick"
        out, extra, more = [], "", []
        if len(hotels) <= 9:
            shown = hotels
        else:
            pages = math.ceil(len(hotels) / PAGE_SIZE)
            page = h["page"] = h.get("page", 0) % pages
            shown = hotels[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
            extra = f" (showing {page * PAGE_SIZE + 1}-{page * PAGE_SIZE + len(shown)})"
            more = [("hact:more", f"➡️ More ({page + 1}/{pages})", "See the next stays")]
            if page == 0:  # everything at a glance, then the list to pick from
                out.append(text_msg(f"🏨 *All {len(hotels)} stays in {city(h['city'])}* (cheapest first)\n" + "\n".join(
                    f"{i}. {x['name']} · {x['stars']}★ · ⭐ {x['rating']} · from {inr(x['rooms'][0]['price_inr'])}"
                    for i, x in enumerate(hotels, 1))))
        rows = [hotel_row(x, tags[x["id"]]) for x in shown] + more + [("nav:menu", "🏠 Main menu", "")]
        return out + [list_msg(
            f"🏨 *{city(h['city'])}* · {fmt_day(check_in)} ➜ {fmt_day(check_out)} ({plural(h['nights'], 'night')}, "
            f"{plural(h['guests'], 'guest')})\nFound *{len(hotels)}* stays{extra}, from just "
            f"*{inr(hotels[0]['rooms'][0]['price_inr'])}* a night.\nPick one 👇", "View hotels", rows, "Available stays")]

    async def _show_hotel(self, s: Session, hotel_id: str) -> list[dict]:
        h = s.ctx["hotel"]
        found = next((x for x in await self._search(h) if x["id"] == hotel_id), None)
        if not found:
            return [buttons_msg("😕 Oh no, that hotel just ran out of rooms for your dates.",
                                [("hact:results", "↩️ Other hotels"), ("hact:dates", "📅 Other dates"), ("nav:menu", "🏠 Menu")])]
        h["hotel_id"] = hotel_id
        h.pop("room_id", None)
        s.step = "awaiting_hotel_room"
        check_in, check_out = self._stay(h)
        rows = [room_row(r, h["nights"]) for r in found["rooms"]] + [("hact:results", "↩️ Other hotels", ""), ("nav:menu", "🏠 Main menu", "")]
        return [image_msg(hotel_image(found), hotel_card(found)),
                list_msg(f"📅 {fmt_day(check_in)} ➜ {fmt_day(check_out)} · {plural(h['nights'], 'night')}\n"
                         f"{cancel_policy(check_in)}\n\nWhich room at *{found['name']}*?", "Choose room", rows, "Rooms")]

    async def _on_room(self, s: Session, room_id: str) -> list[dict]:
        h = s.ctx["hotel"]
        found = next((x for x in await self._search(h) if x["id"] == h["hotel_id"]), None)
        if not found or not any(r["id"] == room_id for r in found["rooms"]):
            return [buttons_msg("😕 That room was just taken. Want to pick another?",
                                [("hact:results", "↩️ Other hotels"), ("hact:dates", "📅 Other dates"), ("nav:menu", "🏠 Menu")])]
        h["room_id"] = room_id
        return self._ask_guest(s)

    # ------------------------------------------------------------------ the guest
    def _ask_guest(self, s: Session) -> list[dict]:
        s.step = "awaiting_hotel_guest"
        name = s.user.get("name")
        if not name or name == "Unknown":
            return self._prompt_name(s)
        return [buttons_msg(f"👤 Great pick! Who's checking in?\nYour name: *{name}*",
                            [("hname:self", "For me"), ("hname:other", "Someone else")])]

    def _prompt_name(self, s: Session) -> list[dict]:
        s.step = "awaiting_hotel_name"
        return [text_msg("✏️ Please type the guest's full name (as on their ID).")]

    async def _on_name_text(self, s: Session, text: str) -> list[dict]:
        h = s.ctx["hotel"]
        if not h.get("room_id"):
            return await self.on_enter(s)
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,59}", text):
            return [text_msg("😕 Please use letters only for the name (2-60 characters).")]
        h["name"] = " ".join(text.split()).title()
        return await self._confirm(s)

    # ------------------------------------------------------------------- booking
    async def _confirm(self, s: Session) -> list[dict]:
        h = s.ctx["hotel"]
        hotel, room = await self._db(self.repo.get_hotel, h["hotel_id"]), await self._db(self.repo.get_room, h["room_id"])
        check_in, check_out = self._stay(h)
        s.step = "awaiting_hotel_confirm"
        total = room["price_inr"] * h["nights"]
        return [buttons_msg(
            f"📝 *Last check before you book!*\n\n{hotel_card_short(hotel)}\n🛏️ {room['room_type']} · {room['bed']}\n"
            f"📅 {fmt_day(check_in)} ➜ {fmt_day(check_out)} ({plural(h['nights'], 'night')})\n"
            f"👤 {h['name']} · {plural(h['guests'], 'guest')}\n"
            f"💰 {inr(room['price_inr'])} × {h['nights']} = *{inr(total)}*\n{cancel_policy(check_in)}\n\nShall I book it?",
            [("hcfm:yes", "✅ Confirm"), ("hcfm:name", "✏️ Change name"), ("hcfm:no", "❌ Cancel")])]

    async def _do_booking(self, s: Session) -> list[dict]:
        h = s.ctx["hotel"]
        hotel, room = await self._db(self.repo.get_hotel, h["hotel_id"]), await self._db(self.repo.get_room, h["room_id"])
        check_in, check_out = self._stay(h)
        if self.checkout is not None and self.checkout.online:  # paid online: into the bundle, held only at checkout
            item = {"kind": "hotel", "hotel_id": hotel["id"], "room_id": room["id"], "name": h["name"], "guests": h["guests"],
                    "check_in": check_in.isoformat(), "check_out": check_out.isoformat(), "amount": room["price_inr"] * h["nights"],
                    "label": f"🏨 {hotel['name']}, {fmt_day(check_in)} ➜ {fmt_day(check_out)}"}
            self.reset(s)
            return await self.checkout.add(s, item, {"to": hotel["city_code"], "date": check_in.isoformat()})
        booking = await self._db(self.repo.create_booking, s.user["id"], hotel, room, h["name"], h["guests"], check_in,
                                 check_out, "confirmed")
        if not booking:
            s.step = "awaiting_hotel_pick"
            return [buttons_msg("😕 Sorry, that room was just taken. Want to see other hotels?",
                                [("hact:results", "↩️ Other hotels"), ("hact:dates", "📅 Other dates"), ("nav:menu", "🏠 Menu")])]
        self.reset(s)
        s.step = "hotel_menu"
        return self._voucher(booking, s.ctx.get("queue"), s.msg_id, await self._tip(booking))

    # ---- what the Checkout asks of a bundleable service
    async def create_pending(self, s: Session, item: dict) -> dict | None:
        hotel, room = await self._db(self.repo.get_hotel, item["hotel_id"]), await self._db(self.repo.get_room, item["room_id"])
        if not hotel or not room:
            return None
        return await self._db(self.repo.create_booking, s.user["id"], hotel, room, item["name"], item["guests"],
                              date.fromisoformat(item["check_in"]), date.fromisoformat(item["check_out"]), "pending")

    async def attach_payment(self, s: Session, booking: dict, link: dict, expires_at: datetime) -> None:
        await self._db(self.repo.create_payment, booking["id"], s.user["id"], booking["total_price_inr"], link["id"],
                       link["short_url"], expires_at)

    async def release_booking(self, booking_id: str) -> None:
        b = await self._db(self.repo.get_booking_with_user, booking_id)
        if b:
            await self._db(self.repo.cancel_payment, b["id"])
            await self._db(self.repo.cancel_booking, b)

    async def booking_state(self, booking_id: str) -> str | None:
        b = await self._db(self.repo.get_booking_with_user, booking_id)
        return b["status"] if b else None

    async def _tip(self, b: dict) -> str:
        return await city_tip(self.advisor, b["hotels"]["city_code"], "Have a wonderful stay!")

    def _voucher(self, b: dict, queue: list | None, msg_id: str | None, tip: str) -> list[dict]:
        """The confirmed-stay messages: voucher, what's next, and a reaction (only if replying to a tap)."""
        h = b["hotels"]
        queue = [x for x in queue or [] if x in NEXT_STEPS and x != "hotel"]
        nxt = queue[0] if queue else "cab"
        ask = (f"\n\nYou also mentioned {NEXT_STEPS[nxt][1]}. Shall we do that next?" if queue
               else "\n\nNeed a ride to the hotel?")
        out = [image_msg(hotel_image(h), f"🎉 *Stay Confirmed!*\n━━━━━━━━━━━━━━━\n{stay_text(b)}"),
               buttons_msg(f"{stay_countdown(b['check_in'])}\n{cancel_policy(b['check_in'])}\n🪪 Carry a valid photo ID at check-in.\n\n"
                           f"💡 *{city(h['city_code'])} tip:* {tip}{ask}",
                           [("hotel:stays", "📋 My Stays"), (f"svc:{nxt}", NEXT_STEPS[nxt][0]), ("nav:menu", "🏠 Menu")])]
        return out + ([reaction_msg(msg_id, "🏨")] if msg_id else [])

    # ------------------------------------------------------------------- payment
    async def _request_payment(self, s: Session, b: dict) -> list[dict]:
        """The room is held on a pending booking; confirm it only once the Razorpay link is paid."""
        h = b["hotels"]
        nights = (date.fromisoformat(b["check_out"]) - date.fromisoformat(b["check_in"])).days
        try:
            link = await self.payments.create_link(b["total_price_inr"], b["ref"], f"{h['name']} {plural(nights, 'night')}"[:255],
                                                   s.phone, b["guest_name"], PAYMENT_WINDOW_MIN)
            await self._db(self.repo.create_payment, b["id"], s.user["id"], b["total_price_inr"], link["id"], link["short_url"],
                           datetime.now(IST) + timedelta(minutes=PAYMENT_WINDOW_MIN + 1))  # +1 min grace for the webhook
        except Exception:
            logger.exception("Could not create a hotel payment link")
            await self._db(self.repo.cancel_booking, b)  # give the room back
            return [buttons_msg("😕 I couldn't set up the payment right now, and I've released your room. Please try again in a minute.",
                                [("hact:results", "↩️ Other hotels"), ("nav:menu", "🏠 Menu")])]
        s.ctx["hpay"] = {"booking_id": b["id"], "link_id": link["id"]}
        s.step = "awaiting_hotel_payment"
        test = "\n🧪 Test mode: no real money is charged." if getattr(self.payments, "test_mode", False) else ""
        return [
            cta_msg(f"💳 *Almost done!* Pay *{inr(b['total_price_inr'])}* to confirm your stay at {h['name']} "
                    f"({fmt_day(b['check_in'])} ➜ {fmt_day(b['check_out'])}).\n🎫 Ref: {b['ref']}\n"
                    f"⏳ The room is held for {PAYMENT_WINDOW_MIN} minutes.{test}", f"Pay {inr(b['total_price_inr'])}", link["short_url"]),
            buttons_msg("I'll confirm your stay here the moment the payment goes through ✅",
                        [("hpay:check", "✅ I've paid"), ("hpay:cancel", "❌ Cancel booking")]),
        ]

    async def _on_payment_tap(self, s: Session, action: str) -> list[dict]:
        pay = s.ctx.get("hpay")
        b = await self._db(self.repo.get_booking_with_user, pay["booking_id"]) if pay else None
        if not b:
            s.ctx.pop("hpay", None)
            return self._home(s, "I couldn't find that payment. Let's start fresh.")
        if action == "cancel":
            if b["status"] == "confirmed":
                return [buttons_msg("This stay is already paid and confirmed, so I can't drop it here. Use *My Stays* to cancel it.",
                                    [("hotel:stays", "📋 My Stays"), ("nav:menu", "🏠 Menu")])]
            await self._db(self.repo.cancel_payment, b["id"])
            await self._db(self.repo.cancel_booking, b)
            try:
                await self.payments.cancel_link(pay["link_id"])
            except Exception:
                logger.warning("Could not cancel the payment link %s", pay["link_id"])
            s.ctx.pop("hpay", None)
            return self._home(s, "No problem, I've cancelled it and released the room. 👍")
        # "I've paid"
        if b["status"] == "confirmed":  # the webhook beat the tap
            s.ctx.pop("hpay", None)
            return [buttons_msg(f"✅ Already confirmed! Your reference is *{b['ref']}*.",
                                [("hotel:stays", "📋 My Stays"), ("nav:menu", "🏠 Menu")])]
        if b["status"] == "cancelled":
            s.ctx.pop("hpay", None)
            return [buttons_msg("⌛ The payment window ended and the room was released. Want to try again?",
                                [("hotel:find", "🏨 Find a Hotel"), ("nav:menu", "🏠 Menu")])]
        try:
            paid = await self.payments.link_status(pay["link_id"]) == "paid"
        except Exception:
            paid = False
        confirmed = await self._db(self.repo.mark_paid, pay["link_id"]) if paid else None
        if not confirmed:
            return [buttons_msg("I haven't received the payment yet. If you've just paid, give it a few seconds and tap again. 🙏",
                                [("hpay:check", "✅ I've paid"), ("hpay:cancel", "❌ Cancel booking")])]
        s.ctx.pop("hpay", None)
        s.step = "hotel_menu"
        return self._voucher(confirmed, s.ctx.get("queue"), s.msg_id, await self._tip(confirmed))

    async def confirm_payment(self, link_id: str) -> tuple[str, list[dict]] | None:
        """Razorpay webhook: the link was paid. Returns (whatsapp number, messages) to send, or None if it isn't ours
        or was already handled."""
        b = await self._db(self.repo.mark_paid, link_id)
        if not b:
            return None
        phone = b["users"]["phone"]
        convo = await self._db(self.repo.get_conversation, phone)
        ctx = dict((convo or {}).get("context") or {})  # (keep ctx["hpay"]: a late "I've paid" tap then gets a friendly answer)
        await self._db(self.repo.save_conversation, phone, "hotel_menu", ctx)
        return phone.lstrip("+"), self._voucher(b, ctx.get("queue"), None, await self._tip(b))

    async def release_expired(self) -> list[tuple[str, list[dict]]]:
        """Sweeper: cancel stays nobody paid for in time. Returns (whatsapp number, messages) to notify."""
        expired = await self._db(self.repo.expire_unpaid, datetime.now(IST))
        return [(b["users"]["phone"].lstrip("+"),
                 [buttons_msg(f"⌛ The payment window for your stay at *{b['hotels']['name']}* ended, so I've released the room. "
                              "Want to try again?", [("hotel:find", "🏨 Find a Hotel"), ("nav:menu", "🏠 Menu")])]) for b in expired]

    # ------------------------------------------------------------------ my stays
    async def _show_stays(self, s: Session) -> list[dict]:
        stays = await self._db(self.repo.list_user_bookings, s.user["id"])
        if not stays:
            return [buttons_msg("You have no stays yet, your first one is just a few taps away! 🙂",
                                [("hotel:find", "🏨 Find a Hotel"), ("nav:menu", "🏠 Menu")])]
        s.step = "awaiting_hotel_stay"
        rows = [(f"hbk:{b['id']}", b["hotels"]["name"],
                 f"{STATUS_ICON.get(b['status'], '')} {b['status'].title()} · {date.fromisoformat(b['check_in']):%d %b} ➜ "
                 f"{date.fromisoformat(b['check_out']):%d %b} · {b['ref']}") for b in stays]
        rows.append(("nav:menu", "🏠 Main menu", ""))
        return [list_msg("📋 *Your stays*\nPick one to see details 👇", "View stays", rows, "Recent stays")]

    @staticmethod
    def _can_cancel(b: dict) -> bool:
        return b["status"] == "confirmed" and date.fromisoformat(b["check_in"]) >= now_ist().date()

    async def _show_stay(self, s: Session, booking_id: str) -> list[dict]:
        b = await self._db(self.repo.get_booking, booking_id, s.user["id"])
        if not b:
            return await self.on_enter(s)
        text = f"{STATUS_ICON.get(b['status'], '')} *{b['status'].title()}*\n{stay_text(b)}"
        if b["status"] == "pending":
            text += "\n\n⏳ Waiting for the payment. Use the payment link I sent, or it will be released automatically."
        elif self._can_cancel(b):
            text += f"\n\n{cancel_policy(b['check_in'])}"
        buttons = [("hotel:stays", "📋 All stays"), ("nav:menu", "🏠 Menu")]
        if self._can_cancel(b):
            buttons.insert(0, (f"hbkc:{b['id']}", "❌ Cancel Stay"))
        return [buttons_msg(text, buttons, image=hotel_image(b["hotels"]))]

    async def _ask_cancel(self, s: Session, booking_id: str) -> list[dict]:
        b = await self._db(self.repo.get_booking, booking_id, s.user["id"])
        if not b or not self._can_cancel(b):
            return await self.on_enter(s)
        until = free_cancel_until(b["check_in"])
        policy = (f"Free cancellation, so your {inr(b['total_price_inr'])} will be refunded in 5-7 working days."
                  if now_ist() <= until else f"Free cancellation ended on {until:%a, %d %b}, so no refund applies.")
        return [buttons_msg(f"⚠️ Cancel stay *{b['ref']}* at {b['hotels']['name']} ({fmt_day(b['check_in'])} ➜ {fmt_day(b['check_out'])})?\n\n{policy}",
                            [(f"hbkcy:{b['id']}", "Yes, cancel it"), (f"hbk:{b['id']}", "No, keep it")])]

    async def _do_cancel(self, s: Session, booking_id: str) -> list[dict]:
        b = await self._db(self.repo.get_booking, booking_id, s.user["id"])
        if not b or not self._can_cancel(b):
            return await self.on_enter(s)
        free = now_ist() <= free_cancel_until(b["check_in"])
        await self._db(self.repo.cancel_booking, b)
        refund = (f"💸 Your {inr(b['total_price_inr'])} refund has been initiated." if free
                  else "Free cancellation had ended, so no refund applies.")
        return self._home(s, f"😢 Sorry to see it go. Stay *{b['ref']}* has been cancelled.\n{refund}\n\nChanged your mind? I'm one tap away.")
