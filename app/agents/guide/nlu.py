"""Typed answers for the Trip Guide's questions: "50000, 5 days, December, beach" fills budget, days, month and the kind of trip
in one message. Offline (regex), no model needed."""
import re
from datetime import date

from app.agents.forex.currencies import parse_amount
from app.core.utils import MONTHS

VIBE_WORDS = {
    "beach": r"\b(beach(?:es)?|sea|island|relax\w*|goa)\b",
    "mountains": r"\b(mountains?|hills?|nature|trek\w*|snow|himalay\w*)\b",
    "city": r"\b(city|cities|culture|heritage|history|museum\w*|sightseeing)\b",
    "adventure": r"\b(adventure|thrill\w*|scuba|diving|safari|paragliding|rafting)\b",
    "food": r"\b(food|foodie|shopping|eat\w*)\b",
    "any": r"\b(any|anything|surprise|whatever|don'?t care|no preference|kuch bhi|koi bhi)\b",
}
YES = re.compile(r"^(yes|y|yeah|yep|haan|ha|sure|i do|i know|know)\b", re.I)
NO = re.compile(r"^(no|n|nope|nahi|nahin|not sure|dont know|don'?t know|suggest|you suggest|suggest places|any idea)\b", re.I)
_MONTH = "|".join(MONTHS)
MONTH_WORD = re.compile(rf"\b({_MONTH})[a-z]*\b")
DAYS = re.compile(r"\b(\d{1,2})\s*(?:days?|din|nights?|raat)\b")
WEEKS = re.compile(r"\b(a|one|1|two|2)\s*weeks?\b")
ROUND = re.compile(r"\b(round|return|both|back|come back|aana jaana|two way)\b")
ONE_WAY = re.compile(r"\b(one ?way|single|no return|not coming back)\b")


def parse_answers(text: str, today: date, expecting: str | None = None) -> dict:
    """What a typed message says: {budget, vibe, days, month ("YYYY-MM"), round_trip}. `expecting` ("budget", "days") lets a
    bare number answer the question that was just asked."""
    t = " ".join(text.lower().replace(",", " ").split())
    found: dict = {}
    without_days = DAYS.sub(" ", t)
    if (amt := parse_amount(without_days)) and amt[1] in (None, "INR") and amt[0] >= 1000:
        found["budget"] = int(amt[0])
    for key, pat in VIBE_WORDS.items():
        if re.search(pat, t):
            found["vibe"] = key
            break
    if m := DAYS.search(t):
        found["days"] = int(m[1])
    elif m := WEEKS.search(t):
        found["days"] = 14 if m[1] in ("two", "2") else 7
    elif expecting == "days" and (m := re.fullmatch(r"\D*(\d{1,2})\D*", t)):
        found["days"] = int(m[1])
    if m := MONTH_WORD.search(t):
        month = MONTHS[m[1][:3]]
        found["month"] = f"{today.year + (1 if month <= today.month else 0)}-{month:02d}"
    elif "next month" in t:
        found["month"] = f"{today.year + (today.month // 12)}-{today.month % 12 + 1:02d}"
    if ONE_WAY.search(t):
        found["round_trip"] = False
    elif ROUND.search(t):
        found["round_trip"] = True
    return found
