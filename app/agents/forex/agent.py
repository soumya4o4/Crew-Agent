from app.agents.coming_soon import ComingSoonAgent


class ForexAgent(ComingSoonAgent):
    name = "forex"
    title = "Forex"
    emoji = "💱"
    owns = frozenset({"forex"})
    blurb = "Soon you'll be able to get currency and travel cards right here."

    def trip_hint(self, trip: dict) -> str:
        if trip["to"] == "DXB":
            return "Flying to *Dubai*? AED exchange will be one tap away."
        return ""
