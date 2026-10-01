"""Flight agent: search, book and manage flights through buttons and lists.

Button/list ids look like "kind:value" (e.g. "from:IDR", "flt:<uuid>"); `owns` lists the kinds.
Per-user state (step + context) lives in the Session the Concierge hands us.

Cities, countries and "what to do next" are never written into this file: airports come from the database, an
international flight triggers a passport/visa check, and the suggestions after a booking follow from the trip itself.
"""
import asyncio
import logging
import re
from datetime import date, datetime, timedelta

from app.agents.base import Agent, Session
from app.agents.flight.formatting import (NEXT_STEPS, PITCH, city_tip, countdown, flight_card, flight_row, flight_tags,
                                          suggest_steps)
from app.core.messages import buttons_msg, cta_msg, list_msg, reaction_msg, text_msg
from app.core.places import CITIES, CITY_ALIASES, city, city_pattern, country_of, is_international, visa_code_for
from app.core.utils import IST, day_greeting, dur, inr, now_ist, parse_date, to_ist

MAX_DAYS_AHEAD = 29
PAYMENT_WINDOW_MIN = 20  # Razorpay needs payment links to live at least 15 min
logger = logging.getLogger(__name__)
GREETINGS = {"hi", "hii", "hiii", "hello", "hey", "hlo", "namaste", "start", "hola", "yo"}
THANKS = {"thanks", "thank you", "thx", "ty", "thanks!", "shukriya", "dhanyavad"}
BYE = {"bye", "goodbye", "see you", "tata", "ok bye"}
SORT_LABELS = {"cheap": "cheapest first", "fast": "fastest first", "time": "by departure time"}
STATUS_ICON = {"confirmed": "✅", "pending": "⏳", "cancelled": "❌"}
MENU_BUTTONS = [("menu:book", "✈️ Book a Flight"), ("menu:quick", "⚡ Quick Trips"), ("menu:bookings", "📋 My Bookings")]
TRIP_KEYS = ("from", "to", "date", "sort", "flight_id", "passenger_name", "flight_summary", "explore", "pre_to",
             "pre_date", "pax", "names", "unit_price", "min_date", "await_flight", "visa_advice", "city_for")
MAX_ROWS = 9  # a WhatsApp list holds 10 rows; the last one is "Another city…" when there are more places than fit


def is_home_passport(citizen: str) -> bool:
    """Our visa desk files applications for Indian passports only (that is what its rules table describes)."""
    return "india" in (citizen or "").lower()


