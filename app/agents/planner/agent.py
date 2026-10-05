"""Trip Planner agent: share a reel and get a trip. A link (Instagram, YouTube, TikTok), a video, a screenshot or just a few
words ("Goa 3 din") tells it where you want to go; it reads the reel to find the place and what to do there, writes a day-by-day
plan, and, for the cities we book, carries on into flights, then hotel, cab and events with the same details.

Button ids: planner:plan / days / fix / book, rdays:<n>. State: ctx["plan"] (the Insight as a dict) and these steps:
planner_wait (waiting for a reel), planner_busy (reading it), planner_review (shown what it found), planner_days, planner_done,
planner_fix (the user corrects the place). Reading a video takes 20-30 seconds, longer than a WhatsApp webhook should be kept
waiting, so it runs in the background: we answer at once and send the result when it is ready (background=False runs it inline).
"""
import asyncio
import logging
from dataclasses import asdict

from app.agents.base import Agent, Session
from app.agents.planner import media as M
from app.agents.planner.analyzer import MAX_DAYS, Insight, format_itinerary
from app.core.geo import maps_link
from app.core.messages import buttons_msg, cta_msg, list_msg, text_msg
from app.core.places import CITIES, city

logger = logging.getLogger(__name__)
ANALYZE_TIMEOUT_S, PLAN_TIMEOUT_S = 60, 30
ACK = "🎬 Got your reel! Taking a look, this takes 20-30 seconds ⏳"
SHARE_HELP = ("Where would you like to travel, and for how many days? (e.g. *Tokyo 5 days* or *Goa 3 days*).\n\n"
              "Or send an Instagram, YouTube or TikTok reel link to turn it into an itinerary.")
MSG_NEED_MORE = ("I couldn't read the caption from this link (Instagram often blocks it). 🙏 Please send the reel's *video* or "
                 "a *screenshot*, or just type where it is.")
MSG_NOT_FOUND = "🤔 I couldn't tell the place from this reel. Send a *screenshot*, or just type the place name, like *Munnar*."
MSG_FAILED = "😕 I had trouble reading the reel. Please send it again, or type the place name."
MSG_TOO_BIG = "😕 That video is too big (up to 16 MB works). Send a shorter screen recording, a screenshot or the place name."


