"""Place data shared by all agents (flights, hotels, cabs, visa...).

Nothing about cities is written down here: airports come from the `airports` table (`load_airports`, called at start-up),
so a new city or country in the database is understood everywhere with no code change. What stays in code is language,
not geography: other names people use for a place ("bombay"), and visa-country spellings ("europe" -> Schengen).
"""
import difflib
import re

CITIES: dict[str, str] = {}           # airport code -> city name
AIRPORT_COUNTRY: dict[str, str] = {}  # airport code -> country name
CITY_ALIASES: dict[str, str] = {}     # what people type (lowercase) -> airport code
AIRPORT_COORDS: dict[str, tuple[float, float]] = {}  # airport code -> (lat, lon), when the table has them
_version = 0                          # bumps on every load, so compiled patterns know when to rebuild

# Other names for a place people type, only used when the place is in the database.
OTHER_NAMES = {"bombay": "mumbai", "new delhi": "delhi", "bangalore": "bengaluru", "madras": "chennai",
               "calcutta": "kolkata", "poona": "pune", "cochin": "kochi", "trivandrum": "thiruvananthapuram",
               "benares": "varanasi", "baroda": "vadodara", "gurgaon": "gurugram", "saigon": "ho chi minh city",
               "peking": "beijing", "new york city": "new york"}


def load_airports(rows: list[dict]) -> None:
    """Fill the registry from `airports` rows ({code, city, name, country}); safe to call again with newer rows."""
    global _version
    for a in rows:
        code = a["code"]
        CITIES[code], AIRPORT_COUNTRY[code] = a["city"], a.get("country") or ""
        if a.get("lat") is not None and a.get("lon") is not None:
            AIRPORT_COORDS[code] = (float(a["lat"]), float(a["lon"]))
        CITY_ALIASES[a["city"].lower()] = code
        CITY_ALIASES[code.lower()] = code
    for other, real in OTHER_NAMES.items():
        if real in CITY_ALIASES:
            CITY_ALIASES.setdefault(other, CITY_ALIASES[real])
    _version += 1


def city(code: str) -> str:
    return CITIES.get(code, code)


def example_route() -> str:
    """"Indore to Goa"-style example for help text, built from whichever cities are loaded."""
    names = list(CITIES.values())
    return f"{names[0]} to {names[-1]}" if len(names) > 1 else "your city to anywhere"


def country_of(code: str) -> str:
    """The country an airport is in ("" if unknown)."""
    return AIRPORT_COUNTRY.get(code, "")


def is_international(a: str, b: str) -> bool:
    ca, cb = country_of(a), country_of(b)
    return bool(ca and cb and ca.lower() != cb.lower())


_city_re: tuple[int, re.Pattern] | None = None


def city_pattern() -> re.Pattern:
    """One regex over every known place name, longest first ("new delhi" before "delhi")."""
    global _city_re
    if _city_re is None or _city_re[0] != _version:
        names = sorted(map(re.escape, CITY_ALIASES), key=len, reverse=True)
        _city_re = (_version, re.compile(r"\b(" + "|".join(names) + r")\b" if names else r"(?!x)x"))
    return _city_re[1]


def fuzzy_city(name: str) -> str | None:
    """Airport code for a misspelt place ("ahemdabad", "bangalor"). Only for text we already know is meant as a city,
    never for free text: "indoor" would become Indore."""
    low = " ".join((name or "").lower().replace(",", " ").split())
    if len(low) < 4:
        return None
    close = difflib.get_close_matches(low, CITY_ALIASES, n=1, cutoff=0.8)
    return CITY_ALIASES[close[0]] if close else None


def find_city(text: str) -> str | None:
    """Airport code for a place named in free text: "chennai", "I am going to Chennai", "lets do bombay"."""
    low = " ".join((text or "").lower().replace(",", " ").split())
    if code := CITY_ALIASES.get(low):
        return code
    m = city_pattern().search(low)
    return CITY_ALIASES[m.group(1)] if m else None


# What people type -> visa_rules country_code. `load_visa_countries` adds every country name in the database.
COUNTRY_ALIASES = {
    "uae": "AE", "united arab emirates": "AE", "dubai": "AE", "abu dhabi": "AE", "emirates": "AE", "sharjah": "AE",
    "singapore": "SG", "thailand": "TH", "bangkok": "TH", "phuket": "TH", "pattaya": "TH",
    "sri lanka": "LK", "srilanka": "LK", "colombo": "LK",
    "uk": "GB", "united kingdom": "GB", "london": "GB", "england": "GB", "britain": "GB",
    "usa": "US", "america": "US", "united states": "US", "new york": "US", "us visa": "US",
    "schengen": "SCHENGEN", "europe": "SCHENGEN", "paris": "SCHENGEN", "france": "SCHENGEN", "germany": "SCHENGEN",
    "italy": "SCHENGEN", "spain": "SCHENGEN", "switzerland": "SCHENGEN", "netherlands": "SCHENGEN", "rome": "SCHENGEN",
    "japan": "JP", "tokyo": "JP", "vietnam": "VN", "hanoi": "VN", "bali": "ID", "indonesia": "ID",
    "maldives": "MV", "nepal": "NP", "kathmandu": "NP", "mauritius": "MU", "malaysia": "MY", "kuala lumpur": "MY",
}
COUNTRY_NAMES = {"AE": "UAE", "SG": "Singapore", "TH": "Thailand", "LK": "Sri Lanka", "GB": "United Kingdom",
                 "US": "United States", "SCHENGEN": "Schengen (Europe)", "JP": "Japan", "VN": "Vietnam",
                 "ID": "Indonesia (Bali)", "MV": "Maldives", "NP": "Nepal", "MU": "Mauritius", "MY": "Malaysia"}
_country_version = 0


def load_visa_countries(rows: list[dict]) -> None:
    """Teach the registry the countries in `visa_rules` ({country_code, country_name})."""
    global _country_version
    for r in rows:
        COUNTRY_NAMES.setdefault(r["country_code"], r["country_name"])
        COUNTRY_ALIASES.setdefault(r["country_name"].lower(), r["country_code"])
    _country_version += 1


def visa_code_for(country_name: str) -> str | None:
    """"United Arab Emirates" -> "AE": the visa_rules code for a country name, if we have one."""
    low = (country_name or "").strip().lower()
    if low in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[low]
    return next((c for c, n in COUNTRY_NAMES.items() if n.lower() == low), None)


_country_re: tuple[int, re.Pattern] | None = None


def country_pattern() -> re.Pattern:
    global _country_re
    if _country_re is None or _country_re[0] != _country_version + len(COUNTRY_ALIASES):
        names = sorted(map(re.escape, COUNTRY_ALIASES), key=len, reverse=True)
        _country_re = (_country_version + len(COUNTRY_ALIASES), re.compile(r"\b(" + "|".join(names) + r")\b"))
    return _country_re[1]
