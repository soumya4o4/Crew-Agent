"""Around Me agent: find cafés, food, ATMs, pharmacies, petrol pumps, sights, parks and malls near the user, or search for
anything by name ("biryani", "bookstore"), and get directions on Google Maps.

Button ids: near:* (a category, search, again), nplc:<n> (a result). Events have their own agent (svc:events).
State lives in ctx["near"] = {cat, text, where, results}. "where" is a typed area; otherwise the pin the user shared
(ctx["loc"], fresh for a few hours) is used. With neither, the agent asks for a pin or an area name.
"""
import logging

from app.agents.base import Agent, Session
from app.core.geo import PLACE_CATEGORIES, category_for, fmt_dist, fresh_location, location_age_min, maps_link
from app.core.messages import buttons_msg, cta_msg, list_msg, location_request_msg, text_msg

logger = logging.getLogger(__name__)
RADII_M = (2500, 6000)  # look close first, then a bit further
TEXT_STEPS = ("awaiting_near_query", "awaiting_near_where")


class NearbyAgent(Agent):
    name = "nearby"
    title = "Around Me"
    emoji = "🧭"
    menu_desc = "Cafés, food, ATMs, sights near you"
    owns = frozenset({"near", "nplc"})

    def __init__(self, geo):
        self.geo = geo  # GeoService (or a fake with the same methods)

    def reset(self, s: Session) -> None:
        s.ctx.pop("near", None)

    def expects_text(self, s: Session) -> bool:
        return s.step in TEXT_STEPS

    def expects_location(self, s: Session) -> bool:
        return s.step == "awaiting_near_where"

    # ------------------------------------------------------------------ entry points
    async def on_enter(self, s: Session) -> list[dict]:
        self.reset(s)
        s.ctx["agent"], s.step = "nearby", "near_menu"
        loc = fresh_location(s.ctx)
        where = (f"📍 Using the location you shared {location_age_min(loc)} min ago." if loc
                 else "📍 I'll ask for your location once you pick something.")
        rows = [(f"near:{k}", f"{emoji} {label}", "") for k, (emoji, label, _, _) in PLACE_CATEGORIES.items()]
        rows += [("near:search", "🔎 Search anything", "Type what you want"), ("svc:events", "🎟️ Events near me", "Shows, music, food fests")]
        return [list_msg(f"🧭 *Around Me*\n{where}\nWhat are you looking for?", "Choose", rows, "Nearby")]

    async def start(self, s: Session, slots: dict) -> list[dict]:
        """Free text like "coffee near me" / "nearest atm": the classifier hands us what they want in slots["place"]."""
        place = (slots.get("place") or "").strip()
        if not place:
            return await self.on_enter(s)
        s.ctx["agent"] = "nearby"
        cat = category_for(place)
        return await self._begin(s, cat=cat, text=None if cat else place)

    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        s.ctx["agent"] = "nearby"
        if reply_id:
            kind, _, val = reply_id.partition(":")
            if kind == "nplc":
                return await self._show_place(s, val)
            if val in PLACE_CATEGORIES:
                return await self._begin(s, cat=val)
            if val == "search":
                s.step = "awaiting_near_query"
                return [text_msg("🔎 What are you looking for? For example *biryani*, *bookstore*, *chai* or *dentist*.")]
            if val == "again":
                n = s.ctx.get("near", {})
                if (n.get("cat") or n.get("text")) and self._where(s):
                    return self._show_results(s) if n.get("results") else await self._search(s)
            return await self.on_enter(s)
        if s.step == "awaiting_near_query" and text:
            cat = category_for(text)
            return await self._begin(s, cat=cat, text=None if cat else text[:40])
        if s.step == "awaiting_near_where" and text:
            return await self._on_area_text(s, text)
        return await self.on_enter(s)

    async def on_location(self, s: Session, loc: dict) -> list[dict]:
        n = s.ctx.setdefault("near", {})
        n.pop("where", None)  # the fresh pin wins over an area typed earlier
        return await self._search(s) if (n.get("cat") or n.get("text")) else await self.on_enter(s)

    # --------------------------------------------------------------------- search
    @staticmethod
    def _where(s: Session) -> dict | None:
        if where := s.ctx.get("near", {}).get("where"):
            return where
        if loc := fresh_location(s.ctx):
            return {"lat": loc["lat"], "lon": loc["lon"], "label": "you"}
        return None

    async def _begin(self, s: Session, cat: str | None = None, text: str | None = None) -> list[dict]:
        where = s.ctx.get("near", {}).get("where")
        s.ctx["near"] = {"cat": cat, "text": text, **({"where": where} if where else {})}
        if self._where(s) is None:
            return self._ask_where(s)
        return await self._search(s)

    @staticmethod
    def _what(n: dict) -> str:
        return PLACE_CATEGORIES[n["cat"]][1] if n.get("cat") else f"“{n['text']}”"

    def _ask_where(self, s: Session) -> list[dict]:
        s.step = "awaiting_near_where"
        what = self._what(s.ctx["near"]).lower()
        return [location_request_msg(f"📍 Share your location and I'll find {what} closest to you. "
                                     "I only use it to search, and forget it after a few hours."),
                text_msg("Or type an area or city, like *Vijay Nagar Indore*.")]

    async def _on_area_text(self, s: Session, text: str) -> list[dict]:
        g = await self.geo.geocode(text)
        if not g:
            return [text_msg("😕 I couldn't find that place. Try the area with the city, like *Vijay Nagar Indore*, or share your location.")]
        s.ctx["near"]["where"] = {"lat": g["lat"], "lon": g["lon"], "label": g["name"]}
        return await self._search(s)

    async def _search(self, s: Session) -> list[dict]:
        n, where = s.ctx["near"], self._where(s)
        places, radius = [], RADII_M[0]
        for radius in RADII_M:
            places = await self.geo.places(where["lat"], where["lon"], category=n.get("cat"), text=n.get("text"), radius_m=radius)
            if places:
                break
        if not places:
            s.step = "near_menu"
            return [buttons_msg(f"😕 I couldn't find {self._what(n)} within {RADII_M[-1] // 1000} km. The map service can also be slow, "
                                "so you can try again or look for something else.",
                                [("near:again", "🔁 Try again"), ("svc:nearby", "🧭 Around Me"), ("nav:menu", "🏠 Menu")])]
        n["results"] = [{k: p[k] for k in ("name", "lat", "lon", "dist_m", "info", "hours")} for p in places[:9]]
        n["further"] = radius > RADII_M[0]
        return self._show_results(s)

    def _show_results(self, s: Session) -> list[dict]:
        n, where = s.ctx["near"], self._where(s)
        s.step = "awaiting_near_pick"
        emoji = PLACE_CATEGORIES[n["cat"]][0] if n.get("cat") else "🔎"
        rows = [(f"nplc:{i}", p["name"], " · ".join(x for x in (fmt_dist(p["dist_m"]), p["info"]) if x))
                for i, p in enumerate(n["results"])]
        rows.append(("nav:menu", "🏠 Main menu", ""))
        label = "you" if not where or where["label"] == "you" else where["label"]
        far = f"\n(Nothing within {RADII_M[0] / 1000:g} km, so I looked a bit further.)" if n.get("further") else ""
        return [list_msg(f"{emoji} *{self._what(n).capitalize()}* near {label}\nClosest first 👇{far}", "See places", rows, "Closest places")]

    async def _show_place(self, s: Session, idx: str) -> list[dict]:
        results = s.ctx.get("near", {}).get("results") or []
        if not idx.isdigit() or int(idx) >= len(results):
            return await self.on_enter(s)
        p = results[int(idx)]
        lines = [f"📍 *{p['name']}*", f"{fmt_dist(p['dist_m'])} away" + (f" · {p['info']}" if p["info"] else "")]
        if p.get("hours"):
            lines.append(f"🕐 {p['hours']}")
        return [cta_msg("\n".join(lines), "🗺️ Directions", maps_link(p["lat"], p["lon"])),
                buttons_msg("Anything else?", [("near:again", "↩️ More results"), ("nav:menu", "🏠 Menu")])]