class PlannerAgent(Agent):
    name = "planner"
    title = "Trip Planner"
    emoji = "🗺️"
    menu_desc = "Share a reel, get a trip plan"
    in_menu = False  # the Trip Guide offers it ("From a reel"), and shared reels and "plan a trip" text still reach it
    owns = frozenset({"planner", "rdays"})

    def __init__(self, repo, brain, links=None, download=None, send=None, video_parts=None, background: bool = True):
        self.repo = repo                       # CoreRepo (conversation state for the background result)
        self.brain = brain                     # OpenAIPlanner (or a fake with the same methods)
        self.links = links or M.LinkReader()
        self._download = download              # async (media_id, max_bytes) -> (bytes, mime); default: WhatsApp
        self._send = send                      # async (number, message); default: WhatsApp
        self._video_parts = video_parts or M.video_to_parts
        self.background = background
        self._tasks: set[asyncio.Task] = set()

    async def _db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def reset(self, s: Session) -> None:
        s.ctx.pop("plan", None)

    def expects_text(self, s: Session) -> bool:
        return s.step in ("planner_wait", "planner_fix")

    def expects_media(self, s: Session) -> bool:
        return s.step in ("planner_wait", "planner_fix")

    def claims(self, text: str) -> bool:
        return M.find_reel_url(text) is not None

    def accepts_media(self, media: dict) -> bool:
        return media.get("kind") in ("video", "image")  # nothing else wants a video; a lone screenshot is most likely a reel

    # ------------------------------------------------------------------ entry points
    async def on_enter(self, s: Session) -> list[dict]:
        self.reset(s)
        s.ctx["agent"], s.step = "planner", "planner_wait"
        return [text_msg(f"🗺️ *Trip Planner*\n\n{SHARE_HELP}\n\nI can plan your itinerary day-by-day and help you coordinate flights, stays and cabs.")]

    async def start(self, s: Session, slots: dict) -> list[dict]:
        """Free text like 'plan a trip to Goa' or multi-service 'plan everything flight and hotel and cab'."""
        c = s.ctx
        trip = c.get("trip") or {}
        dest = slots.get("to") or slots.get("unknown_to") or trip.get("to") or trip.get("city") or c.get("to")
        orig = slots.get("from") or trip.get("from") or c.get("from")
        
        has_flight = bool(c.get("flight_id") or c.get("flight_summary") or trip.get("flight_no") or trip.get("summary"))
        flight_sum = c.get("flight_summary") or trip.get("summary") or ""
        pax_names = c.get("names") or trip.get("names") or []

        text_low = (slots.get("text") or "").lower()
        is_bundle = any(w in text_low for w in ("everything", "flight and hotel", "hotel and cab", "flight hotel", "airport", "bundle", "all"))

        # Contextual multi-service bundle coordination
        if (has_flight and (is_bundle or dest)) or (dest and is_bundle):
            dest_name = city(dest) if dest in CITIES else dest
            pax_str = f" for {', '.join(pax_names)}" if pax_names else ""
            flt_line = f"1. ✈️ *Flight*: {flight_sum}{pax_str} (selected & ready to confirm)" if flight_sum else f"1. ✈️ *Flight*: {city(orig) if orig else 'Origin'} ➜ {dest_name}"
            hotel_line = f"2. 🏨 *Hotel*: Finding top-rated stays in {dest_name} for your dates"
            cab_line = f"3. 🚕 *Airport Cab*: Transfer from {dest_name} airport directly to your hotel"

            body = (
                f"🗺️ *Complete {dest_name} Trip Plan*\n\n"
                f"I've bundled your entire trip together:\n\n"
                f"{flt_line}\n"
                f"{hotel_line}\n"
                f"{cab_line}\n\n"
                f"Shall we confirm your flight first to lock in your seats, or explore hotel options right away?"
            )
            s.ctx["agent"] = "planner"
            return [text_msg(body)]

        # If a destination is known (domestic or international)
        if dest:
            s.ctx["agent"] = "planner"
            label = city(dest) if dest in CITIES else dest
            city_code = dest if dest in CITIES else None
            return self._show(s, Insight(label=label, city_code=city_code, confidence="high"))

        return await self.on_enter(s)

    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        s.ctx["agent"] = "planner"
        if reply_id:
            return await self._on_tap(s, reply_id)
        text = text.strip()
        if url := M.find_reel_url(text):
            return await self._run(s, link=url, text=text.replace(url, " ").strip())
        if text and s.step in ("planner_wait", "planner_fix"):
            return await self._run(s, text=text, inline=True, named=True)  # a few words are quick to read: no background job
        return await self.on_enter(s)


    async def on_media(self, s: Session, media: dict, caption: str) -> list[dict]:
        s.ctx["agent"] = "planner"
        return await self._run(s, media=media, text=caption)

    # ----------------------------------------------------------------- reading a reel
    async def _run(self, s: Session, media: dict | None = None, link: str | None = None, text: str = "",
                   inline: bool = False, named: bool = False) -> list[dict]:
        job = {"media": media, "link": link, "text": text, "named": named}
        if inline or not self.background:
            insight, messages = await self._read(**job)
            s.step = self._apply(s.ctx, insight)
            return ([] if inline else [text_msg(ACK)]) + messages
        s.step = "planner_busy"
        task = asyncio.create_task(self._deliver(s.phone, job))
        self._tasks.add(task)  # keep a reference so the task is not garbage collected mid-way
        task.add_done_callback(self._tasks.discard)
        return [text_msg(ACK)]

    async def _deliver(self, phone: str, job: dict) -> None:
        """Background: read the reel, save what we found to the conversation, and message the user."""
        try:
            insight, messages = await self._read(**job)
        except Exception:
            logger.exception("Reel job failed")
            insight, messages = None, [text_msg(MSG_FAILED)]
        convo = await self._db(self.repo.get_conversation, phone)
        ctx = dict((convo or {}).get("context") or {})
        step = self._apply(ctx, insight)
        await self._db(self.repo.save_conversation, phone, step, ctx)
        send = self._send
        if send is None:
            from app.services.whatsapp_service import WhatsAppService
            send = WhatsAppService.send
        for m in messages:
            await send(phone.lstrip("+"), m)

    @staticmethod
    def _apply(ctx: dict, insight: Insight | None) -> str:
        ctx["agent"] = "planner"
        if insight is None:
            return "planner_fix"
        ctx["plan"] = asdict(insight)
        return "planner_review"

    async def _read(self, media: dict | None, link: str | None, text: str,
                    named: bool = False) -> tuple[Insight | None, list[dict]]:
        """Frames + speech + caption -> what and where. Returns (insight, messages to send); insight is None when we need more."""
        frames, transcript = [], ""
        try:
            if link:
                meta = await self.links.read(link)
                text = " ".join(x for x in (text, meta["title"], meta["description"]) if x)
            if media:
                video = media["kind"] == "video"
                download = self._download
                if download is None:
                    from app.services.whatsapp_service import WhatsAppService
                    download = WhatsAppService.download_media
                data, _ = await download(media["id"], M.MAX_VIDEO_BYTES if video else M.MAX_IMAGE_BYTES)
                if video:
                    frames, audio = await self._video_parts(data)
                    transcript = await self._transcribe(audio) if audio else ""
                else:
                    frames = [await asyncio.to_thread(M.shrink_image, data)]
            if not (frames or transcript or text.strip()):
                return None, [text_msg(MSG_NEED_MORE)]
            insight = await asyncio.wait_for(self.brain.analyze(frames, transcript, text, user_named=named), ANALYZE_TIMEOUT_S)
        except ValueError:  # WhatsAppService.download_media: file too large
            return None, [text_msg(MSG_TOO_BIG)]
        except Exception:
            logger.exception("Could not read the reel")
            return None, [text_msg(MSG_FAILED)]
        if insight is None:
            return None, [text_msg(MSG_NOT_FOUND)]
        return insight, self._card(insight)

    async def _transcribe(self, audio: bytes) -> str:
        try:
            return await self.brain.transcribe(audio)
        except Exception:
            logger.warning("Could not transcribe the reel's audio", exc_info=True)  # frames and caption still work
            return ""

    # --------------------------------------------------------------------- screens
    def _show(self, s: Session, insight: Insight) -> list[dict]:
        s.ctx["plan"], s.step = asdict(insight), "planner_review"
        return self._card(insight)

    @staticmethod
    def _card(i: Insight) -> list[dict]:
        lines = [f"🎬 *{i.label}*"]
        if i.confidence == "low":
            lines.append("🤔 Not fully sure, but this looks like the place.")
        if i.places:
            lines.append("📍 " + " · ".join(i.places))
        if i.activities:
            lines.append("🎯 " + ", ".join(i.activities))
        if i.season:
            lines.append(f"🌤️ Best time: {i.season}")
        if i.vibe:
            lines.append(f"✨ {i.vibe}")
        lines.append("\n✈️ I can book flights, hotels, cabs and events here." if i.city_code else
                     "\nℹ️ I can't book this place yet, but I can plan it for you.")
        return [buttons_msg("\n".join(lines), [("planner:plan", "📋 Plan my trip"), ("planner:fix", "✏️ Wrong place?"), ("nav:menu", "🏠 Menu")])]

    def _ask_days(self, s: Session) -> list[dict]:
        s.step = "planner_days"
        plan = s.ctx["plan"]
        rows = [(f"rdays:{n}", f"{n} day" + ("" if n == 1 else "s"), "Suggested for this reel" if n == plan["days"] else "") for n in range(1, MAX_DAYS + 1)]
        return [list_msg(f"📅 *{Insight(**plan).short}*: how many days?", "Choose days", rows, "Trip length")]

    async def _on_tap(self, s: Session, reply_id: str) -> list[dict]:
        kind, _, val = reply_id.partition(":")
        plan = s.ctx.get("plan")
        if kind == "rdays" and plan and val.isdigit() and 1 <= int(val) <= MAX_DAYS:
            return await self._make_plan(s, int(val))
        if kind == "planner":
            if val in ("plan", "days") and plan:
                return self._ask_days(s)
            if val == "fix":
                s.step = "planner_fix"
                return [text_msg("✏️ Where is it? Type the place name (like *Munnar*), or send a screenshot of the reel.")]
            if val == "book" and plan and plan.get("city_code"):
                s.ctx["queue"] = ["hotel", "cab", "events"]  # what the flight flow offers after the ticket
                s.ctx["follow_up"] = {"agent": "flight", "slots": {"to": plan["city_code"]}}
                return [text_msg("Let's start with flights ✈️ then hotel, cab and events.")]
        return await self.on_enter(s)

    async def _make_plan(self, s: Session, days: int) -> list[dict]:
        insight = Insight(**s.ctx["plan"])
        try:
            plan = await asyncio.wait_for(self.brain.itinerary(insight, days, hinglish=bool(s.ctx.get("hinglish"))), PLAN_TIMEOUT_S)
        except Exception:
            logger.exception("Could not write the itinerary")
            return [buttons_msg("😕 I had trouble writing the plan. Try again?", [(f"rdays:{days}", "🔁 Try again"), ("nav:menu", "🏠 Menu")])]
        s.ctx["plan"]["days"], s.step = days, "planner_done"
        out = [text_msg(t) for t in format_itinerary(insight, plan)]
        if insight.city_code:
            return out + [buttons_msg("Like it? You can book flights, hotels, cabs and events right here. 👇",
                                      [("planner:book", "✈️ Book this trip"), ("planner:days", "🔁 Change days"), ("nav:menu", "🏠 Menu")])]
        return out + [cta_msg(f"📍 See {insight.short} on Google Maps.", "🗺️ Open Maps", maps_link(query=insight.label)),
                      buttons_msg(f"ℹ️ I can't book {insight.short} yet, but you have the plan.",
                                  [("planner:days", "🔁 Change days"), ("planner:fix", "✏️ Other place"), ("nav:menu", "🏠 Menu")])]
