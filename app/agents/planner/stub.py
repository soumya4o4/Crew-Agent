from app.agents.coming_soon import ComingSoonAgent


class TripPlannerAgent(ComingSoonAgent):
    """Used when there is no OpenAI key: the real planner (agent.py) needs one to read reels and write plans."""
    name = "planner"
    title = "Trip Planner"
    emoji = "🗺️"
    owns = frozenset({"planner"})
    blurb = "Soon I'll build day-by-day itineraries and tie flights, stays and cabs into one plan."

    def trip_hint(self, trip: dict) -> str:
        return f"I'll plan your *{trip['city']}* trip around your flight on {self._on(trip)}."
