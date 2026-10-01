"""Small helpers shared by every agent: IST time, money/duration formatting, phone, date and time parsing."""
import re
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
WEEKDAYS = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST)


def inr(n: int) -> str:
    return f"₹{n:,}"


def dur(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def day_greeting() -> str:
    h = now_ist().hour
    return "Good morning" if h < 12 else "Good afternoon" if h < 17 else "Good evening"


def normalize_phone(wa_number: str) -> str | None:
    digits = re.sub(r"\D", "", wa_number or "")
    """Any international number is fine (E.164: 8 to 15 digits); a bare 10-digit number is read as Indian."""
    if len(digits) == 10:
        return "+91" + digits
    return "+" + digits if 8 <= len(digits) <= 15 else None


def parse_date(text: str, today: date) -> date | None:
    t = text.strip().lower()
    if t in ("today", "aaj", "aj"):
        return today
    if t in ("tomorrow", "tmrw", "tom", "kal"):
        return today + timedelta(days=1)
    try:
        m = re.fullmatch(r"(\d{1,2})[/\-. ](\d{1,2})(?:[/\-. ](\d{2,4}))?", t)
        if m:
            d, mo, y = int(m[1]), int(m[2]), m[3]
            year = (int(y) + 2000 if y and len(y) == 2 else int(y)) if y else today.year
            result = date(year, mo, d)
        else:
            m = re.fullmatch(r"(\d{1,2})\s*([a-z]{3})[a-z]*(?:\s+(\d{4}))?", t)
            if not m or m[2] not in MONTHS:
                return None
            result = date(int(m[3]) if m[3] else today.year, MONTHS[m[2]], int(m[1]))
        if not m.group(3) and result < today:  # "5/1" typed in December means next year
            result = result.replace(year=result.year + 1)
        return result
    except ValueError:
        return None


_DAY_WORDS = (("day after tomorrow", 2), ("tomorrow", 1), ("tmrw", 1), ("kal", 1), ("today", 0), ("aaj", 0))
_CLOCK = re.compile(r"(?<![\d/:-])(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?![\d/:-])")


def parse_time_text(text: str, now: datetime) -> datetime | None:
    """"6pm", "18:30", "tomorrow 7:30am", "kal 8 am", "15/10 9pm", "now" -> an IST datetime within 30 days."""
    t = " " + text.strip().lower().replace(" at ", " ") + " "
    if t.strip() in ("now", "asap", "abhi", "right now"):
        return now
    day = now.date()
    for word, offset in _DAY_WORDS:
        if word in t:
            day, t = day + timedelta(days=offset), t.replace(word, " ")
            break
    else:
        m = re.search(r"\b(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b", t) or re.search(r"\b(\d{1,2}\s*[a-z]{3,9})\b", t)
        d = parse_date(m.group(1), day) if m else None
        if d:
            day, t = d, t.replace(m.group(1), " ")
    m = _CLOCK.search(t)
    if not m or not (m.group(2) or m.group(3) or t.strip().isdigit()):
        return None  # a bare "18" is fine, but random words aren't a time
    hour, minute, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and hour < 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    when = datetime.combine(day, datetime.min.time(), IST).replace(hour=hour, minute=minute)
    if when < now - timedelta(minutes=1) and day == now.date():
        when += timedelta(days=1)  # "6pm" typed at 7pm means tomorrow
    return when if when <= now + timedelta(days=30) else None
