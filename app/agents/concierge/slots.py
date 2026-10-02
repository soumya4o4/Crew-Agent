"""Pull trip details (cities, date) out of free text like "Indore to Goa this saturday"."""
import re
from datetime import date, timedelta

from app.core.geo import _ALIAS_RE, NEAR_ME
from app.core.places import CITY_ALIASES, COUNTRY_ALIASES, city_pattern, country_pattern
from app.core.utils import WEEKDAYS, parse_date
from app.agents.forex.currencies import find_currency, parse_amount

_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
NUMERIC_DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b")
MONTH_DATE = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s*{_MONTH}\b(?:\s+\d{{4}})?")
WORD_DATE = re.compile(r"\b(today|tomorrow|tmrw|kal|aaj)\b")
WEEKDAY = re.compile(r"\b(?:this |next |coming )?(" + "|".join(WEEKDAYS) + r")\b")


def extract_date(t: str, today: date) -> str | None:
    for pat in (WORD_DATE, NUMERIC_DATE, MONTH_DATE):
        m = pat.search(t)
        if m:
            d = parse_date(re.sub(r"(\d)(st|nd|rd|th)", r"\1", m.group(0)), today)
            if d:
                return d.isoformat()
    m = WEEKDAY.search(t)
    if m:
        days = (WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
        return (today + timedelta(days=days)).isoformat()
    if re.search(r"\bweekend\b", t):
        return (today + timedelta(days=(5 - today.weekday()) % 7)).isoformat()
    return None


def extract_cities(t: str) -> dict:
    tagged: dict[str, str] = {}  # "from" / "to" -> code, when a preposition tells us
    untagged: list[str] = []
    for m in city_pattern().finditer(t):
        code, before = CITY_ALIASES[m.group(1)], t[max(0, m.start() - 12):m.start()]
        if re.search(r"\bfrom\s+$", before):
            tagged.setdefault("from", code)
        elif re.search(r"\b(to|for|in|at|towards)\s+$", before):
            tagged.setdefault("to", code)
        elif code not in untagged:
            untagged.append(code)
    frm, to = tagged.get("from"), tagged.get("to")
    untagged = [c for c in untagged if c not in (frm, to)]
    if not frm and not to:
        if len(untagged) >= 2:
            frm, to = untagged[0], untagged[1]
        elif untagged:
            to = untagged[0]  # a lone city is almost always the destination
    elif not frm and untagged:
        frm = untagged[0]
    elif not to and untagged:
        to = untagged[0]
    return {k: v for k, v in (("from", frm), ("to", to)) if v and v != (frm if k == "to" else None)}


def extract_slots(text: str, today: date) -> dict:
    t = text.lower()
    slots = extract_cities(t)
    if d := extract_date(t, today):
        slots["date"] = d
    if hhmm := extract_time(t):
        slots["time"] = hhmm
    if country := find_country(t):
        slots["country"] = country
    if place := find_place(t):
        slots["place"] = place
    if NEAR_ME.search(t):
        slots["near_me"] = True
    if cur := find_currency(t):  # "100 usd", "dirhams for dubai": only when a currency is named do numbers mean money
        slots["currency"] = cur
        if (amt := parse_amount(t)) and amt[1] in (None, cur, "INR") and "date" not in slots:
            slots["amount"] = amt[0]
            if amt[1] == "INR":
                slots["amount_inr"] = True
    return slots


_TIME_HHMM = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b")
_TIME_AMPM = re.compile(r"\b(\d{1,2})\s*(am|pm)\b")
_TIME_BAJE = re.compile(r"\b(\d{1,2})\s*baje\b")
_EVENING = re.compile(r"\b(shaam|sham|raat|evening|night|dopahar|afternoon)\b")


def extract_time(t: str) -> str | None:
    """"6pm", "18:30", "shaam 6 baje" -> "HH:MM" (24h)."""
    if m := _TIME_HHMM.search(t):
        hour, minute, ap = int(m[1]), int(m[2]), m[3]
    elif m := _TIME_AMPM.search(t):
        hour, minute, ap = int(m[1]), 0, m[2]
    elif m := _TIME_BAJE.search(t):
        hour, minute, ap = int(m[1]), 0, "pm" if _EVENING.search(t) and int(m[1]) < 12 else None
    else:
        return None
    if ap == "pm" and hour < 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}" if hour < 24 and minute < 60 else None


def find_place(text: str) -> str | None:
    """"coffee near me" / "nearest atm" -> the word they used for it ("coffee", "atm"), for the Around Me agent."""
    m = _ALIAS_RE.search(text.lower())
    return m.group(1) if m else None


def find_country(text: str) -> str | None:
    """"visa for dubai" / "thailand" -> a visa_rules country code."""
    m = country_pattern().search(text.lower())
    return COUNTRY_ALIASES[m.group(1)] if m else None
