"""Understand a whole flight request in one message: "Delhi to Paris, 12-23, round trip, 2 people" gives the route, the
outbound and return dates and the party size, so the bot only asks for what is still missing. Offline, no model needed."""
import re
from datetime import date, timedelta

from app.agents.concierge.slots import MONTH_DATE, NUMERIC_DATE, extract_cities, extract_date
from app.core.utils import MONTHS, parse_date

ROUND_TRIP = re.compile(r"\b(round ?trip|return|returning|both ways|two ways|aana ?jaana|up ?down|to and fro)\b")
ONE_WAY = re.compile(r"\bone ?way\b")
COMES_BACK = re.compile(r"\b(?:return(?:ing|s)?|back|wapas|come back|coming back|till|until|tak)\b(?:\s+on)?\s+(?!ticket|flight|trip)(.+)$")
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
DAY_RANGE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|–|to|se|till|until|tak)\s*(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*({_MONTH})[a-z]*)?\b(?!\s*[:/])")
PEOPLE = re.compile(r"\b(\d{1,2})\s*(?:people|persons?|travell?ers?|passengers?|adults?|pax|tickets?|log|members?)\b")


def _dates_in(t: str, today: date) -> list[date]:
    found = []
    for pat in (NUMERIC_DATE, MONTH_DATE):
        for m in pat.finditer(t):
            if d := parse_date(re.sub(r"(\d)(st|nd|rd|th)", r"\1", m.group(0)), today):
                found.append((m.start(), d))
    return [d for _, d in sorted(found)]


def _day_range(t: str, today: date) -> list[date]:
    """"12-23" / "12 to 23 oct": two days of one month (this month, or the next if the first day has gone by)."""
    for m in DAY_RANGE.finditer(t):
        a, b = int(m[1]), int(m[2])
        if not 1 <= a < b <= 31:
            continue
        month = MONTHS[m[3][:3]] if m[3] else (today.month if a >= today.day else today.month % 12 + 1)
        year = today.year + (1 if month < today.month else 0)
        try:
            return [date(year, month, a), date(year, month, b)]
        except ValueError:
            continue
    return []


def parse_trip(text: str, today: date) -> dict:
    """{from, to, date, return_date, round_trip, pax}: only what the message says. Dates are ISO strings."""
    t = " ".join(text.lower().replace(",", " ").split())
    found: dict = {}
    back = None
    if m := COMES_BACK.search(t):  # "... return on sunday": the part after the word is the return date
        if back := extract_date(m[1], today):
            t = t[:m.start()]
    found.update(extract_cities(t))
    dates = _dates_in(t, today) or _day_range(t, today)
    if not dates and (d := extract_date(t, today)):
        dates = [date.fromisoformat(d)]
    if dates:
        found["date"] = dates[0].isoformat()
        if len(dates) > 1 and dates[1] >= dates[0]:
            back = dates[1].isoformat()
    if back:
        found["return_date"] = back
    if (back or ROUND_TRIP.search(t)) and not ONE_WAY.search(t):
        found["round_trip"] = True
    if m := PEOPLE.search(t):
        found["pax"] = int(m[1])
    elif re.search(r"\b(solo|alone|akela|akele)\b", t):
        found["pax"] = 1
    return found
