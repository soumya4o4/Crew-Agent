"""Turn a free-text message into an Intent: what the user wants, the details they already gave,
and what to offer when the request is incomplete or has several parts.

KeywordClassifier works offline and costs nothing. LLMClassifier (Claude) and OpenAIClassifier
understand messy, multi-part, Hinglish requests; the router falls back to keywords if the LLM fails.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date

from app.agents.concierge.slots import extract_slots
from app.core.places import CITIES, CITY_ALIASES, COUNTRY_ALIASES

SERVICES = ("flight", "hotel", "cab", "nearby", "planner", "events", "visa", "forex")
INTENTS = SERVICES + ("bookings", "help", "buddy", "smalltalk", "unknown")
THANKS = {"thanks", "thank you", "thx", "ty", "shukriya", "dhanyavad"}
BYE = {"bye", "goodbye", "see you", "tata", "ok bye"}

# Order matters: when several match, this is the natural order of a trip.
PATTERNS = {
    "flight": r"\b(flights?|fly|flying|airlines?|plane|air ?tickets?|udaan|jahaz)\b",
    "visa": r"\b(visas?|passport|immigration)\b",
    "forex": r"\b(forex|currency|exchange|dollars?|usd|aed|dirhams?|euros?|eur|gbp|travel card)\b",
    "hotel": r"\b(hotels?|stay|room|resort|hostel|homestay|accommodation|lodging|kamra|rukna)\b",
    "cab": r"\b(cabs?|taxi|uber|ola|ride|pick ?up|airport transfer|chauffeur|gaadi|gadi)\b",
    "planner": r"\b(reels?|itinerary|plan(?:ning)? (?:a |my |the )?(?:trip|holiday|vacation)|trip plan|holiday|vacation|getaway|things to do)\b",
    "events": r"\b(events?|concerts?|festivals?|matches|gigs?|shows?)\b",
    "nearby": r"\b(near ?by|near me|around me|aas ?paas|paas mein|nearest|closest|close by)\b",
    "bookings": r"\b(my bookings?|pnr|(?:show|view|see|check) my trips?|booking status|cancel (?:my )?(?:booking|ticket|flight))\b",
}


@dataclass
class Intent:
    name: str                                         # main thing the user wants
    slots: dict = field(default_factory=dict)         # from / to / date they already gave
    reply: str = ""                                   # friendly text for small-talk
    also: list[str] = field(default_factory=list)     # other services they asked for, in trip order
    options: list[str] = field(default_factory=list)  # what to offer when the request is unclear
    question: str = ""                                # clarifying question to ask with those options


class KeywordClassifier:
    def classify(self, text: str, today: date) -> Intent:
        t = text.lower().strip()
        if t in ("help", "?", "what can you do", "what can you do?"):
            return Intent("help")
        if t in THANKS:
            return Intent("smalltalk", reply="Anytime! 😊 Happy to help.")
        if t in BYE:
            return Intent("smalltalk", reply="Safe travels! ✈️ Just say *hi* whenever you need me.")

        slots = extract_slots(t, today)
        hits = [name for name, pat in PATTERNS.items() if re.search(pat, t)]
        if len(hits) > 1 and "nearby" in hits:  # "hotel near me" is a hotel request; "near me" only matters on its own
            hits.remove("nearby")
        if "bookings" in hits:
            return Intent("bookings")
        if hits:
            return Intent(hits[0], slots, also=hits[1:])
        if slots.get("from") and slots.get("to"):  # "Indore to Goa tomorrow" with no keyword
            return Intent("flight", slots)
        return Intent("unknown", slots)


class LLMClassifier:
    """An LLM picks the intent and extracts details via a forced tool call. This one talks to Claude;
    OpenAIClassifier below swaps only the provider call."""
    _service_list = {"type": "array", "items": {"type": "string", "enum": list(SERVICES)}}
    TOOL = {
        "name": "route_message",
        "description": "Decide what the user wants, which services are involved, and what to offer if unclear.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": list(INTENTS), "description": "The main thing they want now"},
                "also": {**_service_list, "description": "Other services they asked for, in the natural order of a trip"},
                "from_city": {"type": "string", "description": "Departure city, only if stated or clearly implied"},
                "to_city": {"type": "string", "description": "Destination city, only if stated or clearly implied"},
                "date": {"type": "string", "description": "Travel date as YYYY-MM-DD, only if stated"},
                "place": {"type": "string", "description": "For nearby: what they want to find near them, short (e.g. 'coffee', 'atm', 'biryani', 'pharmacy')"},
                "country": {"type": "string", "description": "Destination COUNTRY for visa or forex questions (e.g. 'Thailand'), if stated"},
                "time": {"type": "string", "description": "Time of day as 24h HH:MM, only if stated ('shaam 6 baje' = 18:00)"},
                "options": {**_service_list, "description": "If the request is vague, up to 3 services to offer, most useful first"},
                "question": {"type": "string", "description": "If vague: one short clarifying question to ask with those options"},
                "reply": {"type": "string", "description": "Only for smalltalk: one short friendly sentence"},
            },
            "required": ["intent"],
        },
    }

    def __init__(self, client, model: str):
        self.client, self.model = client, model

    @classmethod
    def create(cls, api_key: str, model: str) -> "LLMClassifier":
        from anthropic import AsyncAnthropic  # imported lazily so the app runs without the key/package
        return cls(AsyncAnthropic(api_key=api_key), model)

    def _system(self, today: date) -> str:
        return (
            "You are the routing brain of a worldwide WhatsApp travel concierge. Understand what the user really "
            "wants and call route_message. Users write English, Hindi or Hinglish, often short or messy.\n"
            "Services: flight (search/book flights), hotel (stays), cab (rides, airport pickup/drop), nearby (find "
            "places near the user: cafés, food, ATMs, pharmacies, petrol, parks, malls, sights; put what they want in "
            "`place`), planner (itineraries, multi-day trip planning), events (concerts, shows, festivals, things to do "
            "near them), visa, forex (currency, travel cards). Flight, hotel, cab and visa are live; still route to the others. Other intents: bookings "
            "(view/cancel an existing booking or PNR), help (what can you do), buddy (personal talk: feelings, worries, "
            "problems, advice, general questions, anything that is not a travel request), smalltalk (greeting, thanks, "
            "chit-chat), unknown.\n"
            "Rules:\n"
            "- A problem or question about a trip that is ALREADY booked (running late, traffic, lost, directions, delays, "
            "what to carry, bored between flights) is `buddy`, not `flight`. A message with 'near me' / 'nearby' for a "
            "place type is `nearby`; for shows or things to do it is `events`.\n"
            "- Several things in one message: put the most useful to do first in `intent` and the rest in `also`, in "
            "trip order (flight, visa, forex, hotel, cab, events).\n"
            "- Vague message (e.g. 'I want to go to Goa', 'planning a Dubai trip'): intent unknown, `options` = the "
            "2-3 most useful services for them, `question` = one short friendly question. Keep any city/date you got.\n"
            "- Route ONLY the latest message. Earlier messages are context for words like 'there', 'same dates', "
            "'also'; never re-queue things already handled or asked earlier.\n"
            "- Use the trip on record for 'there' / 'same dates'.\n"
            f"- Cities we can book: {', '.join(sorted(CITIES.values()))}. If the user names any other city, still put it in "
            "to_city / from_city exactly as they wrote it.\n"
            f"- Today is {today.isoformat()} ({today:%A}). Resolve 'tomorrow', 'this Saturday', 'next week' to "
            "YYYY-MM-DD. Leave details empty if not given.\n"
            "- reply/question: English, max 25 words, friendly. Never invent bookings, prices or availability."
        )

    async def _call(self, system: str, prompt: str) -> dict:
        """One forced tool call to the provider; returns the tool arguments as a dict."""
        resp = await self.client.messages.create(
            model=self.model, max_tokens=400, system=system,
            tools=[self.TOOL], tool_choice={"type": "tool", "name": "route_message"},
            messages=[{"role": "user", "content": prompt}],
        )
        return next(b.input for b in resp.content if b.type == "tool_use")

    async def classify(self, text: str, today: date, history: list[dict], active: str | None,
                       trip: dict | None = None) -> Intent:
        lines = [f"{'User' if m['role'] == 'user' else 'Bot'}: {m['content']}" for m in history]
        prompt = ("Recent conversation:\n" + "\n".join(lines) + "\n\n" if lines else "")
        if trip:
            prompt += f"Trip on record: {trip.get('from')} to {trip.get('to')} ({trip.get('city')}) on {trip.get('date')}\n"
        if active:
            prompt += f"(The user is currently in the {active} flow.)\n"
        data = await self._call(self._system(today), prompt + f"Latest message: {text}")

        slots = {}
        for key, field_name in (("from", "from_city"), ("to", "to_city")):
            name = (data.get(field_name) or "").strip()
            if code := CITY_ALIASES.get(name.lower()):
                slots[key] = code
            elif key == "to" and re.fullmatch(r"[^\W\d_][\w .'\-]{1,39}", name):
                slots["unknown_to"] = name  # a real-looking place we have no airport for: the Concierge says so honestly
        if slots.get("from") == slots.get("to"):
            slots.pop("to", None)
        try:
            slots["date"] = date.fromisoformat(data["date"]).isoformat()
        except (KeyError, ValueError, TypeError):
            pass
        if code := COUNTRY_ALIASES.get((data.get("country") or "").strip().lower()):
            slots["country"] = code
        if isinstance(data.get("place"), str) and data["place"].strip():
            slots["place"] = data["place"].strip()[:40]
        if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", data.get("time") or ""):
            slots["time"] = data["time"]
        name = data.get("intent") if data.get("intent") in INTENTS else "unknown"
        services = lambda key: [x for x in dict.fromkeys(data.get(key) or []) if x in SERVICES and x != name]
        return Intent(name, slots, reply=(data.get("reply") or "").strip()[:300], also=services("also"),
                      options=services("options")[:3], question=(data.get("question") or "").strip()[:300])


class OpenAIClassifier(LLMClassifier):
    """Same routing prompt and tool, served by OpenAI."""

    @classmethod
    def create(cls, api_key: str, model: str) -> "OpenAIClassifier":
        from openai import AsyncOpenAI  # imported lazily so the app runs without the key/package
        return cls(AsyncOpenAI(api_key=api_key), model)

    async def _call(self, system: str, prompt: str) -> dict:
        tool = {"type": "function", "function": {
            "name": self.TOOL["name"], "description": self.TOOL["description"], "parameters": self.TOOL["input_schema"]}}
        resp = await self.client.chat.completions.create(
            model=self.model, max_tokens=400,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            tools=[tool], tool_choice={"type": "function", "function": {"name": self.TOOL["name"]}},
        )
        return json.loads(resp.choices[0].message.tool_calls[0].function.arguments)
