"""Currency reference data (ISO 4217 codes and the words people use for them) and amount parsing. This is language and
standards data, not business logic: rates come live from the rate service, and any code the rate service knows works even if
it is not listed here."""
import re

CURRENCIES = {  # code -> (name, flag)
    "USD": ("US Dollar", "🇺🇸"), "EUR": ("Euro", "🇪🇺"), "GBP": ("British Pound", "🇬🇧"), "AED": ("UAE Dirham", "🇦🇪"),
    "SGD": ("Singapore Dollar", "🇸🇬"), "THB": ("Thai Baht", "🇹🇭"), "JPY": ("Japanese Yen", "🇯🇵"),
    "AUD": ("Australian Dollar", "🇦🇺"), "CAD": ("Canadian Dollar", "🇨🇦"), "CHF": ("Swiss Franc", "🇨🇭"),
    "MYR": ("Malaysian Ringgit", "🇲🇾"), "IDR": ("Indonesian Rupiah", "🇮🇩"), "VND": ("Vietnamese Dong", "🇻🇳"),
    "LKR": ("Sri Lankan Rupee", "🇱🇰"), "NPR": ("Nepali Rupee", "🇳🇵"), "MVR": ("Maldivian Rufiyaa", "🇲🇻"),
    "MUR": ("Mauritian Rupee", "🇲🇺"), "NZD": ("New Zealand Dollar", "🇳🇿"), "HKD": ("Hong Kong Dollar", "🇭🇰"),
    "CNY": ("Chinese Yuan", "🇨🇳"), "KRW": ("South Korean Won", "🇰🇷"), "SAR": ("Saudi Riyal", "🇸🇦"),
    "QAR": ("Qatari Riyal", "🇶🇦"), "TRY": ("Turkish Lira", "🇹🇷"), "ZAR": ("South African Rand", "🇿🇦"),
}
POPULAR = ["USD", "EUR", "GBP", "AED", "SGD", "THB", "JPY", "AUD"]  # shown first when we know nothing about the trip

# What people type -> code. (Words that are also common English, like "try" or "won", are left out on purpose.)
ALIASES = {
    "dollar": "USD", "dollars": "USD", "us dollar": "USD", "usd": "USD", "bucks": "USD",
    "euro": "EUR", "euros": "EUR", "eur": "EUR", "pound": "GBP", "pounds": "GBP", "sterling": "GBP", "gbp": "GBP",
    "dirham": "AED", "dirhams": "AED", "aed": "AED", "sgd": "SGD", "singapore dollar": "SGD", "singapore dollars": "SGD",
    "baht": "THB", "thb": "THB", "yen": "JPY", "jpy": "JPY", "aud": "AUD", "australian dollar": "AUD", "australian dollars": "AUD",
    "cad": "CAD", "canadian dollar": "CAD", "canadian dollars": "CAD", "chf": "CHF", "franc": "CHF", "francs": "CHF",
    "myr": "MYR", "ringgit": "MYR", "idr": "IDR", "rupiah": "IDR", "vnd": "VND", "lkr": "LKR", "npr": "NPR", "mvr": "MVR",
    "rufiyaa": "MVR", "mur": "MUR", "nzd": "NZD", "hkd": "HKD", "cny": "CNY", "yuan": "CNY", "renminbi": "CNY", "krw": "KRW",
    "sar": "SAR", "riyal": "SAR", "qar": "QAR", "lira": "TRY", "zar": "ZAR",
}
_ALIAS_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, ALIASES), key=len, reverse=True)) + r")\b")

# The visa_rules country code -> its currency, and country names (as in the airports table) -> currency.
VISA_COUNTRY_CURRENCY = {"AE": "AED", "SG": "SGD", "TH": "THB", "LK": "LKR", "GB": "GBP", "US": "USD", "SCHENGEN": "EUR",
                         "JP": "JPY", "VN": "VND", "ID": "IDR", "MV": "MVR", "NP": "NPR", "MU": "MUR", "MY": "MYR"}
COUNTRY_CURRENCY = {
    "united states": "USD", "usa": "USD", "united arab emirates": "AED", "uae": "AED", "united kingdom": "GBP", "uk": "GBP",
    "france": "EUR", "germany": "EUR", "italy": "EUR", "spain": "EUR", "netherlands": "EUR", "portugal": "EUR",
    "greece": "EUR", "austria": "EUR", "belgium": "EUR", "ireland": "EUR", "finland": "EUR", "switzerland": "CHF",
    "japan": "JPY", "singapore": "SGD", "thailand": "THB", "australia": "AUD", "canada": "CAD", "new zealand": "NZD",
    "china": "CNY", "hong kong": "HKD", "south korea": "KRW", "saudi arabia": "SAR", "qatar": "QAR", "turkey": "TRY",
    "south africa": "ZAR", "malaysia": "MYR", "indonesia": "IDR", "vietnam": "VND", "sri lanka": "LKR", "nepal": "NPR",
    "maldives": "MVR", "mauritius": "MUR",
}


def currency_for(country: str) -> str | None:
    """"United Arab Emirates" (or a visa code like "AE") -> "AED". None when we don't know."""
    low = (country or "").strip().lower()
    return COUNTRY_CURRENCY.get(low) or VISA_COUNTRY_CURRENCY.get((country or "").strip().upper())


def find_currency(text: str) -> str | None:
    m = _ALIAS_RE.search(text.lower())
    return ALIASES[m.group(1)] if m else None


def name_of(code: str) -> str:
    return CURRENCIES.get(code, (code, ""))[0]


def flag_of(code: str) -> str:
    return CURRENCIES.get(code, ("", "💱"))[1]


_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lac|lakhs|crore)?\b", re.I)
_INR_WORDS = re.compile(r"(₹|\brs\.?|\binr\b|\brupees?\b|\brupaye\b|\brupay\w*)", re.I)
_MULTIPLIER = {"k": 1_000, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "crore": 10_000_000}


def parse_amount(text: str) -> tuple[float, str | None] | None:
    """"500 usd" -> (500.0, "USD"); "₹40k" -> (40000.0, "INR"); "1.5 lakh" -> (150000.0, None); "abc" -> None.
    The unit is None when the person gave only a number."""
    m = _NUMBER.search(text.replace("₹", " ₹ "))
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", "")) * _MULTIPLIER.get((m.group(2) or "").lower(), 1)
    except ValueError:
        return None
    if value <= 0:
        return None
    # "100 usd in inr" asks for rupees as the answer; the rupee is the unit of the amount only when it sits next to the number
    asked = re.sub(r"\b(?:in|to|into|se|mein|me)\s+(?:inr|rupees?|rs\.?|rupaye|₹)", " ", text, flags=re.I)
    unit = "INR" if _INR_WORDS.search(asked) else find_currency(text)
    return value, unit
