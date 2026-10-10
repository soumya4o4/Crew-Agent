"""Flight agent: search, book and manage flights through buttons and lists.

Button/list ids look like "kind:value" (e.g. "from:IDR", "flt:<uuid>"); `owns` lists the kinds.
Per-user state (step + context) lives in the Session the Concierge hands us.

Flights are live: every search asks Duffel (`self.live`) and mirrors the offers into our `flights` table, so a booking points at a
real row. Cities, countries and "what to do next" are never written into this file: airports come from the database, an
international flight triggers a passport/visa check, and the suggestions after a booking follow from the trip itself.

Money moves in this order: the traveller confirms, the Checkout holds a pending booking (re-pricing the offer first), the
traveller pays on Razorpay, and only then is the airline ticket issued. A ticket that cannot be issued is refunded at once.
"""
import asyncio
import logging
import random
import re
from datetime import date, datetime, timedelta

from app.agents.base import Agent, Session
from app.agents.flight.nlu import parse_trip
from app.agents.flight.formatting import (NEXT_STEPS, PITCH, city_tip, countdown, flight_card, flight_row, flight_tags, pnr_of,
                                          suggest_steps)
from app.agents.flight.travellers import parse_travellers
from app.core.geo import fresh_location, nearest_city
from app.core.config import settings
from app.core.messages import buttons_msg, list_msg, location_request_msg, reaction_msg, text_msg
from app.core.places import CITIES, city, example_route, find_city, country_of, is_international
from app.core.utils import IST, day_greeting, inr, now_ist, parse_date, to_ist
from app.services.duffel import GONE, DuffelError

MAX_DAYS_AHEAD = 29
EXPLORE_TRIES = 8  # cities "Surprise me" prices; each one is a live search
SUGGEST_DAYS = 6  # days after an empty one that we look at for "when does it fly?"
logger = logging.getLogger(__name__)
GREETINGS = {"hi", "hii", "hiii", "hello", "hey", "hlo", "namaste", "start", "hola", "yo"}
THANKS = {"thanks", "thank you", "thx", "ty", "thanks!", "shukriya", "dhanyavad"}
BYE = {"bye", "goodbye", "see you", "tata", "ok bye"}
SORT_LABELS = {"cheap": "cheapest first", "fast": "fastest first", "time": "by departure time"}
SORT_WORDS = {"cheapest": "cheap", "cheap": "cheap", "sasta": "cheap", "sabse sasta": "cheap", "fastest": "fast", "fast": "fast",
              "jaldi": "fast", "quickest": "fast", "by time": "time", "time": "time", "earliest": "time"}
ASKING_FOR_DATES = re.compile(r"\b(suggest|available|availability|which date|what date|kaun ?si|konsi|kab|when|options?|cheap\w*|sasta|dates?)\b")
STATUS_ICON = {"confirmed": "✅", "pending": "⏳", "cancelled": "❌"}
MENU_BUTTONS = [("menu:book", "🔍 Find flights"), ("menu:bookings", "🎫 My trips"), ("nav:menu", "🏠 Main menu")]
TRIP_KEYS = ("from", "to", "date", "sort", "flight_id", "passenger_name", "flight_summary", "explore", "pre_to",
             "pre_date", "pax", "names", "unit_price", "min_date", "await_flight", "visa_advice", "city_for", "await_place",
             "return_date", "pax_hint", "tdet", "cancel_quote")
YES = {"yes", "y", "ok", "okay", "confirm", "book", "book it", "book now", "haan", "ha", "kar do", "done", "sure", "go ahead",
       "do this now", "do it now", "do it", "proceed", "confirm it", "confirm booking", "kar do abhi", "yes please", "yep", "yeah", "done deal"}
NO = {"no", "n", "cancel", "nahi", "nope", "drop it"}
PASSPORT_FIX = ("change passport", "wrong passport", "passport change")
MAX_PAX = 6
MAX_ROWS = 8  # a WhatsApp list holds 10 rows; the last one is "Another city…" when there are more places than fit


def is_home_passport(citizen: str) -> bool:
    """Our visa desk files applications for Indian passports only (that is what its rules table describes)."""
    return "india" in (citizen or "").lower()


