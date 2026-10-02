"""Buddy agent: a friend on WhatsApp for any small or big problem, and a companion on the road. It listens, remembers what
matters, knows the user's booked trip (flight, hotel, where they are), helps when something goes wrong on the way to the
airport, and finds things nearby.

Button ids: buddy:forgetyes, buddy:forgetno. Free text reaches Buddy when the Concierge decides the message is personal
talk (or small talk), and keeps coming here while the step is "buddy_chat" (unless the user clearly asks to book something).
The model can ask the app to act (see brain.ACTIONS): send a Google Maps button to the airport or hotel, request the user's
location, or hand over to Around Me / Events (the Concierge continues with them via ctx["follow_up"]).
What Buddy remembers lives in `user_memories`; "forget me" deletes it together with the stored chat history and location.
"""
import asyncio
import logging
import re
from datetime import date

from app.agents.base import Agent, Session
from app.agents.buddy import trip as T
from app.agents.buddy.brain import SERVICES, MAX_SUGGEST
from app.agents.buddy.needs import next_needs
from app.core import safety
from app.core.geo import fresh_location, maps_link
from app.core.messages import buttons_msg, cta_msg, location_request_msg, text_msg
from app.core.places import AIRPORT_COORDS, city
from app.core.utils import now_ist

logger = logging.getLogger(__name__)
LLM_TIMEOUT_S = 25
HISTORY_LIMIT = 10
MENU_BUTTON = ("nav:menu", "🏠 Menu")
CATEGORY_ICON = {"about": "🙂", "people": "👥", "plans": "📅", "worries": "💭", "likes": "❤️"}
FALLBACK = "Hmm, mera dimaag abhi thoda atak gaya. 😅 Ek baar phir bolo?"
FORGET = re.compile(r"\b(forget me|forget everything|delete (?:all )?my (?:data|memory|memories|chats?|history)|"
                    r"clear my (?:data|memory|memories|chats?|history)|(?:meri|mera) (?:memory|data|chats?) "
                    r"(?:delete|clear|hata)\w*|sab (?:kuch )?bhool ja\w*)\b")
SHOW = re.compile(r"\b(what do you (?:remember|know) about me|kya yaad hai|mere baare mein kya (?:pata|yaad|jaante)|"
                  r"show my memor(?:y|ies))\b")
# a "place" that is really something to attend: it belongs to the events agent, not a map search
EVENTISH = re.compile(r"\b(events?|shows?|concerts?|festivals?|gigs?|things to do|activit\w*|exciting|fun|entertainment|happening)\b")
# "book a flight to Goa" is a request for a service; "flight mein kitna time hai?" is a question for Buddy
BOOKING_ASK = re.compile(r"\b(book|booking|search|find|dhundh\w*|dhoondh\w*|chahiye|i need|need a|want a|looking for|show me)\b")


