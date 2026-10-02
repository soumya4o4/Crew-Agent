"""The Concierge: the one entry point for every WhatsApp message.

It owns the conversation (state, dedupe, chat history), works out what the user needs, and hands
the message to the right specialist agent. Agents never talk to each other directly; they share
the Session context (e.g. `ctx["trip"]`) which the Concierge persists between messages.
"""
import asyncio
import logging

from app.agents.base import Agent, Session
from app.agents.concierge.classifiers import Intent
from app.agents.concierge.router import IntentRouter
from app.core.messages import buttons_msg, list_msg, text_msg
from app.core.geo import fresh_location, save_location
from app.core.places import city, example_route
from app.core.safety import CRISIS_MESSAGE, is_crisis
from app.core.utils import day_greeting, normalize_phone, now_ist

logger = logging.getLogger(__name__)
GREETINGS = {"hi", "hii", "hiii", "hello", "hey", "hlo", "namaste", "start", "hola", "yo"}
MENU_WORDS = {"menu", "restart", "reset", "home", "cancel"}
HISTORY_LIMIT = 4  # enough for "yes" / "that one"; more makes the model re-queue old requests


class Concierge:
    def __init__(self, repo, agents: list[Agent], router: IntentRouter):
        self.repo = repo
        self.agents = {a.name: a for a in agents}
        self.by_kind = {kind: a for a in agents for kind in a.owns}
        self.router = router

    async def _db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # -------------------------------------------------------------------- entry point
    async def handle(self, wa_number: str, profile_name: str, msg_id: str, text: str, reply_id: str | None,
                     media: dict | None = None, location: dict | None = None) -> list[dict]:
        phone = normalize_phone(wa_number)
        if not phone:
            return [text_msg("Sorry, I couldn't read your phone number. Please message me from your WhatsApp number.")]

        convo = await self._db(self.repo.get_conversation, phone)
        ctx = dict((convo or {}).get("context") or {})
        if msg_id and ctx.get("last_msg_id") == msg_id:
            return []  # WhatsApp retried this webhook; already handled
        ctx["last_msg_id"] = msg_id
        if ctx.get("loc") and not fresh_location(ctx):
            ctx.pop("loc")  # a shared location is only kept for a few hours

        user = await self._db(self.repo.get_or_create_user, phone, profile_name)
        s = Session(phone, user, (convo or {}).get("current_step", "start"), msg_id, ctx)

        if location:
            out = await self._on_location(s, save_location(s.ctx, **location))
        elif media:
            out = await self._on_media(s, media, text.strip())
        else:
            out = await self._dispatch(s, text.strip(), reply_id)
        out = out + await self._follow_up(s)
        await self._db(self.repo.save_conversation, s.phone, s.step, s.ctx)
        await self._remember(s, "📍 (shared a location)" if location else text, out)
        return out

    async def _on_location(self, s: Session, loc: dict) -> list[dict]:
        """A shared pin: the agent that asked for it uses it, otherwise we keep it and offer what it can be used for."""
        active = self.agents.get(s.ctx.get("agent"))
        if active and active.expects_location(s):
            return await active.on_location(s, loc)
        buttons = [(f"svc:{n}", f"{self.agents[n].emoji} {self.agents[n].title}") for n in ("nearby", "buddy") if n in self.agents]
        s.step = "menu"
        return [buttons_msg("📍 Got your location! I'll use it for the next few hours to find things near you and to guide you. "
                            "What do you need?", (buttons + [("nav:menu", "🏠 Menu")])[:3])]

    async def _follow_up(self, s: Session) -> list[dict]:
        """An agent can ask for another one to continue right after its reply (Buddy: "show me cafés nearby")."""
        fu = s.ctx.pop("follow_up", None)
        agent = self.agents.get(fu.get("agent")) if fu else None
        if agent is None:
            return []
        s.ctx["agent"] = agent.name
        return await agent.start(s, fu.get("slots") or {})

    async def _on_media(self, s: Session, media: dict, caption: str) -> list[dict]:
        """A photo or PDF: only an agent that asked for one (e.g. visa documents) can use it."""
        active = self.agents.get(s.ctx.get("agent"))
        if active and active.expects_media(s):
            return await active.on_media(s, media, caption)
        for agent in self.agents.values():  # e.g. a reel video the user forwards: the planner reads it
            if agent.accepts_media(media):
                s.ctx["agent"] = agent.name
                return await agent.on_media(s, media, caption)
        return self._main_menu(s, note="Thanks! I can't use files at the moment, but you can start a visa application "
                                       "from the menu and send your documents there. 📎")

    async def confirm_payment(self, link_id: str) -> tuple[str, list[dict]] | None:
        """Razorpay webhook: ask each agent that takes payments whether this link is theirs."""
        for agent in self.agents.values():
            if hasattr(agent, "confirm_payment") and (result := await agent.confirm_payment(link_id)):
                return result
        return None

    async def release_expired(self) -> list[tuple[str, list[dict]]]:
        """Sweeper: every agent that holds inventory for unpaid bookings releases the ones past their window."""
        notices = []
        for agent in self.agents.values():
            if hasattr(agent, "release_expired"):
                try:
                    notices += await agent.release_expired()
                except Exception:  # one agent's problem (say, a table not created yet) must not hide the others' notices
                    logger.exception("Releasing expired %s bookings failed", agent.name)
        return notices

    async def _dispatch(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        if reply_id:
            kind, _, val = reply_id.partition(":")
            if kind == "nav":
                return self._main_menu(s)
            if kind == "svc":
                return await self._enter(s, val)
            agent = self.by_kind.get(kind)
            if agent is None:
                return self._main_menu(s, note="That option is no longer valid, let's start over.")
            s.ctx["agent"] = agent.name
            return await agent.process(s, text, reply_id)

        low = text.lower()
        if low in GREETINGS or low in MENU_WORDS:
            return self._main_menu(s, greet=True)
        if is_crisis(text):  # before anything else, and independent of the LLM
            return self._crisis(s)
        for agent in self.agents.values():  # "forget me", a reel link: some messages belong to one agent wherever you are
            if agent.claims(text):
                s.ctx["agent"] = agent.name
                return await agent.process(s, text, None)

        active = self.agents.get(s.ctx.get("agent"))
        if active and active.expects_text(s):  # mid-question ("which date?"): the active agent gets the answer...
            other = self.router.keywords.classify(text, now_ist().date())
            if active.keeps_text(text, other, set(self.agents)):
                return await active.process(s, text, None)
            return await self._apply(s, other, text)  # ...unless they clearly switched topic ("actually, a hotel")

        # Chat history only helps mid-conversation ("yes", "there"). From the menu it just drags old requests along.
        history = await self._history(s) if self.router.uses_llm and active else []
        intent = await self.router.classify(text, history, active.name if active else None, s.ctx.get("trip"))
        return await self._apply(s, intent, text)

    def _crisis(self, s: Session) -> list[dict]:
        """Self-harm talk: a caring message with helplines. If Buddy exists the chat carries on with it."""
        logger.warning("Safety flag self_harm (pattern) for user %s", s.user["id"])  # no message content in logs
        if "buddy" in self.agents:
            s.ctx["agent"], s.step = "buddy", "buddy_chat"
        return [text_msg(CRISIS_MESSAGE)]

    async def _history(self, s: Session) -> list[dict]:
        """Recent chat for the LLM. Best-effort: routing still works if the `messages` table is missing."""
        try:
            return await self._db(self.repo.recent_messages, s.user["id"], HISTORY_LIMIT)
        except Exception:
            logger.warning("Could not read chat history (is the `messages` table created?)")
            return []

    # --------------------------------------------------------- understanding the request
    def _is_live(self, name: str) -> bool:
        return self.agents[name].menu_desc != "Coming soon"

    async def _apply(self, s: Session, intent: Intent, text: str = "") -> list[dict]:
        """Act on what the user wants: do the first thing now, queue the rest, or ask with options."""
        if intent.name == "bookings":
            s.ctx["agent"] = "flight"
            return await self.agents["flight"].process(s, "", "menu:bookings")
        if intent.name == "help":
            return self._main_menu(s, note=self._help_text())
        if "buddy" in self.agents and text and self._is_personal(intent):
            s.ctx["queue"], s.ctx["agent"] = [], "buddy"
            return await self.agents["buddy"].process(s, text, None)

        if (place := intent.slots.get("unknown_to")) and "flight" in self.agents \
                and intent.name not in ("bookings", "help", "nearby", "visa", "forex"):
            s.ctx["queue"], s.ctx["agent"] = [], "flight"
            return await self.agents["flight"].trip_idea(s, place)

        wanted = [n for n in dict.fromkeys([intent.name, *intent.also]) if n in self.agents]
        if wanted:
            wanted.sort(key=lambda n: not self._is_live(n))  # stable: what we can actually do now goes first
            first, rest = wanted[0], wanted[1:]
            s.ctx["queue"], s.ctx["agent"] = rest, first
            out = await self.agents[first].start(s, intent.slots)
            if rest:
                later = " and ".join(self.agents[n].title.lower() for n in rest)
                out.insert(0, text_msg(f"Got it! Let's start with {self.agents[first].title.lower()}, "
                                       f"then I'll help with {later}. 👍"))
            return out
        return self._clarify(s, intent)

    @staticmethod
    def _is_personal(intent: Intent) -> bool:
        """Talk that isn't a service request: feelings, advice, chit-chat. A vague trip idea ("I want to go to Goa")
        still gets the service options instead."""
        return intent.name == "buddy" or (intent.name in ("smalltalk", "unknown") and not intent.options
                                          and not intent.slots.get("to"))

    def _clarify(self, s: Session, intent: Intent) -> list[dict]:
        """Request too vague to act on: offer the most useful next steps instead of a generic menu."""
        options = [o for o in intent.options if o in self.agents][:3]
        question = intent.question or intent.reply
        if not options and intent.slots.get("to"):  # they named a place but not what they need
            options = ["flight", "hotel", "planner"]
            question = f"Nice, *{city(intent.slots['to'])}*! 😍 What do you need?"
        if not options:
            return self._main_menu(s, note=intent.reply or "I can help with flights today, and hotels, cabs, trip "
                                                           "planning, events, visa & forex soon. What do you need? 👇")
        s.step = "menu"
        s.ctx["pending_slots"] = intent.slots  # carried into whichever option they tap
        buttons = [(f"svc:{o}", f"{self.agents[o].emoji} {self.agents[o].title}") for o in options]
        return [buttons_msg(question or "What would you like help with? 👇", buttons)]

    async def _enter(self, s: Session, name: str) -> list[dict]:
        agent = self.agents.get(name)
        if agent is None:
            return self._main_menu(s)
        s.ctx["agent"] = name
        s.ctx["queue"] = [n for n in s.ctx.get("queue") or [] if n != name]
        slots = s.ctx.pop("pending_slots", None)
        return await (agent.start(s, slots) if slots else agent.on_enter(s))

    # ---------------------------------------------------------------------- screens
    def _main_menu(self, s: Session, greet: bool = False, note: str = "") -> list[dict]:
        s.step = "menu"
        for key in ("agent", "queue", "pending_slots"):
            s.ctx.pop(key, None)
        for agent in self.agents.values():
            agent.reset(s)
        if greet:
            first = (s.user.get("name") or "").split(" ")[0]
            first = first if first and first != "Unknown" else "there"
            body = (f"{day_greeting()}, {first}! 👋\nI'm your travel concierge: flights, stays, cabs, trip plans, events, visa & forex "
                    "in one chat ⚡\n\nWhat do you need today? Pick below, or just type it, "
                    f"like *{example_route()} tomorrow*.")
        else:
            body = note or "What do you need next? 👇"
        rows = [(f"svc:{a.name}", f"{a.emoji} {a.title}", a.menu_desc) for a in self.agents.values() if a.in_menu]
        rows.append(("menu:bookings", "📋 My Bookings", "Your flights & trips"))
        return [list_msg(body, "Choose service", rows, "Services")]

    def _help_text(self) -> str:
        live = [a.title for a in self.agents.values() if a.menu_desc != "Coming soon"]
        soon = [a.title for a in self.agents.values() if a.menu_desc == "Coming soon"]
        return (f"ℹ️ *What I can do*\n• Live now: {', '.join(live)}\n• Coming soon: {', '.join(soon)}\n"
                f"• Just type what you need, like *{example_route()} this Saturday*\n• Type *menu* anytime to start over")

    # ----------------------------------------------------------------------- memory
    async def _remember(self, s: Session, text: str, out: list[dict]) -> None:
        """Best-effort chat history; the bot keeps working if the `messages` table isn't created yet."""
        try:
            await self._db(self.repo.log_message, s.user["id"], "user", s.ctx.get("agent"), text)
            for m in out:
                if body := m.get("body"):
                    await self._db(self.repo.log_message, s.user["id"], "assistant", s.ctx.get("agent"), body)
        except Exception:
            logger.warning("Could not save chat history (is the `messages` table created?)", exc_info=False)