class FlightAgent(Agent):
    name = "flight"
    title = "Flights"
    emoji = "✈️"
    menu_desc = "Search, book & manage trips"
    owns = frozenset({"menu", "trip", "from", "to", "date", "sort", "flt", "act", "pax", "name", "cfm", "pay", "bk", "bkc", "bkcy", "cz"})

    def __init__(self, repo, payments=None, advisor=None):
        self.repo = repo  # FlightRepo
        self.payments = payments  # RazorpayGateway (or None: bookings confirm instantly)
        self.advisor = advisor  # TravelAdvisor (or None: no visa answers or city tips, just generic wording)

    def reset(self, s: Session) -> None:
        for k in TRIP_KEYS:
            s.ctx.pop(k, None)

    def expects_text(self, s: Session) -> bool:
        return s.step in ("awaiting_date", "awaiting_name", "awaiting_citizen", "awaiting_city")

    async def on_enter(self, s: Session) -> list[dict]:
        return self._menu(s, note="✈️ *Flights*\nWhat would you like to do?")

    async def start(self, s: Session, slots: dict) -> list[dict]:
        """Free text like "Indore to Goa tomorrow": pre-fill what we heard, ask only for the rest."""
        for k in TRIP_KEYS:
            s.ctx.pop(k, None)
        c, today = s.ctx, now_ist().date()
        frm, to, d = slots.get("from"), slots.get("to"), slots.get("date")
        frm, to = (x if x in CITIES else None for x in (frm, to))
        if to == frm:
            to = None
        if d and not (today <= date.fromisoformat(d) <= today + timedelta(days=MAX_DAYS_AHEAD)):
            d = None
        if frm:
            c["from"] = frm
        if to:
            c["to" if frm else "pre_to"] = to
        if d:
            c["date" if (frm and to) else "pre_date"] = d
        heard = [f"from {city(frm)}" if frm else "", f"to {city(to)}" if to else "",
                 f"on {date.fromisoformat(d):%a, %d %b}" if d else ""]
        heard = " ".join(h for h in heard if h)
        intro = [text_msg(f"Got it! ✈️ Flying {heard}.")] if heard else []
        return intro + await self._advance(s)

    async def _db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # -------------------------------------------------------------------- entry point
    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        """Run one message against an already-loaded session (the Concierge calls this)."""
        return await (self._on_reply(s, reply_id) if reply_id else self._on_text(s, text.strip()))

    async def _on_text(self, s: Session, text: str) -> list[dict]:
        low = text.lower()
        if low in GREETINGS or low in ("menu", "restart", "reset", "home", "cancel"):
            return self._menu(s, greet=True)
        if s.step == "awaiting_date":
            return await self._on_date_text(s, text)
        if s.step == "awaiting_name":
            return self._on_name_text(s, text)
        if s.step == "awaiting_citizen":
            return await self._on_citizen_text(s, text)
        if s.step == "awaiting_city":
            return await self._on_city_text(s, text)
        if low in ("change passport", "wrong passport", "passport change"):
            s.ctx.pop("citizen", None)
            return [text_msg("👍 Okay! I'll ask for your passport country again on your next international flight.")]
        if low in ("help", "?"):
            return self._help(s)
        if low in THANKS:
            return self._menu(s, note="Anytime! 😊 Happy to help. Anything else?")
        if low in BYE:
            return self._menu(s, note="Safe travels! ✈️ Just say *hi* whenever you need me.")
        return self._menu(s, note="Hmm, I didn't catch that 🤔 Tap an option below, or type *help*.")

    async def _on_reply(self, s: Session, reply_id: str) -> list[dict]:
        kind, _, val = reply_id.partition(":")
        c = s.ctx
        if kind == "nav" or (kind == "menu" and val == "home"):
            return self._menu(s)
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
            c.pop("to", None)
            c.pop("date", None)
            if c.get("explore"):
                return self._ask_date(s)
            pre_to, pre_date = c.pop("pre_to", None), c.pop("pre_date", None)
            if pre_to and pre_to != val:  # user already told us where they're going
                c["to"] = pre_to
                if pre_date:
                    c["date"] = pre_date
            return await self._advance(s)
        elif kind == "to" and c.get("from"):
            c["to"] = val
            pre_date = c.pop("pre_date", None)
            if pre_date:
                c["date"] = pre_date
                return self._ask_sort(s)
            return self._ask_date(s)
        elif kind == "date" and c.get("from") and (c.get("to") or c.get("explore")):
            if val == "more":
                s.step = "awaiting_date"
                return [text_msg("📅 Type the date, e.g. *15/10* or *15 Oct* (within the next 30 days).")]
            c["date"] = val
            return await self._show_explore(s) if c.get("explore") else self._ask_sort(s)
        elif kind == "sort" and c.get("date"):
            c["sort"] = val
            return await self._show_results(s)
        elif kind == "flt":
            return await self._show_flight(s, val)
        elif kind == "cz" and c.get("await_flight"):
            return await self._on_citizen_tap(s, val)
        elif kind == "act" and val == "date" and c.get("from"):
            return self._ask_date(s)
        elif kind == "act" and val == "results" and c.get("date"):
            return await (self._show_explore(s) if c.get("explore") else self._show_results(s))
        elif kind == "act" and val == "return" and c.get("trip"):
            trip = c["trip"]
            self.reset(s)
            c["from"], c["to"], c["min_date"] = trip["to"], trip["from"], trip["date"]
            return self._ask_date(s)
        elif kind == "act" and val == "book" and c.get("flight_id"):
            return await self._ask_travellers(s)
        elif kind == "pax" and c.get("flight_id") and val.isdigit():
            c["pax"], c["names"] = int(val), []
            return self._ask_passenger(s)
        elif kind == "name" and c.get("flight_id"):
            if val == "self":
                c["names"] = [s.user["name"]]
                return self._next_name_or_confirm(s)
            return self._prompt_name(s)
        elif kind == "cfm" and c.get("flight_id"):
            if val == "yes" and len(c.get("names") or []) == c.get("pax", 1):
                return await self._do_booking(s)
            if val == "name":
                c["names"] = []
                return self._ask_passenger(s)
            return self._menu(s, note="No worries, I've dropped that booking. 👍")
        elif kind == "pay" and val in ("check", "cancel"):
            return await self._on_payment_tap(s, val)
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
    def _city_rows(kind: str, airports: list[dict], first_country: str, exclude: str | None = None) -> list[tuple[str, str, str]]:
        """Airport rows for a list message, the traveller's own country first; the tail becomes "Another city…"."""
        pool = sorted((a for a in airports if a["code"] != exclude), key=lambda a: (a["country"] != first_country, a["country"], a["city"]))
        rows = [(f"{kind}:{a['code']}", a["city"], a["name"] if a["country"] == first_country else f"{a['country']} · {a['name']}")
                for a in pool]
        if len(rows) > MAX_ROWS + 1:
            rows = rows[:MAX_ROWS] + [(f"{kind}:more", "Another city…", "Type the city name")]
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

    async def _ask_origin(self, s: Session) -> list[dict]:
        s.step = "awaiting_origin"
        airports = await self._db(self.repo.list_airports)
        home = await self._home_country(s, airports)
        rows = self._city_rows("from", [a for a in airports if a["country"] == home] or airports, home)
        if len(rows) < len(airports) and not any(r[0] == "from:more" for r in rows):  # flying from abroad: type the city
            rows = rows[:MAX_ROWS] + [("from:more", "Another city…", "Type the city name")]
        body = ("🎲 *Surprise me!* Where are you flying from?\nI'll find the cheapest getaways."
                if s.ctx.get("explore") else "🛫 *Where are you flying from?*")
        return [list_msg(body, "Choose city", rows, "Departure city")]

    async def _advance(self, s: Session) -> list[dict]:
        """Jump to the first step whose answer we don't have yet."""
        c = s.ctx
        if not c.get("from"):
            return await self._ask_origin(s)
        if not c.get("to"):
            return await self._ask_dest(s)
        if not c.get("date"):
            return self._ask_date(s)
        return self._ask_sort(s)

    async def _ask_dest(self, s: Session) -> list[dict]:
        s.step = "awaiting_dest"
        airports = await self._db(self.repo.list_airports)
        rows = self._city_rows("to", airports, country_of(s.ctx["from"]), exclude=s.ctx["from"])
        return [list_msg(f"🛬 *Where to from {city(s.ctx['from'])}?* 🌍", "Choose city", rows, "Destination")]

    async def _on_city_text(self, s: Session, text: str) -> list[dict]:
        """"Another city…": the traveller typed a name. We can only fly where the airports table has an airport."""
        kind = s.ctx.get("city_for")
        low = " ".join(text.lower().split())
        code = CITY_ALIASES.get(low)
        if not code and (m := city_pattern().search(low)):
            code = CITY_ALIASES[m.group(1)]
        served = {a["code"] for a in await self._db(self.repo.list_airports)}
        if not code or code not in served or (kind == "to" and code == s.ctx.get("from")):
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
        if d is None or d < today or d > today + timedelta(days=MAX_DAYS_AHEAD):
            return [text_msg("😕 I couldn't understand that date, or it's out of range. Please type a date within the next 30 days, e.g. *15/10* or *tomorrow*.")]
        s.ctx["date"] = d.isoformat()
        return await self._show_explore(s) if s.ctx.get("explore") else self._ask_sort(s)

    def _ask_sort(self, s: Session) -> list[dict]:
        s.step = "awaiting_sort"
        d = date.fromisoformat(s.ctx["date"])
        return [buttons_msg(f"📅 {d:%A, %d %b}, nice choice!\nHow should I sort the flights?",
                            [("sort:cheap", "💸 Cheapest"), ("sort:fast", "⚡ Fastest"), ("sort:time", "🕐 By time")])]

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

    async def _show_explore(self, s: Session) -> list[dict]:
        c = s.ctx
        d = date.fromisoformat(c["date"])
        start = datetime.combine(d, datetime.min.time(), IST)
        flights = await self._db(self.repo.search_from, c["from"], start, start + timedelta(days=1))
        flights = [f for f in flights if to_ist(f["departure_time"]) > now_ist()]
        cheapest: dict[str, dict] = {}
        for f in flights:
            best = cheapest.get(f["to_code"])
            if best is None or f["price_inr"] < best["price_inr"]:
                cheapest[f["to_code"]] = f
        picks = sorted(cheapest.values(), key=lambda f: f["price_inr"])[:9]
        if not picks:
            s.step = "awaiting_date"
            return [buttons_msg(f"😕 No flights from {city(c['from'])} on {d:%d %b}. Try another day?",
                                [("act:date", "📅 Other date"), ("nav:menu", "🏠 Menu")])]
        rows = [(f"flt:{f['id']}", f"{city(f['to_code'])} · {inr(f['price_inr'])}",
                 f"{country_of(f['to_code'])} · {to_ist(f['departure_time']):%H:%M}") for f in picks]
        rows.append(("nav:menu", "🏠 Main menu", ""))
        s.step = "awaiting_flight"
        return [list_msg(f"🎲 *Getaways from {city(c['from'])}* · {d:%a, %d %b}\n"
                         f"The cheapest flight to each city. Fancy *{city(picks[0]['to_code'])}* for {inr(picks[0]['price_inr'])}? 😉",
                         "See getaways", rows, "Cheapest per city")]

    # ------------------------------------------------------------------------- results
    async def _show_results(self, s: Session) -> list[dict]:
        c = s.ctx
        d = date.fromisoformat(c["date"])
        start = datetime.combine(d, datetime.min.time(), IST)
        flights = await self._db(self.repo.search_flights, c["from"], c["to"], start, start + timedelta(days=1))
        flights = [f for f in flights if to_ist(f["departure_time"]) > now_ist()]
        if not flights:
            s.step = "awaiting_date"
            later = await self._db(self.repo.search_flights, c["from"], c["to"], start + timedelta(days=1), start + timedelta(days=8))
            nxt = min(later, key=lambda f: f["departure_time"]) if later else None
            msg = f"😕 No flights for {city(c['from'])} ➜ {city(c['to'])} on {d:%a, %d %b}."
            if not nxt:
                return [buttons_msg(msg + " Try another day?", [("act:date", "📅 Other date"), ("menu:book", "🔄 New route"), ("nav:menu", "🏠 Menu")])]
            nd = to_ist(nxt["departure_time"]).date()
            cheapest_next = min(f["price_inr"] for f in later if to_ist(f["departure_time"]).date() == nd)
            return [buttons_msg(f"{msg}\n\n💡 Next available: *{nd:%a, %d %b}* from {inr(cheapest_next)}.",
                                [(f"date:{nd.isoformat()}", f"📅 {nd:%a %d %b}"), ("act:date", "📅 Other date"), ("nav:menu", "🏠 Menu")])]

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
        tip = await self._cheaper_nearby(c, d, cheapest)
        return [list_msg(
            f"✈️ *{city(c['from'])} ➜ {city(c['to'])}* · {d:%a, %d %b}\n"
            f"Found *{len(flights)}* flights{extra}, from just *{inr(cheapest)}*, {SORT_LABELS[c.get('sort', 'time')]}.{tip}\nPick your favourite 👇",
            "View flights", rows, "Available flights")]

    async def _cheaper_nearby(self, c: dict, d: date, cheapest: int) -> str:
        """Tip when the day before or after is clearly cheaper (>= 7% less)."""
        best = None
        for delta in (-1, 1):
            day = d + timedelta(days=delta)
            if day < now_ist().date() or day > now_ist().date() + timedelta(days=MAX_DAYS_AHEAD):
                continue
            start = datetime.combine(day, datetime.min.time(), IST)
            found = [f for f in await self._db(self.repo.search_flights, c["from"], c["to"], start, start + timedelta(days=1))
                     if to_ist(f["departure_time"]) > now_ist()]
            if found:
                price = min(f["price_inr"] for f in found)
                if price <= cheapest * 0.93 and (best is None or price < best[1]):
                    best = (day, price)
        return f"\n💡 *{best[0]:%a, %d %b}* is {inr(cheapest - best[1])} cheaper (from {inr(best[1])})." if best else ""

    async def _show_flight(self, s: Session, flight_id: str) -> list[dict]:
        f = await self._db(self.repo.get_flight, flight_id)
        if not f or f["status"] == "cancelled" or f["seats_left"] <= 0:
            return [buttons_msg("😕 Oh no, this flight is no longer available.",
                                [("act:results", "↩️ Other flights"), ("nav:menu", "🏠 Menu")])]
        c = s.ctx
        international = is_international(f["from_code"], f["to_code"])
        if international and not c.get("citizen"):  # entry rules depend on the passport: ask before showing the flight
            c["await_flight"] = flight_id
            return self._ask_citizen(s, f)
        c["flight_id"] = flight_id
        dep = to_ist(f["departure_time"])
        c["flight_summary"] = (f"{f['flight_no']} · {city(f['from_code'])} ➜ {city(f['to_code'])}\n"
                               f"🛫 {dep:%a, %d %b} · {dep:%H:%M}")
        s.step = "awaiting_flight_action"
        seats = f"🔥 Only {f['seats_left']} seats left!" if f["seats_left"] <= 5 else f"💺 {f['seats_left']} seats available"
        note, advice = await self._travel_note(s, f) if international else ("", None)
        buttons = [("act:book", "✅ Book Now"), ("act:results", "↩️ Other flights"), ("nav:menu", "🏠 Menu")]
        if international and (advice is None or advice.needs_visa) and is_home_passport(c.get("citizen", "")):
            c["pending_slots"] = {"country": visa_code_for(country_of(f["to_code"])), "to": f["to_code"]}
            buttons[2] = ("svc:visa", "🛂 Visa help")
        return [buttons_msg(f"{flight_card(f)}\n{seats}\n\n💰 *{inr(f['price_inr'])}* per person{note}", buttons)]

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
        f = await self._db(self.repo.get_flight, c["await_flight"])
        if val == "home" and f:
            c["citizen"] = country_of(f["from_code"])
            return await self._show_flight(s, c.pop("await_flight"))
        s.step = "awaiting_citizen"
        return [text_msg("✏️ Type your passport country, like *Nepal* or *United Kingdom*.")]

    async def _on_citizen_text(self, s: Session, text: str) -> list[dict]:
        if not s.ctx.get("await_flight"):
            return self._menu(s, greet=True)
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,39}", text):
            return [text_msg("😕 Please type just the country name, like *India* or *United States*.")]
        s.ctx["citizen"] = " ".join(text.split()).title()
        return await self._show_flight(s, s.ctx.pop("await_flight"))

    async def _travel_note(self, s: Session, f: dict):
        """The entry-rules paragraph for an international flight, and the advice behind it (None when we couldn't tell)."""
        citizen, country = s.ctx["citizen"], country_of(f["to_code"])
        advice = await self.advisor.visa_check(citizen, country) if self.advisor else None
        s.ctx["visa_advice"] = {"to": f["to_code"], "needs": advice.needs_visa if advice else None,
                                "status": advice.status if advice else "unknown"}
        if advice:
            note = f"\n\n{advice.icon} *{advice.headline}* · {citizen} passport → {country}\n{advice.summary}"
        else:
            note = f"\n\n🛂 *{country}* may need a visa for a {citizen} passport. Worth checking before you pay."
        note += "\n📘 Passport should be valid 6+ months after your trip. Rules change, so confirm with the embassy."
        if not is_home_passport(citizen) and (advice is None or advice.needs_visa):
            note += "\nℹ️ My visa desk handles Indian passports for now; please apply on the official site."
        return note + "\n(Wrong passport? Type *change passport*.)", advice

    # ------------------------------------------------------------------------- booking
    async def _ask_travellers(self, s: Session) -> list[dict]:
        f = await self._db(self.repo.get_flight, s.ctx["flight_id"])
        if not f or f["status"] == "cancelled" or f["seats_left"] <= 0:
            return [buttons_msg("😕 Oh no, this flight is no longer available.",
                                [("act:results", "↩️ Other flights"), ("nav:menu", "🏠 Menu")])]
        top = min(f["seats_left"], 6)
        s.ctx["unit_price"] = f["price_inr"]
        if top == 1:  # only one seat left, no need to ask
            s.ctx["pax"], s.ctx["names"] = 1, []
            return self._ask_passenger(s)
        s.step = "awaiting_pax"
        rows = [(f"pax:{n}", f"{n} traveller{'s' if n > 1 else ''}", f"{inr(f['price_inr'] * n)} total") for n in range(1, top + 1)]
        return [list_msg("👥 *How many travellers?*", "Choose number", rows, "Travellers")]

    def _ask_passenger(self, s: Session) -> list[dict]:
        s.step = "awaiting_passenger"
        pax = s.ctx.get("pax", 1)
        name = s.user.get("name")
        if not name or name == "Unknown":
            return self._prompt_name(s)
        who = "Who's flying?" if pax == 1 else f"Let's add your {pax} travellers. Is *traveller 1* you?"
        return [buttons_msg(f"👤 Great pick! {who}\nYour name: *{name}*",
                            [("name:self", "For me"), ("name:other", "Someone else")])]

    def _prompt_name(self, s: Session) -> list[dict]:
        s.step = "awaiting_name"
        n, pax = len(s.ctx.get("names") or []) + 1, s.ctx.get("pax", 1)
        label = "the passenger's" if pax == 1 else f"traveller {n} of {pax}'s"
        return [text_msg(f"✏️ Please type {label} full name (as on their ID).")]

    def _next_name_or_confirm(self, s: Session) -> list[dict]:
        return self._prompt_name(s) if len(s.ctx["names"]) < s.ctx.get("pax", 1) else self._confirm(s)

    def _on_name_text(self, s: Session, text: str) -> list[dict]:
        if not s.ctx.get("flight_id"):
            return self._menu(s, greet=True)
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,59}", text):
            return [text_msg("😕 Please use letters only for the name (2-60 characters).")]
        s.ctx.setdefault("names", []).append(" ".join(text.split()).title())
        return self._next_name_or_confirm(s)

    def _confirm(self, s: Session) -> list[dict]:
        s.step = "awaiting_confirm"
        c = s.ctx
        pax, unit = c.get("pax", 1), c.get("unit_price", 0)
        people = "\n".join(f"👤 {n}" for n in c["names"])
        total = f"💰 {inr(unit)} × {pax} = *{inr(unit * pax)}*" if pax > 1 else f"💰 *{inr(unit)}*"
        return [buttons_msg(
            f"📝 *Last check before takeoff!*\n\n✈️ {c.get('flight_summary', '')}\n{people}\n{total}\n\nShall I book it?",
            [("cfm:yes", "✅ Confirm"), ("cfm:name", "✏️ Change names"), ("cfm:no", "❌ Cancel")])]

    async def _do_booking(self, s: Session) -> list[dict]:
        f = await self._db(self.repo.get_flight, s.ctx["flight_id"])
        names, pax = s.ctx["names"], s.ctx.get("pax", 1)
        pay_online = self.payments is not None and self.payments.enabled
        booking = (await self._db(self.repo.create_booking, s.user["id"], f, ", ".join(names), pax,
                                  "pending" if pay_online else "confirmed") if f else None)
        if not booking:
            s.step = "awaiting_flight"
            return [buttons_msg("😕 Sorry, there aren't enough seats left on this flight. Want to see other flights?",
                                [("act:results", "↩️ Other flights"), ("nav:menu", "🏠 Menu")])]
        for k in ("flight_id", "passenger_name", "flight_summary", "pax", "names", "unit_price"):
            s.ctx.pop(k, None)
        if pay_online:
            return await self._request_payment(s, booking, f, names)
        s.ctx["trip"] = self._trip_from(f)
        s.step = "menu"
        return self._ticket(booking, f, s.ctx, s.msg_id, await city_tip(self.advisor, f["to_code"], "Have a wonderful trip!"))

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
        ticket = (f"🎉 *Booking Confirmed!*\n━━━━━━━━━━━━━━━\n"
                  f"{who}\n🎫 PNR: *{booking['pnr']}*\n━━━━━━━━━━━━━━━\n"
                  f"{flight_card(f)}\n━━━━━━━━━━━━━━━\n💰 Total: *{inr(booking['total_price_inr'])}*"
                  + (f" ({pax} travellers)" if pax > 1 else ""))
        trip = self._trip_from(f)
        visa = ctx.get("visa_advice")
        visa = visa if visa and visa.get("to") == f["to_code"] else None
        queue = [x for x in ctx.get("queue") or [] if x in NEXT_STEPS]  # services the user also asked for
        steps = suggest_steps(trip, queue, visa)
        if queue:
            ask = f"\n\nYou also mentioned {NEXT_STEPS[queue[0]][1]}. Shall we do that next?"
        else:
            pitch = [PITCH[x].format(city=trip["city"]) for x in steps[:3]]
            ask = f"\n\nNext, I can help with {', '.join(pitch[:-1])} or {pitch[-1]}."
        heads_up = ""
        if trip["intl"] and (visa is None or visa.get("needs")):
            heads_up = (f"\n\n🛂 *Heads up:* {trip['country']} needs a visa check for your passport. "
                        "Visas take time, so it's best to start today.")
        buttons = [(f"svc:{x}", NEXT_STEPS[x][0]) for x in steps[:2]] + [("act:return", "↩️ Return Flight")]
        out = [text_msg(ticket),
               buttons_msg(f"{countdown(to_ist(f['departure_time']))}{heads_up}\n\n💡 *{city(f['to_code'])} tip:* {tip}{ask}", buttons)]
        return out + ([reaction_msg(msg_id, "🎉")] if msg_id else [])

    async def _request_payment(self, s: Session, booking: dict, f: dict, names: list[str]) -> list[dict]:
        """Seats are held on a pending booking; confirm it only once the Razorpay link is paid."""
        total = booking["total_price_inr"]
        try:
            link = await self.payments.create_link(total, booking["pnr"], f"Flight {f['flight_no']} {f['from_code']}-{f['to_code']}",
                                                   s.phone, names[0], PAYMENT_WINDOW_MIN)
            await self._db(self.repo.create_payment, booking["id"], s.user["id"], total, link["id"], link["short_url"],
                           datetime.now(IST) + timedelta(minutes=PAYMENT_WINDOW_MIN + 1))  # +1 min grace for the webhook
        except Exception:
            logger.exception("Could not create a payment link")
            await self._db(self.repo.cancel_booking, booking)  # give the seats back
            return [buttons_msg("😕 I couldn't set up the payment right now, and I've released your seats. Please try again in a minute.",
                                [("act:results", "↩️ Other flights"), ("nav:menu", "🏠 Menu")])]
        s.ctx["pay"] = {"booking_id": booking["id"], "link_id": link["id"]}
        s.step = "awaiting_payment"
        test = "\n🧪 Test mode: no real money is charged." if getattr(self.payments, "test_mode", False) else ""
        return [
            cta_msg(f"💳 *Almost done!* Pay *{inr(total)}* to confirm your seats on {f['flight_no']} "
                    f"({city(f['from_code'])} ➜ {city(f['to_code'])}).\n🎫 Ref: {booking['pnr']}\n"
                    f"⏳ Seats are held for {PAYMENT_WINDOW_MIN} minutes.{test}", f"Pay {inr(total)}", link["short_url"]),
            buttons_msg("I'll confirm your ticket here the moment the payment goes through ✅",
                        [("pay:check", "✅ I've paid"), ("pay:cancel", "❌ Cancel booking")]),
        ]

    async def _on_payment_tap(self, s: Session, action: str) -> list[dict]:
        pay = s.ctx.get("pay")
        b = await self._db(self.repo.get_booking_with_user, pay["booking_id"]) if pay else None
        if not b:
            s.ctx.pop("pay", None)
            return self._menu(s, note="I couldn't find that payment. Let's start fresh.")
        if action == "cancel":
            if b["status"] == "confirmed":
                return [buttons_msg("This booking is already paid and confirmed, so I can't drop it here. Use *My Bookings* to cancel it.",
                                    [("menu:bookings", "📋 My Bookings"), ("nav:menu", "🏠 Menu")])]
            await self._db(self.repo.cancel_payment, b["id"])
            await self._db(self.repo.cancel_booking, b)
            try:
                await self.payments.cancel_link(pay["link_id"])
            except Exception:
                logger.warning("Could not cancel the payment link %s", pay["link_id"])
            s.ctx.pop("pay", None)
            return self._menu(s, note="No problem, I've cancelled it and released your seats. 👍")
        # "I've paid"
        if b["status"] == "confirmed":  # the webhook beat the tap
            s.ctx.pop("pay", None)
            return [buttons_msg(f"✅ Already confirmed! Your PNR is *{b['pnr']}*.",
                                [("menu:bookings", "📋 My Bookings"), ("nav:menu", "🏠 Menu")])]
        if b["status"] == "cancelled":
            s.ctx.pop("pay", None)
            return [buttons_msg("⌛ The payment window ended and the seats were released. Want to try again?",
                                [("menu:book", "✈️ Book a Flight"), ("nav:menu", "🏠 Menu")])]
        try:
            paid = await self.payments.link_status(pay["link_id"]) == "paid"
        except Exception:
            paid = False
        confirmed = await self._db(self.repo.mark_paid, pay["link_id"]) if paid else None
        if not confirmed:
            return [buttons_msg("I haven't received the payment yet. If you've just paid, give it a few seconds and tap again. 🙏",
                                [("pay:check", "✅ I've paid"), ("pay:cancel", "❌ Cancel booking")])]
        s.ctx.pop("pay", None)
        s.ctx["trip"] = self._trip_from(confirmed["flights"])
        s.step = "menu"
        tip = await city_tip(self.advisor, confirmed["flights"]["to_code"], "Have a wonderful trip!")
        return self._ticket(confirmed, confirmed["flights"], s.ctx, s.msg_id, tip)

    async def confirm_payment(self, link_id: str) -> tuple[str, list[dict]] | None:
        """Razorpay webhook: the link was paid. Returns (whatsapp number, messages) to send, or None if already handled."""
        b = await self._db(self.repo.mark_paid, link_id)
        if not b:
            return None
        phone = b["users"]["phone"]
        convo = await self._db(self.repo.get_conversation, phone)
        ctx = dict((convo or {}).get("context") or {})
        ctx["trip"] = self._trip_from(b["flights"])  # (keep ctx["pay"]: a late "I've paid" tap then gets a friendly answer)
        await self._db(self.repo.save_conversation, phone, "menu", ctx)
        tip = await city_tip(self.advisor, b["flights"]["to_code"], "Have a wonderful trip!")
        return phone.lstrip("+"), self._ticket(b, b["flights"], ctx, None, tip)

    async def release_expired(self) -> list[tuple[str, list[dict]]]:
        """Sweeper: cancel bookings nobody paid for in time. Returns (whatsapp number, messages) to notify."""
        expired = await self._db(self.repo.expire_unpaid, datetime.now(IST))
        return [(b["users"]["phone"].lstrip("+"),
                 [buttons_msg(f"⌛ The payment window for *{b['flights']['flight_no']}* ({city(b['flights']['from_code'])} ➜ "
                              f"{city(b['flights']['to_code'])}) ended, so I've released the seats. Want to try again?",
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
            rows.append((f"bk:{b['id']}", f"{b['pnr']} · {f['from_code']}→{f['to_code']}",
                         f"{STATUS_ICON.get(b['status'], '')} {b['status'].title()} · {dep:%d %b, %H:%M}"))
        rows.append(("nav:menu", "🏠 Main menu", ""))
        return [list_msg("📋 *Your trips*\nPick one to see details 👇", "View bookings", rows, "Recent bookings")]

    async def _show_booking(self, s: Session, booking_id: str) -> list[dict]:
        b = await self._db(self.repo.get_booking, booking_id, s.user["id"])
        if not b:
            return self._menu(s, note="Booking not found.")
        f = b["flights"]
        pax = b.get("passengers") or 1
        text = (f"{STATUS_ICON.get(b['status'], '')} *{b['status'].title()}*\n🎫 PNR: *{b['pnr']}*\n"
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
        refund = "This is a refundable fare. The amount will be returned in 5-7 working days." if f["refundable"] \
            else "This is a non-refundable fare. No refund will be given."
        return [buttons_msg(f"⚠️ Cancel PNR *{b['pnr']}* ({city(f['from_code'])} ➜ {city(f['to_code'])})?\n\n{refund}",
                            [(f"bkcy:{b['id']}", "Yes, cancel it"), (f"bk:{b['id']}", "No, keep it")])]

    async def _do_cancel(self, s: Session, booking_id: str) -> list[dict]:
        b = await self._db(self.repo.get_booking, booking_id, s.user["id"])
        if not b or b["status"] == "cancelled":
            return self._menu(s, note="This booking is already cancelled or not found.")
        await self._db(self.repo.cancel_booking, b)
        s.step = "menu"
        refund = (f"💸 Your {inr(b['total_price_inr'])} refund has been initiated."
                  if b["flights"]["refundable"] else "Non-refundable fare, so no refund applies.")
        return [buttons_msg(f"😢 Sorry to see it go. PNR *{b['pnr']}* has been cancelled.\n{refund}\n\nChanged your mind? I'm one tap away.",
                            [("menu:bookings", "📋 My Bookings"), ("menu:book", "✈️ New Booking")])]