class FlightAgent(Agent):
    name = "flight"
    title = "Flights"
    emoji = "✈️"
    menu_desc = "Search, book & manage trips"
    owns = frozenset({"menu", "trip", "from", "to", "date", "sort", "flt", "act", "pax", "name", "cfm", "bk", "bkc", "bkcy", "cz"})
    bundleable = True

    def __init__(self, repo, payments=None, advisor=None, live=None):
        self.repo = repo  # FlightRepo
        self.payments = payments  # RazorpayGateway (or None: bookings confirm instantly)
        self.advisor = advisor  # TravelAdvisor (or None: no visa answers or city tips, just generic wording)
        self.live = live  # DuffelClient (or None: flights cannot be searched)

    def reset(self, s: Session) -> None:
        for k in TRIP_KEYS:
            s.ctx.pop(k, None)

    def expects_text(self, s: Session) -> bool:
        return s.step in ("awaiting_trip", "awaiting_date", "awaiting_name", "awaiting_citizen", "awaiting_city", "awaiting_flight",
                          "awaiting_flight_action", "awaiting_confirm")

    def keeps_text(self, text: str, other, known: set[str]) -> bool:
        if text.lower().strip() in PASSPORT_FIX:  # "change passport" would otherwise read as a visa request
            return True
        return super().keeps_text(text, other, known)

    def expects_location(self, s: Session) -> bool:
        return s.step == "awaiting_origin_loc"

    async def on_location(self, s: Session, loc: dict) -> list[dict]:
        """A shared pin picks the departure airport: the closest one we know within 150 km."""
        code = nearest_city(loc["lat"], loc["lon"], max_km=150)
        if not code:
            s.step = "awaiting_origin"
            return [text_msg("📍 Thanks! I don't have an airport near you yet, so please pick a city from the list instead."),
                    *await self._ask_origin(s)]
        return await self._use_origin(s, code, f"📍 Your nearest airport is *{city(code)}*.")

    async def _use_origin(self, s: Session, code: str, note: str = "") -> list[dict]:
        out = await self._on_reply(s, f"from:{code}")
        return ([text_msg(note)] if note else []) + out

    async def on_enter(self, s: Session) -> list[dict]:
        """No menu to click through: one question that takes the whole trip in a single message."""
        for k in TRIP_KEYS:
            s.ctx.pop(k, None)
        return self._ask_trip(s, intro="✈️ *Flights*\n")

    async def start(self, s: Session, slots: dict) -> list[dict]:
        """Free text like "Indore to Goa tomorrow, round trip": take everything we heard, ask only for the rest."""
        for k in TRIP_KEYS:
            s.ctx.pop(k, None)
        today = now_ist().date()
        said = parse_trip(slots["text"], today) if slots.get("text") else {}
        for k in ("from", "to", "date", "sort_by", "max_budget"):
            if slots.get(k):
                said[k] = slots[k]
        notes = self._take(s, said)
        c = s.ctx
        heard = " ".join(x for x in (f"{city(c['from'])}" if c.get("from") else "", f"➜ {city(c['to'])}" if c.get("to") else "",
                                     f"on {date.fromisoformat(c['date']):%a, %d %b}" if c.get("date") else "",
                                     f"return {date.fromisoformat(c['return_date']):%d %b}" if c.get("return_date") else "") if x)
        is_h = c.get("hinglish", False)
        intro_text = f"✈️ {heard} — chaliye check karte hain." if is_h else f"✈️ Searching flights for {heard}..."
        intro = [text_msg(intro_text)] if heard else []
        return intro + [text_msg(n) for n in notes] + await self._advance(s)

    def _take(self, s: Session, said: dict) -> list[str]:
        """Keep what the message told us about the trip (only valid parts); returns notes about parts we could not use."""
        c, notes, today = s.ctx, [], now_ist().date()
        frm, to = (said.get(k) if said.get(k) in CITIES else None for k in ("from", "to"))
        if frm and frm == to:
            to = None
        if frm:
            c["from"] = frm
        if to and to != c.get("from"):
            c["to"] = to
        for key in ("date", "return_date"):
            if d := said.get(key):
                if today <= date.fromisoformat(d) <= today + timedelta(days=MAX_DAYS_AHEAD):
                    c[key] = d
                elif key == "date":
                    notes.append("😕 I can book flights within the next 30 days, so please pick a closer date.")
        if c.get("return_date") and c.get("date") and c["return_date"] < c["date"]:
            c.pop("return_date")
        if 1 <= said.get("pax", 0) <= 6:
            c["pax_hint"] = said["pax"]
        if said.get("sort_by"):
            c["sort"] = said["sort_by"]
        if said.get("max_budget"):
            c["max_budget"] = said["max_budget"]

        # Keep shared trip context synced for other agents
        trip = c.setdefault("trip", {})
        if c.get("from"):
            trip["from"] = c["from"]
        if c.get("to"):
            trip["to"] = c["to"]
            trip["city"] = city(c["to"])
        if c.get("date"):
            trip["date"] = c["date"]

        return notes

    def _ask_trip(self, s: Session, intro: str = "") -> list[dict]:
        """One plain-text question for everything still missing (route, date)."""
        s.step = "awaiting_trip"
        c = s.ctx
        is_h = c.get("hinglish", False)
        have_from = city(c["from"]) if c.get("from") else ""
        have_to = city(c["to"]) if c.get("to") else ""
        if have_from and have_to:
            msg = f"✈️ {have_from} ➜ {have_to} — kis date ko nikalna hai?" if is_h else f"✈️ What date would you like to fly from {have_from} to {have_to}?"
        elif have_from:
            msg = f"✈️ {have_from} se kahan jaana hai, aur kab?" if is_h else f"✈️ Where would you like to fly to from {have_from}, and on which date?"
        elif have_to:
            msg = f"✈️ {have_to} ke liye kahan se nikalna hai aur kab?" if is_h else f"✈️ Where are you flying to {have_to} from, and on which date?"
        else:
            msg = f"Kahan se kahan jaana hai aur kab? Ek message mein bata do, jaise *{example_route()}, 15 Oct*." if is_h else f"Where would you like to travel, and on which date? (e.g. *{example_route()}, 15 Oct*)"
        return [text_msg((intro + msg).strip())]

    async def _on_trip_text(self, s: Session, text: str) -> list[dict]:
        """Any typed message while we are working out the trip: it can carry the route, dates and party all at once."""
        c, today = s.ctx, now_ist().date()
        said = parse_trip(text, today)
        if said.get("to") and not said.get("from") and c.get("to") and not c.get("from"):
            said["from"] = said.pop("to")  # a lone city answers whichever end is still empty
        elif said.get("from") and not said.get("to") and c.get("from") and not c.get("to"):
            said["to"] = said.pop("from")
        notes = self._take(s, said)
        if not any(k in said for k in ("from", "to", "date")) and not notes:
            low = text.lower().strip()
            if c.get("from") and c.get("to") and ASKING_FOR_DATES.search(low):
                return await self._suggest_dates(s)
            if self.advisor:
                if res := await self._ask_llm_assistant(s, text):
                    return res
            return self._ask_trip(s)
        return [text_msg(n) for n in notes] + await self._advance(s)

    async def _db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def _ask_llm_assistant(self, s: Session, text: str) -> list[dict] | None:
        """Consult TravelAdvisor LLM when user sends free text during flight selection or booking.
        Understands confirmation variations ('do this now', 'book it', 'go ahead'), multi-service intents,
        and answers flight questions naturally.
        """
        if not self.advisor or not hasattr(self.advisor, "evaluate_flight_intent"):
            return None
        c = s.ctx
        f = await self._db(self.repo.get_flight, c.get("flight_id")) if c.get("flight_id") else None
        pax_info = ", ".join(f"{d['name']} ({d.get('gender')}, {d.get('dob')})" for d in self._details(c)) if c.get("names") else ""
        flight_sum = c.get("flight_summary") or (f"{f['flight_no']} · {city(f['from_code'])} ➜ {city(f['to_code'])} · ₹{f['price_inr']}" if f else "")
        lang = "Hinglish" if c.get("hinglish") else "professional English"

        res = await self.advisor.evaluate_flight_intent(s.step, flight_sum, pax_info, text, lang)
        if not res:
            return None

        intent, reply, action = res.get("intent"), res.get("reply", ""), res.get("action", "none")

        if intent == "confirm" or action == "book":
            if s.step == "awaiting_confirm":
                return await self._on_reply(s, "cfm:yes")
            if s.step == "awaiting_flight_action":
                return await self._on_reply(s, "act:book")

        if intent == "cancel" or action == "cancel":
            return await self._on_reply(s, "cfm:no")

        if action == "fastest":
            c["sort"] = "fast"
            out = await self._show_results(s)
            return [text_msg(reply)] + out if reply else out

        if action == "cheapest":
            c["sort"] = "cheap"
            out = await self._show_results(s)
            return [text_msg(reply)] + out if reply else out

        if intent == "multi_service":
            trip = c.setdefault("trip", {})
            if f:
                trip["from"], trip["to"], trip["city"] = f["from_code"], f["to_code"], city(f["to_code"])
                trip["date"] = c.get("date")
                trip["summary"] = c.get("flight_summary")
                trip["unit_price"] = f["price_inr"]
                trip["flight_no"] = f["flight_no"]
                trip["arrival"] = f.get("arrival_time")
            if c.get("names"):
                trip["names"] = c["names"]

            dest = trip.get("city") or (city(c["to"]) if c.get("to") else "Destination")
            msg_text = reply or f"I can coordinate your complete {dest} trip — flight, hotel, and airport cab! Let me know if you would like to confirm the flight ticket first or explore hotels."
            return [text_msg(msg_text)]

        if action == "web_search" and res.get("query"):
            intermediate_msg = reply or ("Ek sec bhai, main check karke batata hoon..." if lang == "Hinglish" else "One moment, let me check that for you...")
            try:
                from app.services.whatsapp_service import WhatsAppService
                await WhatsAppService.send(s.phone, text_msg(intermediate_msg))
            except Exception:
                pass
            try:
                from tavily import TavilyClient
                from app.core.config import settings
                def _do_search():
                    if not settings.TAVILY_API_KEY:
                        raise ValueError("TAVILY_API_KEY is missing")
                    client = TavilyClient(api_key=settings.TAVILY_API_KEY)
                    return client.search(res["query"], search_depth="basic", max_results=3).get("results", [])
                results = await self._db(_do_search)
                search_text = "\n".join(f"- {r['title']}: {r.get('content', '')}" for r in results) if results else "No results found."
                follow_up = f"System: Web search results for '{res['query']}':\n{search_text}\n\nNow, answer the user's question naturally using these facts."
                res2 = await self.advisor.evaluate_flight_intent(s.step, flight_sum, pax_info, text + "\n\n" + follow_up, lang)
                if res2 and res2.get("reply"):
                    return [text_msg(res2["reply"])]
            except Exception as e:
                logger.exception(f"Web search failed: {e}")
                fail_msg = "Ek sec bhai, abhi search nahi ho raha. Thodi der mein dobara try karte hain." if lang == "Hinglish" else "Sorry, I couldn't search the web right now."
                return [text_msg((reply + "\n\n" + fail_msg) if reply else fail_msg)]

        if intent == "flight_question" and reply:
            return [text_msg(reply)]

        if reply:
            return [text_msg(reply)]

        return None

    # -------------------------------------------------------------------- entry point
    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        """Run one message against an already-loaded session (the Concierge calls this)."""
        return await (self._on_reply(s, reply_id) if reply_id else self._on_text(s, text.strip()))

    async def _on_text(self, s: Session, text: str) -> list[dict]:
        low = text.lower()
        if low in GREETINGS or low in ("menu", "restart", "reset", "home", "cancel"):
            return self._menu(s, greet=True)
        if low in PASSPORT_FIX:
            s.ctx.pop("citizen", None)
            if s.ctx.get("flight_id"):  # looking at a flight right now: ask at once and show it again with the right advice
                s.ctx["await_flight"], s.step = s.ctx.pop("flight_id"), "awaiting_citizen"
                return [text_msg("✏️ Which passport will you travel on? Type the country, like *Nepal* or *United Kingdom*.")]
            return [text_msg("👍 Okay! I'll ask for your passport country again on your next international flight.")]
        if s.step == "awaiting_trip":
            return await self._on_trip_text(s, text)
        if s.step == "awaiting_date":
            if not s.ctx.get("explore") and any(k in parse_trip(text, now_ist().date()) for k in ("date", "return_date")):
                return await self._on_trip_text(s, text)
            return await self._on_date_text(s, text)
        if s.step == "awaiting_name":
            return self._on_name_text(s, text)
        if s.step == "awaiting_flight_action" and s.ctx.get("flight_id"):
            return await self._on_booking_text(s, text)
        if s.step == "awaiting_confirm" and s.ctx.get("flight_id"):
            if low in YES:
                return await self._on_reply(s, "cfm:yes")
            if low in NO:
                return await self._on_reply(s, "cfm:no")
            if (people := self._read_travellers(s, text)) and all(p.get("name") for p in people):
                return self._use_travellers(s, people[:MAX_PAX])
            # Consult LLM brain if confirmation message has extra words or questions
            if res := await self._ask_llm_assistant(s, text):
                return res
            return self._confirm(s)
        if s.step == "awaiting_citizen":
            return await self._on_citizen_text(s, text)
        if s.step == "awaiting_city":
            return await self._on_city_text(s, text)
        if s.step == "awaiting_flight" and s.ctx.get("date") and not s.ctx.get("explore"):  # "cheapest", "fastest": re-sort the list
            if low in SORT_WORDS:
                s.ctx["sort"] = SORT_WORDS[low]
            elif any(k in parse_trip(text, now_ist().date()) for k in ("from", "to", "date")):  # "make it 15 oct" / "to goa instead"
                return await self._on_trip_text(s, text)
            elif res := await self._ask_llm_assistant(s, text):
                return res
            return await self._show_results(s)
        if low in ("help", "?"):
            return self._help(s)
        if low in THANKS:
            return self._menu(s, note="Anytime! 😊 Happy to help. Anything else?")
        if low in BYE:
            return self._menu(s, note="Safe travels! ✈️ Jab bhi zarurat ho, bata dena.")
        return self._menu(s)

    async def _on_reply(self, s: Session, reply_id: str) -> list[dict]:
        kind, _, val = reply_id.partition(":")
        c = s.ctx
        if kind == "nav" or (kind == "menu" and val == "home"):
            return self._menu(s)
        if kind == "from" and val == "loc":
            if loc := fresh_location(c):
                if code := nearest_city(loc["lat"], loc["lon"], max_km=150):
                    return await self._use_origin(s, code, f"📍 Using your nearest airport: *{city(code)}*.")
            s.step = "awaiting_origin_loc"
            return [location_request_msg("📍 Share your location and I'll pick the airport closest to you. "
                                         "I only use it for this, and forget it after a few hours.")]
        if kind in ("from", "to") and val == "more":
            c["city_for"], s.step = kind, "awaiting_city"
            return [text_msg("🏙️ Type the city you want, like *Paris* or *Pune*." if kind == "to"
                             else "🏙️ Type the city you're flying from.")]
        if kind == "menu":
            if val == "book":
                c.pop("explore", None)
                return await self._ask_origin(s)
            if val == "quick":
                return await self._quick_trips(s)
            if val == "bookings":
                return await self._show_bookings(s)
            if val == "help":
                return self._help(s)
        elif kind == "trip":
            return await self._on_trip(s, val)
        elif kind == "from":
            c["from"] = val
            if c.get("explore"):
                c.pop("to", None)
                c.pop("date", None)
                return self._ask_date(s)
            pre_to, pre_date = c.pop("pre_to", None), c.pop("pre_date", None)
            if pre_to and pre_to != val:  # user already told us where they're going
                c["to"] = pre_to
                if pre_date:
                    c["date"] = pre_date
            elif c.get("to") == val:
                c.pop("to", None)
            return await self._advance(s)
        elif kind == "to" and c.get("from"):
            c["to"] = val
            pre_date = c.pop("pre_date", None)
            if pre_date:
                c["date"] = pre_date
                return await self._show_results(s)
            return self._ask_date(s)
        elif kind == "date" and c.get("from") and (c.get("to") or c.get("explore")):
            if val == "more":
                s.step = "awaiting_date"
                return [text_msg("📅 Type the date, e.g. *15/10* or *15 Oct* (within the next 30 days).")]
            c["date"] = val
            return await self._show_explore(s) if c.get("explore") else await self._show_results(s)
        elif kind == "sort" and c.get("date"):
            c["sort"] = val
            return await self._show_results(s)
        elif kind == "flt":
            return await self._show_flight(s, val)
        elif kind == "cz" and (c.get("await_flight") or c.get("await_place")):
            return await self._on_citizen_tap(s, val)
        elif kind == "act" and val == "date" and c.get("from"):
            return self._ask_date(s)
        elif kind == "act" and val == "results" and c.get("date"):
            return await (self._show_explore(s) if c.get("explore") else self._show_results(s))
        elif kind == "act" and val == "return" and c.get("trip"):
            trip = c["trip"]
            back = trip.get("return_date")
            self.reset(s)
            c["from"], c["to"], c["min_date"] = trip["to"], trip["from"], trip["date"]
            if back and date.fromisoformat(back) >= now_ist().date():  # they already told us when they come back
                c["date"] = back
                return await self._advance(s)
            return self._ask_date(s)
        elif kind == "act" and val == "book" and c.get("flight_id"):
            return await self._ask_travellers(s)
        elif kind == "pax" and c.get("flight_id") and val.isdigit():
            c["pax"], c["names"], c["tdet"] = int(val), [], {}
            return self._ask_passenger(s)
        elif kind == "cfm" and c.get("flight_id"):
            if val == "yes" and len(c.get("names") or []) == c.get("pax", 1) and not self._missing(c):
                return await self._do_booking(s)
            if val == "name":
                c["names"], c["tdet"] = [], {}
                return self._prompt_name(s)
            return self._menu(s, note="No worries, I've dropped that booking. 👍")
        elif kind == "bk":
            return await self._show_booking(s, val)
        elif kind == "bkc":
            return await self._ask_cancel(s, val)
        elif kind == "bkcy":
            return await self._do_cancel(s, val)
        return self._menu(s, note="That option is no longer valid, let's start over.")

    # ------------------------------------------------------------------------ screens
    def _menu(self, s: Session, greet: bool = False, note: str = "") -> list[dict]:
        s.step = "menu"
        for k in TRIP_KEYS:
            s.ctx.pop(k, None)
        first = (s.user.get("name") or "").split(" ")[0]
        first = first if first and first != "Unknown" else "there"
        if greet:
            body = (f"{day_greeting()}, {first}! 👋\nI'm your travel concierge. I'll get you on a flight in under a minute ⚡\n\n"
                    "Where to? Pick below, or type *help* anytime.")
        else:
            body = note or "What would you like to do next?"
        return [buttons_msg(body, MENU_BUTTONS)]

    def _help(self, s: Session) -> list[dict]:
        s.step = "menu"
        return [buttons_msg(
            "ℹ️ *How it works*\n"
            "• *Book a Flight*: pick route, date and flight in about 2 minutes\n"
            "• *Quick Trips*: repeat your last route, try a popular one, or let me surprise you 🎲\n"
            "• *My Bookings*: view your PNR or cancel a booking\n"
            "• Flying abroad? I'll check the visa for your passport and help you apply\n"
            "• Type *menu* anytime to start over",
            MENU_BUTTONS)]

    # ------------------------------------------------------------- choosing cities
    @staticmethod
    def _city_rows(kind: str, airports: list[dict], first_country: str, exclude: str | None = None,
                   limit: int = MAX_ROWS) -> list[tuple[str, str, str]]:
        """Airport rows for a list message, the traveller's own country first; the tail becomes "Another city…"."""
        pool = sorted((a for a in airports if a["code"] != exclude), key=lambda a: (a["country"] != first_country, a["country"], a["city"]))
        rows = [(f"{kind}:{a['code']}", a["city"], a["name"] if a["country"] == first_country else f"{a['country']} · {a['name']}")
                for a in pool]
        if len(rows) > limit + 1:
            rows = rows[:limit] + [(f"{kind}:more", "Another city…", "Type the city name")]
        return rows

    async def _home_country(self, s: Session, airports: list[dict]) -> str:
        """Where this traveller usually flies from: their last booking, else the country with the most airports."""
        last = await self._db(self.repo.list_user_bookings, s.user["id"], 1)
        if last:
            return country_of(last[0]["flights"]["from_code"])
        counts: dict[str, int] = {}
        for a in airports:
            counts[a["country"]] = counts.get(a["country"], 0) + 1
        return max(counts, key=counts.get) if counts else ""

    async def _ask_origin(self, s: Session, intro: str = "") -> list[dict]:
        if not s.ctx.get("explore"):
            return self._ask_trip(s, intro)
        s.step = "awaiting_origin"
        airports = await self._db(self.repo.list_airports)
        home = await self._home_country(s, airports)
        local = [a for a in airports if a["country"] == home] or airports
        rows = [("from:loc", "📍 Use my location", "Nearest airport to you")]
        last = None if s.ctx.get("explore") else await self._db(self.repo.list_user_bookings, s.user["id"], 1)
        if last:
            a, b = last[0]["flights"]["from_code"], last[0]["flights"]["to_code"]
            rows.append((f"trip:{a}-{b}", f"🔁 {city(a)} → {city(b)}"[:24], "Repeat your last trip"))
        limit = MAX_ROWS - (1 if last else 0)
        city_rows = self._city_rows("from", local, home, limit=limit)
        if len(local) < len(airports) and not any(r[0] == "from:more" for r in city_rows):  # flying from abroad: type the city
            city_rows = city_rows[:limit] + [("from:more", "Another city…", "Type the city name")]
        rows += city_rows
        body = ("🎲 *Surprise me!* Where are you flying from?\nI'll find the cheapest getaways."
                if s.ctx.get("explore") else "🛫 *Where are you flying from?*")
        return [list_msg(intro + body, "Choose city", rows, "Departure city")]

    async def _advance(self, s: Session) -> list[dict]:
        """Jump to the first step whose answer we don't have yet."""
        c = s.ctx
        if not (c.get("from") and c.get("to") and c.get("date")):
            return self._ask_trip(s)
        return await self._show_results(s)

    async def _on_city_text(self, s: Session, text: str) -> list[dict]:
        """"Another city…": the traveller typed a name. We can only fly where the airports table has an airport."""
        kind = s.ctx.get("city_for")
        code = find_city(text)
        served = {a["code"] for a in await self._db(self.repo.list_airports)}
        if not code or (kind == "from" and code not in served) or (kind == "to" and code == s.ctx.get("from")):
            return [text_msg(f"😕 I can't book flights for *{text[:40]}* yet. Try another city, or type *menu*.")]
        s.ctx.pop("city_for", None)
        return await self._on_reply(s, f"{kind}:{code}")

    def _ask_date(self, s: Session) -> list[dict]:
        s.step = "awaiting_date"
        today = now_ist().date()
        first = max(today, date.fromisoformat(s.ctx["min_date"])) if s.ctx.get("min_date") else today  # return trips start at the outbound day
        rows = []
        for i in range(9):
            d = first + timedelta(days=i)
            if d > today + timedelta(days=MAX_DAYS_AHEAD):
                break
            title = (f"Today · {d:%a %d %b}" if d == today else f"Tomorrow · {d:%a %d %b}" if d == today + timedelta(days=1)
                     else f"{d:%A} · {d:%d %b}")
            rows.append((f"date:{d.isoformat()}", title, "Weekend vibes 🎉" if d.weekday() >= 5 else ""))
        rows.append(("date:more", "Another date…", "Type a date yourself"))
        route = f"Exploring from *{city(s.ctx['from'])}*" if s.ctx.get("explore") \
            else f"*{city(s.ctx['from'])} ➜ {city(s.ctx['to'])}*"
        head = "↩️ *Return trip*\n" if s.ctx.get("min_date") else ""
        return [list_msg(f"{head}📅 {route}\nWhich day are we flying? (or type a date, e.g. 15/10)", "Choose date", rows, "Travel date")]

    async def _on_date_text(self, s: Session, text: str) -> list[dict]:
        if not (s.ctx.get("from") and (s.ctx.get("to") or s.ctx.get("explore"))):
            return self._menu(s, greet=True)
        today = now_ist().date()
        d = parse_date(text, today)
        if d is None and not s.ctx.get("explore") and ASKING_FOR_DATES.search(text.lower()):
            return await self._suggest_dates(s)                    # "suggest me a date": show when flights actually run
        if d is None or d < today or d > today + timedelta(days=MAX_DAYS_AHEAD):
            if self.advisor:
                if res := await self._ask_llm_assistant(s, text):
                    return res
            is_h = s.ctx.get("hinglish", False)
            prompt = ("Agli date 30 din ke andar bataiye (jaise *15 Oct* ya *tomorrow*)." if is_h
                      else "Please mention a travel date within the next 30 days (e.g. *15 Oct* or *tomorrow*).")
            return [text_msg(prompt)]
        s.ctx["date"] = d.isoformat()
        return await self._show_explore(s) if s.ctx.get("explore") else await self._show_results(s)

    # --------------------------------------------------------------- quick trips / explore
    async def _quick_trips(self, s: Session) -> list[dict]:
        s.step = "awaiting_trip"
        for k in TRIP_KEYS:
            s.ctx.pop(k, None)
        rows = []
        last = await self._db(self.repo.list_user_bookings, s.user["id"], 1)
        last_route = (last[0]["flights"]["from_code"], last[0]["flights"]["to_code"]) if last else None
        if last_route:
            a, b = last_route
            rows.append((f"trip:{a}-{b}", f"🔁 {city(a)} → {city(b)}"[:24], "Repeat your last trip"))
        for a, b in await self._db(self.repo.popular_routes, 6):
            if (a, b) != last_route:
                rows.append((f"trip:{a}-{b}", f"{city(a)} → {city(b)}",
                             f"{country_of(b)} · popular" if is_international(a, b) else "Popular route"))
        rows.append(("trip:explore", "🎲 Surprise me", "Cheapest getaways from your city"))
        return [list_msg("⚡ *Quick Trips*\nOne tap and we're halfway there. Where to?", "See trips", rows[:10], "Popular right now")]

    async def _on_trip(self, s: Session, val: str) -> list[dict]:
        s.ctx.pop("to", None)
        if val == "explore":
            s.ctx["explore"] = True
            return await self._ask_origin(s)
        a, _, b = val.partition("-")
        if a not in CITIES or b not in CITIES:
            return self._menu(s, note="That trip isn't available, let's start fresh.")
        s.ctx.pop("explore", None)
        s.ctx["from"], s.ctx["to"] = a, b
        return self._ask_date(s)

    async def _search(self, frm: str, to: str, day: date, pax: int = 1) -> list[dict] | None:
        """Flights on a day that have not left yet, live from Duffel and mirrored into our table (so each has an id).
        None if the airlines could not be asked."""
        if self.live is None:
            return None
        try:
            found = await self.live.search(frm, to, day, pax)
        except Exception:
            logger.exception("Flight search %s-%s on %s failed", frm, to, day)
            return None
        return await self._db(self.repo.sync_live_flights, [f for f in found if to_ist(f["departure_time"]) > now_ist()])

    @staticmethod
    def _search_failed() -> list[dict]:
        return [buttons_msg("😕 I couldn't reach the airlines just now. Please try again in a moment.",
                            [("act:results", "🔄 Try again"), ("nav:menu", "🏠 Menu")])]

    async def _show_explore(self, s: Session) -> list[dict]:
        """"Surprise me": the cheapest flight to a handful of cities we pick, priced live."""
        c = s.ctx
        d = date.fromisoformat(c["date"])
        popular = [b for a, b in await self._db(self.repo.popular_routes, 12) if a == c["from"]]
        others = [a["code"] for a in await self._db(self.repo.list_airports) if a["code"] not in (c["from"], *popular)]
        picks = (popular + random.sample(others, min(len(others), EXPLORE_TRIES)))[:EXPLORE_TRIES]
        results = await asyncio.gather(*(self._search(c["from"], to, d) for to in picks))
        if picks and all(r is None for r in results):
            return self._search_failed()
        cheapest = [min(found, key=lambda f: f["price_inr"]) for found in results if found]
        cheapest.sort(key=lambda f: f["price_inr"])
        if not cheapest:
            s.step = "awaiting_date"
            return [buttons_msg(f"😕 No flights from {city(c['from'])} on {d:%d %b}. Try another day?",
                                [("act:date", "📅 Other date"), ("nav:menu", "🏠 Menu")])]
        rows = [(f"flt:{f['id']}", f"{city(f['to_code'])} · {inr(f['price_inr'])}",
                 f"{country_of(f['to_code'])} · {to_ist(f['departure_time']):%H:%M}") for f in cheapest[:9]]
        rows.append(("nav:menu", "🏠 Main menu", ""))
        s.step = "awaiting_flight"
        return [list_msg(f"🎲 *Getaways from {city(c['from'])}* · {d:%a, %d %b}\n"
                         f"The cheapest flight to each city. Fancy *{city(cheapest[0]['to_code'])}* for {inr(cheapest[0]['price_inr'])}? 😉",
                         "See getaways", rows, "Cheapest per city")]

    # ------------------------------------------------------------------------- results
    async def _show_results(self, s: Session) -> list[dict]:
        c = s.ctx
        d = date.fromisoformat(c["date"])
        flights = await self._search(c["from"], c["to"], d, c.get("pax_hint") or 1)
        if flights is None:
            return self._search_failed()
        if not flights:
            return await self._suggest_dates(s, f"😕 No flights for {city(c['from'])} ➜ {city(c['to'])} on {d:%a, %d %b}.")

        if c.get("max_budget"):
            flights = [f for f in flights if f["price_inr"] <= c["max_budget"]]
            if not flights:
                return [buttons_msg(f"😕 No flights found under {inr(c['max_budget'])} for this route.",
                                    [("act:date", "📅 Other date"), ("nav:menu", "🏠 Menu")])]

        tags = flight_tags(flights)
        key = {"cheap": lambda f: (f["price_inr"], f["departure_time"]),
               "fast": lambda f: (f["duration_min"], f["price_inr"]),
               "time": lambda f: f["departure_time"]}[c.get("sort", "time")]
        flights.sort(key=key)
        shown = flights[:9]
        rows = [flight_row(f, tags[f["id"]]) for f in shown] + [("nav:menu", "🏠 Main menu", "")]
        s.step = "awaiting_flight"
        cheapest = min(f["price_inr"] for f in flights)
        extra = f" (showing top {len(shown)})" if len(flights) > len(shown) else ""
        is_h = c.get("hinglish", False)
        if is_h:
            prompt_line = f"✈️ *{city(c['from'])} ➜ {city(c['to'])}* · {d:%a, %d %b}\n*{len(flights)}* flights mili hain{extra}, starting from *{inr(cheapest)}* ({SORT_LABELS[c.get('sort', 'time')]}).\nNeeche se flight choose karein, ya *cheapest* / *fastest* likhein."
        else:
            prompt_line = f"✈️ *{city(c['from'])} ➜ {city(c['to'])}* · {d:%a, %d %b}\nFound *{len(flights)}* flights{extra} starting from *{inr(cheapest)}* ({SORT_LABELS[c.get('sort', 'time')]}).\nSelect a flight below, or type *cheapest* / *fastest* to re-sort."
        return [list_msg(prompt_line, "View flights", rows, "Available flights")]

    async def _suggest_dates(self, s: Session, header: str = "") -> list[dict]:
        """The next few days this route has flights, with the cheapest fare each day. Nothing in that window: say so plainly."""
        c = s.ctx
        today = now_ist().date()
        first = date.fromisoformat(c["date"]) + timedelta(days=1) if c.get("date") else today
        days = [d for d in (first + timedelta(days=i) for i in range(SUGGEST_DAYS)) if today <= d <= today + timedelta(days=MAX_DAYS_AHEAD)]
        results = await asyncio.gather(*(self._search(c["from"], c["to"], d, c.get("pax_hint") or 1) for d in days))
        if days and all(r is None for r in results):
            return self._search_failed()
        by_day = {d: found for d, found in zip(days, results) if found}
        if not by_day:
            return self._no_route(s)
        cheapest_day = min(by_day, key=lambda d: min(f["price_inr"] for f in by_day[d]))
        rows = []
        for day in sorted(by_day)[:MAX_ROWS]:
            fares = [f["price_inr"] for f in by_day[day]]
            tag = " · 💸 cheapest" if day == cheapest_day else ""
            rows.append((f"date:{day.isoformat()}", f"{day:%a, %d %b}", f"from {inr(min(fares))} · {len(fares)} flight{'s' if len(fares) > 1 else ''}{tag}"))
        rows.append(("date:more", "Another date…", "Type a date yourself"))
        s.step = "awaiting_date"
        lead = f"{header}\n\n" if header else ""
        return [list_msg(f"{lead}📅 *{city(c['from'])} ➜ {city(c['to'])}* has flights on these days. Pick one 👇",
                         "Choose date", rows, "Available dates")]

    def _no_route(self, s: Session) -> list[dict]:
        """Nothing flies this route in the days we looked at."""
        c = s.ctx
        c.pop("date", None)
        s.step = "menu"
        return [buttons_msg(f"😕 I couldn't find flights from *{city(c['from'])}* to *{city(c['to'])}* around these dates. "
                            "Try other dates, or another city?",
                            [("act:date", "📅 Other date"), ("menu:book", "🔍 New search"), ("nav:menu", "🏠 Menu")])]

    async def _show_flight(self, s: Session, flight_id: str) -> list[dict]:
        f = await self._db(self.repo.get_flight, flight_id)
        if not f or f["status"] == "cancelled" or to_ist(f["departure_time"]) <= now_ist():
            return [buttons_msg("😕 Oh no, this flight is no longer available.",
                                [("act:results", "↩️ Other flights"), ("nav:menu", "🏠 Menu")])]
        c = s.ctx
        c["flight_id"] = flight_id
        dep = to_ist(f["departure_time"])
        c["flight_summary"] = (f"{f['flight_no']} · {city(f['from_code'])} ➜ {city(f['to_code'])}\n"
                               f"🛫 {dep:%a, %d %b} · {dep:%H:%M}")
        s.step = "awaiting_flight_action"
        buttons = [("act:book", "✅ Book Now"), ("act:results", "↩️ Other flights"), ("nav:menu", "🏠 Menu")]
        is_h = c.get("hinglish", False)
        footer = ("Tap *Book Now*, ya traveller ka naam likhein (jaise *Rahul Verma, 14/03/1992, M*)." if is_h
                  else "Tap *Book Now*, or type the traveller name(s) (e.g. *Rahul Verma, 14/03/1992, M*) to proceed.")
        return [buttons_msg(f"{flight_card(f)}\n\n💰 *{inr(f['price_inr'])}* per person\n\n{footer}", buttons)]

    # ------------------------------------------------------------- passport and visa
    def _ask_citizen(self, s: Session, f: dict) -> list[dict]:
        s.step = "awaiting_citizen"
        home, dest = country_of(f["from_code"]), country_of(f["to_code"])
        return [buttons_msg(
            f"🌍 *{city(f['to_code'])}, {dest}* is an international trip, and entry rules depend on your passport.\n\n"
            "Which passport will you travel on? I'll check if you need a visa.",
            [("cz:home", f"🛂 {home}"), ("cz:other", "🌍 Other passport")])]

    async def _on_citizen_tap(self, s: Session, val: str) -> list[dict]:
        c = s.ctx
        if val == "home":
            f = await self._db(self.repo.get_flight, c["await_flight"]) if c.get("await_flight") else None
            c["citizen"] = country_of(f["from_code"]) if f else await self._home_country(s, await self._db(self.repo.list_airports))
            return await self._after_citizen(s)
        s.step = "awaiting_citizen"
        return [text_msg("✏️ Type your passport country, like *Nepal* or *United Kingdom*.")]

    async def _on_citizen_text(self, s: Session, text: str) -> list[dict]:
        if not (s.ctx.get("await_flight") or s.ctx.get("await_place")):
            return self._menu(s, greet=True)
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,39}", text):
            return [text_msg("😕 Please type just the country name, like *India* or *United States*.")]
        s.ctx["citizen"] = " ".join(text.split()).title()
        return await self._after_citizen(s)

    async def _after_citizen(self, s: Session) -> list[dict]:
        c = s.ctx
        if c.get("await_flight"):
            return await self._show_flight(s, c.pop("await_flight"))
        return await self._idea_advice(s, c.pop("await_place"))

    async def trip_idea(self, s: Session, place: str) -> list[dict]:
        """They want to go somewhere we have no airport for: say so honestly and offer the plan instead."""
        for k in TRIP_KEYS:
            s.ctx.pop(k, None)
        found = await self.advisor.place_country(place) if self.advisor else None
        name, country = found or (place, "")
        s.step = "menu"
        return [buttons_msg(f"🌍 *{name}*" + (f", {country}" if country else "") +
                            f"\n\n✈️ I can't book flights to {name} yet, but I can plan the trip for you.",
                            [("svc:planner", "🗺️ Plan My Trip"), ("nav:menu", "🏠 Menu")])]

    # ------------------------------------------------------------------------- booking
    async def _start_booking(self, s: Session) -> list[dict] | None:
        """Remember the fare of the flight they are looking at. A message if it has gone, else None."""
        f = await self._db(self.repo.get_flight, s.ctx["flight_id"])
        if not f or f["status"] == "cancelled" or to_ist(f["departure_time"]) <= now_ist():
            return [buttons_msg("😕 Oh no, this flight is no longer available.",
                                [("act:results", "↩️ Other flights"), ("nav:menu", "🏠 Menu")])]
        s.ctx["unit_price"] = f["price_inr"]
        return None

    async def _ask_travellers(self, s: Session) -> list[dict]:
        if gone := await self._start_booking(s):
            return gone
        s.ctx["pax"], s.ctx["names"], s.ctx["tdet"] = min(s.ctx.get("pax_hint") or 1, MAX_PAX), [], {}
        return self._ask_passenger(s)

    def _ask_passenger(self, s: Session) -> list[dict]:
        """One traveller we know by name goes straight on; otherwise one question takes every name, birth date and gender."""
        c = s.ctx
        name = s.user.get("name")
        if c.get("pax", 1) == 1 and name and name != "Unknown" and not c.get("names"):
            return self._use_travellers(s, [{"name": name}])
        if len(c.get("names") or []) == c.get("pax", 1):
            return self._after_travellers(s)
        return self._prompt_name(s)

    def _prompt_name(self, s: Session) -> list[dict]:
        s.step = "awaiting_name"
        pax = s.ctx.get("pax", 1)
        who = "the traveller's full name, date of birth and gender" if pax == 1 else \
            f"the full name, date of birth and gender of all {pax} travellers, one per line"
        return [text_msg(f"✏️ Please type {who}, as on the ID. The airline needs them for the ticket.\n"
                         "Like: *Rahul Verma, 14/03/1992, M*")]

    def _use_travellers(self, s: Session, people: list[dict]) -> list[dict]:
        """Take the travellers a message named, fill in what we remember about them, then ask for whatever is still missing."""
        c = s.ctx
        saved = (s.user.get("preferences") or {}).get("travellers") or {}
        if "pax" not in c or len(people) > c.get("pax", 0):
            c["pax"] = len(people)
        c["names"] = [p["name"] for p in people]
        c["tdet"] = {}
        for p in people:
            det = {k: p[k] for k in ("dob", "gender") if p.get(k)}
            for k, v in (saved.get(p["name"].lower()) or {}).items():
                det.setdefault(k, v)
            c["tdet"][p["name"]] = det
        return self._after_travellers(s)

    @staticmethod
    def _missing(c: dict) -> list[str]:
        """The travellers we still need a date of birth or a gender for."""
        return [n for n in c.get("names") or [] if not all((c.get("tdet") or {}).get(n, {}).get(k) for k in ("dob", "gender"))]

    def _after_travellers(self, s: Session) -> list[dict]:
        missing = self._missing(s.ctx)
        if not missing:
            return self._confirm(s)
        s.step = "awaiting_name"
        if len(missing) == 1:
            n = missing[0]
            det = s.ctx.get("tdet", {}).get(n, {})
            needs = []
            if not det.get("dob"):
                needs.append("date of birth")
            if not det.get("gender"):
                needs.append("gender")
            needs_str = " and ".join(f"*{x}*" for x in needs)
            example = "M" if "gender" in needs and "dob" not in needs else ("14/03/1992" if "dob" in needs and "gender" not in needs else "14/03/1992 M")
            return [text_msg(f"✏️ For *{n}* I also need the {needs_str}, because the airline needs them for "
                             f"the ticket. Like: *{example}*")]
        return [text_msg(f"✏️ I still need the date of birth and gender of {', '.join(f'*{n}*' for n in missing)}. One per line, like:\n"
                         f"*{missing[0]} 14/03/1992 M*")]

    def _read_travellers(self, s: Session, text: str) -> list[dict] | None:
        own = s.user.get("name") if s.user.get("name") != "Unknown" else ""
        if text.lower().strip() in YES | NO:
            return None
        return parse_travellers(text, now_ist().date(), own or "")

    async def _on_booking_text(self, s: Session, text: str) -> list[dict]:
        """Typed words under a flight card: "book" (or a tap on Book Now), traveller names, or "other flights"."""
        low = text.lower().strip()
        if low in YES or re.fullmatch(r"book( it| now| this)?( for me)?", low):
            return await self._on_reply(s, "act:book")
        if re.search(r"\b(other|another|more|different|back)\b.*\b(flights?|options?)\b|\bflights?\b.*\b(other|another|more)\b", low):
            return await self._show_results(s)
        if (people := self._read_travellers(s, text)) and all(p.get("name") for p in people):
            pax = s.ctx.get("pax_hint") or 1
            if len(people) < pax:
                s.ctx["pax"] = pax
                s.ctx["names"] = [p["name"] for p in people]
                s.ctx["tdet"] = {p["name"]: {k: p[k] for k in ("dob", "gender") if p.get(k)} for p in people}
                s.step = "awaiting_name"
                return [text_msg(f"😕 I have {pax} adults on this booking. I have details for {len(people)} traveller(s). Please provide all {pax} names, one per line.")]
            return await self._start_booking(s) or self._use_travellers(s, people[:MAX_PAX])

        # If user writes conversational text or questions, let LLM assistant handle it
        if self.advisor:
            if res := await self._ask_llm_assistant(s, text):
                return res

        is_h = s.ctx.get("hinglish", False)
        prompt = ("*Book Now* tap karein, ya traveller ka naam likhein (jaise *Rahul Verma, 14/03/1992, M*)." if is_h
                  else "Tap *Book Now*, or type the traveller name(s), like *Rahul Verma, 14/03/1992, M*.")
        return [text_msg(f"👍 {prompt}")]

    def _on_name_text(self, s: Session, text: str) -> list[dict]:
        c = s.ctx
        if not c.get("flight_id"):
            return self._menu(s, greet=True)
        if not (people := self._read_travellers(s, text)):
            return [text_msg("😕 Please use letters only for the names (2-60 characters each), and write the date of birth like 14/03/1992.")]
        missing = self._missing(c)
        named = [p for p in people if p.get("name")]
        if c.get("names") and missing and len(named) == len([p for p in people if p.get("name")]):
            known = {n.lower(): n for n in c["names"]}
            if all(p["name"].lower() in known for p in named):  # details for the people we already have
                unnamed = [p for p in people if not p.get("name")]
                for p in named:
                    c["tdet"][known[p["name"].lower()]].update({k: p[k] for k in ("dob", "gender") if p.get(k)})
                for p, n in zip(unnamed, missing):
                    c["tdet"][n].update({k: p[k] for k in ("dob", "gender") if p.get(k)})
                return self._after_travellers(s)
        if len(named) < len(people):
            return [text_msg("😕 Please start each traveller with the full name, like *Rahul Verma, 14/03/1992, M*.")]
        pax = c.get("pax", 1)
        if len(people) < pax:
            return [text_msg(f"😕 I need {pax} travellers, one per line. You gave {len(people)}.")]
        return self._use_travellers(s, people[:MAX_PAX])

    @staticmethod
    def _details(c: dict) -> list[dict]:
        """[{name, dob, gender}] in booking order."""
        return [{"name": n, **{k: (c.get("tdet") or {}).get(n, {}).get(k) for k in ("dob", "gender")}} for n in c["names"]]

    def _confirm(self, s: Session) -> list[dict]:
        s.step = "awaiting_confirm"
        c = s.ctx
        pax, unit = c.get("pax", 1), c.get("unit_price", 0)
        people = "\n".join(f"👤 {d['name']} · {date.fromisoformat(d['dob']):%d %b %Y} · {'Male' if d['gender'] == 'm' else 'Female'}"
                           for d in self._details(c))
        total = f"💰 {inr(unit)} × {pax} = *{inr(unit * pax)}*" if pax > 1 else f"💰 *{inr(unit)}*"
        is_h = c.get("hinglish", False)
        footer = ("🪪 ID proof aur ticket par naam match hona chahiye.\n\nKya main ticket confirm kar doon?" if is_h
                  else "🪪 Government ID must match the passenger names.\n\nShall I go ahead and confirm this ticket for you?")
        return [buttons_msg(
            f"📝 *Booking Confirmation Check*\n\n✈️ {c.get('flight_summary', '')}\n{people}\n{total}\n\n{footer}",
            [("cfm:yes", "✅ Confirm"), ("cfm:name", "✏️ Change names"), ("cfm:no", "❌ Cancel")])]

    def _contact(self, s: Session) -> dict:
        """Who the airline reaches: what they gave at checkout, else this WhatsApp number and the fallback email."""
        c = s.ctx.get("contact") or {}
        return {"email": c.get("email") or settings.DUFFEL_CONTACT_EMAIL, "phone": c.get("phone") or re.sub(r"\D", "", s.phone)}

    def _save_profiles(self, s: Session, details: list[dict]) -> None:
        """Remember each traveller's birth date and gender, so the next booking only needs the name."""
        prefs = s.user.get("preferences") or {}
        s.user["preferences"] = prefs
        for d in details:
            prefs.setdefault("travellers", {})[d["name"].lower()] = {"dob": d["dob"], "gender": d["gender"]}
            try:
                self.repo.save_traveller(s.user["id"], d["name"], d["dob"], d["gender"])
            except Exception:
                logger.warning("Could not save a traveller profile")

    async def _reprice(self, f: dict, pax: int) -> dict | None:
        """The flight priced right now for exactly this many travellers (a fresh offer row), or None if it is gone.
        A flight that did not come from Duffel has nothing to ask and stays as it is."""
        if self.live is None or not f.get("duffel_offer_id"):
            return f
        try:
            fresh = await self.live.refresh(f, pax)
        except Exception:
            logger.exception("Could not re-price flight %s", f["id"])
            return None
        return (await self._db(self.repo.sync_live_flights, [fresh]))[0] if fresh else None

    async def _reserve(self, s: Session, item: dict, status: str) -> dict | None:
        """Hold the traveller's choice as a booking. The fare is asked for again first: a quote is only good for a short while,
        and a dearer one than they agreed to counts as the flight being gone. None if it cannot be held."""
        f = await self._db(self.repo.get_flight, item["flight_id"])
        fresh = await self._reprice(f, item["pax"]) if f else None
        if not fresh or fresh["price_inr"] * item["pax"] > item["amount"] + max(100, item["amount"] // 50):
            return None
        return await self._db(self.repo.create_booking, s.user["id"], fresh, ", ".join(item["names"]), item["pax"], status,
                              item["details"], self._contact(s))

    def _gone(self, s: Session) -> list[dict]:
        s.step = "awaiting_flight"
        return [buttons_msg("😕 Sorry, that fare has just changed or sold out. Want to see the flights again?",
                            [("act:results", "↩️ Other flights"), ("nav:menu", "🏠 Menu")])]

    async def _do_booking(self, s: Session) -> list[dict]:
        c = s.ctx
        f = await self._db(self.repo.get_flight, c["flight_id"])
        if not f:
            return self._gone(s)
        names, pax, details = c["names"], c.get("pax", 1), self._details(c)
        label = f"✈️ {f['flight_no']} {city(f['from_code'])} ➜ {city(f['to_code'])}, {to_ist(f['departure_time']):%a %d %b}"
        if c.get("return_date"):
            label += f"\n  Return: 🔄 {city(f['to_code'])} ➜ {city(f['from_code'])}, {date.fromisoformat(c['return_date']):%a %d %b}"
        item = {"kind": "flight", "flight_id": f["id"], "names": names, "details": details, "pax": pax, "amount": f["price_inr"] * pax,
                "label": label}
        
        if c.get("return_date"):
            item["kind"] = "flight_outbound"
            item["agent"] = "flight"
            s.ctx.setdefault("cart", []).append(item)
            c["is_return_leg"] = True
            c["from"], c["to"] = c["to"], c["from"]
            c["date"] = c["return_date"]
            c.pop("return_date")
            return [text_msg(f"✅ Outbound flight added to your bundle.\n\nNow let's pick your return flight on {date.fromisoformat(c['date']):%a, %d %b}.")] + await self._show_results(s)
            
        if c.get("is_return_leg"):
            item["kind"] = "flight_return"
            item["agent"] = "flight"
        self._save_profiles(s, details)
        for k in ("flight_id", "passenger_name", "flight_summary", "pax", "names", "unit_price", "tdet"):
            c.pop(k, None)
        c["trip"] = self._trip_from(f) | ({"return_date": c["return_date"]} if c.get("return_date") else {})
        if self.checkout is not None and self.checkout.online:  # paid online: into the bundle, held only at checkout
            return await self.checkout.add(s, item, {"to": f["to_code"], "date": to_ist(f["arrival_time"]).date().isoformat()})
        booking = await self._reserve(s, item, "confirmed")
        ticket = await self._issue(booking) if booking else None
        if not ticket:
            if booking:
                await self._db(self.repo.cancel_booking, booking)
            return self._gone(s)
        s.step = "menu"
        return self._ticket(ticket, ticket["flights"], c, s.msg_id, await city_tip(self.advisor, f["to_code"], "Have a wonderful trip!"))

    # ---- what the Checkout asks of a bundleable service
    async def create_pending(self, s: Session, item: dict) -> dict | None:
        return await self._reserve(s, item, "pending")

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

    # ---- the airline ticket
    async def _issue(self, b: dict) -> dict | None:
        """Internal booking: we no longer contact airlines/Duffel to issue an order, nor do we generate fake PNRs."""
        return b

    async def _refund(self, b: dict, amount_inr: int) -> bool:
        """Give money back on the payment link this booking was paid with. False if nothing could be refunded."""
        pay = await self._db(self.repo.get_payment_for_booking, b["id"]) if self.payments is not None else None
        if not amount_inr or not pay:
            return False
        try:
            return await self.payments.refund(pay["link_id"], amount_inr)
        except Exception:
            logger.exception("Refund of Rs %s on booking %s failed: refund it by hand", amount_inr, b["id"])
            return False

    async def _ticket_failed(self, b: dict) -> list[dict]:
        """The traveller paid but the airline would not issue the ticket: cancel the booking and send the money back."""
        await self._db(self.repo.cancel_booking, b)
        paid = self.payments is not None and await self._db(self.repo.get_payment_for_booking, b["id"]) is not None
        refunded = await self._refund(b, b["total_price_inr"])
        after = (f"💸 Your *{inr(b['total_price_inr'])}* is on its way back to you, usually within 5-7 working days." if refunded else
                 "💸 I'll make sure your payment comes back to you. Our team will contact you shortly." if paid else "")
        return [buttons_msg(f"😕 Sorry, the airline couldn't issue this ticket (the fare may have changed), so I've cancelled it.\n{after}".strip(),
                            [("menu:book", "✈️ Search again"), ("nav:menu", "🏠 Menu")])]

    # ---------------------------------------------------------------------- payment
    @staticmethod
    def _trip_from(f: dict) -> dict:
        return {"from": f["from_code"], "to": f["to_code"], "city": city(f["to_code"]),
                "country": country_of(f["to_code"]), "intl": is_international(f["from_code"], f["to_code"]),
                "date": to_ist(f["departure_time"]).date().isoformat(), "flight_no": f["flight_no"],
                "arrival": to_ist(f["arrival_time"]).isoformat()}

    def _ticket(self, booking: dict, f: dict, ctx: dict, msg_id: str | None, tip: str) -> list[dict]:
        """The confirmed-booking messages: ticket, what's next, and a reaction (only if replying to a tap)."""
        pax = booking.get("passengers") or 1
        names = [n.strip() for n in booking["passenger_name"].split(",")]
        who = f"👤 *{names[0]}*" if pax == 1 else "👥 " + "\n👥 ".join(f"*{n}*" for n in names)
        ticket = (f"💳 *Payment received. Your booking has been recorded successfully.*\n━━━━━━━━━━━━━━━\n"
                  f"{who}\n🎫 Internal Booking Ref: *{pnr_of(booking)}*\n━━━━━━━━━━━━━━━\n"
                  f"{flight_card(f)}\n━━━━━━━━━━━━━━━\n💰 Total: *{inr(booking['total_price_inr'])}*"
                  + (f" ({pax} travellers)" if pax > 1 else ""))
        trip = self._trip_from(f)
        if ctx.get("return_date"):
            trip["return_date"] = ctx["return_date"]
            if isinstance(ctx.get("trip"), dict):
                ctx["trip"]["return_date"] = ctx["return_date"]
        visa = None  # visa is only discussed when the traveller asks for it
        queue = [x for x in ctx.get("queue") or [] if x in NEXT_STEPS]  # services the user also asked for
        bundled = tuple(ctx.get("bundle_kinds") or ())  # paid together with this ticket: not offered again
        steps = suggest_steps(trip, queue, done=bundled)
        if queue:
            ask = f"\n\nYou also mentioned {NEXT_STEPS[queue[0]][1]}. Shall we do that next?"
        elif "hotel" in bundled:  # the stay's voucher follows right after this ticket and asks what is next
            ask = ""
        else:
            pitch = [PITCH[x].format(city=trip["city"]) for x in steps[:3]]
            listed = pitch[0] if len(pitch) == 1 else f"{', '.join(pitch[:-1])} or {pitch[-1]}"
            ask = f"\n\nWant me to arrange {listed}, or anything else to make your trip comfortable?"
        heads_up = ""
        if trip.get("return_date"):
            heads_up += f"\n\n↩️ You're coming back on *{date.fromisoformat(trip['return_date']):%a, %d %b}*. Tap *Return Flight* and I'll find flights for that day."
        buttons = [(f"svc:{x}", NEXT_STEPS[x][0]) for x in steps[:2]] + [("act:return", "↩️ Return Flight")]
        out = [text_msg(ticket),
               buttons_msg(f"{countdown(to_ist(f['departure_time']))}{heads_up}\n\n💡 *{city(f['to_code'])} tip:* {tip}{ask}", buttons)]
        return out + ([reaction_msg(msg_id, "🎉")] if msg_id else [])

    async def confirm_payment(self, link_id: str) -> tuple[str, list[dict]] | None:
        """Razorpay webhook: the link was paid. Issues the ticket. Returns (whatsapp number, messages) to send, or None if it
        isn't ours or was already handled."""
        b = await self._db(self.repo.mark_paid, link_id)
        if not b:
            return None
        phone = b["users"]["phone"]
        ticket = await self._issue(b)
        if not ticket:
            return phone.lstrip("+"), await self._ticket_failed(b)
        convo = await self._db(self.repo.get_conversation, phone)
        ctx = dict((convo or {}).get("context") or {})
        ctx["trip"] = self._trip_from(b["flights"]) | ({"return_date": ctx["return_date"]} if ctx.get("return_date") else {})
        await self._db(self.repo.save_conversation, phone, "menu", ctx)
        tip = await city_tip(self.advisor, b["flights"]["to_code"], "Have a wonderful trip!")
        return phone.lstrip("+"), self._ticket(ticket, b["flights"], ctx, None, tip)

    async def release_expired(self) -> list[tuple[str, list[dict]]]:
        """Sweeper: cancel bookings nobody paid for in time. Returns (whatsapp number, messages) to notify."""
        expired = await self._db(self.repo.expire_unpaid, datetime.now(IST))
        return [(b["users"]["phone"].lstrip("+"),
                 [buttons_msg(f"⌛ The payment window for *{b['flights']['flight_no']}* ({city(b['flights']['from_code'])} ➜ "
                              f"{city(b['flights']['to_code'])}) ended, so I've cancelled the booking. Want to try again?",
                              [("menu:book", "✈️ Book a Flight"), ("nav:menu", "🏠 Menu")])]) for b in expired]

    # ---------------------------------------------------------------- my bookings
    async def _show_bookings(self, s: Session) -> list[dict]:
        bookings = await self._db(self.repo.list_user_bookings, s.user["id"])
        if not bookings:
            return [buttons_msg("You have no bookings yet, your first trip is just a few taps away! 🙂",
                                [("menu:book", "✈️ Book a Flight"), ("menu:quick", "⚡ Quick Trips"), ("nav:menu", "🏠 Menu")])]
        s.step = "awaiting_booking"
        rows = []
        for b in bookings:
            f = b["flights"]
            dep = to_ist(f["departure_time"])
            rows.append((f"bk:{b['id']}", f"{pnr_of(b)} · {f['from_code']}→{f['to_code']}",
                         f"{STATUS_ICON.get(b['status'], '')} {b['status'].title()} · {dep:%d %b, %H:%M}"))
        rows.append(("nav:menu", "🏠 Main menu", ""))
        
        last = bookings[0]
        lf = last["flights"]
        summary = (f"📋 *Your Latest Trip*\n"
                   f"{STATUS_ICON.get(last['status'], '')} {last['status'].title()} · PNR: *{pnr_of(last)}*\n"
                   f"✈️ {city(lf['from_code'])} ➜ {city(lf['to_code'])}\n"
                   f"📅 {to_ist(lf['departure_time']):%d %b, %H:%M}\n\n"
                   "Tap below to see details of this or any other booking 👇")
        
        return [list_msg(summary, "All bookings", rows, "Your trips")]

    async def _show_booking(self, s: Session, booking_id: str) -> list[dict]:
        b = await self._db(self.repo.get_booking, booking_id, s.user["id"])
        if not b:
            return self._menu(s, note="Booking not found.")
        f = b["flights"]
        pax = b.get("passengers") or 1
        text = (f"{STATUS_ICON.get(b['status'], '')} *{b['status'].title()}*\n🎫 PNR: *{pnr_of(b)}*\n"
                f"{'👥' if pax > 1 else '👤'} {b['passenger_name']}\n\n{flight_card(f)}\n\n💰 {inr(b['total_price_inr'])}"
                + (f" ({pax} travellers)" if pax > 1 else ""))
        buttons = [("nav:menu", "🏠 Menu")]
        upcoming = b["status"] != "cancelled" and to_ist(f["departure_time"]) > now_ist()
        if upcoming and to_ist(f["departure_time"]) - now_ist() <= timedelta(hours=48):
            text += "\n\n✅ Web check-in is open. Carry a valid photo ID to the airport."
        if upcoming:
            buttons.insert(0, (f"bkc:{b['id']}", "❌ Cancel Booking"))
        buttons.insert(-1, ("menu:bookings", "📋 All bookings"))
        return [buttons_msg(text, buttons)]

    async def _ask_cancel(self, s: Session, booking_id: str) -> list[dict]:
        b = await self._db(self.repo.get_booking, booking_id, s.user["id"])
        if not b or b["status"] == "cancelled":
            return self._menu(s, note="This booking is already cancelled or not found.")
        f = b["flights"]
        if b.get("duffel_order_id") and self.live is not None:  # ask the airline what this ticket is worth back, right now
            try:
                q = await self.live.cancel_quote(b["duffel_order_id"])
            except Exception:
                logger.exception("Cancel quote failed for booking %s", b["id"])
                return [buttons_msg(f"😕 This ticket can't be cancelled here (the airline wouldn't take it back). Please contact the airline "
                                    f"with your PNR *{pnr_of(b)}*.", [(f"bk:{b['id']}", "↩️ My booking"), ("nav:menu", "🏠 Menu")])]
            total = float(f.get("offer_total") or 0)
            back = min(b["total_price_inr"], round(b["total_price_inr"] * q["refund_amount"] / total)) if total else 0
            s.ctx["cancel_quote"] = {"booking_id": b["id"], "quote_id": q["quote_id"], "refund_inr": back}
            refund = (f"The airline refunds *{inr(back)}* of your {inr(b['total_price_inr'])}, and I'll send it back to you in 5-7 working days."
                      if back else "This is a non-refundable fare. No refund will be given.")
        else:
            refund = "This is a refundable fare. The amount will be returned in 5-7 working days." if f["refundable"] \
                else "This is a non-refundable fare. No refund will be given."
        return [buttons_msg(f"⚠️ Cancel PNR *{pnr_of(b)}* ({city(f['from_code'])} ➜ {city(f['to_code'])})?\n\n{refund}",
                            [(f"bkcy:{b['id']}", "Yes, cancel it"), (f"bk:{b['id']}", "No, keep it")])]

    async def _do_cancel(self, s: Session, booking_id: str) -> list[dict]:
        b = await self._db(self.repo.get_booking, booking_id, s.user["id"])
        if not b or b["status"] == "cancelled":
            return self._menu(s, note="This booking is already cancelled or not found.")
        back = 0
        if b.get("duffel_order_id") and self.live is not None:
            quote = s.ctx.get("cancel_quote")
            if not quote or quote["booking_id"] != b["id"]:  # the tap is stale: show the terms again first
                return await self._ask_cancel(s, booking_id)
            try:
                await self.live.cancel(quote["quote_id"])
            except Exception:
                logger.exception("Airline would not cancel booking %s", b["id"])
                return [buttons_msg("😕 The airline wouldn't cancel that just now. Your ticket is still valid, please try again in a few minutes.",
                                    [(f"bk:{b['id']}", "↩️ My booking"), ("nav:menu", "🏠 Menu")])]
            back = quote["refund_inr"]
            s.ctx.pop("cancel_quote", None)
        elif b["flights"]["refundable"]:
            back = b["total_price_inr"]
        await self._db(self.repo.cancel_booking, b)
        s.step = "menu"
        if not back:
            refund = "Non-refundable fare, so no refund applies."
        elif await self._refund(b, back):
            refund = f"💸 Your {inr(back)} refund has been initiated."
        elif self.payments is not None and await self._db(self.repo.get_payment_for_booking, b["id"]):
            refund = f"💸 Your {inr(back)} refund is being processed, our team will send it to you shortly."
        else:
            refund = "Nothing was paid online, so there is nothing to refund."
        return [buttons_msg(f"😢 Sorry to see it go. PNR *{pnr_of(b)}* has been cancelled.\n{refund}\n\nChanged your mind? I'm one tap away.",
                            [("menu:bookings", "📋 My Bookings"), ("menu:book", "✈️ New Booking")])]
