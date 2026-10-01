"""Base for services that aren't live yet: explains what's coming and records interest."""
import asyncio
from datetime import date

from app.agents.base import Agent, Session
from app.core.messages import buttons_msg


class ComingSoonAgent(Agent):
    blurb = ""
    menu_desc = "Coming soon"

    def __init__(self, repo):
        self.repo = repo

    def trip_hint(self, trip: dict) -> str:
        """Optional line that uses the trip the flight agent saved (destination, date)."""
        return ""

    @staticmethod
    def _on(trip: dict) -> str:
        return f"{date.fromisoformat(trip['date']):%a, %d %b}"

    async def on_enter(self, s: Session) -> list[dict]:
        s.ctx["agent"] = self.name
        hint = self.trip_hint(s.ctx["trip"]) if s.ctx.get("trip") else ""
        body = f"{self.emoji} *{self.title}*: coming soon!\n{self.blurb}" + (f"\n\n{hint}" if hint else "")
        return [buttons_msg(body, [(f"{self.name}:notify", "🔔 Notify me"), ("svc:flight", "✈️ Flights"),
                                   ("nav:menu", "🏠 Main menu")])]

    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        if reply_id == f"{self.name}:notify":
            await asyncio.to_thread(self.repo.add_interest, s.user["id"], self.name)
            return [buttons_msg(f"🔔 Done! I'll message you the moment this goes live.",
                                [("svc:flight", "✈️ Flights"), ("nav:menu", "🏠 Main menu")])]
        return await self.on_enter(s)