class BuddyAgent(Agent):
    name = "buddy"
    title = "Buddy"
    emoji = "💬"
    menu_desc = "Talk, vent, ask anything"
    owns = frozenset({"buddy"})

    def __init__(self, repo, brain, geo=None, hotels=None):
        self.repo = repo      # BuddyRepo
        self.brain = brain    # OpenAIBuddy (or a fake with the same `respond`)
        self.geo = geo        # GeoService: travel times and place names. None: Buddy works without them
        self.hotels = hotels  # HotelRepo (search_hotels): other stays to suggest. None: no suggestions from inventory

    async def _db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def _safe(self, fn, *args):
        """Memory and trip lookups are best-effort: Buddy still chats if a table isn't created yet."""
        try:
            return await self._db(fn, *args)
        except Exception:
            logger.warning("Buddy lookup failed (is the `user_memories` table created?)", exc_info=True)
            return None

    def claims(self, text: str) -> bool:
        """"forget me" and "what do you remember about me" work from anywhere in the bot."""
        low = text.lower()
        return bool(FORGET.search(low) or SHOW.search(low))

    def expects_text(self, s: Session) -> bool:
        return s.step in ("buddy_chat", "buddy_wait_location")

    def expects_location(self, s: Session) -> bool:
        return s.step == "buddy_wait_location"

    def keeps_text(self, text: str, other, known: set[str]) -> bool:
        if other.name in ("bookings", "help"):
            return False
        if other.name in known and other.name != self.name:
            return not ((other.slots.get("from") and other.slots.get("to")) or BOOKING_ASK.search(text.lower()))
        return True

    @staticmethod
    def _first_name(s: Session) -> str:
        first = (s.user.get("name") or "").split(" ")[0]
        return "" if first in ("", "Unknown", "Traveller") else first

    # ------------------------------------------------------------------ entry points
    async def on_enter(self, s: Session) -> list[dict]:
        s.ctx["agent"], s.step = "buddy", "buddy_chat"
        first = self._first_name(s)
        return [text_msg(f"💬 *Buddy*\nHey{' ' + first if first else ''}! Main yahin hoon. Jo bhi mann mein hai bolo: din kaisa gaya, "
                         "koi tension, koi plan, ya bas gappe. Trip par ho to raasta, timing, nearby jagah, sab mein madad karunga. 🙂\n\n"
                         "_Tip: *what do you remember about me* se dekho mujhe kya yaad hai, aur *forget me* se sab delete._")]

    async def start(self, s: Session, slots: dict) -> list[dict]:
        return await self.on_enter(s)

    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        s.ctx["agent"], s.step = "buddy", "buddy_chat"
        if reply_id:
            return await self._on_tap(s, reply_id.partition(":")[2])
        text = text.strip()
        if not text:
            return await self.on_enter(s)
        low = text.lower()
        if FORGET.search(low):
            return self._ask_forget()
        if SHOW.search(low):
            return await self._show_memories(s)
        return await self._chat(s, text)

    async def on_location(self, s: Session, loc: dict) -> list[dict]:
        """The user shared a pin after we asked: carry on the conversation, now with their location in the facts."""
        s.ctx["agent"], s.step = "buddy", "buddy_chat"
        s.ctx.pop("buddy_pending", None)
        return await self._chat(s, "(I just shared my location)")

    # --------------------------------------------------------------------- trip facts
    async def _trip(self, s: Session) -> T.TripInfo:
        """The facts about the booked trip and where the user is, computed here so the model never has to guess."""
        bookings = await self._safe(self.repo.upcoming_flights, s.user["id"]) or []
        stays = await self._safe(self.repo.user_stays, s.user["id"]) or []
        stay = await self._safe(self.repo.current_stay, s.user["id"]) or (stays[0] if stays else None)
        stays = stays or ([stay] if stay else [])
        options = await self._stay_options(stays[:2])
        payments = await self._safe(self.repo.paid_payments, s.user["id"]) or []
        loc = fresh_location(s.ctx)
        label = route = None
        if loc:
            label = loc.get("label") or loc.get("name") or (loc.get("address") or "").split(",")[0] or None
            if not label and self.geo:
                label = await self.geo.reverse(loc["lat"], loc["lon"])
                loc["label"] = label or ""
            nxt = T.pick_next_flight(bookings, now_ist())
            if nxt and self.geo and nxt["flights"]["from_code"] in AIRPORT_COORDS and T.to_ist(nxt["flights"]["departure_time"]) > now_ist():
                route = await self.geo.route((loc["lat"], loc["lon"]), AIRPORT_COORDS[nxt["flights"]["from_code"]])
        return T.describe(now_ist(), bookings, stay, loc, label, route, stays, options, payments)

    async def _stay_options(self, stays: list[dict]) -> dict:
        """Other hotels with a free room in the same city on the same nights, so Buddy suggests real inventory only:
        the two cheapest and the two best rated, never the one already booked."""
        out: dict = {}
        for st in stays:
            try:
                if not (self.hotels and st.get("hotels") and st.get("check_in") and st.get("check_out")):
                    continue
                found = await self._db(self.hotels.search_hotels, st["hotels"]["city_code"],
                                       date.fromisoformat(str(st["check_in"])[:10]),
                                       date.fromisoformat(str(st["check_out"])[:10]), st.get("guests") or 1)
                found = [h for h in found if h["id"] != st["hotels"].get("id")]
                cheap = sorted(found, key=lambda h: h["rooms"][0]["price_inr"])[:2]
                best = sorted((h for h in found if h not in cheap), key=lambda h: -float(h.get("rating") or 0))[:2]
                out[st.get("id") or st.get("ref")] = cheap + best
            except Exception:
                logger.warning("Could not look up other stays", exc_info=True)
        return out

    # ------------------------------------------------------------------------ chat
    async def _chat(self, s: Session, text: str) -> list[dict]:
        memories = await self._safe(self.repo.list_memories, s.user["id"]) or []
        history = await self._safe(self.repo.recent_messages, s.user["id"], HISTORY_LIMIT) or []
        trip = await self._trip(s)
        try:
            r = await asyncio.wait_for(self.brain.respond(
                name=self._first_name(s), today=now_ist().date(), memories=memories, history=history, text=text,
                trip=s.ctx.get("trip"), context=trip.text), LLM_TIMEOUT_S)
        except Exception:
            logger.exception("Buddy could not answer")
            return [text_msg(FALLBACK)]
        reply = r.reply
        if r.risk != "none":
            logger.warning("Safety flag %s for user %s", r.risk, s.user["id"])  # no message content in logs
            reply += safety.RISK_NOTES[r.risk]
        if r.remember:
            await self._safe(self.repo.add_memories, s.user["id"], r.remember)
        if "hotel" in r.suggest and trip.stay and trip.stay.get("hotels"):  # "other stays" opens the hotel search in that city
            s.ctx["pending_slots"] = {"to": trip.stay["hotels"]["city_code"], "date": str(trip.stay["check_in"])[:10]}
        out = self._reply_messages(reply, self._buttons(s, r, trip, text))
        return out + self._act(s, r, trip)

    @staticmethod
    def _buttons(s: Session, r, trip: T.TripInfo, text: str) -> list[tuple[str, str]]:
        """The next-step buttons: what the facts say they need (a flight tomorrow, no hotel yet...) first, then whatever the
        model suggested. Nothing when they are upset, and nothing extra when we are already sending them somewhere."""
        wanted = [(k, SERVICES[k]) for k in r.suggest if k in SERVICES]
        if r.risk == "none" and r.action == "none":
            for k, label in next_needs(now=now_ist(), text=text, flight=trip.flight, stay=trip.stay, trip=s.ctx.get("trip"),
                                       has_location=bool(trip.loc)):
                if all(k != w[0] for w in wanted):
                    wanted.append((k, label))
        return [(f"svc:{k}", label) for k, label in wanted[:MAX_SUGGEST]]

    @staticmethod
    def _reply_messages(reply: str, buttons: list[tuple[str, str]]) -> list[dict]:
        if not buttons:
            return [text_msg(reply)]
        if len(reply) <= 1000:
            return [buttons_msg(reply, buttons)]
        return [text_msg(reply), buttons_msg("Chaho to ye bhi dekh sakte hain 👇", buttons)]

    def _act(self, s: Session, r, trip: T.TripInfo) -> list[dict]:
        """What the model asked the app to do besides replying."""
        if r.action == "airport_route" and trip.flight:
            f = trip.flight["flights"]
            lat, lon = AIRPORT_COORDS.get(f["from_code"], (None, None))
            target = f"{city(f['from_code'])} Airport"
            body = f"🗺️ Navigate to *{target}*" + (f"\n{trip.route['km']} km · about {trip.route['minutes']} min (traffic ke bina)" if trip.route else "")
            out = [cta_msg(body, "🗺️ Open Maps", maps_link(lat, lon) if lat is not None else maps_link(query=target))]
            if not trip.loc:
                out += self._ask_location(s, "airport_route", "Location share karo, to exact nikalne ka time nikaal sakte hain.")
            return out
        if r.action == "stay_route" and trip.stay:
            h = trip.stay["hotels"]
            return [cta_msg(f"🗺️ Navigate to *{h['name']}*, {h['area']}", "🗺️ Open Maps",
                            maps_link(query=f"{h['name']}, {h['area']}, {city(h['city_code'])}"))]
        if r.action == "ask_location" and not trip.loc:
            return self._ask_location(s, "ask_location", "Location share karo, phir sahi raasta aur timing nikaal sakte hain.")
        if r.action == "nearby" and r.query and not EVENTISH.search(r.query.lower()):
            s.ctx["follow_up"] = {"agent": "nearby", "slots": {"place": r.query}}
        elif r.action == "events" or (r.action == "nearby" and EVENTISH.search(r.query.lower())):  # "events" is not a place on a map
            s.ctx["follow_up"] = {"agent": "events", "slots": {}}
        return []

    @staticmethod
    def _ask_location(s: Session, pending: str, why: str) -> list[dict]:
        s.step, s.ctx["buddy_pending"] = "buddy_wait_location", pending
        return [location_request_msg(f"📍 {why} Ye sirf kuch ghante ke liye use hoga.")]

    # ------------------------------------------------------------------ memory screens
    async def _show_memories(self, s: Session) -> list[dict]:
        memories = await self._safe(self.repo.list_memories, s.user["id"], 15)
        if not memories:
            return [text_msg("Abhi mujhe tumhare baare mein kuch khaas yaad nahi. Baat karte-karte important cheezein yaad rakhne ki "
                             "koshish rahegi. 🙂")]
        lines = "\n".join(f"{CATEGORY_ICON.get(m['category'], '•')} {m['content']}" for m in memories)
        return [buttons_msg(f"Mujhe tumhare baare mein ye yaad hai:\n\n{lines}\n\nSab delete karwana ho to bolo *forget me*.",
                            [MENU_BUTTON])]

    @staticmethod
    def _ask_forget() -> list[dict]:
        return [buttons_msg("Tumhari saari memory, chat history aur shared location delete ho jayegi. Bookings aur payment records "
                            "rahenge, wo zaroori records hain. Pakka delete karein?",
                            [("buddy:forgetyes", "🗑️ Yes, delete"), ("buddy:forgetno", "↩️ No, keep")])]

    async def _on_tap(self, s: Session, val: str) -> list[dict]:
        if val == "forgetyes":
            try:
                await self._db(self.repo.forget_user, s.user["id"])
            except Exception:
                logger.exception("Could not forget user %s", s.user["id"])
                return [buttons_msg("Delete nahi ho paya. Thodi der baad phir try karo. 🙏", [MENU_BUTTON])]
            for key in ("loc", "buddy_pending", "near", "ev"):
                s.ctx.pop(key, None)
            return [buttons_msg("Ho gaya ✅ Tumhari memory, chat history aur location delete ho gayi. Ab se fresh start!", [MENU_BUTTON])]
        if val == "forgetno":
            return [text_msg("Theek hai, kuch delete nahi kiya. 🙂 Bolo, kya chal raha hai?")]
        return await self.on_enter(s)
