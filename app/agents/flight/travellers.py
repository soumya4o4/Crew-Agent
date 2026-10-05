"""Read travellers out of a message: "Rahul Verma, 14/03/1992, M" or "Rahul Verma 14 Mar 1992 male; Priya Verma 2/7/1994 F".
The airline needs each person's name, date of birth and gender to issue a ticket, and people send them in whatever order and
punctuation they like, so this takes the words apart instead of expecting a form. Offline, no model needed."""
import re
from datetime import date

from app.core.utils import MONTHS

NAME_PART = re.compile(r"[A-Za-z][A-Za-z .'\-]{1,59}")
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
DOB_NUMERIC = re.compile(r"\b(\d{1,2})[/\-. ](\d{1,2})[/\-. ](\d{4})\b")
DOB_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DOB_WORDS = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})[a-z]*\.?,?\s+(\d{{4}})\b", re.I)
GENDER_WORDS = {"m": "m", "male": "m", "man": "m", "f": "f", "female": "f", "woman": "f"}
TITLES = {"mr": "m", "mrs": "f", "ms": "f", "miss": "f"}
SPLIT = re.compile(r"\s*(?:,|&|\band\b|\+)\s*")
BOOK_PREFIX = re.compile(r"^\s*(?:please\s+)?(?:book(?:\s+it)?|tickets?)?\s*(?:for|ke liye)?\s+", re.I)
SELF_WORDS = ("me", "myself", "mere liye", "for me")


def _valid(day: int, month: int, year: int, today: date) -> date | None:
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    return d if date(1900, 1, 1) <= d <= today else None


def find_dob(text: str, today: date) -> tuple[date | None, str]:
    """(date of birth, the text without it). Day first, as people in India write it: 03/04/1990 is 3 April."""
    for pat, order in ((DOB_ISO, "ymd"), (DOB_NUMERIC, "dmy"), (DOB_WORDS, "dMy")):
        for m in pat.finditer(text):
            g = m.groups()
            if order == "ymd":
                d = _valid(int(g[2]), int(g[1]), int(g[0]), today)
            elif order == "dmy":
                d = _valid(int(g[0]), int(g[1]), int(g[2]), today)
            else:
                d = _valid(int(g[0]), MONTHS[g[1].lower()[:3]], int(g[2]), today)
            if d:
                return d, (text[:m.start()] + " " + text[m.end():])
    return None, text


def _gender_and_name(words: list[str]) -> tuple[str | None, list[str]]:
    """Pull "M", "female" off the end of a name, or "Mr"/"Mrs" off the front."""
    gender = None
    if words and words[-1].lower().strip(".") in GENDER_WORDS:
        gender, words = GENDER_WORDS[words[-1].lower().strip(".")], words[:-1]
    if words and words[0].lower().strip(".") in TITLES:
        gender, words = gender or TITLES[words[0].lower().strip(".")], words[1:]
    return gender, words


def parse_travellers(text: str, today: date, own_name: str = "") -> list[dict] | None:
    """[{name, dob, gender}] for everyone in the message; each key is only there if the message gave it, and a person who is
    only described (a date and a gender with no name) has no name. None if the text does not look like travellers."""
    if re.match(r"(?i)\s*(?:please\s+)?(?:book|tickets?)\b", text):
        text = BOOK_PREFIX.sub("", text, count=1)
    people: list[dict] = []
    for chunk in re.split(r"[;\n]+", text):
        for token in SPLIT.split(chunk.strip()):
            if not token.strip():
                continue
            dob, rest = find_dob(token, today)
            gender, words = _gender_and_name(rest.split())
            name = " ".join(words)
            if name.lower() in SELF_WORDS:
                if not own_name:
                    return None
                name = own_name
            elif name and not NAME_PART.fullmatch(name):
                return None
            if name or not people:
                if not (name or dob or gender):
                    return None
                people.append({"name": " ".join(name.split()).title()} if name else {})
            if dob:
                people[-1]["dob"] = dob.isoformat()
            if gender:
                people[-1]["gender"] = gender
    return people or None
