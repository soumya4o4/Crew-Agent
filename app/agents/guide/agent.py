"""Trip Guide: for someone who doesn't know where to start. It asks a few easy questions (passport, where from, where to, when,
how long, round trip or one way; or, if they don't know where to go, budget and the kind of trip), then builds a personal
roadmap of the WHOLE journey, from leaving home to getting back home: documents, flights there and back, the stay, money,
every ride to and from the airport, packing and the return day. Each step explains itself and has buttons that start the right
service already filled in. Steps tick themselves off when the traveller books with us, and the roadmap can be reopened any time.

Button ids: guide:* (new, road, notes), gpass:, gkn:, gbud:, gvibe:, gdur:, gmon:, gdest:, gorg:, gwhen:, gdays:, gtrip:,
gstep:, gact:<step>:<n>, gdone:
State: ctx["gtmp"] while answering the questions; ctx["guide"] = the finished roadmap (it survives the main menu).
"""
import asyncio
import logging
import re
from datetime import date, timedelta

from app.agents.base import Agent, Session
from app.agents.forex.currencies import currency_for
from app.agents.guide import roadmap as R
from app.core.messages import buttons_msg, list_msg, text_msg
from app.core.places import CITIES, CITY_ALIASES, city, city_pattern, country_of, visa_code_for
from app.core.utils import inr, now_ist, parse_date

logger = logging.getLogger(__name__)
TEXT_STEPS = ("awaiting_guide_passport", "awaiting_guide_place", "awaiting_guide_date", "awaiting_guide_origin")
ASKING = ("gpass", "gkn", "gbud", "gvibe", "gdur", "gmon", "gdest", "gorg", "gwhen", "gdays", "gtrip")
BUDGETS = [(30_000, "Under ₹30,000"), (70_000, "₹30,000 - ₹70,000"), (150_000, "₹70,000 - ₹1.5 lakh"), (300_000, "₹1.5 lakh and more")]
VIBES = [("beach", "🏖️ Beach & relax"), ("mountains", "🏔️ Mountains & nature"), ("city", "🏙️ City & culture"),
         ("adventure", "🧗 Adventure"), ("food", "🍜 Food & shopping"), ("any", "🎲 Surprise me")]
DURATIONS = [(4, "3-4 days"), (6, "5-7 days"), (9, "8-10 days"), (12, "11-14 days")]
VISA_TYPE_STATUS = {"e-visa": "e_visa", "sticker": "required", "visa-free": "not_required", "on-arrival": "on_arrival"}
DEMONYMS = {"indian": "india", "american": "united states", "british": "united kingdom", "emirati": "united arab emirates",
            "nepali": "nepal", "pakistani": "pakistan", "bangladeshi": "bangladesh", "srilankan": "sri lanka", "chinese": "china",
            "japanese": "japan", "french": "france", "german": "germany", "australian": "australia", "canadian": "canada"}


def norm_country(text: str) -> str:
    low = " ".join((text or "").lower().split())
    return DEMONYMS.get(low, low)


def same_country(a: str, b: str) -> bool:
    """Is a traveller of nationality `a` going to country `b` at home? ("Indian" and "India" are the same place.)"""
    a, b = norm_country(a), norm_country(b)
    return bool(a and b and (a == b or (visa_code_for(a) and visa_code_for(a) == visa_code_for(b))))


