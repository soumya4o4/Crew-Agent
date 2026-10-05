"""Understand a stay request in free text: "3 nights in Goa from tomorrow for 2" fills city, check-in, nights and guests
at once, so the hotel agent only asks for what is still missing. Offline (regex), no model needed."""
import re
from datetime import date

from app.agents.concierge.slots import extract_date
from app.core.geo import NEAR_ME
from app.core.places import find_city

NIGHTS = re.compile(r"\b(\d{1,2})\s*[- ]?\s*(?:nights?|raat(?:e|ein)?)\b")
DAYS = re.compile(r"\b(\d{1,2})\s*[- ]?\s*(?:days?|din)\b")
GUESTS = re.compile(r"\b(\d{1,2})\s*(?:guests?|people|persons?|adults?|pax|log|members?|travell?ers?)\b|\bfor\s+(\d{1,2})\b(?!\s*(?:nights?|days?|raat|din|[/-]))")
BARE_NUMBER = re.compile(r"\D*(\d{1,2})\D*")
GUEST_WORDS = (("solo", 1), ("alone", 1), ("akela", 1), ("akele", 1), ("couple", 2), ("family", 4))


def parse_stay(text: str, today: date, expecting: str | None = None) -> dict:
    """What the message says about the stay: {city, check_in, nights, guests, near_me}, only the parts it mentions.
    `expecting` ("nights" / "guests") lets a bare "2" answer the question that was just asked."""
    t = " ".join(text.lower().replace(",", " ").split())
    found: dict = {}
    if code := find_city(t):
        found["city"] = code
    elif NEAR_ME.search(t):
        found["near_me"] = True

    # Try to extract a date range first
    range_match = re.search(r"(.+?)(?:\bto\b|\btill\b|\band\b|[-])(?:\s*check\s*out)?\s*(.+)", t)
    d1, nights_from_range = None, None
    if range_match:
        d1 = extract_date(range_match.group(1), today)
        if d1:
            d1_date = date.fromisoformat(d1)
            d2 = extract_date(range_match.group(2), d1_date)
            if not d2:
                m2 = re.search(r"\b(\d{1,2})\b", range_match.group(2))
                if m2:
                    try:
                        day2 = int(m2.group(1))
                        d2_date = d1_date.replace(day=day2)
                        if d2_date <= d1_date:
                            if d1_date.month == 12:
                                d2_date = d2_date.replace(year=d1_date.year + 1, month=1)
                            else:
                                d2_date = d2_date.replace(month=d1_date.month + 1)
                        d2 = d2_date.isoformat()
                    except ValueError:
                        pass
            if d2:
                d2_date = date.fromisoformat(d2)
                nights_from_range = (d2_date - d1_date).days
                found["check_out"] = d2

    if not d1:
        d1 = extract_date(t, today)

    if d1:
        found["check_in"] = d1
        
    if nights_from_range and nights_from_range > 0:
        found["nights"] = nights_from_range
    elif m := NIGHTS.search(t):
        found["nights"] = int(m[1])
    elif m := DAYS.search(t):  # "3 days" in a trip means 2 nights
        found["nights"] = max(1, int(m[1]) - 1)

    if m := GUESTS.search(t):
        found["guests"] = int(m[1] or m[2])
    else:
        found.update(next(({"guests": n} for word, n in GUEST_WORDS if re.search(rf"\b{word}\b", t)), {}))
    if expecting in ("nights", "guests") and expecting not in found and (m := BARE_NUMBER.fullmatch(t)):
        found[expecting] = int(m[1])
    return found