class GuideAgent(Agent):
    name = "guide"
    title = "Trip Guide"
    emoji = "🧳"
    menu_desc = "Step-by-step help for any trip"
    owns = frozenset({"guide", "gpass", "gkn", "gbud", "gvibe", "gdur", "gmon", "gdest", "gorg", "gwhen", "gdays", "gtrip", "gstep", "gact", "gdone"})

    def __init__(self, repo, advisor=None):
        self.repo = repo        # GuideRepo
        self.advisor = advisor  # TravelAdvisor: ideas, visa advice, destination notes. None: simpler wording

    async def _db(self, fn, *args, **kw):
        return await asyncio.to_thread(lambda: fn(*args, **kw))

    async def _safe(self, fn, *args, **kw):
        try:
            return await self._db(fn, *args, **kw)
        except Exception:
            logger.warning("Guide lookup failed", exc_info=True)
            return None

    def reset(self, s: Session) -> None:
        for key in ("gtmp", "gideas"):  # (the finished roadmap, ctx["guide"], is kept)
            s.ctx.pop(key, None)

    def expects_text(self, s: Session) -> bool:
        return s.step in TEXT_STEPS

    # ------------------------------------------------------------------ entry points
    async def on_enter(self, s: Session) -> list[dict]:
        self.reset(s)
        s.step = "guide_menu"
        road = s.ctx.get("guide")
        buttons = [("guide:new", "🚀 Plan a trip")]
        if road:
            buttons.append(("guide:road", f"📋 {road['dest']['place']} plan"[:20]))
        buttons.append(("svc:planner", "🎬 From a reel"))
        return [buttons_msg("🧳 *Trip Guide*\nNew to travelling, or not sure where to start? I'll walk you through the whole journey, "
                            "one step at a time: from leaving your home to getting back home. Where to go, what to do first, what to "
                            "book and when, and what each thing needs.\n\nNothing to prepare. Just answer a few easy questions.", buttons)]

    async def start(self, s: Session, slots: dict) -> list[dict]:
        """Free text like "help me plan a trip to Goa": a known place skips the where question."""
        self.reset(s)
        s.ctx["gtmp"] = {}
        if (code := slots.get("to")) in CITIES:
            s.ctx["gtmp"]["dest"] = self._dest(city(code), country_of(code), code)
            return await self._need_passport_or_continue(s)
        return await self.on_enter(s)

    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        if not reply_id:
            if s.step == "awaiting_guide_passport":
                return await self._on_passport_text(s, text)
            if s.step == "awaiting_guide_place":
                return await self._on_place_text(s, text)
            if s.step == "awaiting_guide_origin":
                return await self._on_origin_text(s, text)
            if s.step == "awaiting_guide_date":
                return await self._on_date_text(s, text)
            return await self.on_enter(s)
        kind, _, val = reply_id.partition(":")
        t = s.ctx.setdefault("gtmp", {}) if kind in ASKING else None
        if kind == "guide":
            if val == "new":
                s.ctx["gtmp"] = {}
                return await self._need_passport_or_continue(s)
            if val == "road":
                return await self._show_roadmap(s)
            if val == "notes":
                return await self._show_notes(s)
        elif kind == "gpass":
            if val == "home" and t.get("home"):
                s.ctx["citizen"] = t["home"]
                return await self._after_passport(s)
            s.step = "awaiting_guide_passport"
            return [text_msg("✏️ Type your passport country, like *India* or *Nepal*.")]
        elif kind == "gkn":
            if val == "yes":
                s.step = "awaiting_guide_place"
                return [text_msg("📍 Type the place, like *Dubai*, *Goa* or *Bali*.")]
            return self._ask_budget(s)
        elif kind == "gbud" and val.isdigit():
            t["budget"] = int(val)
            return self._ask_vibe(s)
        elif kind == "gvibe":
            t["vibe"] = val
            return self._ask_duration(s)
        elif kind == "gdur" and val.isdigit():
            t["days"] = int(val)
            return self._ask_month(s)
        elif kind == "gmon":
            t["month"] = val
            return await self._suggest(s)
        elif kind == "gdest" and val.isdigit() and int(val) < len(s.ctx.get("gideas") or []):
            idea = s.ctx["gideas"][int(val)]
            t["dest"] = await self._resolve(idea["place"], idea["country"])
            return await self._after_destination(s)
        elif kind == "gorg":
            if val == "more":
                s.step = "awaiting_guide_origin"
                return [text_msg("🏙️ Type the city you'll start from.")]
            return await self._set_origin(s, val)
        elif kind == "gwhen":
            if val == "more":
                s.step = "awaiting_guide_date"
                return [text_msg("📅 Type the date you want to travel, like *15/12* or *20 Dec*.")]
            return await self._set_travel(s, date.fromisoformat(val))
        elif kind == "gdays" and val.isdigit():
            t["days"] = int(val)
            return await self._after_destination(s)
        elif kind == "gtrip" and val in ("round", "one"):
            t["round_trip"] = val == "round"
            return await self._build(s)
        elif kind == "gstep":
            return await self._show_step(s, val)
        elif kind == "gact":
            return await self._do_action(s, val)
        elif kind == "gdone":
            g = s.ctx.get("guide")
            if g and val not in g["done"]:
                g["done"].append(val)
            return await self._show_roadmap(s)
        return await self.on_enter(s)

    # ----------------------------------------------------------------- the questions
    async def _need_passport_or_continue(self, s: Session) -> list[dict]:
        if s.ctx.get("citizen"):
            return await self._after_passport(s)
        t = s.ctx.setdefault("gtmp", {})
        t["home"] = await self._db(self.repo.home_country, s.user["id"])
        s.step = "guide_passport"
        buttons = ([("gpass:home", f"🛂 {t['home']}"[:20])] if t["home"] else []) + [("gpass:other", "🌍 Other passport")]
        return [buttons_msg("Let's plan it together! 🌍\n\nFirst: which passport do you travel on? Entry rules depend on it, and it "
                            "decides what you need to do first.", buttons)]

    async def _on_passport_text(self, s: Session, text: str) -> list[dict]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,39}", text.strip()):
            return [text_msg("😕 Please type just the country name, like *India* or *United States*.")]
        s.ctx["citizen"] = " ".join(text.split()).title()
        return await self._after_passport(s)

    async def _after_passport(self, s: Session) -> list[dict]:
        t = s.ctx.setdefault("gtmp", {})
        if t.get("dest"):
            return await self._after_destination(s)
        s.step = "guide_known"
        return [buttons_msg("Do you already know where you want to go?",
                            [("gkn:yes", "✅ Yes, I know"), ("gkn:no", "🎲 Suggest places"), ("nav:menu", "🏠 Menu")])]

    def _ask_budget(self, s: Session) -> list[dict]:
        s.step = "guide_budget"
        rows = [(f"gbud:{v}", label, "per person, whole trip") for v, label in BUDGETS]
        return [list_msg("💰 *What's your budget?*\nPer person, for the whole trip: flights, stay and food. A rough idea is fine.",
                         "Choose budget", rows, "Budget")]

    def _ask_vibe(self, s: Session) -> list[dict]:
        s.step = "guide_vibe"
        return [list_msg("✨ *What kind of trip do you like?*", "Choose style", [(f"gvibe:{k}", label, "") for k, label in VIBES], "Trip style")]

    def _ask_duration(self, s: Session) -> list[dict]:
        s.step = "guide_days"
        return [list_msg("🗓️ *How many days do you have?*", "Choose days", [(f"gdur:{v}", label, "") for v, label in DURATIONS], "Days")]

    def _ask_month(self, s: Session) -> list[dict]:
        s.step = "guide_month"
        today, rows = now_ist().date(), []
        for i in range(1, 7):
            m = date(today.year + (today.month - 1 + i) // 12, (today.month - 1 + i) % 12 + 1, 1)
            rows.append((f"gmon:{m:%Y-%m}", f"{m:%B %Y}", ""))
        return [list_msg("📅 *Which month do you want to travel?*", "Choose month", rows, "Month")]

    async def _suggest(self, s: Session) -> list[dict]:
        t = s.ctx["gtmp"]
        month = date.fromisoformat(t["month"] + "-01")
        ideas = await self.advisor.suggest_destinations(citizen=s.ctx.get("citizen", ""), budget_inr=t["budget"], vibe=t["vibe"],
                                                        days=t["days"], month=f"{month:%B}") if self.advisor else []
        if not ideas:  # no AI: the places people fly to most from here
            ideas = [{"place": city(c), "country": country_of(c), "why": "Popular with travellers, and we can book it for you.",
                      "cost_inr": None, "entry": ""} for c in await self._db(self.repo.popular_destinations, 3)]
        if not ideas:
            s.step = "awaiting_guide_place"
            return [text_msg("😕 I can't suggest places right now. Type a place you'd like to visit, like *Dubai* or *Goa*.")]
        s.ctx["gideas"] = ideas
        lines = [f"✨ *Ideas for you* ({month:%B}, {t['days']} days)\n"]
        for i, idea in enumerate(ideas, start=1):
            cost = f" · about {inr(idea['cost_inr'])} per person" if idea.get("cost_inr") else ""
            lines.append(f"*{i}. {idea['place']}, {idea['country']}*{cost}\n{idea['why']}" + (f"\n🛂 {idea['entry']}" if idea.get("entry") else ""))
        s.step = "guide_pick"
        buttons = [(f"gdest:{i}", f"{i}. {idea['place']}"[:20]) for i, idea in enumerate(ideas)]
        return [buttons_msg("\n\n".join(lines) + "\n\nPick one and I'll build your plan 👇", buttons)]

    # ------------------------------------------------------------------ destination
    @staticmethod
    def _dest(place: str, country: str, code: str | None) -> dict:
        vcode = visa_code_for(country) if country else None
        return {"place": place, "country": country, "code": code, "vcode": vcode,
                "currency": currency_for(vcode or country) if country else None}

    async def _resolve(self, name: str, country: str = "") -> dict:
        """A typed place -> {place, country, code, vcode, currency}. Our own airport cities are exact; anything else asks the AI."""
        low = " ".join(name.lower().split())
        code = CITY_ALIASES.get(low) or (CITY_ALIASES[m.group(1)] if (m := city_pattern().search(low)) else None)
        if code:
            return self._dest(city(code), country_of(code), code)
        if not country and self.advisor:
            found = await self.advisor.place_country(name)
            name, country = found if found else (name, "")
        return self._dest(name.strip().title()[:40], country, None)

    async def _on_place_text(self, s: Session, text: str) -> list[dict]:
        if len(text.strip()) < 2:
            return [text_msg("📍 Type the place, like *Dubai*, *Goa* or *Bali*.")]
        s.ctx.setdefault("gtmp", {})["dest"] = await self._resolve(text)
        return await self._after_destination(s)

    async def _after_destination(self, s: Session) -> list[dict]:
        """Ask for whatever we still don't know, in this order: where from, when, how long, round trip or one way."""
        t = s.ctx["gtmp"]
        if not t.get("origin"):
            return await self._ask_origin(s)
        if t.get("month") and not t.get("travel"):  # picked from ideas: they already told us the month
            t["travel"] = max(date.fromisoformat(t["month"] + "-15"), now_ist().date() + timedelta(days=14)).isoformat()
        if not t.get("travel"):
            return self._ask_when(s)
        if not t.get("days"):
            return self._ask_days(s)
        if t.get("round_trip") is None:
            s.step = "guide_trip_type"
            return [buttons_msg("Will you come back home after the trip?\n\nIf so, I'll plan the return flight and the ride home too.",
                                [("gtrip:round", "↩️ Round trip"), ("gtrip:one", "➡️ One-way")])]
        return await self._build(s)

    async def _ask_origin(self, s: Session) -> list[dict]:
        choices = await self._safe(self.repo.origin_choices, s.user["id"]) or []
        s.step = "guide_origin"
        rows = [(f"gorg:{c['code']}", c["city"], "Same as your last trip" if c["last"] else c["name"]) for c in choices]
        rows = rows[:9] + [("gorg:more", "Another city…", "Type the city name")]
        return [list_msg(f"🏠 *Which city will you start from?*\nI'll plan your trip from your door to *{s.ctx['gtmp']['dest']['place']}*, "
                         "and back home again.", "Choose city", rows, "Starting city")]

    async def _set_origin(self, s: Session, code: str) -> list[dict]:
        if code not in CITIES:
            return await self._ask_origin(s)
        s.ctx.setdefault("gtmp", {})["origin"] = {"code": code, "city": city(code), "country": country_of(code)}
        return await self._after_destination(s)

    async def _on_origin_text(self, s: Session, text: str) -> list[dict]:
        low = " ".join(text.lower().split())
        code = CITY_ALIASES.get(low) or (CITY_ALIASES[m.group(1)] if (m := city_pattern().search(low)) else None)
        if not code:
            return [text_msg(f"😕 I can't find *{text[:40]}* as a starting city yet. Try the nearest big city, like *Delhi* or *Mumbai*.")]
        return await self._set_origin(s, code)

    def _ask_when(self, s: Session) -> list[dict]:
        s.step = "guide_when"
        today = now_ist().date()
        rows = [(f"gwhen:{(today + timedelta(days=n)).isoformat()}", label, f"{today + timedelta(days=n):%a, %d %b}")
                for n, label in ((14, "In 2 weeks"), (30, "In 1 month"), (60, "In 2 months"), (90, "In 3 months"), (180, "In 6 months"))]
        rows.append(("gwhen:more", "Another date…", "Type a date"))
        place = s.ctx["gtmp"]["dest"]["place"]
        return [list_msg(f"📅 *When do you want to go to {place}?*\nA rough date is fine, you can change it later.", "Choose date", rows, "Travel date")]

    async def _on_date_text(self, s: Session, text: str) -> list[dict]:
        d = parse_date(text, now_ist().date())
        if d is None:
            return [text_msg("😕 I couldn't read that date. Try *15/12* or *20 Dec*.")]
        return await self._set_travel(s, d)

    async def _set_travel(self, s: Session, d: date) -> list[dict]:
        today = now_ist().date()
        if d <= today or d > today + timedelta(days=365):
            return [text_msg("😕 Please pick a date between tomorrow and the next 12 months.")]
        s.ctx.setdefault("gtmp", {})["travel"] = d.isoformat()
        return await self._after_destination(s)

    def _ask_days(self, s: Session) -> list[dict]:
        s.step = "guide_days"
        rows = [(f"gdays:{v}", f"{v} days", "") for v in (3, 5, 7, 10, 14)]
        return [list_msg("🗓️ *How many days will you stay there?*", "Choose days", rows, "Days")]

    # -------------------------------------------------------------------- the roadmap
    async def _build(self, s: Session) -> list[dict]:
        t, citizen = s.ctx["gtmp"], s.ctx.get("citizen", "")
        dest, origin, travel = t["dest"], t["origin"], date.fromisoformat(t["travel"])
        intl = not same_country(origin["country"], dest["country"]) if dest["country"] and origin["country"] else True
        status, summary, processing, has_rule = None, "", None, False
        if intl:
            rule = await self._safe(self.repo.visa_rule, dest["vcode"]) if dest["vcode"] and "india" in norm_country(citizen) else None
            if rule:  # our own rules table first: it is what the visa desk actually applies
                status, processing = VISA_TYPE_STATUS.get(rule["visa_type"]), rule.get("processing_days") or None
                has_rule = rule["visa_type"] in ("e-visa", "sticker")
                summary = rule.get("notes", "")
            elif self.advisor and dest["country"]:
                advice = await self.advisor.visa_check(citizen, dest["country"])
                if advice:
                    status, summary = advice.status, advice.summary
        notes = await self.advisor.trip_notes(place=dest["place"], country=dest["country"], month=f"{travel:%B}", days=t["days"]) \
            if self.advisor and dest["country"] else None
        cabs = sorted(await self._safe(self.repo.cab_cities) or [])
        round_trip = t.get("round_trip", True)
        s.ctx["guide"] = {"dest": dest, "origin": origin, "travel": t["travel"], "days": t["days"], "round_trip": round_trip,
                          "back": (travel + timedelta(days=t["days"])).isoformat() if round_trip else None, "intl": intl,
                          "visa_status": status, "visa_summary": summary, "visa_days": processing, "has_visa_rule": has_rule,
                          "notes": notes, "cab_cities": cabs, "done": []}
        s.ctx.pop("gtmp", None)
        return await self._show_roadmap(s)

    async def _steps(self, s: Session) -> tuple[dict, list[R.Step]]:
        g = s.ctx["guide"]
        travel = date.fromisoformat(g["travel"])
        steps = R.build_steps(travel=travel, days=g["days"], intl=g["intl"], round_trip=g["round_trip"], visa_status=g["visa_status"],
                              visa_processing_days=g.get("visa_days"))
        progress = await self._safe(self.repo.progress, s.user["id"], dest_code=g["dest"]["code"], origin_code=g["origin"]["code"],
                                    travel=travel, back=date.fromisoformat(g["back"]) if g["back"] else None,
                                    currency=g["dest"]["currency"], visa_code=g["dest"]["vcode"]) or {}
        return g, R.apply_progress(steps, set(g["done"]), progress, g["round_trip"])

    @staticmethod
    def _actions(g: dict, step_id: str) -> list[dict]:
        """The things a step can start: {label, desc, agent, slots}. Each opens a service with the trip already filled in."""
        dest, origin, travel, back = g["dest"], g["origin"], g["travel"], g["back"]
        bookable, cabs = dest["code"] in CITIES, set(g.get("cab_cities") or [])
        out: list[dict] = []
        if step_id == "visa" and g.get("has_visa_rule") and dest["vcode"]:
            out.append({"label": "🛂 Start my visa", "desc": f"Apply for {dest['country']}", "agent": "visa", "slots": {"country": dest["vcode"]}})
        elif step_id == "flights" and bookable:
            out.append({"label": "✈️ Flight there", "desc": f"{origin['city']} to {dest['place']} · {date.fromisoformat(travel):%d %b}",
                        "agent": "flight", "slots": {"from": origin["code"], "to": dest["code"], "date": travel}})
            if back:
                out.append({"label": "↩️ Flight back", "desc": f"{dest['place']} to {origin['city']} · {date.fromisoformat(back):%d %b}",
                            "agent": "flight", "slots": {"from": dest["code"], "to": origin["code"], "date": back}})
        elif step_id == "hotel" and bookable:
            out.append({"label": f"🏨 Stay in {dest['place']}"[:24], "desc": f"Check-in {date.fromisoformat(travel):%d %b}", "agent": "hotel",
                        "slots": {"to": dest["code"], "date": travel}})
        elif step_id == "forex" and dest["vcode"]:
            out.append({"label": "💱 Get currency", "desc": f"For {dest['country']}", "agent": "forex", "slots": {"country": dest["vcode"]}})
        elif step_id == "plan" and bookable:
            out.append({"label": "🗺️ Plan activities", "desc": f"Things to do in {dest['place']}", "agent": "planner", "slots": {"to": dest["code"]}})
        elif step_id == "rides":
            if origin["code"] in cabs:
                out.append({"label": "🚕 Home to airport", "desc": f"In {origin['city']} · {date.fromisoformat(travel):%d %b}", "agent": "cab",
                            "slots": {"to": origin["code"], "date": travel}})
            if dest["code"] in cabs:
                out.append({"label": "🚕 Airport to stay", "desc": f"In {dest['place']} · {date.fromisoformat(travel):%d %b}", "agent": "cab",
                            "slots": {"to": dest["code"], "date": travel}})
                if back:
                    out.append({"label": "🚕 Stay to airport", "desc": f"In {dest['place']} · {date.fromisoformat(back):%d %b}", "agent": "cab",
                                "slots": {"to": dest["code"], "date": back}})
            if back and origin["code"] in cabs:
                out.append({"label": "🚕 Airport to home", "desc": f"In {origin['city']} · {date.fromisoformat(back):%d %b}", "agent": "cab",
                            "slots": {"to": origin["code"], "date": back}})
        elif step_id == "return" and origin["code"] in cabs and back:
            out.append({"label": "🚕 Airport to home", "desc": f"In {origin['city']} · {date.fromisoformat(back):%d %b}", "agent": "cab",
                        "slots": {"to": origin["code"], "date": back}})
        return out

    async def _show_roadmap(self, s: Session) -> list[dict]:
        g = s.ctx.get("guide")
        if not g:
            return await self.on_enter(s)
        today, travel = now_ist().date(), date.fromisoformat(g["travel"])
        if travel < today:
            s.ctx.pop("guide", None)
            return [buttons_msg("🎒 That trip date has passed. Ready to plan a new one?", [("guide:new", "🚀 Plan a trip"), ("nav:menu", "🏠 Menu")])]
        g, steps = await self._steps(s)
        dest, origin = g["dest"], g["origin"]
        done = sum(1 for x in steps if x.state == "done")
        nxt = R.next_step(steps)
        trip = f"🛫 {origin['city']} to {dest['place']} · {travel:%d %b}" + (f", back {date.fromisoformat(g['back']):%d %b}" if g["back"] else ", one way")
        lines = [f"🧳 *{dest['place']}" + (f", {dest['country']}" if dest["country"] else "") + f"* · {g['days']} days", trip]
        if g["intl"]:
            lines.append(f"🛂 {s.ctx.get('citizen', 'Your')} passport" + {"not_required": " · no visa needed ✅", "on_arrival": " · visa on arrival 🛬",
                         "e_visa": " · e-visa needed 💻", "required": " · visa needed 🛂"}.get(g["visa_status"], " · check the visa rules"))
        lines.append(f"✅ {done} of {len(steps)} steps done")
        if nxt:
            lines.append(f"\n👉 *Next:* {nxt.title} ({R.when_text(nxt.due, today, nxt.state).lower()})")
        else:
            lines.append("\n🎉 Everything is done. Have a wonderful trip, and a safe journey home!")
        if warn := R.timeline_warning(steps, today, travel):
            lines.append(f"\n{warn}")
        lines.append("\nTap a step to see what to do and how 👇")
        s.step = "guide_roadmap"
        rows = [(f"gstep:{x.id}", f"{R.STATE_ICON[x.state]} {x.title}"[:24], (R.when_text(x.due, today, x.state) + (f" · {x.note}" if x.note else ""))[:72])
                for x in steps]
        rows.append(("guide:notes", "🌍 Know before you go", "Weather, packing, SIM, safety"))
        return [list_msg("\n".join(lines), "See steps", rows[:10], "Your journey")]

    def _step_text(self, g: dict, step: R.Step, today: date) -> str:
        dest, origin, notes = g["dest"], g["origin"], g.get("notes") or {}
        lines = [f"{R.STATE_ICON[step.state]} *{step.title}*", f"📅 {R.when_text(step.due, today, step.state)}", "", step.why]
        if step.note:
            lines.append(f"\nℹ️ {step.note}")
        extra = {"visa": g.get("visa_summary") or "", "visa_arrival": g.get("visa_summary") or "",
                 "forex": notes.get("sim_money", ""), "insurance": notes.get("health", ""),
                 "pack": " ".join(x for x in (notes.get("weather", ""), ("Pack: " + ", ".join(notes["packing"]) + ".") if notes.get("packing") else "",
                                              notes.get("plugs", "")) if x)}.get(step.id, "")
        if extra:
            lines.append(f"\n💡 {extra}")
        if step.id == "rides":
            legs = [f"🏠➡️🛫 {origin['city']}: home to the airport, on {date.fromisoformat(g['travel']):%d %b}",
                    f"🛬➡️🏨 {dest['place']}: airport to your stay, on {date.fromisoformat(g['travel']):%d %b}"]
            if g["back"]:
                legs += [f"🏨➡️🛫 {dest['place']}: stay to the airport, on {date.fromisoformat(g['back']):%d %b}",
                         f"🛬➡️🏠 {origin['city']}: airport to home, on {date.fromisoformat(g['back']):%d %b}"]
            lines.append("\n" + "\n".join(legs))
            if dest["code"] not in set(g.get("cab_cities") or []):
                lines.append(f"\nℹ️ I can't book cabs in {dest['place']} yet: use the airport's official taxi counter or a local ride app there.")
        elif step.id in ("flights", "hotel", "plan") and dest["code"] not in CITIES:
            lines.append(f"\n😕 I can't book that for {dest['place']} yet. Please book it with a partner you trust.")
        return "\n".join(lines)

    async def _show_step(self, s: Session, step_id: str) -> list[dict]:
        if not s.ctx.get("guide"):
            return await self.on_enter(s)
        g, steps = await self._steps(s)
        step = next((x for x in steps if x.id == step_id), None)
        if not step:
            return await self._show_roadmap(s)
        text = self._step_text(g, step, now_ist().date())
        actions = self._actions(g, step.id) if step.state != "done" else []
        extras = ([(f"gdone:{step.id}", "✅ Mark done")] if step.state != "done" else []) + [("guide:road", "⬅️ Roadmap")]
        if len(actions) + len(extras) <= 3:
            return [buttons_msg(text, [(f"gact:{step.id}:{i}", a["label"]) for i, a in enumerate(actions)] + extras)]
        rows = [(f"gact:{step.id}:{i}", a["label"], a["desc"]) for i, a in enumerate(actions)] + \
               [(extras[0][0], "✅ Mark done", "I've done this one")] * (step.state != "done") + [("guide:road", "⬅️ Roadmap", "Back to your plan")]
        return [list_msg(text, "Do this now", rows, "Options")]

    async def _do_action(self, s: Session, val: str) -> list[dict]:
        """gact:<step>:<n>: open the service for that step, with the trip already filled in."""
        step_id, _, n = val.rpartition(":")
        g = s.ctx.get("guide")
        actions = self._actions(g, step_id) if g else []
        if not g or not n.isdigit() or int(n) >= len(actions):
            return await self._show_roadmap(s)
        a = actions[int(n)]
        s.ctx["follow_up"] = {"agent": a["agent"], "slots": a["slots"]}  # the Concierge starts that agent right after this reply
        return []

    async def _show_notes(self, s: Session) -> list[dict]:
        g = s.ctx.get("guide")
        if not g:
            return await self.on_enter(s)
        n, dest = g.get("notes") or {}, g["dest"]
        parts = []
        for icon, key in (("🌤️", "weather"), ("🔌", "plugs"), ("📶", "sim_money"), ("🛡️", "safety"), ("💉", "health")):
            if n.get(key):
                parts.append(f"{icon} {n[key]}")
        if n.get("packing"):
            parts.append("🎒 Pack: " + ", ".join(n["packing"]))
        if n.get("culture"):
            parts.append("🙏 Good to know:\n" + "\n".join(f"• {x}" for x in n["culture"]))
        if n.get("must_do"):
            parts.append("⭐ Must do:\n" + "\n".join(f"• {x}" for x in n["must_do"]))
        body = (f"🌍 *{dest['place']}: know before you go*\n\n" + "\n\n".join(parts)) if parts else \
            f"🌍 I don't have notes for *{dest['place']}* yet. Ask Buddy about the weather, what to pack, or local tips!"
        buttons = [("svc:planner", "🗺️ Plan activities")] if dest["code"] in CITIES else [("svc:buddy", "💬 Ask Buddy")]
        return [buttons_msg(body + "\n\n_Rules change, so confirm important details with official sources._",
                            buttons + [("guide:road", "⬅️ Roadmap"), ("guide:new", "🔁 New trip")])]
